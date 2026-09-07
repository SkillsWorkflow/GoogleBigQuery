# Packaged named-query catalog

Source catalog: <https://documentation.skillsworkflow.com/docs/build-and-extend/api/data-extraction-api>

Verified: 2026-09-04

The authoritative packaged list is `config/queries.json`. It contains 70 entries: 69 documented `DE-*` queries plus `EstimatedPlannedActualMonthly`.

Tenants can publish additional named queries or omit documented queries. Use `SW_QUERY_OVERRIDES_JSON` to add, disable, filter, reschedule, or supply a stable order field without rebuilding the shared image.
