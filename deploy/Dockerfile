# Build from repo root:
#   docker build -f deploy/Dockerfile -t deploy-runner .
#
# Run with the repo mounted at /workspace and the cluster artifact at
# infrastructure/state/<cluster-name>.json:
#
#   docker run --rm -v "$(pwd):/workspace" -w /workspace \
#     -e CLUSTER_NAME=campaign-setting-query-engine \
#     -e IMAGE_ORGANIZATION=... \
#     -e LETSENCRYPT_EMAIL=... \
#     deploy-runner

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates jq \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

COPY deploy/src/deploy-k3s.sh /app/deploy-k3s.sh
RUN chmod +x /app/deploy-k3s.sh

ENTRYPOINT ["/app/deploy-k3s.sh"]
