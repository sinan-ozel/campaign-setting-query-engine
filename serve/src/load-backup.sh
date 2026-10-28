#!/usr/bin/env sh
set -eu
# Loads a downloaded Fuseki backup (.nq.gz) into an empty Fuseki for the
# read-only serve mode (serve/docker-compose.yaml), then exits.
#
# The backup filename is the version contract: campaign_v<major>.<minor>_*.nq.gz
# (written by download-backup/). A backup is only loaded when its major.minor
# matches this image's server/__init__.py, because the MCP server's tools are
# built from the same ontology that produced the graph. A minor bump means the
# ontology changed and the books must be re-ingested.
#
# Env:
#   BACKUP_FILE      file name inside /backups to load (default: the newest
#                    campaign_v<major>.<minor>_*.nq.gz)
#   FUSEKI_URL       default http://fuseki:3030
#   FUSEKI_DATASET   default campaign
#   FUSEKI_PASSWORD  Fuseki admin password

FUSEKI_URL="${FUSEKI_URL:-http://fuseki:3030}"
DATASET="${FUSEKI_DATASET:-campaign}"
AUTH="admin:${FUSEKI_PASSWORD:?Set FUSEKI_PASSWORD to the Fuseki admin password.}"
META_GRAPH="urn:csqe:meta"
CS="http://campaignsetting.io/ontology#"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' /app/server/__init__.py)
MAJOR_MINOR=$(echo "${VERSION}" | cut -d. -f1,2)
echo "Server version ${VERSION}: loading a v${MAJOR_MINOR} backup."

if [ -n "${BACKUP_FILE:-}" ]; then
  FILE="/backups/${BACKUP_FILE}"
else
  FILE=$(ls -t /backups/campaign_v"${MAJOR_MINOR}"_*.nq.gz 2>/dev/null | head -1 || true)
fi

if [ -z "${FILE}" ] || [ ! -f "${FILE}" ]; then
  echo "ERROR: no backup for v${MAJOR_MINOR} in ./backups/." >&2
  echo "Backups present:" >&2
  ls -1 /backups >&2 || true
  echo "Download one with the 'k3s: Download Fuseki backup' task, or check out" >&2
  echo "the release whose version matches the backup you have." >&2
  exit 1
fi

NAME=$(basename "${FILE}")
FILE_MM=$(echo "${NAME}" | sed -n 's/^campaign_v\([0-9]*\.[0-9]*\)_.*\.nq\.gz$/\1/p')
if [ "${FILE_MM}" != "${MAJOR_MINOR}" ]; then
  echo "ERROR: ${NAME} is not a v${MAJOR_MINOR} backup (expected campaign_v${MAJOR_MINOR}_*.nq.gz)." >&2
  echo "Its ontology does not match this server. Use a v${MAJOR_MINOR} backup, or check out" >&2
  echo "the release that matches v${FILE_MM:-unknown}." >&2
  exit 1
fi

sparql() {
  curl -sf -u "${AUTH}" "${FUSEKI_URL}/${DATASET}/sparql" \
    -H "Accept: text/csv" --data-urlencode "query=$1" | tail -n +2 | tr -d '\r'
}

LOADED=$(sparql "SELECT ?f WHERE { GRAPH <${META_GRAPH}> { ?s <${CS}loadedFrom> ?f } }")
if [ "${LOADED}" = "${NAME}" ]; then
  echo "${NAME} is already loaded; nothing to do."
  exit 0
fi
TRIPLES=$(sparql "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }")
if [ "${TRIPLES}" != "0" ]; then
  echo "ERROR: the Fuseki volume already holds ${TRIPLES} triples (loaded from: ${LOADED:-unknown})." >&2
  echo "To load ${NAME} instead, wipe it first:" >&2
  echo "  docker compose -f serve/docker-compose.yaml down -v" >&2
  exit 1
fi

echo "Loading ${NAME} ($(du -h "${FILE}" | cut -f1)) into ${DATASET}..."
gunzip -c "${FILE}" | curl -sf -u "${AUTH}" -X POST \
  -H "Content-Type: application/n-quads" --data-binary @- \
  "${FUSEKI_URL}/${DATASET}/data" > /dev/null

curl -sf -u "${AUTH}" "${FUSEKI_URL}/${DATASET}/update" \
  --data-urlencode "update=INSERT DATA { GRAPH <${META_GRAPH}> { <urn:csqe:dataset> <${CS}loadedFrom> \"${NAME}\" } }" > /dev/null

echo "Loaded ${NAME}: $(sparql "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }") triples."
