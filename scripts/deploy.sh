#!/usr/bin/env bash
set -euo pipefail

required=(PROJECT_ID CREDENTIALS_FILE)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 2
  fi
done

if [[ ! -f "$CREDENTIALS_FILE" ]]; then
  echo "Credentials file does not exist: $CREDENTIALS_FILE" >&2
  exit 2
fi

REGION="${REGION:-europe-west1}"
BQ_LOCATION="${BQ_LOCATION:-EU}"
BQ_DATASET="${BQ_DATASET:-skills_workflow}"
SCHEDULE="${SCHEDULE:-0 * * * *}"
TIME_ZONE="${TIME_ZONE:-UTC}"
JOB_NAME="${JOB_NAME:-skills-workflow-loader}"
SCHEDULER_JOB_NAME="${SCHEDULER_JOB_NAME:-skills-workflow-loader-hourly}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-skills-workflow-loader}"
SCHEDULER_SA_NAME="${SCHEDULER_SA_NAME:-skills-workflow-scheduler}"
SECRET_NAME="${SECRET_NAME:-skills-workflow-credentials}"
REPOSITORY="${REPOSITORY:-skills-workflow}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="${SCHEDULER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/loader:latest"

gcloud services enable \
  artifactregistry.googleapis.com \
  bigquery.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  --project "$PROJECT_ID"

if ! gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPOSITORY" \
    --repository-format docker \
    --location "$REGION" \
    --description "Skills Workflow loader images" \
    --project "$PROJECT_ID"
fi

if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name "Skills Workflow BigQuery loader" \
    --project "$PROJECT_ID"
fi

if ! gcloud iam service-accounts describe "$SCHEDULER_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SCHEDULER_SA_NAME" \
    --display-name "Skills Workflow scheduler" \
    --project "$PROJECT_ID"
fi

for role in roles/bigquery.dataEditor roles/bigquery.jobUser roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role "$role" \
    --condition=None \
    --quiet >/dev/null
done

if ! gcloud secrets describe "$SECRET_NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud secrets create "$SECRET_NAME" --replication-policy automatic --project "$PROJECT_ID"
fi
gcloud secrets versions add "$SECRET_NAME" --data-file "$CREDENTIALS_FILE" --project "$PROJECT_ID"

gcloud builds submit "$PACKAGE_DIR" --tag "$IMAGE" --project "$PROJECT_ID"

gcloud run jobs deploy "$JOB_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --service-account "$RUNTIME_SA" \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},BQ_DATASET=${BQ_DATASET},BQ_LOCATION=${BQ_LOCATION}" \
  --set-secrets "SW_CREDENTIALS_JSON=${SECRET_NAME}:latest" \
  --cpu 1 \
  --memory 1Gi \
  --max-retries 1 \
  --task-timeout 3600s

gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --member "serviceAccount:${SCHEDULER_SA}" \
  --role roles/run.invoker \
  --quiet >/dev/null

RUN_URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run"
if gcloud scheduler jobs describe "$SCHEDULER_JOB_NAME" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_JOB_NAME" \
    --location "$REGION" \
    --project "$PROJECT_ID" \
    --schedule "$SCHEDULE" \
    --time-zone "$TIME_ZONE" \
    --uri "$RUN_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA"
else
  gcloud scheduler jobs create http "$SCHEDULER_JOB_NAME" \
    --location "$REGION" \
    --project "$PROJECT_ID" \
    --schedule "$SCHEDULE" \
    --time-zone "$TIME_ZONE" \
    --uri "$RUN_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA"
fi

echo "Deployment complete. Start the first synchronization with:"
echo "gcloud run jobs execute $JOB_NAME --region $REGION --project $PROJECT_ID --wait"

