#!/bin/bash
# Compose-like entrypoint for the XRD Kubernetes Job.
# Same contract as: docker compose run --rm xrd
#
# Prerequisites:
#   - kubectl pointed at a local cluster (Docker Desktop / kind / minikube)
#   - GROBID:  kubectl apply -k k8s/
#   - Secret:  kubectl create secret generic xrd-openai \
#                --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"
#   - PDFs in ./papers/  →  results land in ./results/
#
# Usage (from repo root):
#   ./k8s/run.sh
#   XRD_PAPERS=/path/to/pdfs XRD_RESULTS=/path/to/out ./k8s/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

JOB=xrd-process
PAPERS="${XRD_PAPERS:-$ROOT/papers}"
RESULTS="${XRD_RESULTS:-$ROOT/results}"

mkdir -p "$PAPERS" "$RESULTS"

if ! command -v envsubst >/dev/null 2>&1; then
  echo "error: envsubst is required (macOS: brew install gettext && brew link --force gettext)" >&2
  exit 1
fi

pdf_count="$(find "$PAPERS" -maxdepth 1 -type f \( -iname '*.pdf' \) | wc -l | tr -d ' ')"
if [[ "$pdf_count" -eq 0 ]]; then
  echo "error: no PDFs in $PAPERS — put files there first (same as docker compose)" >&2
  exit 1
fi

echo "Input:  $PAPERS  ($pdf_count PDF(s))"
echo "Output: $RESULTS"

kubectl delete job "$JOB" --ignore-not-found >/dev/null

export HOST_PAPERS="$PAPERS"
export HOST_RESULTS="$RESULTS"
envsubst '${HOST_PAPERS} ${HOST_RESULTS}' < "$ROOT/k8s/xrd-process.yaml" \
  | kubectl apply -f -

# Best-effort: wait for GROBID if the Deployment exists.
kubectl rollout status deployment/grobid --timeout=180s >/dev/null 2>&1 || true

echo "Waiting for Job/$JOB to complete..."
kubectl wait --for=condition=complete "job/$JOB" --timeout=1800s

echo "----- Job logs -----"
kubectl logs "job/$JOB"
echo "--------------------"
echo "Done. Results are in: $RESULTS"
