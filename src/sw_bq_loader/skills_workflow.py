from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from .config import Credentials, QueryConfig

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUSES = {429, 502, 503, 504}


class SkillsWorkflowError(RuntimeError):
    pass


class RowLimitExceeded(SkillsWorkflowError):
    pass


@dataclass(frozen=True)
class Page:
    rows: list[dict[str, Any]]
    total_count: int | None


class SkillsWorkflowClient:
    def __init__(
        self,
        credentials: Credentials,
        timeout_seconds: int = 90,
        session: requests.Session | None = None,
    ) -> None:
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.user_id = credentials.user_id

    def prepare(self) -> None:
        if not self.user_id and self.credentials.username:
            self.user_id = self._authenticate_user()
            if not self.user_id:
                raise SkillsWorkflowError(
                    "Authentication succeeded but returned no user ID"
                )

    def fetch_all(
        self,
        query: QueryConfig,
        page_size: int,
        default_max_rows: int,
    ) -> list[dict[str, Any]]:
        max_rows = query.max_rows or default_max_rows
        base_builder: dict[str, Any] = {"skip": 0, "take": page_size}
        if query.filters is not None:
            base_builder["filters"] = query.filters
        if query.order_by_field:
            base_builder["orderBy"] = [
                {"field": query.order_by_field, "direction": "asc"}
            ]

        first_page = self._execute_page(query.name, base_builder)
        if len(first_page.rows) < page_size:
            return first_page.rows

        if not query.order_by_field:
            inferred_order = _infer_order_field(first_page.rows)
            if inferred_order:
                LOGGER.info(
                    "Using inferred order field %s for %s", inferred_order, query.name
                )
                base_builder["orderBy"] = [
                    {"field": inferred_order, "direction": "asc"}
                ]
                first_page = self._execute_page(query.name, base_builder)

        rows = list(first_page.rows)
        seen_pages = {_page_fingerprint(first_page.rows)}
        while len(rows) < max_rows:
            if (
                first_page.total_count is not None
                and len(rows) >= first_page.total_count
            ):
                return rows
            builder = dict(base_builder)
            builder["skip"] = len(rows)
            builder["take"] = min(page_size, max_rows - len(rows))
            page = self._execute_page(query.name, builder)
            if not page.rows:
                return rows
            fingerprint = _page_fingerprint(page.rows)
            if fingerprint in seen_pages:
                raise SkillsWorkflowError(
                    f"{query.name} returned a repeated page; configure order_by_field"
                )
            seen_pages.add(fingerprint)
            rows.extend(page.rows)
            if len(page.rows) < builder["take"]:
                return rows

        if first_page.total_count is None or len(rows) < first_page.total_count:
            raise RowLimitExceeded(
                f"{query.name} exceeded its {max_rows:,}-row safety limit"
            )
        return rows

    def _execute_page(self, name: str, query_builder: dict[str, Any]) -> Page:
        url = f"{self.credentials.base_url}/api/v3/analytics/named-query/{quote(name, safe='')}/dynamic-execute"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-AppTenant": self.credentials.tenant_id,
            "X-AppId": self.credentials.app_id,
            "X-AppSecret": self.credentials.app_secret,
        }
        if self.user_id:
            headers["X-AppUser"] = self.user_id

        response = self._request(
            "POST", url, headers=headers, json={"queryBuilder": query_builder}
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise SkillsWorkflowError(f"{name} returned unreadable JSON") from error
        return _parse_page(payload)

    def _authenticate_user(self) -> str:
        url = f"{self.credentials.base_url}/api/auth/token"
        payload = {
            "UserName": self.credentials.username,
            "Password": self.credentials.password,
            "Persistent": True,
            "LoginAsDelegate": False,
            "DelegatedUserName": "",
        }
        response = self._request(
            "POST", url, headers={"Content-Type": "application/json"}, json=payload
        )
        try:
            body = response.json()
        except ValueError as error:
            raise SkillsWorkflowError(
                "Authentication returned unreadable JSON"
            ) from error
        if not isinstance(body, dict):
            return ""
        user = body.get("User") if isinstance(body.get("User"), dict) else {}
        return str(
            body.get("UserId")
            or body.get("userId")
            or user.get("Id")
            or user.get("id")
            or ""
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(3):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout_seconds, **kwargs
                )
            except requests.RequestException as error:
                if attempt == 2:
                    raise SkillsWorkflowError(
                        f"Skills Workflow request failed: {error.__class__.__name__}"
                    ) from error
                _backoff(attempt)
                continue
            if 200 <= response.status_code < 300:
                return response
            if response.status_code not in RETRYABLE_STATUSES or attempt == 2:
                detail = _safe_error_detail(response)
                raise SkillsWorkflowError(
                    f"Skills Workflow returned HTTP {response.status_code}{detail}"
                )
            _backoff(attempt)
        raise SkillsWorkflowError("Skills Workflow request failed")


def _backoff(attempt: int) -> None:
    time.sleep((2**attempt) + random.random() / 2)


def _safe_error_detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    message = body.get("message") or body.get("Message") or body.get("error")
    if not isinstance(message, str):
        return ""
    message = message.replace("\n", " ").replace("\r", " ")[:300]
    return f": {message}" if message else ""


def _parse_page(payload: Any) -> Page:
    if isinstance(payload, list):
        rows = payload
        count = None
    elif isinstance(payload, dict):
        rows = next(
            (
                payload[key]
                for key in ("data", "Data", "items", "Items", "results", "Results")
                if isinstance(payload.get(key), list)
            ),
            [],
        )
        raw_count = next(
            (
                payload[key]
                for key in ("totalCount", "TotalCount", "count", "Count")
                if payload.get(key) is not None
            ),
            None,
        )
        try:
            count = int(raw_count) if raw_count is not None else None
        except (TypeError, ValueError):
            count = None
    else:
        raise SkillsWorkflowError("Named query response was not an array or object")
    if not all(isinstance(row, dict) for row in rows):
        raise SkillsWorkflowError("Named query returned a row that was not an object")
    return Page(rows=rows, total_count=count)


def _infer_order_field(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    names: list[str] = []
    for row in rows:
        for name in row:
            if name not in names:
                names.append(name)
    exact_id = next((name for name in names if name.lower() == "id"), "")
    return exact_id or next((name for name in names if name.lower().endswith("id")), "")


def _page_fingerprint(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "empty"
    return json.dumps(
        [len(rows), rows[0], rows[-1]],
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
