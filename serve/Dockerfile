# One-shot loader for the read-only serve mode: loads a downloaded backup
# from ./backups/ into Fuseki. See serve/docker-compose.yaml.

FROM alpine:3.20

RUN apk add --no-cache curl coreutils

COPY server/__init__.py /app/server/__init__.py
COPY serve/src/load-backup.sh /app/load-backup.sh
RUN chmod +x /app/load-backup.sh

ENTRYPOINT ["/app/load-backup.sh"]
