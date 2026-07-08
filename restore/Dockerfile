# Build from repo root:
#   docker build -f restore/Dockerfile -t restore-runner .
#
# Run with the repo mounted at /workspace and the cluster artifact at
# output/<cluster-name>.json:
#
#   docker run --rm -v "$(pwd):/workspace" -w /workspace \
#     -e CLUSTER_NAME=campaign-setting-query-engine \
#     restore-runner

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates jq coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

COPY restore/src/restore-from-backup.sh /app/restore-from-backup.sh
RUN chmod +x /app/restore-from-backup.sh

ENTRYPOINT ["/app/restore-from-backup.sh"]
