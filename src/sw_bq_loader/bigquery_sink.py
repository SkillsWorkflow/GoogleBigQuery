from __future__ import annotations

import logging
from datetime import datetime, timezone

from google.cloud import bigquery

from .config import Settings
from .transform import TransformedData, table_name_for_query

LOGGER = logging.getLogger(__name__)


SYNC_SCHEMA = [
    bigquery.SchemaField("sync_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("tenant", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("query_name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("table_name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("finished_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("row_count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("error_message", "STRING", mode="NULLABLE"),
]

CATALOG_SCHEMA = [
    bigquery.SchemaField("query_name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("table_name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("enabled", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("refresh_minutes", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("order_by_field", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("has_filters", "BOOLEAN", mode="REQUIRED"),
]


class BigQuerySink:
    def __init__(
        self, settings: Settings, client: bigquery.Client | None = None
    ) -> None:
        self.settings = settings
        self.client = client or bigquery.Client(
            project=settings.project_id, location=settings.location
        )
        self.dataset_ref = bigquery.DatasetReference(
            settings.project_id, settings.dataset_id
        )

    def initialize(self) -> None:
        dataset = bigquery.Dataset(self.dataset_ref)
        dataset.location = self.settings.location
        dataset.description = "Skills Workflow named-query snapshots for Looker Studio"
        self.client.create_dataset(dataset, exists_ok=True)
        self._ensure_table(
            "_sw_sync_runs", SYNC_SCHEMA, "Skills Workflow loader execution history"
        )
        self._replace_catalog()

    def last_successes(self) -> dict[str, datetime]:
        table_id = self._table_id("_sw_sync_runs")
        sql = f"""
select
  query_name,
  max(finished_at) as finished_at
from `{table_id}`
where status = 'SUCCESS'
group by query_name
"""
        result: dict[str, datetime] = {}
        for row in self.client.query(sql, location=self.settings.location).result():
            result[str(row["query_name"])] = row["finished_at"]
        return result

    def replace_snapshot(
        self, query_name: str, sync_id: str, data: TransformedData
    ) -> str:
        table_name = table_name_for_query(query_name)
        target_id = self._table_id(table_name)
        stage_name = f"_stage_{table_name[:800]}_{sync_id.replace('-', '')[:12]}"
        stage_id = self._table_id(stage_name)
        try:
            if data.rows:
                config = bigquery.LoadJobConfig(
                    schema=data.schema,
                    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                    create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
                    write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                )
                self.client.load_table_from_json(
                    data.rows,
                    stage_id,
                    job_config=config,
                    location=self.settings.location,
                ).result()
            else:
                stage = bigquery.Table(stage_id, schema=data.schema)
                stage.description = f"Empty staging snapshot for {query_name}"
                self.client.create_table(stage)

            copy_config = bigquery.CopyJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE
            )
            self.client.copy_table(
                stage_id,
                target_id,
                job_config=copy_config,
                location=self.settings.location,
            ).result()
            try:
                table = self.client.get_table(target_id)
                table.description = (
                    f"Current snapshot of Skills Workflow named query {query_name}"
                )
                table.labels = {
                    "source": "skills-workflow",
                    "tenant": _label_value(self.settings.credentials.tenant),
                }
                self.client.update_table(table, ["description", "labels"])
            except Exception:
                LOGGER.warning(
                    "Snapshot copied but table metadata could not be updated for %s",
                    query_name,
                    exc_info=True,
                )
            return table_name
        finally:
            self.client.delete_table(stage_id, not_found_ok=True)

    def record_sync(
        self,
        sync_id: str,
        query_name: str,
        started_at: datetime,
        status: str,
        row_count: int,
        error_message: str = "",
    ) -> None:
        finished_at = datetime.now(timezone.utc)
        row = {
            "sync_id": sync_id,
            "tenant": self.settings.credentials.tenant,
            "query_name": query_name,
            "table_name": table_name_for_query(query_name),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "status": status,
            "row_count": row_count,
            "error_message": error_message[:1000] or None,
        }
        config = bigquery.LoadJobConfig(
            schema=SYNC_SCHEMA,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        self.client.load_table_from_json(
            [row],
            self._table_id("_sw_sync_runs"),
            job_config=config,
            location=self.settings.location,
        ).result()

    def _replace_catalog(self) -> None:
        rows = [
            {
                "query_name": query.name,
                "table_name": table_name_for_query(query.name),
                "enabled": query.enabled,
                "refresh_minutes": query.refresh_minutes,
                "order_by_field": query.order_by_field or None,
                "has_filters": query.filters is not None,
            }
            for query in self.settings.queries
        ]
        config = bigquery.LoadJobConfig(
            schema=CATALOG_SCHEMA,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        self.client.load_table_from_json(
            rows,
            self._table_id("_sw_query_catalog"),
            job_config=config,
            location=self.settings.location,
        ).result()

    def _ensure_table(
        self, name: str, schema: list[bigquery.SchemaField], description: str
    ) -> None:
        table = bigquery.Table(self._table_id(name), schema=schema)
        table.description = description
        self.client.create_table(table, exists_ok=True)

    def _table_id(self, name: str) -> str:
        return f"{self.settings.project_id}.{self.settings.dataset_id}.{name}"


def _label_value(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "_-" else "-"
        for character in value.lower()
    )
    return normalized[:63] or "unknown"
