# Skills Workflow → BigQuery connector

This package loads Skills Workflow analytics named queries into BigQuery so report authors can build their own reports with Looker Studio's native BigQuery connector.

It is tenant-neutral. Deploy one isolated instance per Skills Workflow tenant. The deployment stores each agency's application credentials in Google Secret Manager; credentials are never placed in BigQuery, Looker Studio, the container image, or report fields.

## Architecture

```text
Cloud Scheduler (hourly)
        |
        v
Cloud Run Job
        |
        +-- reads tenant credentials from Secret Manager
        +-- calls Skills Workflow analytics/named-query
        +-- paginates and normalizes every enabled query
        v
BigQuery dataset
        |
        v
Looker Studio native BigQuery data source
```

The catalog contains 59 supported `DE-*` queries plus `EstimatedPlannedActualMonthly`. `DE-*` count endpoints are intentionally excluded, and the expense-type endpoint is named `DE-ExpenseTypes`. The scheduler starts the loader hourly, but the per-query `refresh_minutes` setting determines whether a query is due. Operational queries default to hourly, other transactional queries to every four hours, and reference data to daily.

## Data behavior

- Each named query becomes one BigQuery table, such as `DE-Projects` → `de_projects`.
- Every successful refresh atomically replaces the current snapshot table.
- The previous good table remains available if API extraction or staging fails.
- Source fields are normalized to lowercase `snake_case` for BigQuery and Looker Studio.
- Numeric identifier-like fields are stored as strings to avoid losing leading zeroes or precision.
- Objects and arrays are stored as JSON text.
- Each row includes `sw_tenant`, `sw_query`, `sw_loaded_at`, `sw_sync_id`, and `sw_row_hash`.
- `_sw_query_catalog` documents the available tables and refresh interval.
- `_sw_sync_runs` records successes, failures, row counts, and timestamps.
- Loads are full current snapshots by default. This is the only tenant-neutral behavior that does not require undocumented primary keys or update-date fields.

## Prerequisites

Install and initialize the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), select a billing-enabled Google Cloud project, and make sure the deploying account can enable APIs, create service accounts, modify IAM, build containers, deploy Cloud Run jobs, create secrets, and create Scheduler jobs.

Use a dedicated Google Cloud project for each agency where possible. The included deployment script grants the loader project-level BigQuery Data Editor access; project isolation keeps that scope limited to one tenant.

## 1. Prepare the tenant secret

Copy `credentials.example.json` to a file outside source control and replace the placeholders:

```json
{
  "tenant": "agency-slug",
  "tenant_id": "tenant-application-id",
  "app_id": "application-id",
  "app_secret": "application-secret",
  "user_id": "optional-operational-user-id"
}
```

The loader accepts either snake_case or the Skills Workflow-style keys `tenantId`, `appId`, and `appSecret`.

If the tenant requires a user context and the user ID is not known, omit `user_id` and add `username` and `password`. The loader authenticates at the beginning of each run and uses the returned user ID. Store a dedicated read-only integration user's credentials rather than a person's account.

Never send this file by email, commit it, or place it in a Looker Studio configuration. The deployment script uploads it directly to Secret Manager.

## 2. Deploy

From this directory, run:

```bash
chmod +x scripts/deploy.sh scripts/grant_report_access.sh

PROJECT_ID="your-google-cloud-project" \
CREDENTIALS_FILE="/absolute/path/to/credentials.json" \
BQ_DATASET="skills_workflow" \
REGION="europe-west1" \
BQ_LOCATION="EU" \
./scripts/deploy.sh
```

Optional deployment variables:

| Variable | Default | Purpose |
|---|---|---|
| `SCHEDULE` | `0 * * * *` | Scheduler cron expression |
| `TIME_ZONE` | `UTC` | Scheduler timezone |
| `JOB_NAME` | `skills-workflow-loader` | Cloud Run job name |
| `SECRET_NAME` | `skills-workflow-credentials` | Secret Manager secret |
| `REPOSITORY` | `skills-workflow` | Artifact Registry repository |

The script is rerunnable. A subsequent run builds a new image, adds a new secret version, updates the Cloud Run job, and updates the schedule.

## 3. Run and verify the first synchronization

```bash
gcloud run jobs execute skills-workflow-loader \
  --region europe-west1 \
  --project your-google-cloud-project \
  --wait
```

Then open BigQuery in Google Cloud Console and inspect:

```sql
select
  query_name,
  status,
  row_count,
  finished_at,
  error_message
from `your-google-cloud-project.skills_workflow._sw_sync_runs`
order by finished_at desc
```

If a named query is not enabled for a tenant, the other queries still load and the failure is recorded. Disable unavailable queries with an override as described below, then redeploy.

## 4. Give report authors access

Prefer granting access to a Google Group rather than individual accounts:

```bash
PROJECT_ID="your-google-cloud-project" \
MEMBER="group:report-authors@example.com" \
./scripts/grant_report_access.sh
```

This grants BigQuery Data Viewer and BigQuery Job User roles in the dedicated tenant project. Report authors can query the dataset but cannot change loader secrets or Cloud Run configuration.

## 5. Connect Looker Studio

For each source a report author needs:

1. In Looker Studio, select **Create → Data source**.
2. Choose Google's **BigQuery** connector.
3. Select the Google Cloud project, the agency dataset, and a table such as `de_projects`.
4. Select **Connect**, review field types and default aggregations, and choose **Create report** or **Add to report**.
5. Repeat for additional tables. Use Looker Studio data blending for simple combinations; create a BigQuery view for reusable multi-table business logic.

No Skills Workflow credentials are requested in Looker Studio. Google IAM controls who can access the BigQuery data.

## Query customization

The built-in catalog is `config/queries.json`. Each entry supports:

```json
{
  "name": "DE-TimeSheets",
  "enabled": true,
  "refresh_minutes": 60,
  "order_by_field": "TimeSheetId",
  "filters": [["Date", ">=", "2025-01-01"]],
  "max_rows": 500000
}
```

Do not edit the catalog for tenant-specific changes if the same image will be reused. Set `SW_QUERY_OVERRIDES_JSON` on that tenant's Cloud Run job instead. It is a JSON object keyed by named query and can also introduce custom named queries:

```json
{
  "DE-History": {"enabled": false},
  "DE-TimeSheets": {"refresh_minutes": 30, "order_by_field": "TimeSheetId"},
  "Agency-Custom-Query": {"enabled": true, "refresh_minutes": 240}
}
```

Other runtime controls:

| Environment variable | Default | Purpose |
|---|---:|---|
| `SW_PAGE_SIZE` | `2000` | API rows requested per page |
| `SW_MAX_ROWS_PER_QUERY` | `500000` | Safety limit; exceeding it fails without replacing the prior table |
| `SW_REQUEST_TIMEOUT_SECONDS` | `90` | HTTP request timeout |
| `SW_ONLY_QUERIES` | empty | Comma-separated subset, useful for tests |
| `FORCE_SYNC` | `false` | Ignore refresh intervals |
| `FAIL_JOB_ON_QUERY_ERROR` | `true` | Mark the Cloud Run execution failed if any query fails |

Because filters and field names can differ by tenant, validate custom `queryBuilder` filters in a non-production dataset first.

## Schema changes

The loader discovers the schema from every snapshot. When Skills Workflow adds or changes a returned field, the next successful load updates the BigQuery table schema. Existing Looker Studio data sources may require **Refresh fields** before the new field appears.

A query that returns no rows is still published as an empty table containing only the five loader metadata fields. Its source columns appear after the query returns data in a later refresh.

## Cost and safety controls

- The loader uses BigQuery batch load and copy jobs, not paid streaming inserts.
- Full snapshot extraction can be expensive for very large tenant queries at the Skills Workflow API side. Use supported filters or split large historical loads after identifying stable primary-key and update-date fields.
- Partitioning is not applied to current snapshot tables because the appropriate business date differs by named query. Create reporting views or derived partitioned tables for high-volume history.
- Configure a Google Cloud billing budget and BigQuery project query quota before broadly sharing report access.
- Keep Cloud Run and BigQuery in compatible regions and do not log or echo the secret JSON.

## Local validation

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
PYTHONPATH=src pytest -q
```

The tests do not call Skills Workflow or Google Cloud.
