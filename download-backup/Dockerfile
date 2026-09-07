# Build from repo root:
#   docker build -f download-backup/Dockerfile -t download-backup-runner .
#
# Run with the repo mounted at /workspace and the cluster artifact at
# infrastructure/state/<cluster-name>.json:
#
#   docker run --rm -v "$(pwd):/workspace" -w /workspace \
#     -e CLUSTER_NAME=campaign-setting-query-engine \
#     download-backup-runner

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates jq coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

COPY download-backup/src/download-fuseki-backup.sh /app/download-fuseki-backup.sh
RUN chmod +x /app/download-fuseki-backup.sh

ENTRYPOINT ["/app/download-fuseki-backup.sh"]
