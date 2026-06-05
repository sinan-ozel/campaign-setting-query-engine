FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY README.md .
COPY server/__init__.py ./server/__init__.py
COPY pdf_worker/src/ ./pdf_worker/

RUN pip install --no-cache-dir ".[pdf]"

CMD ["python", "-m", "pdf_worker.main"]
