#!/usr/bin/env bash
set -euo pipefail
# The one deploy script — used both locally and in CI via the deploy-runner
# image (deploy/Dockerfile), so there's nothing else to keep in sync.
# Fully idempotent — safe to run top-to-bottom on a fresh or existing cluster.
#
# Local, from the repo root (after `docker build -f deploy/Dockerfile -t
# deploy-runner .`), with the cluster artifact already at
# infrastructure/state/<CLUSTER_NAME>.json (see the "k3s: Fetch artifact" /
# "k3s: Provision (AWS)" tasks):
#
#   docker run --rm -v "$(pwd):/workspace" -w /workspace --env-file .env \
#     deploy-runner
#
# CI passes the same env vars individually via -e from GitHub Secrets
# instead of --env-file (see .github/workflows/deploy.yaml).
#
# GPU node scheduling and the NVIDIA container runtime are handled entirely
# by k3s-anywhere's GPU cloud-init + this chart's nvidia-device-plugin
# DaemonSet — no SSH-based node bootstrapping needed here.

CLUSTER_NAME="${CLUSTER_NAME:-campaign-setting-query-engine}"
ARTIFACT="infrastructure/state/${CLUSTER_NAME}.json"
export KUBECONFIG="infrastructure/state/${CLUSTER_NAME}-kubeconfig.yaml"

IMAGE_ORGANIZATION="${IMAGE_ORGANIZATION:?IMAGE_ORGANIZATION is required — the Docker Hub org/username the images were published under.}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
LLM_IMAGE_TAG="${LLM_IMAGE_TAG:-qwen3.5-9b-q4km}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required to register the cert-manager ACME account for the ClusterIssuer.}"

# ── Kubeconfig ────────────────────────────────────────────────────────────────

jq -r '.kubeconfig' "${ARTIFACT}" > "${KUBECONFIG}"
kubectl cluster-info

# ── Wait for Longhorn CSI ───────────────────────────────────────────────────
# On a fresh cluster Longhorn takes ~3-5 min to initialise. Helm must not run
# until the CSI driver is registered, otherwise PVCs stay Pending and the
# deploy times out.

echo "Waiting for Longhorn manager (up to 5 min)..."
kubectl rollout status daemonset/longhorn-manager    -n longhorn-system --timeout=300s
echo "Waiting for Longhorn CSI plugin (up to 5 min)..."
kubectl rollout status daemonset/longhorn-csi-plugin -n longhorn-system --timeout=300s

# ── cert-manager ─────────────────────────────────────────────────────────────

helm repo add jetstack https://charts.jetstack.io --force-update
helm repo update jetstack
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set installCRDs=true \
  --wait --timeout 5m

# cert-manager's webhook can take a few extra seconds to start admitting
# ClusterIssuer/Certificate CRDs after its Deployments report Ready.
sleep 10

# ── Domain ────────────────────────────────────────────────────────────────
# No external DNS for this deployment — nip.io resolves any subdomain of
# <ip-with-dashes>.nip.io back to the embedded IP, so cert-manager can still
# issue a real Let's Encrypt cert over HTTP-01 through Traefik (k3s's
# built-in ingress controller — nothing extra to install for that part).

ELASTIC_IP=$(jq -r '.elastic_ip // empty' "${ARTIFACT}")
if [ -z "${ELASTIC_IP}" ]; then
  echo "::error::No elastic_ip in ${ARTIFACT} — set ELASTIC_IP_COUNT=1 in the cluster config and re-provision." >&2
  exit 1
fi
DOMAIN="$(echo "${ELASTIC_IP}" | tr '.' '-').nip.io"
echo "Domain: ${DOMAIN}"

# ── Deploy ───────────────────────────────────────────────────────────────

helm upgrade --install csqe chart/ \
  --set image.organization="${IMAGE_ORGANIZATION}" \
  --set image.tag="${IMAGE_TAG}" \
  --set domain="${DOMAIN}" \
  --set server.ingress.enabled=true \
  --set dashboard.ingress.enabled=true \
  --set inspector.enabled=true \
  --set inspector.ingress.enabled=true \
  --set llmServer.enabled=true \
  --set llmServer.image.tag="${LLM_IMAGE_TAG}" \
  --set certManager.enabled=true \
  --set certManager.issuer.email="${LETSENCRYPT_EMAIL}" \
  --atomic --timeout 30m

# ── Verify rollout ─────────────────────────────────────────────────────────

kubectl rollout status deployment/csqe-fuseki     --timeout=5m
kubectl rollout status deployment/csqe-server     --timeout=5m
kubectl rollout status deployment/csqe-dashboard  --timeout=5m
kubectl rollout status deployment/csqe-inspector  --timeout=5m
kubectl rollout status deployment/csqe-llm-server --timeout=10m

echo ""
echo "Deployed. Reachable at:"
echo "  Dashboard:  https://${DOMAIN}"
echo "  MCP server: https://mcp.${DOMAIN}/mcp"
echo "  Inspector:  https://inspector.${DOMAIN}  (kubectl logs deploy/csqe-inspector for the auth token)"
