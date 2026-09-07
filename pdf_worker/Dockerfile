FROM python:3.12-slim

WORKDIR /app

# tesseract-ocr: pymupdf4llm's force_ocr/use_ocr path shells out to Tesseract;
# without the binary present it silently no-ops instead of erroring, so
# image-based PDFs would "succeed" with near-empty extracted text.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY README.md .
COPY server/__init__.py ./server/__init__.py
COPY pdf_worker/src/ ./pdf_worker/

RUN pip install --no-cache-dir ".[pdf]"

CMD ["python", "-m", "pdf_worker.main"]
