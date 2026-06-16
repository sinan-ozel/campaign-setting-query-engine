"""Redis pipeline-state helpers for the mcp-server."""

import os
from datetime import datetime, timezone

import redis.asyncio as aioredis

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

PAGE_SIZE = 10  # hardcoded; change here to adjust all paginated status views


def _redis() -> aioredis.Redis:
    return aioredis.from_url(_REDIS_URL, decode_responses=True)


def _state_key(document_id: str) -> str:
    return f"doc:{document_id}:state"


def _doc_id_from_key(key: str) -> str:
    return key.split(":")[1]


async def redis_reachable() -> bool:
    """Return True if Redis responds to PING."""
    r = _redis()
    return await r.ping()


async def get_doc_status(document_id: str) -> dict | None:
    """Return the full state hash for one document, or None if unknown."""
    r = _redis()
    data = await r.hgetall(_state_key(document_id))
    if not data:
        return None
    data["document_id"] = document_id
    return data


async def list_doc_statuses(page: int = 1) -> dict:
    """Return a paginated list of all document states from Redis."""
    r = _redis()
    pattern = "doc:*:state"
    all_keys: list[str] = []
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match=pattern, count=100)
        all_keys.extend(keys)
        if cursor == 0:
            break

    all_keys.sort()
    total = len(all_keys)
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_keys = all_keys[start:end]

    documents = []
    for key in page_keys:
        data = await r.hgetall(key)
        doc_id = _doc_id_from_key(key)
        data["document_id"] = doc_id
        documents.append(data)

    return {
        "documents": documents,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
    }


async def set_doc_pending(
    document_id: str,
    title: str,
    edition: str,
    canon_type: str,
) -> None:
    """Write a PENDING state entry for a newly submitted document."""
    r = _redis()
    now = datetime.now(timezone.utc).isoformat()
    await r.hset(
        _state_key(document_id),
        mapping={
            "status": "PENDING",
            "title": title,
            "edition": edition,
            "canon_type": canon_type,
            "created_at": now,
            "ingestion_started_at": now,
            "updated_at": now,
        },
    )


async def document_id_exists(document_id: str) -> bool:
    """Return True if a state entry already exists for this document_id."""
    r = _redis()
    return bool(await r.exists(_state_key(document_id)))


async def requeue_doc(document_id: str) -> bool:
    """Reset a FAILED document to PENDING.

    Returns False if not in FAILED.
    """
    r = _redis()
    key = _state_key(document_id)
    status = await r.hget(key, "status")
    if status != "FAILED":
        return False
    now = datetime.now(timezone.utc).isoformat()
    await r.hset(
        key,
        mapping={
            "status": "PENDING",
            "error": "",
            "updated_at": now,
        },
    )
    return True


async def list_all_completed() -> list[dict]:
    """Return every document that has reached COMPLETED status."""
    r = _redis()
    all_keys: list[str] = []
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match="doc:*:state", count=100)
        all_keys.extend(keys)
        if cursor == 0:
            break

    completed = []
    for key in all_keys:
        data = await r.hgetall(key)
        if data.get("status") == "COMPLETED":
            doc_id = _doc_id_from_key(key)
            completed.append(
                {
                    "document_id": doc_id,
                    "title": data.get("title", ""),
                    "entity_count": int(data.get("entity_count", 0)),
                    "triple_count": int(data.get("triple_count", 0)),
                    "completed_at": data.get("completed_at", ""),
                }
            )

    completed.sort(key=lambda d: d["completed_at"])
    return completed


async def restart_doc(document_id: str) -> bool:
    """Force any document back to PENDING and release its lock.

    Works regardless of current status. Deletes the distributed lock so a
    worker can claim the document on the next poll.
    """
    r = _redis()
    key = _state_key(document_id)
    exists = await r.exists(key)
    if not exists:
        return False
    now = datetime.now(timezone.utc).isoformat()
    pipe = r.pipeline()
    pipe.hset(
        key,
        mapping={
            "status": "PENDING",
            "ingestion_started_at": now,
            "updated_at": now,
            "error": "",
        },
    )
    pipe.delete(f"doc:{document_id}:lock")
    await pipe.execute()
    return True
