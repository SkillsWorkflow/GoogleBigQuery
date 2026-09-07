from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_DATASET_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
_QUERY_PATTERN = re.compile(r"^[A-Za-z0-9_. -]{1,200}$")


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Credentials:
    tenant: str
    tenant_id: str
    app_id: str
    app_secret: str
    user_id: str = ""
    username: str = ""
    password: str = ""

    @property
    def base_url(self) -> str:
        return f"https://apiv2-{self.tenant}.skillsworkflow.com"

    @classmethod
    def from_json(cls, value: str) -> Credentials:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("SW_CREDENTIALS_JSON is not valid JSON") from error
        if not isinstance(payload, dict):
            raise TypeError("SW_CREDENTIALS_JSON must contain a JSON object")

        tenant = str(payload.get("tenant", "")).strip()
        tenant = re.sub(r"^https?://", "", tenant, flags=re.IGNORECASE)
        tenant = re.sub(r"\.skillsworkflow\.com/?$", "", tenant, flags=re.IGNORECASE)
        if not _TENANT_PATTERN.fullmatch(tenant):
            raise ValueError("tenant must be a Skills Workflow tenant slug")

        credentials = cls(
            tenant=tenant,
            tenant_id=str(
                payload.get("tenant_id", payload.get("tenantId", ""))
            ).strip(),
            app_id=str(
                payload.get("app_id", payload.get("appId", payload.get("appkey", "")))
            ).strip(),
            app_secret=str(
                payload.get(
                    "app_secret", payload.get("appSecret", payload.get("appsecret", ""))
                )
            ),
            user_id=str(payload.get("user_id", payload.get("userId", ""))).strip(),
            username=str(payload.get("username", "")).strip(),
            password=str(payload.get("password", "")),
        )
        missing = [
            name
            for name in ("tenant_id", "app_id", "app_secret")
            if not getattr(credentials, name)
        ]
        if missing:
            raise ValueError(
                "Missing Skills Workflow credentials: " + ", ".join(missing)
            )
        if bool(credentials.username) != bool(credentials.password):
            raise ValueError(
                "username and password must either both be provided or both be omitted"
            )
        return credentials


@dataclass(frozen=True)
class QueryConfig:
    name: str
    refresh_minutes: int = 240
    enabled: bool = True
    order_by_field: str = ""
    filters: list[Any] | None = None
    max_rows: int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QueryConfig:
        name = str(value.get("name", "")).strip()
        if not _QUERY_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid named query: {name!r}")
        refresh_minutes = int(value.get("refresh_minutes", 240))
        if refresh_minutes < 1:
            raise ValueError(f"refresh_minutes must be positive for {name}")
        filters = value.get("filters")
        if filters is not None and not isinstance(filters, list):
            raise ValueError(f"filters must be an array for {name}")
        max_rows = value.get("max_rows")
        if max_rows is not None and int(max_rows) < 1:
            raise ValueError(f"max_rows must be positive for {name}")
        return cls(
            name=name,
            refresh_minutes=refresh_minutes,
            enabled=bool(value.get("enabled", True)),
            order_by_field=str(value.get("order_by_field", "")).strip(),
            filters=filters,
            max_rows=int(max_rows) if max_rows is not None else None,
        )


def load_queries(path: str, overrides_json: str = "") -> list[QueryConfig]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError("Query configuration must be a JSON array")
    values: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            raise TypeError("Each query configuration must be an object")
        name = str(item.get("name", ""))
        if name not in values:
            order.append(name)
        values[name] = dict(item)

    if overrides_json.strip():
        overrides = json.loads(overrides_json)
        if not isinstance(overrides, dict):
            raise ValueError(
                "SW_QUERY_OVERRIDES_JSON must be a JSON object keyed by query name"
            )
        for name, changes in overrides.items():
            if not isinstance(changes, dict):
                raise TypeError(f"Override for {name} must be an object")
            if name not in values:
                order.append(name)
                values[name] = {"name": name}
            values[name].update(changes)
            values[name]["name"] = name

    return [QueryConfig.from_dict(values[name]) for name in order]


@dataclass(frozen=True)
class Settings:
    project_id: str
    dataset_id: str
    location: str
    credentials: Credentials
    queries: list[QueryConfig] = field(default_factory=list)
    page_size: int = 2000
    max_rows_per_query: int = 500_000
    request_timeout_seconds: int = 90
    force_sync: bool = False
    fail_job_on_query_error: bool = True
    only_queries: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> Settings:
        project_id = os.getenv(
            "GCP_PROJECT", os.getenv("GOOGLE_CLOUD_PROJECT", "")
        ).strip()
        if not project_id:
            raise ValueError("GCP_PROJECT is required")
        dataset_id = os.getenv("BQ_DATASET", "skills_workflow").strip()
        if not _DATASET_PATTERN.fullmatch(dataset_id):
            raise ValueError("BQ_DATASET is not a valid BigQuery dataset ID")
        credentials_json = os.getenv("SW_CREDENTIALS_JSON", "")
        if not credentials_json:
            raise ValueError("SW_CREDENTIALS_JSON is required")
        query_path = os.getenv("SW_QUERY_CONFIG", "/app/config/queries.json")
        queries = load_queries(query_path, os.getenv("SW_QUERY_OVERRIDES_JSON", ""))
        only = frozenset(
            item.strip()
            for item in os.getenv("SW_ONLY_QUERIES", "").split(",")
            if item.strip()
        )
        unknown = only.difference(query.name for query in queries)
        if unknown:
            raise ValueError(
                "SW_ONLY_QUERIES contains unknown queries: "
                + ", ".join(sorted(unknown))
            )
        return cls(
            project_id=project_id,
            dataset_id=dataset_id,
            location=os.getenv("BQ_LOCATION", "EU").strip(),
            credentials=Credentials.from_json(credentials_json),
            queries=queries,
            page_size=_int_env("SW_PAGE_SIZE", 2000, 100, 5000),
            max_rows_per_query=_int_env(
                "SW_MAX_ROWS_PER_QUERY", 500_000, 1, 20_000_000
            ),
            request_timeout_seconds=_int_env("SW_REQUEST_TIMEOUT_SECONDS", 90, 10, 600),
            force_sync=_bool_env("FORCE_SYNC", False),
            fail_job_on_query_error=_bool_env("FAIL_JOB_ON_QUERY_ERROR", True),
            only_queries=only,
        )
