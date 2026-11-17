#!/usr/bin/env bash
# Deploy the Corridor Grant Advisor to Cloud Run. Run from this folder in Cloud Shell:
#   bash deploy.sh                      # BigQuery tables in US
#   BQ_LOCATION=us-central1 bash deploy.sh   # if you moved the dataset for the reservation
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-grant-advisor}"
BQ_LOCATION="${BQ_LOCATION:-US}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.6-flash}"
SA_NAME="grant-advisor-sa"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

[[ -n "${PROJECT_ID}" ]] || { echo "Set a project: gcloud config set project ID"; exit 1; }
echo "Project ${PROJECT_ID} | region ${REGION} | BigQuery location ${BQ_LOCATION}"

echo "== Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com bigquery.googleapis.com aiplatform.googleapis.com \
  --project "${PROJECT_ID}"
# The managed BigQuery MCP server may need an explicit opt-in; harmless if already on.
gcloud beta services mcp enable bigquery.googleapis.com --project "${PROJECT_ID}" 2>/dev/null \
  || echo "  (mcp enable skipped - it is on by default in newer projects)"

echo "== Service account"
gcloud iam service-accounts describe "${SA}" --project "${PROJECT_ID}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${SA_NAME}" --project "${PROJECT_ID}" \
       --display-name "Corridor Grant Advisor"
for ROLE in roles/aiplatform.user roles/bigquery.jobUser roles/bigquery.dataViewer roles/mcp.toolUser; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member "serviceAccount:${SA}" \
    --role "${ROLE}" --condition=None --quiet >/dev/null \
    && echo "  granted ${ROLE}" || echo "  could not grant ${ROLE} (ask a coach if the agent gets 403s)"
done

echo "== Deploying (Cloud Build, ~3-5 min)"
gcloud run deploy "${SERVICE}" --source . --region "${REGION}" --project "${PROJECT_ID}" \
  --service-account "${SA}" --min-instances 1 --memory 1Gi --timeout 300 \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_LOCATION=global,GEMINI_MODEL=${GEMINI_MODEL},BQ_LOCATION=${BQ_LOCATION},BQ_DATASET=a4i_econ,BQ_GRAPH=a4i_econ.LocalEconomy"

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)')"
echo
echo "Deployed: ${URL}"
echo "If the page says Forbidden, your org blocks public services. Open it through a proxy instead:"
echo "  gcloud run services proxy ${SERVICE} --region ${REGION} --port 8080"
