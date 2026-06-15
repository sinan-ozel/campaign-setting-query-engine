"""Folder watcher — registers PDFs from a mounted input directory.

Scans INPUT_DIR recursively for .pdf files not yet known to Redis.
An optional .yaml sidecar alongside each PDF can set title, edition,
canon_type, and tags.

Folder structure encodes metadata:
  - A component matching a known edition token (3e, 4e, 5e, any) sets
    edition for that document.
  - A component matching a known canon_type token (canon, kanon, community)
    sets canon_type.
  - All remaining folder components become tags.
  - Sidecar values always override folder-derived values.

document_id is derived from the relative file path (slugified).
PDFs and synthesized YAML sidecars are uploaded to MinIO so the
existing pdf-worker conversion pipeline can process them unchanged.

Re-ingestion: if a document was previously COMPLETED and the PDF file
on disk is newer than ingestion_completed_at, it is re-uploaded and
reset to PENDING for a fresh pass through the pipeline.
"""

import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import redis
import yaml
from minio import Minio

logger = logging.getLogger("pdf_worker.folder_watcher")

_EDITIONS = {"3e", "4e", "5e", "any"}
_CANON_TYPES = {"canon", "kanon", "community"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_document_id(rel_path: Path) -> str:
    """Slugify relative path (without extension) to a document_id."""
    parts = list(rel_path.with_suffix("").parts)
    slug = "-".join(parts)
    slug = "".join(c if c.isalnum() or c == "-" else "-" for c in slug.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def _prettify(stem: str) -> str:
    return "".join(c if c.isalnum() else " " for c in stem).title().strip()


def _load_sidecar(pdf_path: Path) -> dict:
    sidecar = pdf_path.with_suffix(".yaml")
    if not sidecar.exists():
        return {}
    try:
        return yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("folder_watcher: bad sidecar YAML at %s — %s", sidecar, exc)
        return {}


def _classify_folder_parts(
    parts: list[str],
) -> tuple[str | None, str | None, list[str]]:
    """Split folder path components into (edition, canon_type, tags).

    First component matching a known edition token sets edition.
    First component matching a known canon_type token sets canon_type.
    Remaining components become tags. All comparisons are case-insensitive.
    """
    edition = None
    canon_type = None
    tags: list[str] = []
    for part in parts:
        lower = part.lower()
        if edition is None and lower in _EDITIONS:
            edition = lower
        elif canon_type is None and lower in _CANON_TYPES:
            canon_type = lower
        else:
            tags.append(part)
    return edition, canon_type, tags


def register_new_pdfs(
    input_dir: str,
    r: redis.Redis,
    mc: Minio,
    raw_pdfs_bucket: str,
) -> None:
    """Scan input_dir, register new PDFs and re-register updated ones.

    A COMPLETED document is re-registered if the PDF on disk is newer than
    its ingestion_completed_at timestamp. Documents in any other state
    (in-flight, failed) are left untouched.
    """
    root = Path(input_dir)
    if not root.is_dir():
        logger.warning("folder_watcher: INPUT_DIR %r is not a directory.", input_dir)
        return

    for pdf_path in sorted(root.rglob("*.pdf")):
        rel = pdf_path.relative_to(root)
        document_id = _derive_document_id(rel)
        state_key = f"doc:{document_id}:state"

        is_reingest = False
        if r.exists(state_key):
            completed_at = r.hget(state_key, "ingestion_completed_at")
            if not completed_at:
                continue  # in-flight or failed — don't disturb
            try:
                completed_ts = datetime.fromisoformat(completed_at).timestamp()
                file_mtime = pdf_path.stat().st_mtime
            except (ValueError, OSError):
                continue
            if file_mtime <= completed_ts:
                continue
            logger.info(
                "folder_watcher: %r updated since last ingestion — re-registering.",
                str(rel),
            )
            is_reingest = True

        sidecar = _load_sidecar(pdf_path)

        folder_parts = list(rel.parts[:-1])
        folder_edition, folder_canon, folder_tags = _classify_folder_parts(folder_parts)

        edition = str(sidecar["edition"]) if sidecar.get("edition") else folder_edition
        canon_type = str(sidecar["canon_type"]) if sidecar.get("canon_type") else folder_canon

        sidecar_tags = [str(t) for t in (sidecar.get("tags") or [])]
        seen: set[str] = set()
        tags: list[str] = []
        for t in folder_tags + sidecar_tags:
            if t not in seen:
                seen.add(t)
                tags.append(t)

        metadata: dict = {
            "document_id": document_id,
            "title": str(sidecar.get("title") or _prettify(rel.stem)),
            "tags": tags,
        }
        if edition:
            metadata["edition"] = edition
        if canon_type:
            metadata["canon_type"] = canon_type

        # Upload PDF then YAML sidecar — if either fails it propagates to the
        # caller's scan-loop handler; Redis is only written after both succeed.
        pdf_bytes = pdf_path.read_bytes()
        mc.put_object(
            raw_pdfs_bucket,
            f"{document_id}.pdf",
            io.BytesIO(pdf_bytes),
            length=len(pdf_bytes),
            content_type="application/pdf",
        )
        yaml_bytes = yaml.dump(metadata, allow_unicode=True).encode()
        mc.put_object(
            raw_pdfs_bucket,
            f"{document_id}.yaml",
            io.BytesIO(yaml_bytes),
            length=len(yaml_bytes),
            content_type="application/yaml",
        )

        now = _now()
        mapping: dict = {
            "status": "PENDING",
            "title": metadata["title"],
            "tags": ",".join(tags),
            "source_path": str(rel),
            "ingestion_started_at": now,
            "updated_at": now,
        }
        if edition:
            mapping["edition"] = edition
        if canon_type:
            mapping["canon_type"] = canon_type
        if not is_reingest:
            mapping["created_at"] = now
        r.hset(state_key, mapping=mapping)
        r.delete(f"doc:{document_id}:lock")

        action = "re-registered" if is_reingest else "registered"
        logger.info(
            "folder_watcher: %s %r → %r (edition: %s, canon: %s, tags: %s).",
            action,
            str(rel),
            document_id,
            edition or "(none)",
            canon_type or "(none)",
            tags or "(none)",
        )
