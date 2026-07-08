#!/usr/bin/env bash
set -euo pipefail
# Restores PVCs from their latest Longhorn backup before Helm creates them
# fresh. Safe on a brand-new cluster with no backups yet — skips gracefully
# per volume. Run with the same env/mount conventions as deploy-k3s.sh, and
# always *before* it (see .github/workflows/deploy.yaml's restore job, or
# the "k3s: Restore from backup" task).
#
# This is also how the graph moves to cheap CPU-only infrastructure: point
# this script at the same STATE_BUCKET_NAME/backup bucket from a second,
# GPU_NODE_COUNT=0 cluster, then `helm upgrade --install ... --set
# ingest.enabled=false --set llmServer.enabled=false` — see docs/helm.md.
#
# NOTE: this is the least battle-tested part of the deploy pipeline — it
# manipulates Longhorn's Volume CRD directly (fromBackup, accessMode,
# frontend fields). Verify a real restore after the next provision before
# trusting it blindly; if `kubectl apply` on the Volume manifest fails with
# a schema error, the field names below need adjusting for the installed
# Longhorn version.
#
#   docker run --rm -v "$(pwd):/workspace" -w /workspace \
#     -e CLUSTER_NAME=campaign-setting-query-engine \
#     --entrypoint /app/restore-from-backup.sh \
#     deploy-runner

CLUSTER_NAME="${CLUSTER_NAME:-campaign-setting-query-engine}"
ARTIFACT="output/${CLUSTER_NAME}.json"
export KUBECONFIG="output/${CLUSTER_NAME}-kubeconfig.yaml"
STORAGE_CLASS="longhorn"
NAMESPACE="default"

jq -r '.kubeconfig' "${ARTIFACT}" > "${KUBECONFIG}"

echo "Waiting for Longhorn manager (up to 5 min)..."
kubectl rollout status daemonset/longhorn-manager -n longhorn-system --timeout=300s

# Give Longhorn a moment to sync BackupVolumes/Backups from the configured
# S3 backup target before we go looking for them.
sleep 15

# <pvc-name>:<accessMode>:<sizeGi-display>
VOLUMES=(
  "csqe-fuseki-data:ReadWriteOnce:50Gi"
  "csqe-redis-data:ReadWriteOnce:10Gi"
  "csqe-minio-data:ReadWriteOnce:200Gi"
)

for ENTRY in "${VOLUMES[@]}"; do
  IFS=":" read -r PVC_NAME ACCESS_MODE SIZE <<< "${ENTRY}"

  if kubectl get pvc "${PVC_NAME}" -n "${NAMESPACE}" &>/dev/null; then
    echo "PVC ${NAMESPACE}/${PVC_NAME} already exists — skipping restore."
    continue
  fi

  echo "Looking for latest backup labeled app=${PVC_NAME}..."
  BACKUP_JSON=$(kubectl get backups.longhorn.io -n longhorn-system -o json \
    | jq -c --arg app "${PVC_NAME}" \
        '[.items[] | select(.status.labels.app == $app)]
         | sort_by(.status.snapshotCreatedAt // .metadata.creationTimestamp)
         | last // empty')

  if [ -z "${BACKUP_JSON}" ] || [ "${BACKUP_JSON}" = "null" ]; then
    echo "No backup found for ${PVC_NAME} — Helm will provision an empty volume."
    continue
  fi

  BACKUP_URL=$(echo "${BACKUP_JSON}" | jq -r '.status.url')
  BACKUP_NAME=$(echo "${BACKUP_JSON}" | jq -r '.metadata.name')
  echo "Restoring ${PVC_NAME} from backup ${BACKUP_NAME} (${BACKUP_URL})"

  VOLUME_NAME="${PVC_NAME}"
  [ "${ACCESS_MODE}" = "ReadWriteMany" ] && LH_ACCESS_MODE="rwx" || LH_ACCESS_MODE="rwo"

  cat <<EOF | kubectl apply -f -
apiVersion: longhorn.io/v1beta2
kind: Volume
metadata:
  name: ${VOLUME_NAME}
  namespace: longhorn-system
spec:
  size: "$(numfmt --from=iec-i "${SIZE}")"
  # 1 replica, not the cluster's configured default (see k3s-anywhere's
  # post_provision.sh) — this has to succeed even on a single-node cluster
  # restoring into a cheap query-only deployment (see docs/helm.md).
  numberOfReplicas: 1
  fromBackup: "${BACKUP_URL}"
  frontend: blockdev
  accessMode: ${LH_ACCESS_MODE}
EOF

  echo "Waiting for volume ${VOLUME_NAME} to finish restoring (up to 10 min)..."
  ELAPSED=0
  until [ "$(kubectl get volumes.longhorn.io "${VOLUME_NAME}" -n longhorn-system -o jsonpath='{.status.restoreRequired}' 2>/dev/null)" != "true" ] \
     && [ "$(kubectl get volumes.longhorn.io "${VOLUME_NAME}" -n longhorn-system -o jsonpath='{.status.state}' 2>/dev/null)" = "detached" ]; do
    sleep 10
    ELAPSED=$((ELAPSED + 10))
    [ $ELAPSED -lt 600 ] || { echo "::error::Timeout waiting for volume ${VOLUME_NAME} to restore"; exit 1; }
  done

  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${VOLUME_NAME}-pv
spec:
  capacity:
    storage: ${SIZE}
  accessModes:
    - ${ACCESS_MODE}
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ${STORAGE_CLASS}
  csi:
    driver: driver.longhorn.io
    fsType: ext4
    volumeHandle: ${VOLUME_NAME}
  claimRef:
    namespace: ${NAMESPACE}
    name: ${PVC_NAME}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ${ACCESS_MODE}
  storageClassName: ${STORAGE_CLASS}
  volumeName: ${VOLUME_NAME}-pv
  resources:
    requests:
      storage: ${SIZE}
EOF

  echo "Restored ${NAMESPACE}/${PVC_NAME} from backup ${BACKUP_NAME}."
done
