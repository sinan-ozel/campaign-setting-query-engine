#!/usr/bin/env bash
set -euo pipefail
# Points Longhorn's backup target at this release's versioned path in the
# backup bucket, so every major.minor gets its own backups:
#
#   0.1.x  → s3://<bucket>@<region>/          (bucket root: the backups taken
#                                              before versioned paths existed)
#   x.y.z  → s3://<bucket>@<region>/v<x.y>/   (0.2 and later)
#
# Restoring only ever sees the current major.minor's backups. After a minor
# bump (= an ontology change) the new path is empty, so a fresh cluster starts
# with an empty graph and re-ingests instead of restoring an incompatible one.
#
# Used by .github/workflows/backup.yaml (before taking a backup) and
# restore/src/restore-from-backup.sh (before looking for one). Expects:
#   - the repo root as the working directory (reads server/__init__.py)
#   - kubectl already pointed at the cluster
#   - the cluster artifact at infrastructure/state/<CLUSTER_NAME>.json

CLUSTER_NAME="${CLUSTER_NAME:-campaign-setting-query-engine}"
ARTIFACT="infrastructure/state/${CLUSTER_NAME}.json"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' server/__init__.py)
MAJOR_MINOR=$(echo "${VERSION}" | cut -d. -f1,2)
if [ -z "${MAJOR_MINOR}" ]; then
  echo "::error::Could not read __version__ from server/__init__.py — run this from the repo root."
  exit 1
fi
if [ "${MAJOR_MINOR}" = "0.1" ]; then
  BACKUP_PATH=""
else
  BACKUP_PATH="v${MAJOR_MINOR}/"
fi

ACCESS_KEY=$(jq -r '.backup.access_key' "${ARTIFACT}")
SECRET_KEY=$(jq -r '.backup.secret_key' "${ARTIFACT}")
ENDPOINT=$(jq -r '.backup.endpoint // ""' "${ARTIFACT}")
BUCKET=$(jq -r '.backup.bucket' "${ARTIFACT}")
REGION=$(jq -r '.region // empty' "${ARTIFACT}")
REGION="${REGION:-${AWS_DEFAULT_REGION:?Set AWS_DEFAULT_REGION or add region to ${ARTIFACT}}}"

# Credentials secret (creates or replaces)
kubectl create secret generic longhorn-backup-secret \
  -n longhorn-system \
  --from-literal=AWS_ACCESS_KEY_ID="${ACCESS_KEY}" \
  --from-literal=AWS_SECRET_ACCESS_KEY="${SECRET_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Append custom endpoint if the bucket is not on standard AWS S3
if [ -n "${ENDPOINT}" ]; then
  kubectl patch secret longhorn-backup-secret -n longhorn-system \
    --type=merge \
    -p "{\"stringData\":{\"AWS_ENDPOINTS\":\"${ENDPOINT}\"}}"
fi

TARGET="s3://${BUCKET}@${REGION}/${BACKUP_PATH}"
echo "Longhorn backup target for v${VERSION}: ${TARGET}"

kubectl patch settings.longhorn.io backup-target \
  -n longhorn-system \
  --type=merge \
  -p "{\"value\": \"${TARGET}\"}"

kubectl patch settings.longhorn.io backup-target-credential-secret \
  -n longhorn-system \
  --type=merge \
  -p "{\"value\": \"longhorn-backup-secret\"}"

# Give Longhorn a moment to pick up the new target and sync its backup list.
sleep 10
