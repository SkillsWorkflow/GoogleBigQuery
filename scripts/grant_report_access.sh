#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PROJECT_ID:-}" || -z "${MEMBER:-}" ]]; then
  echo "Usage: PROJECT_ID=your-project MEMBER=user:analyst@example.com $0" >&2
  echo "MEMBER may also be a Google group, for example group:report-users@example.com" >&2
  exit 2
fi

for role in roles/bigquery.dataViewer roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "$MEMBER" \
    --role "$role" \
    --condition=None \
    --quiet
done

echo "Granted BigQuery reporting access to $MEMBER in $PROJECT_ID"

