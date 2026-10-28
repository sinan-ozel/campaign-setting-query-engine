"""Campaign Setting Query Engine — MCP server.

MCP tools (served at /mcp) are for knowledge-graph queries by agents. Admin
HTTP routes (/health, /status, /ingest, /admin/*) are for operators and the
Streamlit dashboard.

The tools form a ladder so that no single call can flood a small model's
context: enumerate names (list_names, search_entities) → pick one entity →
cite it (get_sources) or read it (get_entity, get_relationships). Every
response is additionally capped at MAX_RESPONSE_CHARS.

CSQE_MODE selects how the server runs:
  ingest — full stack: Fuseki + Redis + MinIO, admin/ingestion routes enabled
  serve  — read-only: Fuseki only (loaded from a backup), admin/ingestion
           routes answer 503
"""

import difflib
import functools
import io
import json
import logging
import os
import re
import sys
from typing import Literal

import yaml
from fastmcp import FastMCP
from minio import Minio, S3Error
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from server import __version__
from server import sparql as sq
from server import status as st
from server.openapi_spec import OPENAPI_SPEC

mcp = FastMCP("campaign-query-engine")


class _SuppressMCPUnionValidation(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Failed to validate request:")


class _SuppressHealthChecks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return " /health " not in record.getMessage()


logging.getLogger().addFilter(_SuppressMCPUnionValidation())
logging.getLogger("uvicorn.access").addFilter(_SuppressHealthChecks())

_log = logging.getLogger(__name__)

_MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
_MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
_MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
_RAW_PDFS_BUCKET = "raw-pdfs"

_MODE = os.environ.get("CSQE_MODE", "ingest")
if _MODE not in ("ingest", "serve"):
    _log.error(
        "CSQE_MODE=%r is not valid. Set CSQE_MODE to 'ingest' (full stack with "
        "Redis and MinIO) or 'serve' (read-only, Fuseki only).",
        _MODE,
    )
    sys.exit(1)

try:
    MAX_RESPONSE_CHARS = int(os.environ.get("MAX_RESPONSE_CHARS", "12000"))
except ValueError:
    _log.error(
        "MAX_RESPONSE_CHARS=%r is not an integer. Set it to the largest tool "
        "response, in characters, your MCP client can take (default 12000).",
        os.environ.get("MAX_RESPONSE_CHARS"),
    )
    sys.exit(1)

# Page-size bounds shared by the list-returning tools.
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

CanonFilter = Literal["canon", "kanon", "community", "any"]
EditionFilter = Literal["3e", "4e", "5e", "any"]

_ENTITY_TYPES = sorted(sq.ENTITY_CLASS)
_RELATIONSHIPS = list(sq.REL_SPEC)
_PROPERTIES = sorted(sq.ALLOWED_PROPERTY_NAMES)

_NOT_NULL = {"not": {"type": "null"}}

_EDITION_FIELD = Field(
    default="any",
    description="Only entities mentioned in books of this edition: 3e, 4e, 5e, or any.",
)
_CANON_FIELD = Field(
    default="any",
    description="Only entities mentioned in books of this canonicity: canon, kanon, community, or any.",
)


# ── Response helpers ──────────────────────────────────────────────────────


def _unknown(kind: str, value: str, valid: list[str], example: str) -> dict:
    """Error for an unknown type/relationship/property, with close matches."""
    close = difflib.get_close_matches(value, valid, n=5, cutoff=0.5)
    close += [
        v for v in valid if value.lower() in v.lower() and v not in close
    ][:5]
    return {
        "error": f"Unknown {kind}: {value!r}.",
        "did_you_mean": close[:5],
        "hint": (
            f"Call describe_schema to see every valid {kind} with its meaning. "
            f"Example: {example}"
        ),
    }


def _not_found(name: str, suggestions: list[str]) -> dict:
    return {
        "error": f"No entity named {name!r}.",
        "did_you_mean": suggestions,
        "hint": (
            "Names must match exactly (case-insensitive). Use search_entities "
            "with part of the name to find the exact one, e.g. "
            "search_entities(query='Cannith')."
        ),
    }


def _largest_list(node: object, path: tuple = ()) -> tuple[tuple, int] | None:
    """Return (path, serialized size) of the largest list with >1 item."""
    best = None
    children = (
        node.items()
        if isinstance(node, dict)
        else enumerate(node) if isinstance(node, list) else ()
    )
    if isinstance(node, list) and len(node) > 1:
        best = (path, len(json.dumps(node, ensure_ascii=False)))
    for key, child in children:
        found = _largest_list(child, path + (key,))
        if found and (best is None or found[1] > best[1]):
            best = found
    return best


def _get(node: object, path: tuple) -> object:
    for key in path:
        node = node[key]
    return node


def _fit(result: dict) -> dict:
    """Trim the result until its JSON fits MAX_RESPONSE_CHARS.

    Halves the largest list first, then shortens long strings. Paging
    parameters are the real limit; this is the backstop that keeps any one
    call, whatever the data, from overflowing the client's context.
    """
    size = len(json.dumps(result, ensure_ascii=False))
    if size <= MAX_RESPONSE_CHARS:
        return result
    trimmed = False
    while size > MAX_RESPONSE_CHARS:
        found = _largest_list(result)
        if not found:
            break
        lst = _get(result, found[0])
        del lst[max(1, len(lst) // 2) :]
        trimmed = True
        size = len(json.dumps(result, ensure_ascii=False))
    if size > MAX_RESPONSE_CHARS:
        _shorten_strings(result, 400)
        trimmed = True
    if trimmed:
        result["truncated"] = True
        result["note"] = (
            "Response trimmed to fit the size budget. Narrow the query "
            "(filters, entity_type) or page with offset/page."
        )
    return result


def _shorten_strings(node: object, max_len: int) -> None:
    items = (
        node.items()
        if isinstance(node, dict)
        else enumerate(node) if isinstance(node, list) else ()
    )
    for key, child in list(items):
        if isinstance(child, str) and len(child) > max_len:
            node[key] = child[:max_len] + "…"
        else:
            _shorten_strings(child, max_len)


def _page(names: list[str], total: int, offset: int) -> dict:
    """Common paging envelope for name lists."""
    out = {
        "total": total,
        "offset": offset,
        "count": len(names),
        "names": names,
    }
    if offset + len(names) < total:
        out["truncated"] = True
        out["next_offset"] = offset + len(names)
        out["hint"] = (
            f"Showing {offset + 1}–{offset + len(names)} of {total}. Call again "
            f"with offset={offset + len(names)}, or narrow with filters."
        )
    return out


def _types_of(binding: dict, key: str = "types") -> list[str]:
    return sq.most_specific([sq.short(t) for t in sq.split_agg(binding, key)])


async def _total(query: str) -> int:
    rows = await sq.sparql_select(query)
    return int(sq.val(rows[0], "total") or 0) if rows else 0


async def _resolve(name: str) -> list[dict]:
    """Return [{uri, name, types}] for entities whose label or alias is name.

    Entities whose label is the name come first; alias-only matches follow
    (many places list a famous neighbour, e.g. 'Sharn', among their aliases).
    """
    rows = await sq.sparql_select(sq.build_resolve_query(name))
    found = [
        {
            "uri": sq.val(b, "e"),
            "name": sq.val(b, "label"),
            "types": _types_of(b),
        }
        for b in rows
    ]
    key = name.strip().lower()
    found.sort(key=lambda f: (f["name"].lower() != key, f["name"].lower()))
    return found


async def _suggest(name: str, n: int = 5) -> list[str]:
    """Names containing name (or its longest word), for not-found errors."""
    words = sorted(re.findall(r"[\w'’-]+", name), key=len, reverse=True)
    for text in [name, *words[:1]]:
        rows = await sq.sparql_select(
            sq.build_search_entities_query(text, None, n)
        )
        if rows:
            return [sq.val(b, "name") for b in rows]
    return []


def _sort_pages(pages: list[str]) -> list[str]:
    def key(p: str) -> tuple:
        m = re.search(r"\d+", p)
        return (0, int(m.group())) if m else (1, p)

    return sorted(set(pages), key=key)


# ── Pydantic input models ──────────────────────────────────────────────────


class DescribeSchemaInput(BaseModel):
    """Input for describe_schema (no parameters)."""


class ListNamesInput(BaseModel):
    """Input for list_names."""

    entity_type: str = Field(
        description=(
            "Type to enumerate, e.g. 'River', 'Nation', 'NPC', 'Tradition'. "
            "Subtypes are included (Location includes River, City, ...)."
        ),
        json_schema_extra={"enum": _ENTITY_TYPES, **_NOT_NULL},
    )
    filters: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional property → value filters, all must match "
            "(case-insensitive). Keys come from describe_schema's "
            "filter_properties. Entity-valued properties match the linked "
            "entity's name, e.g. {'nationality': 'Breland'} or "
            "{'category': 'fashion', 'practicedBy': 'Breland'}."
        ),
    )
    edition: EditionFilter = _EDITION_FIELD
    canon_type: CanonFilter = _CANON_FIELD
    limit: int = Field(
        default=_DEFAULT_LIMIT,
        ge=1,
        le=_MAX_LIMIT,
        description=f"Names per page (1–{_MAX_LIMIT}).",
    )
    offset: int = Field(
        default=0, ge=0, description="Names to skip, for paging."
    )


class SearchEntitiesInput(BaseModel):
    """Input for search_entities."""

    query: str = Field(
        min_length=2,
        description="Part of a name or alias, e.g. 'Cannith' or 'Brey'.",
        json_schema_extra=_NOT_NULL,
    )
    entity_type: str | None = Field(
        default=None,
        description="Optional type to search within, e.g. 'Faction'.",
        json_schema_extra={"type": "string", "enum": _ENTITY_TYPES},
    )
    limit: int = Field(
        default=10, ge=1, le=50, description="Maximum matches (1–50)."
    )


class GetSourcesInput(BaseModel):
    """Input for get_sources."""

    names: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Exact entity names (1–10), e.g. ['Brey River', 'Dagger River'].",
        json_schema_extra=_NOT_NULL,
    )
    edition: EditionFilter = _EDITION_FIELD
    canon_type: CanonFilter = _CANON_FIELD


class GetEntityInput(BaseModel):
    """Input for get_entity."""

    name: str = Field(
        description="Exact entity name or alias (case-insensitive), e.g. 'Breland'.",
        json_schema_extra=_NOT_NULL,
    )
    section: Literal["facts", "relationships"] = Field(
        default="facts",
        description=(
            "facts: types, description, aliases and other properties. "
            "relationships: every link to other entities, grouped by "
            "relationship name, one page at a time."
        ),
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page of relationships (60 links per page).",
    )


class GetRelationshipsInput(BaseModel):
    """Input for get_relationships."""

    entity_name: str = Field(
        description="Exact name of the source entity, e.g. 'House Cannith'.",
        json_schema_extra=_NOT_NULL,
    )
    relationship: str = Field(
        description=(
            "Relationship to follow, e.g. 'members', 'capital', 'ruler', "
            "'traditions'. See describe_schema for every name and meaning."
        ),
        json_schema_extra={"enum": _RELATIONSHIPS, **_NOT_NULL},
    )
    limit: int = Field(
        default=_DEFAULT_LIMIT,
        ge=1,
        le=_MAX_LIMIT,
        description=f"Results per page (1–{_MAX_LIMIT}).",
    )
    offset: int = Field(
        default=0, ge=0, description="Results to skip, for paging."
    )


class GetLocationHierarchyInput(BaseModel):
    """Input for get_location_hierarchy."""

    location: str = Field(
        description="Exact name of a location, e.g. 'Sharn'.",
        json_schema_extra=_NOT_NULL,
    )
    depth: Literal["direct", "all"] = Field(
        default="direct",
        description="direct: only places directly inside it. all: every level below it.",
    )
    limit: int = Field(
        default=_DEFAULT_LIMIT,
        ge=1,
        le=_MAX_LIMIT,
        description=f"Children per page (1–{_MAX_LIMIT}).",
    )
    offset: int = Field(
        default=0, ge=0, description="Children to skip, for paging."
    )


class SearchByPropertyInput(BaseModel):
    """Input for search_by_property."""

    entity_type: str = Field(
        description="Type to search within, e.g. 'NPC'.",
        json_schema_extra={"enum": _ENTITY_TYPES, **_NOT_NULL},
    )
    property_name: str = Field(
        description="Property to match, e.g. 'nationality', 'category', 'status'.",
        json_schema_extra={"enum": _PROPERTIES, **_NOT_NULL},
    )
    value: str = Field(
        description="Value to match (case-insensitive), e.g. 'Breland'.",
        json_schema_extra=_NOT_NULL,
    )
    match: Literal["exact", "contains"] = Field(
        default="exact",
        description="exact: whole value equals. contains: value appears inside it.",
    )
    edition: EditionFilter = _EDITION_FIELD
    canon_type: CanonFilter = _CANON_FIELD
    limit: int = Field(
        default=_DEFAULT_LIMIT,
        ge=1,
        le=_MAX_LIMIT,
        description=f"Names per page (1–{_MAX_LIMIT}).",
    )
    offset: int = Field(
        default=0, ge=0, description="Names to skip, for paging."
    )


class SearchTextInput(BaseModel):
    """Input for search_text."""

    query: str = Field(
        min_length=3,
        description=(
            "Words to look for in entity names and descriptions, e.g. "
            "'Breland capital' or 'fashion gloves'."
        ),
        json_schema_extra=_NOT_NULL,
    )
    entity_type: str | None = Field(
        default=None,
        description="Optional type to search within, e.g. 'Nation' or 'Tradition'.",
        json_schema_extra={"type": "string", "enum": _ENTITY_TYPES},
    )
    limit: int = Field(
        default=10, ge=1, le=25, description="Maximum matches (1–25)."
    )


class ListSourcesInput(BaseModel):
    """Input for list_sources (no parameters)."""


# ── MCP tools ─────────────────────────────────────────────────────────────


@mcp.tool()
async def describe_schema(_: DescribeSchemaInput) -> dict:
    """What the knowledge graph can answer: entity types (with subtypes),
    relationships (with meaning) and filterable properties.

    Call this first when unsure which type, relationship or property fits a
    question.
    """
    return {
        "workflow": [
            "1. Find names: list_names(entity_type) to enumerate, search_entities(query) for a partial name.",
            "2. Follow links: get_relationships(entity_name, relationship).",
            "3. Read one entity: get_entity(name) for facts, section='relationships' for all links.",
            "4. Cite: get_sources(names) for books and pages.",
            "5. Nothing fits? search_text(query) searches names and descriptions.",
        ],
        "entity_types": sq.type_tree(),
        "relationships": {k: v["description"] for k, v in sq.REL_SPEC.items()},
        "filter_properties": _PROPERTIES,
        "property_values": {
            **sq.enum_values(),
            "fromYear/untilYear/startYear/endYear": [
                "integer YK year; negative = before YK"
            ],
        },
    }


@mcp.tool()
async def list_names(input: ListNamesInput) -> dict:
    """List the names of every entity of a type, e.g. all rivers, all nations.

    Returns names only, a page at a time, with the total. For where an entity
    appears use get_sources; for details use get_entity.
    """
    if input.entity_type not in sq.ENTITY_CLASS:
        return _unknown(
            "entity_type",
            input.entity_type,
            _ENTITY_TYPES,
            "list_names(entity_type='River')",
        )
    for prop in input.filters:
        if prop not in sq.ALLOWED_PROPERTY_NAMES:
            return _unknown(
                "filter property",
                prop,
                _PROPERTIES,
                "list_names(entity_type='NPC', filters={'nationality': 'Breland'})",
            )
    query = sq.build_list_names_query(
        input.entity_type,
        input.edition,
        input.canon_type,
        input.filters,
        input.limit,
        input.offset,
    )
    names = [sq.val(b, "name") for b in await sq.sparql_select(query)]
    total = await _total(
        sq.build_count_query(
            input.entity_type, input.edition, input.canon_type, input.filters
        )
    )
    return _fit(
        {"entity_type": input.entity_type, **_page(names, total, input.offset)}
    )


@mcp.tool()
async def search_entities(input: SearchEntitiesInput) -> dict:
    """Find entities by part of their name or alias, best matches first.

    Use it to turn 'Cannith' into the exact name 'House Cannith' before calling
    get_entity or get_relationships.
    """
    if input.entity_type and input.entity_type not in sq.ENTITY_CLASS:
        return _unknown(
            "entity_type",
            input.entity_type,
            _ENTITY_TYPES,
            "search_entities(query='Cannith', entity_type='Faction')",
        )
    rows = await sq.sparql_select(
        sq.build_search_entities_query(
            input.query, input.entity_type, input.limit
        )
    )
    matches = []
    for b in rows:
        match = {"name": sq.val(b, "name"), "types": _types_of(b)}
        alias = sq.val(b, "matched")
        if alias and alias != match["name"]:
            match["matched_alias"] = alias
        matches.append(match)
    return _fit(
        {"query": input.query, "count": len(matches), "matches": matches}
    )


@mcp.tool()
async def get_sources(input: GetSourcesInput) -> dict:
    """Where entities appear: books (with edition and canonicity) and pages,
    for each name.

    Use it to cite answers.
    """
    results = []
    for name in input.names:
        found = await _resolve(name)
        if not found:
            results.append(_not_found(name, await _suggest(name)))
            continue
        books = []
        for b in await sq.sparql_select(
            sq.build_sources_query(found[0]["uri"])
        ):
            edition, canon = sq.val(b, "edition"), sq.val(b, "canonType")
            if input.edition != "any" and edition != input.edition:
                continue
            if input.canon_type != "any" and canon != input.canon_type:
                continue
            books.append(
                {
                    "book": sq.val(b, "book"),
                    "edition": edition,
                    "canon_type": canon,
                    "pages": _sort_pages(sq.split_agg(b, "pages")),
                }
            )
        results.append({"name": found[0]["name"], "sources": books})
    return _fit({"results": results})


@mcp.tool()
async def get_entity(input: GetEntityInput) -> dict:
    """Everything known about one entity.

    section='facts' (default): types, description, aliases and other
    properties. section='relationships': links to other entities grouped by
    relationship, 60 per page. For citations use get_sources.
    """
    found = await _resolve(input.name)
    if not found:
        return _not_found(input.name, await _suggest(input.name))
    ent = found[0]
    result: dict = {"name": ent["name"], "types": ent["types"]}
    if len(found) > 1:
        result["other_matches"] = [f["name"] for f in found[1:]]

    if input.section == "facts":
        facts: dict[str, list[str]] = {}
        for b in await sq.sparql_select(sq.build_literals_query(ent["uri"])):
            prop = sq.short(sq.val(b, "p"))
            value = sq.val(b, "o")
            if (
                prop.startswith("http")
                or prop == sq.RDFS + "label"
                or not value
            ):
                continue
            facts.setdefault(prop, [])
            if value not in facts[prop]:
                facts[prop].append(value)
        result["facts"] = {
            k: v[0] if len(v) == 1 else v for k, v in sorted(facts.items())
        }
        result["hint"] = (
            "section='relationships' lists linked entities; get_sources "
            "gives books and pages."
        )
        return _fit(result)

    grouped: dict[str, list[dict]] = {}
    for b in await sq.sparql_select(sq.build_edges_query(ent["uri"])):
        prop = sq.short(sq.val(b, "p"))
        direction = sq.val(b, "dir")
        rel = sq.REL_NAME_BY_EDGE.get((prop, direction)) or (
            prop if direction == "out" else f"{prop} (inverse)"
        )
        grouped.setdefault(rel, []).append(
            {"name": sq.val(b, "otherName"), "types": _types_of(b)}
        )
    flat = [
        (rel, item) for rel, items in sorted(grouped.items()) for item in items
    ]
    per_page = 60
    start = (input.page - 1) * per_page
    page_items: dict[str, list[dict]] = {}
    for rel, item in flat[start : start + per_page]:
        page_items.setdefault(rel, []).append(item)
    result.update(
        {
            "relationships": page_items,
            "total_links": len(flat),
            "page": input.page,
        }
    )
    if start + per_page < len(flat):
        result["truncated"] = True
        result["hint"] = f"More links: call again with page={input.page + 1}."
    return _fit(result)


@mcp.tool()
async def get_relationships(input: GetRelationshipsInput) -> dict:
    """Follow one relationship from an entity, e.g. the members of House
    Cannith, the capital of Breland, the traditions of Aundair.

    Returns names and types, a page at a time. See describe_schema for every
    relationship and its direction.
    """
    if input.relationship not in sq.REL_SPEC:
        return _unknown(
            "relationship",
            input.relationship,
            _RELATIONSHIPS,
            "get_relationships(entity_name='Breland', relationship='capital')",
        )
    found = await _resolve(input.entity_name)
    if not found:
        return _not_found(input.entity_name, await _suggest(input.entity_name))
    uri = found[0]["uri"]
    rows = await sq.sparql_select(
        sq.build_relationship_query(
            uri, input.relationship, input.limit, input.offset
        )
    )
    total = await _total(
        sq.build_relationship_query(uri, input.relationship, 0, 0, count=True)
    )
    results = [
        {"name": sq.val(b, "relName"), "types": _types_of(b)} for b in rows
    ]
    out = {
        "entity": found[0]["name"],
        "relationship": input.relationship,
        "meaning": sq.REL_SPEC[input.relationship]["description"],
        "total": total,
        "offset": input.offset,
        "results": results,
    }
    if input.offset + len(results) < total:
        out["truncated"] = True
        out["next_offset"] = input.offset + len(results)
    if not results and input.offset == 0:
        out["hint"] = (
            "Nothing recorded for this relationship. Try get_entity(name) for "
            "its description, or search_text(query)."
        )
    return _fit(out)


async def _ancestors_nearest_first(uri: str) -> list[dict]:
    """Every location containing uri, ordered by containment distance.

    Fetches the ancestor set and the direct contains edges among it, then walks
    up from uri breadth-first. Cheaper than ordering in SPARQL, which explodes
    on places with several parents.
    """
    rows = await sq.sparql_select(sq.build_location_ancestors_query(uri))
    info = {
        sq.val(b, "ancestor"): {
            "name": sq.val(b, "ancestorName"),
            "types": _types_of(b),
        }
        for b in rows
    }
    if not info:
        return []
    parents: dict[str, set[str]] = {}
    edges = await sq.sparql_select(sq.build_contains_edges_query([uri, *info]))
    for b in edges:
        parents.setdefault(sq.val(b, "child"), set()).add(sq.val(b, "parent"))
    ordered: list[str] = []
    frontier = [uri]
    while frontier:
        nxt = sorted(
            {p for c in frontier for p in parents.get(c, ())} - set(ordered)
        )
        ordered.extend(nxt)
        frontier = nxt
    return [info[u] for u in ordered if u in info]


@mcp.tool()
async def get_location_hierarchy(input: GetLocationHierarchyInput) -> dict:
    """Where a location sits: every place containing it (nearest first) and the
    places inside it, a page at a time."""
    found = await _resolve(input.location)
    if not found:
        return _not_found(input.location, await _suggest(input.location))
    uri = found[0]["uri"]
    ancestors = await _ancestors_nearest_first(uri)
    rows = await sq.sparql_select(
        sq.build_location_children_query(
            uri, input.depth, input.limit, input.offset
        )
    )
    total = await _total(
        sq.build_location_children_query(uri, input.depth, 0, 0, count=True)
    )
    children = [
        {"name": sq.val(b, "childName"), "types": _types_of(b)} for b in rows
    ]
    out = {
        "location": found[0]["name"],
        "ancestors": ancestors,
        "children": children,
        "total_children": total,
        "offset": input.offset,
    }
    if input.offset + len(children) < total:
        out["truncated"] = True
        out["next_offset"] = input.offset + len(children)
    return _fit(out)


@mcp.tool()
async def search_by_property(input: SearchByPropertyInput) -> dict:
    """Names of entities of a type whose property matches a value, e.g. NPCs
    with nationality 'Breland', Traditions with category 'fashion'.

    Entity-valued properties match the linked entity's name.
    """
    if input.entity_type not in sq.ENTITY_CLASS:
        return _unknown(
            "entity_type",
            input.entity_type,
            _ENTITY_TYPES,
            "search_by_property(entity_type='NPC', property_name='nationality', value='Breland')",
        )
    if input.property_name not in sq.ALLOWED_PROPERTY_NAMES:
        return _unknown(
            "property_name",
            input.property_name,
            _PROPERTIES,
            "search_by_property(entity_type='NPC', property_name='nationality', value='Breland')",
        )
    query = sq.build_search_by_property_query(
        input.entity_type,
        input.property_name,
        input.value,
        input.match,
        input.edition,
        input.canon_type,
        input.limit,
        input.offset,
    )
    names = [sq.val(b, "name") for b in await sq.sparql_select(query)]
    total = await _total(
        sq.build_count_query(
            input.entity_type,
            input.edition,
            input.canon_type,
            {input.property_name: input.value},
            input.match,
        )
    )
    return _fit(
        {
            "entity_type": input.entity_type,
            "property_name": input.property_name,
            "value": input.value,
            **_page(names, total, input.offset),
        }
    )


_STOPWORDS = frozenset(
    "the and for with what who which where when how are was were is of in on "
    "to a an about tell me list from that this their there".split()
)


@mcp.tool()
async def search_text(input: SearchTextInput) -> dict:
    """Search inside entity names and descriptions when no relationship or
    property fits the question, e.g. 'Breland capital', 'fashion'.

    Returns the best-matching entities with a snippet of their description and
    their sources.
    """
    if input.entity_type and input.entity_type not in sq.ENTITY_CLASS:
        return _unknown(
            "entity_type",
            input.entity_type,
            _ENTITY_TYPES,
            "search_text(query='fashion', entity_type='Tradition')",
        )
    words = [
        w
        for w in re.findall(r"[\w'’-]+", input.query.lower())
        if len(w) >= 3 and w not in _STOPWORDS
    ]
    if not words:
        return {
            "error": f"No searchable words in {input.query!r}.",
            "hint": "Use at least one word of 3+ letters that is not a stopword, e.g. search_text(query='Breland capital').",
        }
    rows = await sq.sparql_select(
        sq.build_search_text_query(words, input.entity_type, input.limit * 3)
    )
    hits: dict[str, dict] = {}
    for b in rows:
        uri = sq.val(b, "entity")
        if uri in hits:
            continue
        desc = sq.val(b, "desc") or ""
        pos = min(
            (desc.lower().find(w) for w in words if w in desc.lower()),
            default=0,
        )
        start = max(0, pos - 120)
        snippet = (
            ("…" if start else "")
            + desc[start : start + 300]
            + ("…" if start + 300 < len(desc) else "")
        )
        hits[uri] = {
            "name": sq.val(b, "name"),
            "matched_words": int(sq.val(b, "score") or 0),
            "snippet": snippet,
        }
        if len(hits) == input.limit:
            break
    for uri, hit in hits.items():
        refs = []
        for b in await sq.sparql_select(sq.build_sources_query(uri)):
            pages = _sort_pages(sq.split_agg(b, "pages"))
            refs.append(
                f"{sq.val(b, 'book')}"
                + (f" p.{', '.join(pages[:3])}" if pages else "")
            )
        hit["sources"] = refs[:3]
    return _fit(
        {
            "query": input.query,
            "words": words,
            "count": len(hits),
            "matches": list(hits.values()),
        }
    )


@mcp.tool()
async def list_sources(_: ListSourcesInput) -> dict:
    """Every source book in the graph, with edition, canonicity and the number
    of entities it mentions."""
    rows = await sq.sparql_select(sq.build_list_sources_query())
    books = [
        {
            "title": sq.val(b, "title"),
            "edition": sq.val(b, "edition"),
            "canon_type": sq.val(b, "canonType"),
            "entities": int(sq.val(b, "entities") or 0),
        }
        for b in rows
    ]
    return _fit({"count": len(books), "books": books})


# ── Admin HTTP endpoints ───────────────────────────────────────────────────
# These are served alongside /mcp via FastMCP's custom_route mechanism.
# Agents use /mcp; operators and the dashboard use these endpoints.


def _ingest_only(handler):
    """Answer 503 in serve mode, where Redis and MinIO are not deployed."""

    @functools.wraps(handler)
    async def wrapper(request: Request) -> JSONResponse:
        if _MODE == "serve":
            return JSONResponse(
                {
                    "error": (
                        "This server runs in read-only serve mode "
                        "(CSQE_MODE=serve): ingestion and pipeline endpoints are "
                        "disabled. Use an ingest-mode deployment for this."
                    )
                },
                status_code=503,
            )
        return await handler(request)

    return wrapper


@mcp.custom_route("/openapi.json", methods=["GET"])
async def openapi_json(_request: Request) -> JSONResponse:
    """Hand-maintained OpenAPI contract for the admin HTTP endpoints below.

    Validated by pytest-openapi (see tests/mcp_server/docker-compose.yml). The
    /mcp JSON-RPC endpoint and its tools are documented separately via MCP tool
    schemas, not here.
    """
    return JSONResponse(OPENAPI_SPEC)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe — checks Fuseki, and Redis in ingest mode."""
    fuseki_ok = await sq.fuseki_reachable()
    redis_ok = await st.redis_reachable() if _MODE == "ingest" else None
    healthy = fuseki_ok and redis_ok is not False
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "mode": _MODE,
            "version": __version__,
            "fuseki": fuseki_ok,
            "redis": redis_ok,
        },
        status_code=200 if healthy else 503,
    )


@mcp.custom_route("/status", methods=["GET"])
@_ingest_only
async def status_list(request: Request) -> JSONResponse:
    """Paginated list of all document pipeline states."""
    try:
        page = int(request.query_params.get("page", 1))
    except ValueError:
        page = 1
    data = await st.list_doc_statuses(page=max(1, page))
    return JSONResponse(data)


@mcp.custom_route("/status/{document_id}", methods=["GET"])
@_ingest_only
async def status_single(request: Request) -> JSONResponse:
    """Full pipeline state for one document, including in-progress counters."""
    document_id = request.path_params["document_id"]
    doc = await st.get_doc_status(document_id)
    if doc is None:
        return JSONResponse(
            {"error": f"Unknown document_id: {document_id!r}"}, status_code=404
        )
    return JSONResponse(doc)


@mcp.custom_route("/admin/type-assignments", methods=["GET"])
@_ingest_only
async def type_assignments(request: Request) -> JSONResponse:
    """Entity name → canonical type assignments the graph-worker stored in
    Redis, for auditing misclassifications.

    Query: type_filter (optional class name), limit (1–1000, default 200).
    """
    raw_limit = request.query_params.get("limit", "200")
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = -1
    if not 1 <= limit <= 1000:
        return JSONResponse(
            {
                "error": (
                    f"limit must be an integer from 1 to 1000, got {raw_limit!r}. "
                    "Example: /admin/type-assignments?type_filter=Language&limit=100"
                )
            },
            status_code=400,
        )
    type_filter = request.query_params.get("type_filter") or None
    data = await st.list_entity_type_assignments(
        type_filter=type_filter, limit=limit
    )
    return JSONResponse(data)


@mcp.custom_route("/admin/type-conflicts", methods=["GET"])
@_ingest_only
async def type_conflicts(_request: Request) -> JSONResponse:
    """Entities classified as two unrelated types across chunks, flagged for
    review."""
    conflicts = await st.list_type_conflicts()
    return JSONResponse({"conflicts": conflicts, "count": len(conflicts)})


@mcp.custom_route("/ingest", methods=["POST"])
@_ingest_only
async def ingest(request: Request) -> JSONResponse:
    """Accept a PDF + metadata YAML, write to MinIO, set Redis PENDING.

    Content-Type: multipart/form-data
    Fields:
      pdf      — the PDF file
      metadata — YAML string matching the PDF metadata schema
    """
    form = await request.form()
    pdf_file = form.get("pdf")
    metadata_raw = form.get("metadata")

    if pdf_file is None or metadata_raw is None:
        return JSONResponse(
            {"error": "Both 'pdf' and 'metadata' fields are required."},
            status_code=422,
        )

    try:
        metadata = yaml.safe_load(str(metadata_raw))
    except yaml.YAMLError as exc:
        return JSONResponse(
            {"error": f"Invalid YAML in metadata: {exc}"}, status_code=422
        )

    # Validate required fields
    for field in ("document_id", "title", "edition", "canon_type"):
        if not metadata.get(field):
            return JSONResponse(
                {"error": f"metadata.{field} is required."}, status_code=422
            )
    if metadata["edition"] not in ("3e", "4e", "5e", "any"):
        return JSONResponse(
            {"error": "edition must be one of: 3e, 4e, 5e, any"},
            status_code=422,
        )
    if metadata["canon_type"] not in ("canon", "kanon", "community"):
        return JSONResponse(
            {"error": "canon_type must be one of: canon, kanon, community"},
            status_code=422,
        )

    document_id = str(metadata["document_id"])
    if await st.document_id_exists(document_id):
        return JSONResponse(
            {"error": f"document_id {document_id!r} already exists."},
            status_code=409,
        )

    # Read the uploaded PDF into memory
    try:
        pdf_bytes: bytes = await pdf_file.read()
    except OSError as exc:
        return JSONResponse(
            {"error": f"Failed to read uploaded file: {exc}"}, status_code=422
        )

    # Upload PDF and YAML sidecar to MinIO
    minio_client = Minio(
        _MINIO_ENDPOINT.replace("http://", "").replace("https://", ""),
        access_key=_MINIO_ACCESS_KEY,
        secret_key=_MINIO_SECRET_KEY,
        secure=_MINIO_ENDPOINT.startswith("https://"),
    )
    yaml_bytes = yaml.dump(metadata, allow_unicode=True).encode()
    try:
        minio_client.put_object(
            _RAW_PDFS_BUCKET,
            f"{document_id}.pdf",
            io.BytesIO(pdf_bytes),
            length=len(pdf_bytes),
            content_type="application/pdf",
        )
        minio_client.put_object(
            _RAW_PDFS_BUCKET,
            f"{document_id}.yaml",
            io.BytesIO(yaml_bytes),
            length=len(yaml_bytes),
            content_type="application/yaml",
        )
    except S3Error as exc:
        return JSONResponse({"error": f"MinIO error: {exc}"}, status_code=502)
    except Exception as exc:
        _log.exception("Unexpected MinIO upload failure for %r", document_id)
        return JSONResponse(
            {"error": f"MinIO upload failed: {exc}"}, status_code=500
        )

    await st.set_doc_pending(
        document_id,
        title=str(metadata["title"]),
        edition=str(metadata["edition"]),
        canon_type=str(metadata["canon_type"]),
    )

    return JSONResponse(
        {"document_id": document_id, "status": "PENDING"}, status_code=202
    )


@mcp.custom_route("/admin/requeue/{document_id}", methods=["POST"])
@_ingest_only
async def requeue(request: Request) -> JSONResponse:
    """Reset a FAILED document back to PENDING for re-processing."""
    document_id = request.path_params["document_id"]
    ok = await st.requeue_doc(document_id)
    if not ok:
        doc = await st.get_doc_status(document_id)
        if doc is None:
            return JSONResponse(
                {"error": f"Unknown document_id: {document_id!r}"},
                status_code=404,
            )
        return JSONResponse(
            {"error": f"Document is in state {doc['status']!r}, not FAILED."},
            status_code=409,
        )
    return JSONResponse({"document_id": document_id, "status": "PENDING"})


@mcp.custom_route("/admin/restart/{document_id}", methods=["POST"])
@_ingest_only
async def restart(request: Request) -> JSONResponse:
    """Force any document back to PENDING, releasing any stale lock.

    Works regardless of current status. Use for stale in-progress documents or
    to re-trigger ingestion after a content update.
    """
    document_id = request.path_params["document_id"]
    ok = await st.restart_doc(document_id)
    if not ok:
        return JSONResponse(
            {"error": f"Unknown document_id: {document_id!r}"}, status_code=404
        )
    return JSONResponse({"document_id": document_id, "status": "PENDING"})


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log_level = os.environ.get("LOG_LEVEL", "info").lower()
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        log_level=_log_level,
        # This server is reached under a different Host header in every
        # deployment target (docker-compose service names, k8s Service
        # DNS, nip.io Ingress hostnames chosen at deploy time) — there is
        # no fixed hostname to enumerate, so DNS-rebinding Host-header
        # checking is disabled. mcp-server has no browser-facing untrusted
        # clients; dashboard/inspector are the browser-facing pieces and
        # sit behind their own Ingress hosts.
        allowed_hosts=["*"],
    )
