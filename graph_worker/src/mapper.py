"""Ontology mapper, coreference resolver, and SPARQL triple writer.

Converts extracted JSON entities to RDF triples, deduplicates entity URIs
via Redis, and writes batches to Fuseki via SPARQL UPDATE INSERT DATA.
"""

import hashlib
import logging
import os
import re
import uuid
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

_INGESTION_CONFIG_PATH = os.environ.get(
    "INGESTION_CONFIG_PATH", "/config/ingestion_config.yaml"
)

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


def _npc_triples(
    r: redis.Redis,
    npc: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = npc.get("name", "").strip()
    if not name:
        return []
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}NPC> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    for alias in npc.get("aliases") or []:
        if alias and alias != name:
            t.append(f"  {_uri(uri)} <{CS}alias> {_literal(alias)} .")
    for field, prop in [
        ("description", "description"), ("alignment", "alignment"),
    ]:
        if npc.get(field):
            edition = ""
            t.append(f"  {_uri(uri)} <{CS}{prop}> {_literal(npc[field])} .")
    if npc.get("race"):
        race_uri = _get_or_create_uri(r, npc["race"])
        t.append(f"  {_uri(uri)} <{CS}hasRace> {_uri(race_uri)} .")
    if npc.get("character_class"):
        cls_uri = _get_or_create_uri(r, npc["character_class"])
        t.append(f"  {_uri(uri)} <{CS}hasClass> {_uri(cls_uri)} .")
    if npc.get("nationality"):
        nat_uri = _get_or_create_uri(r, npc["nationality"])
        t.append(f"  {_uri(uri)} <{CS}nationality> {_uri(nat_uri)} .")
    if npc.get("location"):
        loc_uri = _get_or_create_uri(r, npc["location"])
        t.append(f"  {_uri(uri)} <{CS}locatedIn> {_uri(loc_uri)} .")
    if npc.get("worships"):
        deity_uri = _get_or_create_uri(r, npc["worships"])
        t.append(f"  {_uri(uri)} <{CS}worships> {_uri(deity_uri)} .")
    for faction_name in npc.get("factions") or []:
        if faction_name:
            f_uri = _get_or_create_uri(r, faction_name)
            t.append(f"  {_uri(uri)} <{CS}memberOf> {_uri(f_uri)} .")
    for rel in npc.get("relationships") or []:
        target = rel.get("target", "").strip()
        rel_type = rel.get("type", "other").lower()
        if not target:
            continue
        t_uri = _get_or_create_uri(r, target)
        if rel_type == "ally":
            t.append(f"  {_uri(uri)} <{CS}alliedWith> {_uri(t_uri)} .")
        elif rel_type == "enemy":
            t.append(f"  {_uri(uri)} <{CS}enemyOf> {_uri(t_uri)} .")
    return t


def _location_triples(
    r: redis.Redis,
    loc: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = loc.get("name", "").strip()
    if not name:
        return []
    type_map = {
        "City": "City", "River": "River", "Region": "Region",
        "Nation": "Nation", "Dungeon": "Dungeon", "Sea": "Sea",
        "Mountain": "Mountain", "Forest": "Forest", "Ruin": "Ruin",
        "Plane": "Plane",
    }
    loc_class = type_map.get(loc.get("type", ""), "Location")
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}{loc_class}> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    for alias in loc.get("aliases") or []:
        if alias and alias != name:
            t.append(f"  {_uri(uri)} <{CS}alias> {_literal(alias)} .")
    if loc.get("description"):
        t.append(f"  {_uri(uri)} <{CS}description> {_literal(loc['description'])} .")
    if loc.get("parent_location"):
        parent_uri = _get_or_create_uri(r, loc["parent_location"])
        t.append(f"  {_uri(parent_uri)} <{CS}contains> {_uri(uri)} .")
    if loc.get("controlling_faction"):
        f_uri = _get_or_create_uri(r, loc["controlling_faction"])
        t.append(f"  {_uri(uri)} <{CS}controlledBy> {_uri(f_uri)} .")
    return t


def _faction_triples(
    r: redis.Redis,
    faction: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = faction.get("name", "").strip()
    if not name:
        return []
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}Faction> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    if faction.get("type"):
        t.append(f"  {_uri(uri)} <{CS}factionType> {_literal(faction['type'])} .")
    if faction.get("headquarters"):
        hq_uri = _get_or_create_uri(r, faction["headquarters"])
        t.append(f"  {_uri(uri)} <{CS}factionLocatedIn> {_uri(hq_uri)} .")
    if faction.get("leader"):
        l_uri = _get_or_create_uri(r, faction["leader"])
        t.append(f"  {_uri(l_uri)} <{CS}leaderOf> {_uri(uri)} .")
    for loc_name in faction.get("operates_in") or []:
        if loc_name:
            loc_uri = _get_or_create_uri(r, loc_name)
            t.append(f"  {_uri(uri)} <{CS}operatesIn> {_uri(loc_uri)} .")
    for ally_name in faction.get("allies") or []:
        if ally_name:
            a_uri = _get_or_create_uri(r, ally_name)
            t.append(f"  {_uri(uri)} <{CS}factionAlly> {_uri(a_uri)} .")
    for enemy_name in faction.get("enemies") or []:
        if enemy_name:
            e_uri = _get_or_create_uri(r, enemy_name)
            t.append(f"  {_uri(uri)} <{CS}factionEnemy> {_uri(e_uri)} .")
    return t


def _religion_triples(
    r: redis.Redis,
    religion: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = religion.get("name", "").strip()
    if not name:
        return []
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}Religion> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    if religion.get("description"):
        t.append(f"  {_uri(uri)} <{CS}description> {_literal(religion['description'])} .")
    if religion.get("primary_deity"):
        d_uri = _get_or_create_uri(r, religion["primary_deity"])
        t.append(f"  {_uri(uri)} <{CS}primaryDeity> {_uri(d_uri)} .")
    return t


def _deity_triples(
    r: redis.Redis,
    deity: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = deity.get("name", "").strip()
    if not name:
        return []
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}Deity> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    if deity.get("description"):
        t.append(f"  {_uri(uri)} <{CS}description> {_literal(deity['description'])} .")
    if deity.get("alignment"):
        t.append(f"  {_uri(uri)} <{CS}alignment> {_literal(deity['alignment'])} .")
    if deity.get("religion"):
        rel_uri = _get_or_create_uri(r, deity["religion"])
        t.append(f"  {_uri(uri)} <{CS}deityOf> {_uri(rel_uri)} .")
    return t


def _race_triples(
    r: redis.Redis,
    race: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = race.get("name", "").strip()
    if not name:
        return []
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}Race> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    if race.get("description"):
        t.append(f"  {_uri(uri)} <{CS}description> {_literal(race['description'])} .")
    for region in race.get("native_regions") or []:
        if region:
            reg_uri = _get_or_create_uri(r, region)
            t.append(f"  {_uri(uri)} <{CS}nativeRegion> {_uri(reg_uri)} .")
    return t


def _class_triples(
    r: redis.Redis,
    cls: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = cls.get("name", "").strip()
    if not name:
        return []
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}CharacterClass> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    if cls.get("description"):
        t.append(f"  {_uri(uri)} <{CS}description> {_literal(cls['description'])} .")
    return t


def _skill_triples(
    r: redis.Redis,
    skill: dict,
    book_uri: str,
    page_ref: str | None,
) -> list[str]:
    name = skill.get("name", "").strip()
    if not name:
        return []
    uri = _get_or_create_uri(r, name)
    t: list[str] = [
        f"  {_uri(uri)} <{RDF}type> <{CS}Skill> .",
        f"  {_uri(uri)} <{RDFS}label> {_literal(name)} .",
        f"  {_uri(uri)} <{CS}mentionedIn> {_uri(book_uri)} .",
    ]
    if page_ref:
        t.append(f"  {_uri(uri)} <{CS}pageNumber> {_literal(page_ref)} .")
    if skill.get("description"):
        t.append(f"  {_uri(uri)} <{CS}description> {_literal(skill['description'])} .")
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

    handlers = [
        ("npcs", _npc_triples),
        ("locations", _location_triples),
        ("factions", _faction_triples),
        ("religions", _religion_triples),
        ("deities", _deity_triples),
        ("races", _race_triples),
        ("classes", _class_triples),
        ("skills", _skill_triples),
    ]
    for key, fn in handlers:
        for item in entities.get(key) or []:
            pr = item.get("page_reference") or page_ref
            all_triples.extend(fn(r, item, book_uri, pr))

    return all_triples


def write_triples_to_fuseki(
    document_id: str,
    triples: list[str],
) -> tuple[int, int]:
    """INSERT DATA into the document's named graph. Returns (entity_count, triple_count)."""
    if not triples:
        return 0, 0

    named_graph = f"http://campaignsetting.io/doc/{document_id}"
    triple_block = "\n".join(triples)
    query = (
        f"INSERT DATA {{\n  GRAPH <{named_graph}> {{\n{triple_block}\n  }}\n}}"
    )

    response = httpx.post(
        f"{_FUSEKI_ENDPOINT}/update",
        data={"update": query},
        timeout=120.0,
    )
    response.raise_for_status()

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
