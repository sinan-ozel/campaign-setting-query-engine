"""Ontology mapper, coreference resolver, and SPARQL triple writer.

Converts extracted JSON entities to RDF triples, deduplicates entity URIs
via Redis, and writes batches to Fuseki via SPARQL UPDATE INSERT DATA.
"""

import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
import redis
import yaml

logger = logging.getLogger("graph_worker.mapper")

CS = "http://campaignsetting.io/ontology#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XSD = "http://www.w3.org/2001/XMLSchema#"

_FUSEKI_ENDPOINT = os.environ.get(
    "FUSEKI_ENDPOINT", "http://localhost:3030/campaign"
)
_FUSEKI_USER = os.environ.get("FUSEKI_USER", "admin")
_FUSEKI_PASSWORD = os.environ.get("FUSEKI_PASSWORD", "")

_ONTOLOGY_SCHEMA_PATH = os.environ.get(
    "ONTOLOGY_SCHEMA_PATH", "/config/ontology_schema.yaml"
)


def _load_ontology_schema() -> dict:
    try:
        with open(_ONTOLOGY_SCHEMA_PATH) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(
            "Ontology schema not found at %r. "
            "Set ONTOLOGY_SCHEMA_PATH or mount config/ontology_schema.yaml "
            "to /config/ontology_schema.yaml.",
            _ONTOLOGY_SCHEMA_PATH,
        )
        sys.exit(1)


def _build_subclass_map(schema: dict) -> dict[str, list[str]]:
    """Build {parent_class: [direct_child_classes]} from entity_types subtypes."""
    children: dict[str, set[str]] = {}
    for type_def in schema["entity_types"].values():
        uri_suffix = type_def["uri_suffix"]
        for subtype_def in (type_def.get("subtypes") or {}).values():
            if isinstance(subtype_def, str):
                children.setdefault(uri_suffix, set()).add(subtype_def)
            elif isinstance(subtype_def, dict):
                child = subtype_def["class"]
                parents = subtype_def.get("parents", [uri_suffix])
                if parents:
                    children.setdefault(parents[0], set()).add(child)
                for i in range(len(parents) - 1):
                    children.setdefault(parents[i + 1], set()).add(parents[i])
        default = type_def.get("default_subtype")
        if default:
            child = default["class"]
            parents = default.get("parents", [uri_suffix])
            if parents:
                children.setdefault(parents[0], set()).add(child)
            for i in range(len(parents) - 1):
                children.setdefault(parents[i + 1], set()).add(parents[i])
    return {k: sorted(v) for k, v in children.items()}


_ONTOLOGY = _load_ontology_schema()
_SUBCLASS_MAP: dict[str, list[str]] = _build_subclass_map(_ONTOLOGY)

_NULL_NAMES: frozenset[str] = frozenset(
    {"null", "none", "n/a", "unknown", "unnamed", "name", "entity name"}
)


def _valid_name(v: object) -> str | None:
    """Return v as a non-empty, non-placeholder string, or None.

    Guards every call site that converts an LLM value to a URI so that
    placeholder strings like 'null', 'none', 'n/a' never become real URIs.
    """
    s = str(v).strip() if v else ""
    return s if s and s.lower() not in _NULL_NAMES else None


def uri_slug(name: str) -> str:
    """'Sharn, City of Towers' → 'Sharn_City_of_Towers'"""
    slug = re.sub(r"[^\w\s-]", "", name)
    return re.sub(r"[\s-]+", "_", slug.strip())


def _entity_uri(name: str) -> str:
    return f"{CS}{uri_slug(name)}"


def _sparql_escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _literal(value: str) -> str:
    return f'"{_sparql_escape_literal(str(value))}"'


def _uri(full_uri: str) -> str:
    return f"<{full_uri}>"


def _get_or_create_uri(r: redis.Redis, name: str) -> str:
    """Look up or create a URI for the given entity name via Redis.

    Uses exact slug matching only. Embedding-based coreference was removed
    because cosine similarity in the fantasy-RPG embedding space produced
    catastrophic false merges (many unrelated entities collapsed onto a single
    URI), corrupting GROUP_CONCAT queries and making the graph unusable.
    """
    redis_key = f"entity:{uri_slug(name)}"
    existing = r.get(redis_key)
    if existing:
        return existing
    uri = _entity_uri(name)
    r.set(redis_key, uri)
    return uri


def _source_book_uri(yaml_meta: dict) -> str:
    """Return the cs: URI for the SourceBook node."""
    if yaml_meta.get("source_book_uri"):
        raw = yaml_meta["source_book_uri"]
        if raw.startswith("cs:"):
            return f"{CS}{raw[3:]}"
        return raw
    slug = uri_slug(yaml_meta.get("title", yaml_meta.get("document_id", "unknown")))
    return f"{CS}book_{slug}"


def _ensure_source_book_triples(yaml_meta: dict) -> list[str]:
    """Return INSERT DATA triples that create/ensure the SourceBook node."""
    book_uri = _source_book_uri(yaml_meta)
    triples = [
        f"  {_uri(book_uri)} <{RDF}type> <{CS}SourceBook> .",
        f"  {_uri(book_uri)} <{RDFS}label> {_literal(yaml_meta.get('title', ''))} .",
        f"  {_uri(book_uri)} <{CS}edition> {_literal(yaml_meta.get('edition', ''))} .",
        f"  {_uri(book_uri)} <{CS}canonType> {_literal(yaml_meta.get('canon_type', ''))} .",
    ]
    if yaml_meta.get("publisher"):
        triples.append(
            f"  {_uri(book_uri)} <{CS}publisher> {_literal(yaml_meta['publisher'])} ."
        )
    if yaml_meta.get("publication_year"):
        triples.append(
            f"  {_uri(book_uri)} <{CS}publicationYear> "
            f"{_literal(str(yaml_meta['publication_year']))} ."
        )
    return triples


def _resolve_class(entity: dict, type_def: dict) -> tuple[str, list[str]]:
    """Return (primary_class, extra_parent_classes) based on YAML subtype config.

    Three subtype styles:
      - string value → explicit parent is uri_suffix (Location, Faction)
      - dict {class, parents} → explicit parent list (Item hierarchy)
      - no match → default_subtype if present, else uri_suffix with no extras
    """
    uri_suffix = type_def["uri_suffix"]
    subtypes = type_def.get("subtypes", {})
    subtype_field = type_def.get("subtype_field")

    if not subtypes or not subtype_field:
        return uri_suffix, []

    subtype_val = entity.get(subtype_field, "")
    subtype_def = subtypes.get(subtype_val)

    if subtype_def is None:
        default = type_def.get("default_subtype")
        if default:
            return default["class"], default.get("parents", [])
        return uri_suffix, []
    if isinstance(subtype_def, str):
        if subtype_def == uri_suffix:
            return uri_suffix, []
        return subtype_def, [uri_suffix]
    if isinstance(subtype_def, dict):
        return subtype_def["class"], subtype_def.get("parents", [])
    return uri_suffix, []


def _bfs_reachable(start: str, target: str) -> bool:
    """Return True if target is reachable from start via BFS through _SUBCLASS_MAP."""
    if start == target:
        return True
    visited: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for child in _SUBCLASS_MAP.get(current, []):
            if child == target:
                return True
            queue.append(child)
    return False


def _resolve_type_conflict(
    existing_type: str,
    new_type: str,
) -> tuple[str, bool]:
    """Return (preferred_type, flag_for_review).

    new_type is subclass of existing → prefer new_type (refinement).
    existing_type is subclass of new → keep existing_type (already specific).
    Unrelated → keep existing_type, flag for user review.
    """
    if _bfs_reachable(existing_type, new_type):
        return new_type, False
    if _bfs_reachable(new_type, existing_type):
        return existing_type, False
    return existing_type, True


def _register_entity_type(
    r: redis.Redis,
    slug: str,
    primary_class: str,
) -> tuple[str, bool]:
    """Register entity type in Redis. Return (class_to_write, should_write_type_triple).

    First seen → store and write.
    New is subclass of existing → refine to more specific type (update + write).
    Existing is subclass of new → skip type triple (already more specific).
    Unrelated conflict → skip type triple, add to review:type_conflicts.
    """
    type_key = f"entity_type:{slug}"
    existing = r.get(type_key)

    if not existing:
        r.set(type_key, primary_class)
        return primary_class, True

    if existing == primary_class:
        return primary_class, True

    preferred, needs_review = _resolve_type_conflict(existing, primary_class)

    if needs_review:
        r.sadd(
            "review:type_conflicts",
            f"{slug.replace('_', ' ')}|{existing}|{primary_class}",
        )
        logger.info(
            "mapper: type conflict '%s': stored=%s new=%s — keeping %s, flagged",
            slug.replace("_", " "), existing, primary_class, preferred,
        )
        return preferred, False

    if preferred != existing:
        logger.debug(
            "mapper: type refined '%s': %s → %s",
            slug.replace("_", " "), existing, preferred,
        )
        r.set(type_key, preferred)
        return preferred, True

    return preferred, False


def _entity_triples(
    r: redis.Redis,
    entity: dict,
    book_uri: str,
    page_refs: list[str],
    type_name: str,
    type_def: dict,
) -> list[str]:
    """Generic triple writer driven entirely by the YAML property maps.

    To add a new field to any entity type: add one line to the right
    property map in ontology_schema.yaml. No Python changes needed.
    """
    name = (entity.get("name") or "").strip()
    if not name or name.lower() in _NULL_NAMES:
        logger.warning(
            "mapper: skipping %s with null/placeholder name (book=%s) — raw: %s",
            type_name, book_uri, entity,
        )
        return []

    uri = _get_or_create_uri(r, name)
    primary_class, extra_classes = _resolve_class(entity, type_def)

    book_slug = book_uri.split("#")[-1] if "#" in book_uri else uri_slug(book_uri)
    entity_slug = uri_slug(name)

    resolved_class, write_type = _register_entity_type(r, entity_slug, primary_class)

    t: list[str] = []
    if write_type:
        t.append(f"  {_uri(uri)} <{RDF}type> <{CS}{resolved_class}> .")
        for extra in extra_classes:
            t.append(f"  {_uri(uri)} <{RDF}type> <{CS}{extra}> .")
    t.extend([
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ])

    # Write a cs:Mention node per (entity, book, page) so page and book stay paired.
    for ref in page_refs or [None]:
        page_slug = uri_slug(str(ref)) if ref else "nopage"
        mention_uri = f"{CS}mention_{entity_slug}_in_{book_slug}_p{page_slug}"
        t.append(f"  {_uri(uri)} <{CS}hasMention> {_uri(mention_uri)} .")
        t.append(f"  {_uri(mention_uri)} <{CS}inBook> {_uri(book_uri)} .")
        if ref:
            t.append(f"  {_uri(mention_uri)} <{CS}atPage> {_literal(ref)} .")

    for alias in entity.get("aliases") or []:
        if alias and alias != name:
            t.append(f"  {_uri(uri)} <{CS}alias> {_literal(alias)} .")

    # Scalar string → literal triple
    for field, prop in (type_def.get("datatype_properties") or {}).items():
        if entity.get(field):
            t.append(f"  {_uri(uri)} <{CS}{prop}> {_literal(entity[field])} .")

    # List of strings → one literal triple each
    for field, prop in (type_def.get("list_datatype_properties") or {}).items():
        for val in entity.get(field) or []:
            if val:
                t.append(f"  {_uri(uri)} <{CS}{prop}> {_literal(str(val))} .")

    # Named entity → URI, current → cs:prop → target
    for field, prop in (type_def.get("object_properties") or {}).items():
        linked = _valid_name(entity.get(field))
        if linked:
            target = _get_or_create_uri(r, linked)
            t.append(f"  {_uri(uri)} <{CS}{prop}> {_uri(target)} .")

    # List of named entities → one URI triple each
    for field, prop in (type_def.get("list_object_properties") or {}).items():
        for val in entity.get(field) or []:
            linked = _valid_name(val)
            if linked:
                target = _get_or_create_uri(r, linked)
                t.append(f"  {_uri(uri)} <{CS}{prop}> {_uri(target)} .")

    # Reverse: looked-up entity is the subject
    # (parent cs:contains child, leader cs:leaderOf faction)
    for field, prop in (type_def.get("reverse_object_properties") or {}).items():
        linked = _valid_name(entity.get(field))
        if linked:
            target = _get_or_create_uri(r, linked)
            t.append(f"  {_uri(target)} <{CS}{prop}> {_uri(uri)} .")

    # Symmetric: write both directions
    for field, prop in (type_def.get("symmetric_object_properties") or {}).items():
        for val in entity.get(field) or []:
            linked = _valid_name(val)
            if linked:
                target = _get_or_create_uri(r, linked)
                t.append(f"  {_uri(uri)} <{CS}{prop}> {_uri(target)} .")
                t.append(f"  {_uri(target)} <{CS}{prop}> {_uri(uri)} .")

    # Typed relationship array: each element has target + type
    rel_types = type_def.get("relationship_types") or {}
    for rel in entity.get("relationships") or []:
        target_name = _valid_name((rel.get("target") or "").strip())
        rel_type = (rel.get("type") or "other").lower()
        if not target_name or rel_type not in rel_types:
            continue
        target = _get_or_create_uri(r, target_name)
        rel_def = rel_types[rel_type]
        prop = rel_def["property"]
        t.append(f"  {_uri(uri)} <{CS}{prop}> {_uri(target)} .")
        if rel_def.get("symmetric"):
            t.append(f"  {_uri(target)} <{CS}{prop}> {_uri(uri)} .")

    return t


def entities_to_triples(
    r: redis.Redis,
    entities: dict[str, list[Any]],
    yaml_meta: dict,
    page_ref: str | None,
) -> list[str]:
    """Convert extracted entity JSON to a flat list of Turtle triple strings."""
    book_uri = _source_book_uri(yaml_meta)
    all_triples = _ensure_source_book_triples(yaml_meta)

    for type_name, type_def in _ONTOLOGY["entity_types"].items():
        llm_key = type_def.get("llm_key")
        if not llm_key:
            continue
        for entity in entities.get(llm_key) or []:
            raw = entity.get("page_references") or []
            if not raw and entity.get("page_reference"):
                raw = [entity["page_reference"]]
            page_refs_entity = [str(x) for x in raw if x] or (
                [page_ref] if page_ref else []
            )
            all_triples.extend(
                _entity_triples(r, entity, book_uri, page_refs_entity, type_name, type_def)
            )

    return all_triples


_INSERT_BATCH_SIZE = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_triples_to_fuseki(
    document_id: str,
    triples: list[str],
    r: redis.Redis | None = None,
    batch_size: int = _INSERT_BATCH_SIZE,
) -> tuple[int, int]:
    """INSERT DATA into the document's named graph in batches.

    Sends triples in chunks of batch_size so Fuseki never receives a single
    giant UPDATE body. If r is provided, writes loading_batch progress to
    Redis after each batch. Returns (entity_count, triple_count).
    """
    if not triples:
        return 0, 0

    named_graph = f"http://campaignsetting.io/doc/{document_id}"
    total_batches = (len(triples) + batch_size - 1) // batch_size

    for i, start in enumerate(range(0, len(triples), batch_size), 1):
        batch = triples[start : start + batch_size]
        triple_block = "\n".join(batch)
        query = (
            f"INSERT DATA {{\n  GRAPH <{named_graph}> {{\n{triple_block}\n  }}\n}}"
        )
        response = httpx.post(
            f"{_FUSEKI_ENDPOINT}/update",
            data={"update": query},
            auth=(_FUSEKI_USER, _FUSEKI_PASSWORD),
            timeout=120.0,
        )
        response.raise_for_status()
        if r is not None:
            r.hset(
                f"doc:{document_id}:state",
                mapping={"loading_batch": f"{i}/{total_batches}", "updated_at": _now()},
            )
        logger.info(
            "mapper: %s — triples %d–%d / %d (batch %d/%d)",
            document_id, start + 1, start + len(batch), len(triples), i, total_batches,
        )

    # Count distinct subject URIs as entity count
    subjects = {line.strip().split(" ")[0] for line in triples if line.strip()}
    entity_count = len(subjects)
    return entity_count, len(triples)


def drop_named_graph(document_id: str) -> None:
    """Remove all triples for a document (called on failure)."""
    named_graph = f"http://campaignsetting.io/doc/{document_id}"
    query = f"DROP GRAPH <{named_graph}>"
    try:
        httpx.post(
            f"{_FUSEKI_ENDPOINT}/update",
            data={"update": query},
            auth=(_FUSEKI_USER, _FUSEKI_PASSWORD),
            timeout=30.0,
        )
    except Exception as exc:
        logger.error("mapper: failed to drop graph for %s: %s", document_id, exc)


def get_known_entity_names(r: redis.Redis, limit: int = 20) -> list[str]:
    """Return up to *limit* canonical entity names from the Redis dedup index."""
    keys = r.keys("entity:*")
    names = []
    for key in keys[:limit]:
        name = key.replace("entity:", "").replace("_", " ")
        names.append(name)
    return sorted(names)
