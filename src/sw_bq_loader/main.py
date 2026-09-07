from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from .bigquery_sink import BigQuerySink
from .config import QueryConfig, Settings
from .skills_workflow import SkillsWorkflowClient
from .transform import transform_rows

LOGGER = logging.getLogger(__name__)


def _is_due(
    query: QueryConfig, last_success: datetime | None, now: datetime, force: bool
) -> bool:
    if force or last_success is None:
        return True
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=timezone.utc)
    return now >= last_success + timedelta(minutes=query.refresh_minutes)


def run() -> int:
    settings = Settings.from_env()
    LOGGER.info(
        "Starting Skills Workflow loader tenant=%s project=%s dataset=%s",
        settings.credentials.tenant,
        settings.project_id,
        settings.dataset_id,
    )
    sink = BigQuerySink(settings)
    sink.initialize()
    last_successes = sink.last_successes()
    client = SkillsWorkflowClient(
        settings.credentials, timeout_seconds=settings.request_timeout_seconds
    )
    client.prepare()

    now = datetime.now(timezone.utc)
    selected = [
        query
        for query in settings.queries
        if query.enabled
        and (not settings.only_queries or query.name in settings.only_queries)
    ]
    due = [
        query
        for query in selected
        if _is_due(query, last_successes.get(query.name), now, settings.force_sync)
    ]
    LOGGER.info("Selected %d queries; %d are due", len(selected), len(due))

    failures: list[str] = []
    for query in due:
        sync_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        try:
            LOGGER.info("Refreshing %s", query.name)
            rows = client.fetch_all(
                query, settings.page_size, settings.max_rows_per_query
            )
            transformed = transform_rows(
                rows, settings.credentials.tenant, query.name, sync_id, started_at
            )
            table_name = sink.replace_snapshot(query.name, sync_id, transformed)
            sink.record_sync(sync_id, query.name, started_at, "SUCCESS", len(rows))
            LOGGER.info("Loaded %d rows into %s", len(rows), table_name)
        except Exception as error:
            message = str(error) or error.__class__.__name__
            failures.append(f"{query.name}: {message}")
            LOGGER.exception("Failed to refresh %s", query.name)
            try:
                sink.record_sync(sync_id, query.name, started_at, "FAILED", 0, message)
            except Exception:
                LOGGER.exception("Could not write failure history for %s", query.name)

    if failures:
        LOGGER.error("%d query refreshes failed", len(failures))
        for failure in failures:
            LOGGER.error("%s", failure)
        return 1 if settings.fail_job_on_query_error else 0
    LOGGER.info("Skills Workflow synchronization completed successfully")
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        raise SystemExit(run())
    except (TypeError, ValueError) as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
