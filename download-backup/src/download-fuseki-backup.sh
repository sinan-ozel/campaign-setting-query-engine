#!/usr/bin/env bash
set -euo pipefail
# Triggers a Fuseki TDB2 backup (N-Quads, preserves named graphs — one per
# ingested document, see graph_worker/src/mapper.py's INSERT DATA { GRAPH
# <...> {...} }) via Fuseki's admin API, then downloads the resulting file
# to the host. This is a copy independent of the S3 target
# .github/workflows/backup.yaml writes to via Longhorn snapshots — for when
# you want your own copy of the graph in hand, e.g. before a risky change,
# or as a second line of defense if the S3 backup target is ever lost.
#
# Local, from the repo root (after `docker build -f
# download-backup/Dockerfile -t download-backup-runner .`), with the cluster
# artifact already at infrastructure/state/<CLUSTER_NAME>.json (see the
# "k3s: Fetch artifact" task):
#
#   docker run --rm -v "$(pwd):/workspace" -w /workspace \
#     -e CLUSTER_NAME=campaign-setting-query-engine \
#     download-backup-runner
#
# Writes the .nq.gz to ./backups/ on the host by default (OUTPUT_DIR).

CLUSTER_NAME="${CLUSTER_NAME:-campaign-setting-query-engine}"
ARTIFACT="infrastructure/state/${CLUSTER_NAME}.json"
export KUBECONFIG="infrastructure/state/${CLUSTER_NAME}-kubeconfig.yaml"
NAMESPACE="default"
RELEASE_NAME="${RELEASE_NAME:-csqe}"
# Must match chart/files/fuseki-config.ttl's fuseki:name.
DATASET="campaign"
OUTPUT_DIR="${OUTPUT_DIR:-backups}"

jq -r '.kubeconfig' "${ARTIFACT}" > "${KUBECONFIG}"

POD=$(kubectl get pod -n "${NAMESPACE}" -l app.kubernetes.io/component=fuseki \
  -o jsonpath='{.items[0].metadata.name}')
if [ -z "${POD}" ]; then
  echo "::error::No fuseki pod found in namespace ${NAMESPACE} (label" \
    "app.kubernetes.io/component=fuseki). Is the ${RELEASE_NAME} release deployed?"
  exit 1
fi

# Read the live secret rather than assuming the chart's "changeme" default,
# so this keeps working after the admin password is rotated.
ADMIN_PASSWORD=$(kubectl get secret "${RELEASE_NAME}-fuseki-creds" -n "${NAMESPACE}" \
  -o jsonpath='{.data.admin-password}' | base64 -d)

echo "Triggering backup of dataset '${DATASET}' on pod ${POD}..."
TASK_ID=$(kubectl exec "${POD}" -n "${NAMESPACE}" -c fuseki -- \
  curl -sf -u "admin:${ADMIN_PASSWORD}" -X POST "http://localhost:3030/\$/backup/${DATASET}" \
  | jq -r '.taskId // empty')

if [ -z "${TASK_ID}" ]; then
  echo "::error::Fuseki did not return a taskId for the backup request — check the pod's own logs."
  exit 1
fi

echo "Waiting for backup task ${TASK_ID} to finish..."
ELAPSED=0
while true; do
  TASK_JSON=$(kubectl exec "${POD}" -n "${NAMESPACE}" -c fuseki -- \
    curl -sf -u "admin:${ADMIN_PASSWORD}" "http://localhost:3030/\$/tasks/${TASK_ID}")
  FINISHED=$(echo "${TASK_JSON}" | jq -r '.finished // empty')
  if [ -n "${FINISHED}" ]; then
    SUCCESS=$(echo "${TASK_JSON}" | jq -r '.success')
    [ "${SUCCESS}" = "true" ] || { echo "::error::Backup task ${TASK_ID} failed: ${TASK_JSON}"; exit 1; }
    break
  fi
  sleep 2
  ELAPSED=$((ELAPSED + 2))
  [ $ELAPSED -lt 120 ] || { echo "::error::Timeout waiting for backup task ${TASK_ID}"; exit 1; }
done

REMOTE_PATH=$(kubectl exec "${POD}" -n "${NAMESPACE}" -c fuseki -- \
  sh -c "ls -t /fuseki/backups/${DATASET}_*.nq.gz | head -1")
FILENAME=$(basename "${REMOTE_PATH}")

mkdir -p "${OUTPUT_DIR}"
kubectl cp "${NAMESPACE}/${POD}:${REMOTE_PATH}" "${OUTPUT_DIR}/${FILENAME}" -c fuseki

echo "Downloaded $(du -h "${OUTPUT_DIR}/${FILENAME}" | cut -f1) to ${OUTPUT_DIR}/${FILENAME}"
