FROM python:3.12-slim

# OCI build-time arguments — populated by CI from pyproject.toml and git context.
# Default to empty strings so local builds work without --build-arg.
ARG VERSION
ARG BUILD_DATE
ARG GIT_REVISION
ARG TITLE
ARG DESCRIPTION
ARG AUTHORS
ARG LICENSES
ARG SOURCE_URL
ARG DOCS_URL
ARG IMAGE_URL

WORKDIR /app

COPY pyproject.toml .
COPY server/ ./server/

RUN pip install --no-cache-dir -e "."

EXPOSE 8000

# https://github.com/opencontainers/image-spec/blob/main/annotations.md
LABEL org.opencontainers.image.title="${TITLE}" \
      org.opencontainers.image.description="${DESCRIPTION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.authors="${AUTHORS}" \
      org.opencontainers.image.licenses="${LICENSES}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.documentation="${DOCS_URL}" \
      org.opencontainers.image.url="${IMAGE_URL}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_REVISION}"

CMD ["python", "server/main.py"]
