"""Ontology mapper, coreference resolver, and SPARQL triple writer.

Converts extracted JSON entities to RDF triples, deduplicates entity URIs
via Redis, and writes batches to Fuseki via SPARQL UPDATE INSERT DATA.
"""

import logging
import os
import re
import sys
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

_INGESTION_CONFIG_PATH = os.environ.get(
    "INGESTION_CONFIG_PATH", "/config/ingestion_config.yaml"
)
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


_ONTOLOGY = _load_ontology_schema()

_AUTO_MERGE = 0.92
_REVIEW_THRESHOLD = 0.80

_embedder = None


def _load_config() -> None:
    global _AUTO_MERGE, _REVIEW_THRESHOLD
    try:
        with open(_INGESTION_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        coref = cfg.get("coreference", {})
        _AUTO_MERGE = float(coref.get("auto_merge_threshold", _AUTO_MERGE))
        _REVIEW_THRESHOLD = float(coref.get("review_threshold", _REVIEW_THRESHOLD))
    except FileNotFoundError:
        logger.info("mapper: no ingestion_config.yaml found, using defaults.")


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding("nomic-ai/nomic-embed-text-v1.5")
        logger.info("mapper: fastembed model loaded.")
    return _embedder


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


def _get_or_create_uri(
    r: redis.Redis, name: str, flag_for_review: bool = False
) -> str:
    """Look up or create a URI for the given entity name via Redis."""
    redis_key = f"entity:{uri_slug(name)}"
    existing = r.get(redis_key)
    if existing:
        return existing

    # Cosine similarity check against all known entity names
    all_keys = r.keys("entity:*")
    if all_keys:
        try:
            embedder = _get_embedder()
            name_vec = list(next(embedder.embed([name])))[0]

            import numpy as np

            best_sim = 0.0
            best_uri = None
            best_name = None
            for key in all_keys[:200]:  # cap to avoid O(n) in large graphs
                existing_uri = r.get(key)
                existing_name = key.replace("entity:", "").replace("_", " ")
                try:
                    other_vec = list(next(embedder.embed([existing_name])))[0]
                    sim = float(
                        np.dot(name_vec, other_vec)
                        / (np.linalg.norm(name_vec) * np.linalg.norm(other_vec) + 1e-10)
                    )
                    if sim > best_sim:
                        best_sim = sim
                        best_uri = existing_uri
                        best_name = existing_name
                except Exception:
                    pass

            if best_sim >= _AUTO_MERGE and best_uri:
                logger.debug(
                    "mapper: '%s' → merged with '%s' (sim=%.3f)",
                    name, best_name, best_sim,
                )
                r.set(redis_key, best_uri)
                return best_uri

            if best_sim >= _REVIEW_THRESHOLD and best_uri:
                logger.info(
                    "mapper: '%s' flagged for review — similar to '%s' (sim=%.3f), new URI.",
                    name, best_name, best_sim,
                )
                r.sadd("review:entity_names", name)

        except Exception as exc:
            logger.warning("mapper: coreference check failed for '%s': %s", name, exc)

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
    if not name:
        logger.warning(
            "mapper: skipping unnamed %s (book=%s) — raw: %s",
            type_name, book_uri, entity,
        )
        return []

    uri = _get_or_create_uri(r, name)
    primary_class, extra_classes = _resolve_class(entity, type_def)

    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}{primary_class}> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    for extra in extra_classes:
        t.append(f"  {_uri(uri)} <{RDF}type> <{CS}{extra}> .")
    for ref in page_refs:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(ref)} .")
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
        if entity.get(field):
            target = _get_or_create_uri(r, entity[field])
            t.append(f"  {_uri(uri)} <{CS}{prop}> {_uri(target)} .")

    # List of named entities → one URI triple each
    for field, prop in (type_def.get("list_object_properties") or {}).items():
        for val in entity.get(field) or []:
            if val:
                target = _get_or_create_uri(r, val)
                t.append(f"  {_uri(uri)} <{CS}{prop}> {_uri(target)} .")

    # Reverse: looked-up entity is the subject
    # (parent cs:contains child, leader cs:leaderOf faction)
    for field, prop in (type_def.get("reverse_object_properties") or {}).items():
        if entity.get(field):
            target = _get_or_create_uri(r, entity[field])
            t.append(f"  {_uri(target)} <{CS}{prop}> {_uri(uri)} .")

    # Symmetric: write both directions
    for field, prop in (type_def.get("symmetric_object_properties") or {}).items():
        for val in entity.get(field) or []:
            if val:
                target = _get_or_create_uri(r, val)
                t.append(f"  {_uri(uri)} <{CS}{prop}> {_uri(target)} .")
                t.append(f"  {_uri(target)} <{CS}{prop}> {_uri(uri)} .")

    # Typed relationship array: each element has target + type
    rel_types = type_def.get("relationship_types") or {}
    for rel in entity.get("relationships") or []:
        target_name = (rel.get("target") or "").strip()
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
    _load_config()
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


def write_triples_to_fuseki(
    document_id: str,
    triples: list[str],
    batch_size: int = _INSERT_BATCH_SIZE,
) -> tuple[int, int]:
    """INSERT DATA into the document's named graph in batches.

    Sends triples in chunks of batch_size so Fuseki never receives a single
    giant UPDATE body. Returns (entity_count, triple_count).
    """
    if not triples:
        return 0, 0

    named_graph = f"http://campaignsetting.io/doc/{document_id}"

    for start in range(0, len(triples), batch_size):
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
        logger.debug(
            "mapper: %s — inserted triples %d–%d / %d",
            document_id, start + 1, start + len(batch), len(triples),
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
