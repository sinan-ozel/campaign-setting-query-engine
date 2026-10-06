# Unit tests for the graph-worker's ontology mapper and prompt builder,
# against the real config/ontology_schema.yaml. No LLM, Fuseki or Redis.
#
#   docker compose -f tests/mapper/docker-compose.yml up --build \
#     --abort-on-container-exit --exit-code-from test-runner

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY README.md .
COPY server/__init__.py ./server/__init__.py
RUN pip install --no-cache-dir ".[graph]" "pytest>=7.0.0"

COPY graph_worker/src/ ./graph_worker/
COPY config/ ./config/
COPY tests/mapper/ ./tests/mapper/

ENV ONTOLOGY_SCHEMA_PATH=/app/config/ontology_schema.yaml \
    LLM_CONFIG_PATH=/app/config/llm.yaml \
    CONTEXT_WINDOW=16384 \
    PYTHONPATH=/app

CMD ["pytest", "tests/mapper/", "-v", "--tb=short"]
