from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_ID_LIKE_PATTERN = re.compile(r"(^id$|id$|code$|number$|reference$)", re.IGNORECASE)


@dataclass(frozen=True)
class TransformedData:
    rows: list[dict[str, Any]]
    schema: list[bigquery.SchemaField]
    field_map: dict[str, str]


def table_name_for_query(name: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    value = re.sub(r"_+", "_", value)
    if not value or value[0].isdigit():
        value = "q_" + value
    return value[:1024]


def transform_rows(
    rows: list[dict[str, Any]],
    tenant: str,
    query_name: str,
    sync_id: str,
    loaded_at: datetime | None = None,
) -> TransformedData:
    loaded_at = loaded_at or datetime.now(timezone.utc)
    source_names: list[str] = []
    for row in rows:
        for name in row:
            if name not in source_names:
                source_names.append(name)
    field_map = _build_field_map(source_names)
    field_types = {
        name: _infer_type(name, [row.get(name) for row in rows])
        for name in source_names
    }

    schema = [
        bigquery.SchemaField(
            field_map[name],
            field_types[name],
            mode="NULLABLE",
            description=f"Skills Workflow field: {name}",
        )
        for name in source_names
    ]
    schema.extend(
        [
            bigquery.SchemaField(
                "sw_tenant",
                "STRING",
                mode="REQUIRED",
                description="Skills Workflow tenant slug",
            ),
            bigquery.SchemaField(
                "sw_query",
                "STRING",
                mode="REQUIRED",
                description="Skills Workflow named query",
            ),
            bigquery.SchemaField(
                "sw_loaded_at",
                "TIMESTAMP",
                mode="REQUIRED",
                description="UTC warehouse load time",
            ),
            bigquery.SchemaField(
                "sw_sync_id",
                "STRING",
                mode="REQUIRED",
                description="Loader synchronization identifier",
            ),
            bigquery.SchemaField(
                "sw_row_hash",
                "STRING",
                mode="REQUIRED",
                description="SHA-256 hash of the source row",
            ),
        ]
    )

    transformed: list[dict[str, Any]] = []
    for row in rows:
        item = {
            field_map[name]: _coerce(row.get(name), field_types[name])
            for name in source_names
        }
        canonical = json.dumps(
            row, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":")
        )
        item.update(
            {
                "sw_tenant": tenant,
                "sw_query": query_name,
                "sw_loaded_at": loaded_at.isoformat(),
                "sw_sync_id": sync_id,
                "sw_row_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            }
        )
        transformed.append(item)
    return TransformedData(rows=transformed, schema=schema, field_map=field_map)


def _build_field_map(source_names: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    used = {"sw_tenant", "sw_query", "sw_loaded_at", "sw_sync_id", "sw_row_hash"}
    for source_name in source_names:
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(source_name))
        value = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
        value = re.sub(r"_+", "_", value)
        if not value:
            value = "field"
        if value[0].isdigit():
            value = "f_" + value
        candidate = value[:300]
        if candidate in used:
            suffix = hashlib.sha256(str(source_name).encode("utf-8")).hexdigest()[:8]
            candidate = f"{value[:291]}_{suffix}"
        used.add(candidate)
        result[source_name] = candidate
    return result


def _infer_type(name: str, values: list[Any]) -> str:
    present = [value for value in values if value is not None and value != ""]
    if not present:
        return "STRING"
    if _ID_LIKE_PATTERN.search(name):
        return "STRING"
    if all(isinstance(value, bool) for value in present):
        return "BOOLEAN"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return "INTEGER"
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in present
    ):
        return "FLOAT"
    if all(
        isinstance(value, str) and _DATE_PATTERN.fullmatch(value) for value in present
    ):
        return "DATE"
    if all(
        isinstance(value, str) and _TIMESTAMP_PATTERN.fullmatch(value)
        for value in present
    ):
        return "TIMESTAMP"
    return "STRING"


def _coerce(value: Any, field_type: str) -> Any:
    if value is None or value == "":
        return None
    if field_type == "STRING":
        if isinstance(value, (dict, list)):
            return json.dumps(
                value,
                sort_keys=True,
                default=str,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return str(value)
    if field_type == "BOOLEAN":
        return bool(value)
    if field_type == "INTEGER":
        return int(value)
    if field_type == "FLOAT":
        return float(value)
    return str(value)
