"""PDF worker — converts PDFs to Markdown page-by-page.

Polls MinIO /raw-pdfs/ for unclaimed documents. Each document must have a
matching .yaml sidecar. Converts the PDF to Markdown using pymupdf4llm,
injecting <!-- page: N --> markers between pages so the graph-worker can
track provenance. Updates Redis state after each page for progress tracking
and lock heartbeat.

Environment variables:
  REDIS_URL          redis://...:6379
  MINIO_ENDPOINT     http://...:9000
  MINIO_ACCESS_KEY   minioadmin
  MINIO_SECRET_KEY   minioadmin
  LOCK_TTL_SECONDS   120   (lock TTL per page; extended after each page)
  POLL_INTERVAL_SECONDS  5
  OCR_WORDS_PER_PAGE_THRESHOLD  50
  OCR_LANGUAGE       eng
"""

import hashlib
import io
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pymupdf4llm
import yaml
from minio import Minio
from pymupdf.mupdf import FzErrorLibrary

import redis

from .folder_watcher import register_new_pdfs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pdf_worker")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
LOCK_TTL_SECONDS = int(os.environ.get("LOCK_TTL_SECONDS", "120"))
LOCK_TTL_MS = LOCK_TTL_SECONDS * 1000
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
OCR_THRESHOLD = int(os.environ.get("OCR_WORDS_PER_PAGE_THRESHOLD", "50"))
OCR_LANGUAGE = os.environ.get("OCR_LANGUAGE", "eng")
INPUT_DIR = os.environ.get("INPUT_DIR", "")

WORKER_ID = os.environ.get("HOSTNAME", f"pdf-worker-{uuid.uuid4().hex[:8]}")

RAW_PDFS_BUCKET = "raw-pdfs"
MARKDOWN_BUCKET = "markdown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_key(document_id: str) -> str:
    return f"doc:{document_id}:state"


def _lock_key(document_id: str) -> str:
    return f"doc:{document_id}:lock"


def _redis_client() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True)


def _minio_client() -> Minio:
    endpoint = MINIO_ENDPOINT.replace("http://", "").replace("https://", "")
    return Minio(
        endpoint,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_ENDPOINT.startswith("https://"),
    )


# ── Redis lock helpers ─────────────────────────────────────────────────────


def try_claim(r: redis.Redis, document_id: str) -> bool:
    """Atomically claim a document. Returns True if this worker claimed it."""
    lock_key = _lock_key(document_id)
    state_key = _state_key(document_id)
    claimed = r.set(lock_key, WORKER_ID, nx=True, px=LOCK_TTL_MS)
    if not claimed:
        return False
    now = _now()
    r.hset(
        state_key,
        mapping={
            "status": "CONVERTING_PDF",
            "worker_id": WORKER_ID,
            "started_at": now,
            "updated_at": now,
        },
    )
    return True


def refresh_and_update(
    r: redis.Redis, document_id: str, current_page: int
) -> bool:
    """Update page progress and extend lock TTL. Returns False if lock lost."""
    r.hset(
        _state_key(document_id),
        mapping={"current_page": current_page, "updated_at": _now()},
    )
    r.pexpire(_lock_key(document_id), LOCK_TTL_MS)
    return True


def release_lock(r: redis.Redis, document_id: str) -> None:
    """Release the lock only if this worker still owns it."""
    script = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
        return redis.call("DEL", KEYS[1])
    else
        return 0
    end
    """
    result = r.eval(script, 1, _lock_key(document_id), WORKER_ID)
    if result == 0:
        logger.warning(
            "pdf_worker: lock for %s was already gone — possible expiry during conversion.",
            document_id,
        )


def set_failed(r: redis.Redis, document_id: str, error: str) -> None:
    status = r.hget(_state_key(document_id), "status") or ""
    r.hset(
        _state_key(document_id),
        mapping={
            "status": "FAILED",
            "error": error[:2000],
            "last_successful_stage": status,
            "updated_at": _now(),
        },
    )


# ── PDF conversion ─────────────────────────────────────────────────────────


def _convert_page_safe(pdf_path: str, pno: int) -> str:
    """Convert one page to Markdown with three-level JPX fallback."""
    try:
        return pymupdf4llm.to_markdown(pdf_path, pages=[pno])
    except FzErrorLibrary as exc:
        if "Failed to decode JPX image" not in str(exc):
            raise

    try:
        return pymupdf4llm.to_markdown(pdf_path, pages=[pno], ignore_images=True)
    except FzErrorLibrary as exc:
        if "Failed to decode JPX image" not in str(exc):
            raise

    logger.warning(
        "pdf_worker: page %d — JPX decode error, inserting placeholder.", pno
    )
    return f"\n[Page {pno + 1} skipped — JPX image decode error]\n"


def _unique_top_level_header(md_text: str) -> str | None:
    """Return the single top-level heading text, or None."""
    headers: list[tuple[int, str]] = []
    for line in md_text.splitlines():
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            headers.append((level, line.lstrip("#").strip()))
    if not headers:
        return None
    min_level = min(lv for lv, _ in headers)
    tops = [txt for lv, txt in headers if lv == min_level]
    return tops[0] if len(tops) == 1 else None


def _build_front_matter(
    pdf_path: str,
    yaml_meta: dict,
    page_count: int,
    full_md: str,
    ocr_used: bool = False,
) -> str:
    """Build YAML front matter from sidecar metadata and PDF file metadata."""
    doc = pymupdf.open(pdf_path)
    pdf_meta = doc.metadata
    doc.close()

    data: dict = {
        "document_id": yaml_meta["document_id"],
        "title": yaml_meta["title"],
        "tags": yaml_meta.get("tags", []),
        "pdf_title": pdf_meta.get("title") or "",
        "pdf_author": pdf_meta.get("author") or "",
        "pages": page_count,
    }
    if yaml_meta.get("edition"):
        data["edition"] = yaml_meta["edition"]
    if yaml_meta.get("canon_type"):
        data["canon_type"] = yaml_meta["canon_type"]
    if ocr_used:
        data["ocr"] = True
    body_title = _unique_top_level_header(full_md)
    if body_title:
        data["body_title"] = body_title

    return "---\n" + yaml.dump(data, allow_unicode=True, sort_keys=False) + "---\n\n"


def convert_pdf(
    r: redis.Redis,
    minio_client: Minio,
    document_id: str,
) -> None:
    """Download, convert, and upload one PDF. Called inside the lock."""
    state_key = _state_key(document_id)

    # Download PDF
    pdf_response = minio_client.get_object(RAW_PDFS_BUCKET, f"{document_id}.pdf")
    pdf_bytes = pdf_response.read()
    pdf_response.close()
    pdf_response.release_conn()

    # Download YAML sidecar
    yaml_response = minio_client.get_object(RAW_PDFS_BUCKET, f"{document_id}.yaml")
    yaml_meta = yaml.safe_load(yaml_response.read())
    yaml_response.close()
    yaml_response.release_conn()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        pdf_path = tmp.name

    try:
        doc = pymupdf.open(pdf_path)
        total_pages = doc.page_count
        doc.close()

        r.hset(state_key, mapping={"total_pages": total_pages, "current_page": 0})

        # First pass — full text extraction
        page_texts: list[str] = []
        for pno in range(total_pages):
            page_md = _convert_page_safe(pdf_path, pno)
            page_texts.append(page_md)
            refresh_and_update(r, document_id, pno + 1)

        full_md = "\n\n".join(page_texts)

        # OCR fallback if the output looks image-based
        total_words = len(full_md.split())
        words_per_page = total_words / total_pages if total_pages else 0
        ocr_used = False
        if words_per_page < OCR_THRESHOLD:
            logger.info(
                "pdf_worker: %s looks image-based (%.1f words/page), retrying with OCR.",
                document_id,
                words_per_page,
            )
            page_texts = []
            for pno in range(total_pages):
                try:
                    page_md = pymupdf4llm.to_markdown(
                        pdf_path,
                        pages=[pno],
                        use_ocr=True,
                        ocr_language=OCR_LANGUAGE,
                    )
                except Exception:
                    page_md = _convert_page_safe(pdf_path, pno)
                page_texts.append(page_md)
                refresh_and_update(r, document_id, pno + 1)
            full_md = "\n\n".join(page_texts)
            ocr_used = True

        # Assemble final Markdown with front matter and page markers
        front_matter = _build_front_matter(
            pdf_path, yaml_meta, total_pages, full_md, ocr_used
        )
        parts = [front_matter]
        for pno, page_md in enumerate(page_texts):
            parts.append(f"<!-- page: {pno + 1} -->\n\n{page_md}")
        final_md = "\n\n".join(parts)

        # Upload to MinIO /markdown/
        md_bytes = final_md.encode("utf-8")
        minio_client.put_object(
            MARKDOWN_BUCKET,
            f"{document_id}.md",
            io.BytesIO(md_bytes),
            length=len(md_bytes),
            content_type="text/markdown",
        )

        r.hset(
            state_key,
            mapping={
                "status": "MARKDOWN_READY",
                "completed_at": _now(),
                "updated_at": _now(),
            },
        )
        logger.info("pdf_worker: %s → MARKDOWN_READY (%d pages).", document_id, total_pages)

    finally:
        Path(pdf_path).unlink(missing_ok=True)


# ── Poll loop ──────────────────────────────────────────────────────────────


def poll_loop() -> None:
    """Continuously scan MinIO /raw-pdfs/ for PENDING documents."""
    r = _redis_client()
    mc = _minio_client()

    # Ensure buckets exist
    for bucket in (RAW_PDFS_BUCKET, MARKDOWN_BUCKET):
        if not mc.bucket_exists(bucket):
            mc.make_bucket(bucket)

    logger.info(
        "pdf_worker started. WORKER_ID=%s  LOCK_TTL=%ss  POLL=%ss",
        WORKER_ID,
        LOCK_TTL_SECONDS,
        POLL_INTERVAL,
    )

    while True:
        try:
            if INPUT_DIR:
                register_new_pdfs(INPUT_DIR, r, mc, RAW_PDFS_BUCKET)

            objects = list(mc.list_objects(RAW_PDFS_BUCKET))
            for obj in objects:
                if not obj.object_name.endswith(".pdf"):
                    continue
                document_id = obj.object_name.removesuffix(".pdf")
                status = r.hget(_state_key(document_id), "status")

                # Only claim PENDING documents — never auto-retry FAILED
                if status not in (None, "PENDING"):
                    continue

                if try_claim(r, document_id):
                    logger.info("pdf_worker: claimed %s.", document_id)
                    try:
                        convert_pdf(r, mc, document_id)
                    except Exception as exc:
                        logger.exception(
                            "pdf_worker: conversion failed for %s.", document_id
                        )
                        set_failed(r, document_id, str(exc))
                    finally:
                        release_lock(r, document_id)

        except Exception as exc:
            logger.error("pdf_worker: scan loop error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll_loop()
