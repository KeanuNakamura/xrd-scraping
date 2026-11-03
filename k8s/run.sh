#!/bin/bash
# Parallel XRD Kubernetes Job — one PDF per pod (Indexed Job).
# Same host I/O contract as: docker compose run --rm xrd
#   PDFs in ./papers/  →  results land in ./results/
#
# Docker Desktop Kubernetes cannot mount macOS /Users via hostPath, so this
# script stages papers into PVC xrd-data, runs the Job, then copies results out.
#
# Prerequisites:
#   - kubectl pointed at a local cluster (Docker Desktop / kind / minikube)
#   - GROBID:  kubectl apply -k k8s/
#   - Secret:  kubectl create secret generic xrd-openai \
#                --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"
#   - PDFs in ./papers/
#
# Usage (from repo root):
#   ./k8s/run.sh
#   XRD_PARALLELISM=4 ./k8s/run.sh
#   XRD_PAPERS=/path/to/pdfs XRD_RESULTS=/path/to/out XRD_PARALLELISM=8 ./k8s/run.sh
#
# Env:
#   XRD_PAPERS           input dir (default: <repo>/papers)
#   XRD_RESULTS          output dir (default: <repo>/results)
#   XRD_PARALLELISM      max concurrent PDF pods (default: 1 — one GROBID on
#                        laptops cannot survive parallel fulltext requests)
#   XRD_GROBID_REPLICAS  GROBID pods (default: 1 — keep low on laptops;
#                        raise only on larger clusters; each replica ~2–4Gi)
#   XRD_DEADLINE         Job activeDeadlineSeconds (default: ceil(N/P)*1800)
#   XRD_PVC_SIZE         PVC size if creating xrd-data (default: 20Gi)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

JOB=xrd-process
PVC=xrd-data
STAGE_POD=xrd-stage
PAPERS="${XRD_PAPERS:-$ROOT/papers}"
RESULTS="${XRD_RESULTS:-$ROOT/results}"
PVC_SIZE="${XRD_PVC_SIZE:-20Gi}"

# Resolve relative paths against repo root; keep absolute paths as-is.
case "$PAPERS" in
  /*) ;;
  *) PAPERS="$ROOT/$PAPERS" ;;
esac
case "$RESULTS" in
  /*) ;;
  *) RESULTS="$ROOT/$RESULTS" ;;
esac

mkdir -p "$PAPERS" "$RESULTS"

if ! command -v envsubst >/dev/null 2>&1; then
  echo "error: envsubst is required (macOS: brew install gettext && brew link --force gettext)" >&2
  exit 1
fi

# Match collect_pdf_paths(): recursive *.pdf under papers/ (skip macOS AppleDouble).
pdf_count="$(find "$PAPERS" -type f \( -iname '*.pdf' \) ! -name '._*' | wc -l | tr -d ' ')"
if [[ "$pdf_count" -eq 0 ]]; then
  echo "error: no PDFs in $PAPERS — put files there first (same as docker compose)" >&2
  exit 1
fi

# Default 1: a single GROBID on Docker Desktop OOMs / connection-resets when
# several workers hit /api/processFulltextDocument at once.
if [[ -n "${XRD_PARALLELISM:-}" ]]; then
  PARALLELISM="$XRD_PARALLELISM"
else
  PARALLELISM=1
fi
if [[ "$PARALLELISM" -lt 1 ]]; then
  echo "error: XRD_PARALLELISM must be >= 1" >&2
  exit 1
fi
if [[ "$PARALLELISM" -gt "$pdf_count" ]]; then
  PARALLELISM="$pdf_count"
fi

# Whole-Job deadline: ~30 min wall time per parallel wave
waves=$(( (pdf_count + PARALLELISM - 1) / PARALLELISM ))
default_deadline=$(( waves * 1800 ))
DEADLINE="${XRD_DEADLINE:-$default_deadline}"

# Default 1: scaling GROBID 1:1 with parallelism OOMs on typical Docker Desktop
# nodes (~8Gi; each GROBID is -Xmx2g).
GROBID_REPLICAS="${XRD_GROBID_REPLICAS:-1}"

if [[ "$PARALLELISM" -gt "$GROBID_REPLICAS" ]]; then
  echo "warning: XRD_PARALLELISM=$PARALLELISM > GROBID replicas=$GROBID_REPLICAS — concurrent fulltext calls often crash GROBID on laptops. Prefer XRD_PARALLELISM=1 (or raise XRD_GROBID_REPLICAS on a bigger node)." >&2
fi

echo "Input:      $PAPERS  ($pdf_count PDF(s))"
echo "Output:     $RESULTS"
echo "Parallelism: $PARALLELISM pod(s)  (completions=$pdf_count)"
echo "GROBID:      Deployment/grobid → $GROBID_REPLICAS replica(s)"
echo "Deadline:    ${DEADLINE}s"
echo "PVC:         $PVC"

ensure_pvc() {
  if kubectl get "pvc/$PVC" >/dev/null 2>&1; then
    return 0
  fi
  echo "Creating PVC/$PVC ($PVC_SIZE)..."
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: $PVC_SIZE
EOF
}

# Helper pod that holds the PVC so we can tar stream papers in / results out.
start_stage_pod() {
  kubectl delete pod "$STAGE_POD" --ignore-not-found >/dev/null 2>&1 || true
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $STAGE_POD
spec:
  restartPolicy: Never
  containers:
    - name: stage
      image: busybox:1.36
      command: ["sleep", "3600"]
      volumeMounts:
        - name: xrd-data
          mountPath: /data
  volumes:
    - name: xrd-data
      persistentVolumeClaim:
        claimName: $PVC
EOF
  kubectl wait --for=condition=Ready "pod/$STAGE_POD" --timeout=120s
}

stop_stage_pod() {
  kubectl delete pod "$STAGE_POD" --ignore-not-found >/dev/null 2>&1 || true
}

stage_papers() {
  echo "Staging papers into PVC/$PVC ..."
  kubectl exec "$STAGE_POD" -- sh -c 'rm -rf /data/papers /data/results && mkdir -p /data/papers /data/results'
  # Preserve relative layout under papers/ (matches collect_pdf_paths).
  # COPYFILE_DISABLE skips macOS AppleDouble (._*) sidecars in the tar stream.
  COPYFILE_DISABLE=1 tar -C "$PAPERS" -cf - . | kubectl exec -i "$STAGE_POD" -- tar -C /data/papers -xf -
  staged="$(kubectl exec "$STAGE_POD" -- sh -c 'find /data/papers -type f \( -iname "*.pdf" \) ! -name "._*" | wc -l' | tr -d '[:space:]')"
  if [[ "$staged" -ne "$pdf_count" ]]; then
    echo "error: staged $staged PDF(s) but expected $pdf_count" >&2
    exit 1
  fi
  echo "Staged $staged PDF(s)."
}

fetch_results() {
  echo "Copying results from PVC/$PVC → $RESULTS ..."
  mkdir -p "$RESULTS"
  kubectl exec "$STAGE_POD" -- sh -c 'cd /data/results && tar -cf - .' | tar -C "$RESULTS" -xf -
}

if kubectl get deployment/grobid >/dev/null 2>&1; then
  kubectl scale deployment/grobid --replicas="$GROBID_REPLICAS"
  kubectl rollout status deployment/grobid --timeout=300s
else
  echo "warning: Deployment/grobid not found — apply with: kubectl apply -k k8s/" >&2
fi

wait_for_grobid() {
  echo "Waiting for GROBID Service to answer /api/isalive ..."
  kubectl delete pod xrd-grobid-wait --ignore-not-found >/dev/null 2>&1 || true
  cat <<'EOF' | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: xrd-grobid-wait
spec:
  restartPolicy: Never
  containers:
    - name: wait
      image: busybox:1.36
      command:
        - sh
        - -c
        - |
          for i in $(seq 1 60); do
            if wget -q -O- http://grobid:8070/api/isalive 2>/dev/null | grep -qi true; then
              echo ready
              exit 0
            fi
            sleep 5
          done
          echo "GROBID not ready" >&2
          exit 1
EOF
  for _ in $(seq 1 64); do
    phase="$(kubectl get pod xrd-grobid-wait -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "$phase" == "Succeeded" ]]; then
      kubectl delete pod xrd-grobid-wait --ignore-not-found >/dev/null 2>&1 || true
      echo "GROBID is alive."
      return 0
    fi
    if [[ "$phase" == "Failed" ]]; then
      kubectl logs xrd-grobid-wait 2>&1 || true
      kubectl delete pod xrd-grobid-wait --ignore-not-found >/dev/null 2>&1 || true
      echo "error: GROBID did not become ready" >&2
      exit 1
    fi
    sleep 5
  done
  kubectl delete pod xrd-grobid-wait --ignore-not-found >/dev/null 2>&1 || true
  echo "error: timed out waiting for GROBID" >&2
  exit 1
}

ensure_pvc
kubectl delete job "$JOB" --ignore-not-found >/dev/null
start_stage_pod
stage_papers
# Release the PVC so Indexed Job pods can attach (RWO).
stop_stage_pod

wait_for_grobid

export XRD_COMPLETIONS="$pdf_count"
export XRD_PARALLELISM="$PARALLELISM"
export XRD_DEADLINE="$DEADLINE"
envsubst '${XRD_COMPLETIONS} ${XRD_PARALLELISM} ${XRD_DEADLINE}' \
  < "$ROOT/k8s/xrd-process.yaml" \
  | kubectl apply -f -

echo "Waiting for Job/$JOB to complete..."
kubectl wait --for=condition=complete "job/$JOB" --timeout="${DEADLINE}s"

echo "----- Job logs (all indexed pods) -----"
kubectl logs -l "job-name=$JOB" --prefix=true --all-containers=true || true
echo "---------------------------------------"

# Release RWO PVC from finished Job pods before the fetch helper attaches.
kubectl delete job "$JOB" --ignore-not-found >/dev/null
kubectl wait --for=delete pod -l "job-name=$JOB" --timeout=120s 2>/dev/null || true

start_stage_pod
fetch_results
stop_stage_pod

echo "Done. Results are in: $RESULTS"
