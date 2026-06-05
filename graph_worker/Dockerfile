FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY README.md .
COPY server/__init__.py ./server/__init__.py
COPY graph_worker/src/ ./graph_worker/

RUN pip install --no-cache-dir ".[graph]"

# Pre-download the fastembed model so containers without internet can start.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('nomic-ai/nomic-embed-text-v1.5')"

CMD ["python", "-m", "graph_worker.main"]
