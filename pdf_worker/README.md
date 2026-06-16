# pdf_worker

Converts PDFs to Markdown and registers them for graph processing.

## What it does

1. **Scans** a mounted `INPUT_DIR` for `.pdf` files (`folder_watcher.py`).
2. **Uploads** each new PDF and a synthesised YAML sidecar to MinIO `/raw-pdfs/`.
3. **Claims** unclaimed documents via a Redis distributed lock.
4. **Converts** the PDF to Markdown page-by-page using `pymupdf4llm`.
5. **Falls back to OCR** if the text yield looks image-based (< 50 words/page).
6. **Uploads** the assembled Markdown to MinIO `/markdown/`.
7. **Sets** Redis state to `MARKDOWN_READY`, which the graph-worker picks up next.

## Source files

| File | Responsibility |
|---|---|
| `src/main.py` | Poll loop, Redis lock helpers, PDF conversion, MinIO upload |
| `src/folder_watcher.py` | Scan `INPUT_DIR`, derive metadata from folder paths, register PDFs |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `MINIO_ENDPOINT` | `http://localhost:9000` | MinIO base URL |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `INPUT_DIR` | _(empty)_ | Directory to watch for PDFs. If empty, folder scanning is skipped and only MinIO is polled. |
| `LOCK_TTL_SECONDS` | `120` | Lock expiry per page. Refreshed after each page is converted. |
| `POLL_INTERVAL_SECONDS` | `5` | Seconds between scan iterations |
| `OCR_WORDS_PER_PAGE_THRESHOLD` | `50` | Falls back to OCR when average words/page is below this |
| `OCR_LANGUAGE` | `eng` | Tesseract language code for OCR fallback |

## Document state machine

The worker only claims documents in `PENDING` (or `null`) state. It never auto-retries `FAILED`.

```
(none)  →  PENDING  →  CONVERTING_PDF  →  MARKDOWN_READY
                                       ↘  FAILED
```

State is stored in Redis hashes at `doc:<document_id>:state`. The lock lives at `doc:<document_id>:lock` and is released with a Lua `GET-then-DEL` script so it is safe under expiry races.

Key fields set at each stage:

| Stage | Fields written |
|---|---|
| PENDING | `status`, `title`, `tags`, `source_path`, `edition`, `canon_type`, `created_at` |
| CONVERTING_PDF | `status`, `worker_id`, `started_at`, `total_pages`, `current_page` |
| MARKDOWN_READY | `status`, `completed_at` |
| FAILED | `status`, `error`, `last_successful_stage` |

## Folder-based metadata (`folder_watcher.py`)

PDFs under `INPUT_DIR` do not need a hand-written sidecar. Metadata is derived from the folder path:

- A folder component that is one of `3e 4e 5e any` → **`edition`**
- A component that is one of `canon kanon community` → **`canon_type`**
- All remaining components become **`tags`**
- A `.yaml` file alongside the PDF can override any of these fields

Example:

```
input/
  5e/
    canon/
      Eberron Rising from the Last War.pdf   # edition=5e, canon_type=canon
    homebrew/
      My Setting.pdf                          # edition=5e, tags=[homebrew]
```

The `document_id` is derived by slugifying the relative path (without extension): spaces and special characters become `-`, runs of `-` are collapsed.

## Re-ingestion

If a PDF on disk has a newer `mtime` than the `ingestion_completed_at` timestamp recorded in Redis, the file is re-uploaded and its state is reset to `PENDING`. In-flight (no `ingestion_completed_at`) or `FAILED` documents are left untouched.

## Markdown output format

The final Markdown written to `/markdown/<document_id>.md` has:

1. **YAML front matter** — `document_id`, `title`, `tags`, `edition`, `canon_type`, page count, PDF embedded metadata, and `ocr: true` if OCR was used.
2. **Page markers** — `<!-- page: N -->` before each page so the graph-worker can track provenance back to a page number.

## JPX image error handling

`pymupdf4llm` can raise `FzErrorLibrary` on pages with JPEG 2000 (JPX) images. The worker retries the page with `ignore_images=True`, and if that also fails, inserts a placeholder:

```
[Page N skipped — JPX image decode error]
```

## Running the tests

```
docker compose -f tests/pdf_worker/docker-compose.yml up --build --abort-on-container-exit
```

Or via VS Code: **Terminal → Run Task → test: pdf-worker**.

The test suite spins up Redis and MinIO, mounts `tests/fixtures/` as the input directory, and asserts that PDFs are converted and appear in MinIO.

## Dependencies

Declared in `pyproject.toml` under the `[pdf]` extras group:

- `pymupdf` — PDF parsing and page rendering
- `pymupdf4llm` — Markdown extraction layer on top of pymupdf
- `pymupdf-layout` — layout-aware text ordering
- `redis`, `minio`, `pyyaml` — infrastructure clients
