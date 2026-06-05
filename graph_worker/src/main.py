"""Graph worker — Markdown → knowledge graph triples.

Polls MinIO /markdown/ for documents in MARKDOWN_READY state. Semantically
chunks the Markdown, classifies each chunk, extracts all entity types via a
single combined LLM call, maps to the ontology, and writes triples to Fuseki.
Extends the Redis lock TTL after each chunk so the lock survives slow LLM calls.

Environment variables:
  FUSEKI_ENDPOINT          http://...:3030/campaign
  REDIS_URL                redis://...:6379
  MINIO_ENDPOINT           http://...:9000
  MINIO_ACCESS_KEY         minioadmin
  MINIO_SECRET_KEY         minioadmin
  LLAMA_CPP_HOST           http://...:8080/v1
  LLM_MODEL                openai/gemma4:e2b
  LOCK_TTL_SECONDS         300
  POLL_INTERVAL_SECONDS    10
  INGESTION_CONFIG_PATH    /config/ingestion_config.yaml
"""

import io
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import redis
import yaml
from minio import Minio

from .chunker import MarkdownChunker
from .extractor import classify_chunk, extract_entities
from . import mapper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("graph_worker")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
LOCK_TTL_SECONDS = int(os.environ.get("LOCK_TTL_SECONDS", "300"))
LOCK_TTL_MS = LOCK_TTL_SECONDS * 1000
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))

WORKER_ID = os.environ.get("HOSTNAME", f"graph-worker-{uuid.uuid4().hex[:8]}")
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
    """Atomically claim a MARKDOWN_READY document for graph processing."""
    lock_key = _lock_key(document_id)
    state_key = _state_key(document_id)
    claimed = r.set(lock_key, WORKER_ID, nx=True, px=LOCK_TTL_MS)
    if not claimed:
        return False
    now = _now()
    r.hset(
        state_key,
        mapping={
            "status": "CLASSIFYING_SECTIONS",
            "worker_id": WORKER_ID,
            "started_at": now,
            "updated_at": now,
        },
    )
    return True


def refresh_lock(r: redis.Redis, document_id: str, current_chunk: int) -> None:
    """Update chunk progress and extend lock TTL."""
    r.hset(
        _state_key(document_id),
        mapping={"current_chunk": current_chunk, "updated_at": _now()},
    )
    r.pexpire(_lock_key(document_id), LOCK_TTL_MS)


def release_lock(r: redis.Redis, document_id: str) -> None:
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
            "graph_worker: lock for %s already gone — possible expiry.",
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


# ── Main ingestion function ────────────────────────────────────────────────


def process_markdown(
    r: redis.Redis,
    minio_client: Minio,
    document_id: str,
) -> None:
    """Download, chunk, extract, and write triples for one document."""
    state_key = _state_key(document_id)

    # Download Markdown from MinIO
    md_response = minio_client.get_object(MARKDOWN_BUCKET, f"{document_id}.md")
    md_text = md_response.read().decode("utf-8")
    md_response.close()
    md_response.release_conn()

    # Parse front matter and body
    book_meta, body = MarkdownChunker.parse_frontmatter(md_text)
    yaml_meta = book_meta  # front matter contains document_id, edition, etc.

    # Extract TOC, chunk body
    toc, body = MarkdownChunker.extract_toc_and_body(body)
    chunks = MarkdownChunker.chunk_markdown(body, book_meta, toc)

    total_chunks = len(chunks)
    r.hset(state_key, mapping={"total_chunks": total_chunks, "current_chunk": 0})
    logger.info(
        "graph_worker: %s — %d chunks to process.", document_id, total_chunks
    )

    all_triples: list[str] = []
    entity_count = 0

    for idx, chunk in enumerate(chunks):
        chunk_text = chunk["text"]
        page_ref = str(chunk["metadata"].get("page_number") or "")

        r.hset(state_key, mapping={"status": "CLASSIFYING_SECTIONS"})

        label = classify_chunk(chunk_text)
        logger.debug("graph_worker: chunk %d/%d → %s", idx + 1, total_chunks, label)

        if label == "ENTITIES":
            r.hset(state_key, mapping={"status": "EXTRACTING_ENTITIES"})
            known = mapper.get_known_entity_names(r, limit=20)
            extracted = extract_entities(chunk_text, known)

            r.hset(state_key, mapping={"status": "MAPPING_TO_ONTOLOGY"})
            triples = mapper.entities_to_triples(r, extracted, yaml_meta, page_ref or None)
            all_triples.extend(triples)

        refresh_lock(r, document_id, idx + 1)

    # Write all triples in one transaction
    r.hset(state_key, mapping={"status": "LOADING_GRAPH", "updated_at": _now()})
    entity_count, triple_count = mapper.write_triples_to_fuseki(
        document_id, all_triples
    )

    r.hset(
        state_key,
        mapping={
            "status": "COMPLETED",
            "completed_at": _now(),
            "updated_at": _now(),
            "entity_count": entity_count,
            "triple_count": triple_count,
        },
    )
    logger.info(
        "graph_worker: %s → COMPLETED (%d triples, ~%d entities).",
        document_id, triple_count, entity_count,
    )


# ── Poll loop ──────────────────────────────────────────────────────────────


def poll_loop() -> None:
    """Continuously scan Redis for MARKDOWN_READY documents."""
    r = _redis_client()
    mc = _minio_client()

    logger.info(
        "graph_worker started. WORKER_ID=%s  LOCK_TTL=%ss  POLL=%ss",
        WORKER_ID, LOCK_TTL_SECONDS, POLL_INTERVAL,
    )

    while True:
        try:
            # Scan Redis for all document state keys
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match="doc:*:state", count=100)
                for key in keys:
                    status = r.hget(key, "status")
                    if status != "MARKDOWN_READY":
                        continue
                    document_id = key.split(":")[1]

                    if try_claim(r, document_id):
                        logger.info("graph_worker: claimed %s.", document_id)
                        try:
                            process_markdown(r, mc, document_id)
                        except Exception as exc:
                            logger.exception(
                                "graph_worker: processing failed for %s.", document_id
                            )
                            mapper.drop_named_graph(document_id)
                            set_failed(r, document_id, str(exc))
                        finally:
                            release_lock(r, document_id)

                if cursor == 0:
                    break

        except Exception as exc:
            logger.error("graph_worker: scan loop error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll_loop()
