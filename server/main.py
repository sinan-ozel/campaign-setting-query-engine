"""Campaign Setting Query Engine — MCP server.

MCP tools (served at /mcp) are for knowledge-graph queries by agents.
Admin HTTP routes (/health, /status, /ingest, /admin/*) are for operators
and the Streamlit dashboard.
"""

import io
import json
import logging
import os
from typing import Literal

import yaml
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from server import sparql as sq
from server import status as st

mcp = FastMCP("campaign-query-engine")


class _SuppressMCPUnionValidation(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Failed to validate request:")


logging.getLogger().addFilter(_SuppressMCPUnionValidation())

_MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
_MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
_MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
_RAW_PDFS_BUCKET = "raw-pdfs"

EntityType = Literal[
    "NPC",
    "Faction",
    "Religion",
    "Deity",
    "Race",
    "CharacterClass",
    "Skill",
    "Location",
    "River",
    "City",
    "Region",
    "Nation",
    "Dungeon",
    "Sea",
    "Mountain",
    "Forest",
    "Ruin",
    "Plane",
]
CanonFilter = Literal["canon", "kanon", "community", "any"]
EditionFilter = Literal["3e", "4e", "5e", "any"]
RelType = Literal[
    "allies",
    "enemies",
    "members",
    "operatesIn",
    "contains",
    "worships",
    "hasPotentialMotive",
    "controlledBy",
    "locatedIn",
    "nationality",
]

# ── Pydantic input models ──────────────────────────────────────────────────


class ListEntitiesInput(BaseModel):
    """Input for list_entities."""

    entity_type: EntityType = Field(
        description="The RDF class to enumerate (e.g. 'River', 'NPC', 'Faction')."
    )
    edition: EditionFilter = Field(
        default="any", description="Filter by edition: 3e, 4e, 5e, or any."
    )
    canon_type: CanonFilter = Field(
        default="any",
        description="Filter by canonicity: canon, kanon, community, or any.",
    )
    filters: dict | None = Field(
        default=None,
        description=(
            "Optional property-value filters, e.g. {'nationality': 'Breland'}. "
            "Keys must be known cs: predicates."
        ),
    )


class GetEntityInput(BaseModel):
    """Input for get_entity."""

    name: str = Field(description="The canonical name of the entity to retrieve.")
    edition: EditionFilter = Field(default="any")
    canon_type: CanonFilter = Field(default="any")
    depth: Literal["summary", "full"] = Field(
        default="summary",
        description="summary: core properties only. full: all relationships.",
    )


class GetRelationshipsInput(BaseModel):
    """Input for get_relationships."""

    entity_name: str = Field(description="Name of the source entity.")
    relationship: RelType = Field(
        description="The relationship to traverse (e.g. 'allies', 'contains')."
    )
    edition: EditionFilter = Field(default="any")
    canon_type: CanonFilter = Field(default="any")


class GetLocationHierarchyInput(BaseModel):
    """Input for get_location_hierarchy."""

    location: str = Field(
        description="Name of the location to look up in the containment hierarchy."
    )
    edition: EditionFilter = Field(default="any")
    canon_type: CanonFilter = Field(default="any")


class SearchByPropertyInput(BaseModel):
    """Input for search_by_property."""

    entity_type: EntityType = Field(
        description="RDF class to search within."
    )
    property_name: str = Field(
        description=(
            "A known cs: predicate name (without prefix), e.g. 'nationality'."
        ),
        json_schema_extra={"not": {"type": "null"}},
    )
    value: str = Field(description="Value to match (case-insensitive).")
    edition: EditionFilter = Field(default="any")
    canon_type: CanonFilter = Field(default="any")


class GetIngestionStatusInput(BaseModel):
    """Input for get_ingestion_status."""

    document_id: str | None = Field(
        default=None,
        description="A specific document_id, or null to list all (first page).",
    )


# ── MCP tools ─────────────────────────────────────────────────────────────


@mcp.tool()
async def list_entities(input: ListEntitiesInput) -> dict:
    """List all entities of a given type.

    Returns name, type, page_reference, source_book, edition, canon_type.
    Primary tool for enumeration: 'list rivers', 'list factions'.
    """
    if input.entity_type not in sq.ENTITY_CLASS:
        return {"error": f"Unknown entity_type: {input.entity_type}"}

    query = sq.build_list_entities_query(
        input.entity_type, input.edition, input.canon_type, input.filters
    )
    bindings = await sq.sparql_select(query)

    results = []
    for b in bindings:
        results.append(
            {
                "name": sq.val(b, "name"),
                "type": input.entity_type,
                "page_reference": sq.val(b, "page"),
                "source_book": sq.val(b, "bookTitle"),
                "edition": sq.val(b, "edition"),
                "canon_type": sq.val(b, "canonType"),
            }
        )

    return {
        "results": results,
        "count": len(results),
        "applied_filters": {
            "entity_type": input.entity_type,
            "edition": input.edition,
            "canon_type": input.canon_type,
        },
    }


@mcp.tool()
async def get_entity(input: GetEntityInput) -> dict:
    """Return all known facts about a named entity.

    summary: core properties. full: all relationships.
    Always includes page_reference and source_book.
    """
    query = sq.build_get_entity_query(
        input.name, input.edition, input.canon_type, input.depth
    )
    bindings = await sq.sparql_select(query)

    if not bindings:
        return {"error": f"Entity not found: {input.name!r}"}

    entity_uri = sq.val(bindings[0], "entity")
    props: dict[str, list] = {}
    source_book = sq.val(bindings[0], "bookTitle")
    edition = sq.val(bindings[0], "edition")
    canon_type = sq.val(bindings[0], "canonType")

    cs_prefix = sq.CS
    rdfs_label = f"{sq.RDFS}label"

    for b in bindings:
        p = sq.val(b, "p") or ""
        o = sq.val(b, "o")
        # Use short names for known cs: and rdfs: predicates
        if p.startswith(cs_prefix):
            short = p[len(cs_prefix):]
        elif p == rdfs_label:
            short = "label"
        else:
            continue
        props.setdefault(short, [])
        if o and o not in props[short]:
            props[short].append(o)

    result = {"entity_uri": entity_uri, "source_book": source_book,
              "edition": edition, "canon_type": canon_type}
    result.update({k: (v[0] if len(v) == 1 else v) for k, v in props.items()})
    return result


@mcp.tool()
async def get_relationships(input: GetRelationshipsInput) -> dict:
    """Traverse a named relationship from an entity.

    Returns related names, types, and page references.
    """
    if input.relationship not in sq.REL_PROPERTY:
        return {"error": f"Unknown relationship: {input.relationship}"}

    query = sq.build_get_relationships_query(
        input.entity_name, input.relationship, input.edition, input.canon_type
    )
    bindings = await sq.sparql_select(query)

    results = []
    for b in bindings:
        rel_type = sq.val(b, "relType") or ""
        if rel_type.startswith(sq.CS):
            rel_type = rel_type[len(sq.CS):]
        results.append(
            {
                "name": sq.val(b, "relName"),
                "type": rel_type,
                "page_reference": sq.val(b, "page"),
                "source_book": sq.val(b, "bookTitle"),
            }
        )

    return {
        "entity": input.entity_name,
        "relationship": input.relationship,
        "results": results,
        "count": len(results),
    }


@mcp.tool()
async def get_location_hierarchy(input: GetLocationHierarchyInput) -> dict:
    """Return the full spatial containment chain for a location.

    Ancestors (up) and direct children (down). Transitive via OWL inference.
    """
    anc_query = sq.build_location_ancestors_query(input.location)
    child_query = sq.build_location_children_query(input.location)

    anc_bindings, child_bindings = (
        await sq.sparql_select(anc_query),
        await sq.sparql_select(child_query),
    )

    def _type_short(b: dict, key: str) -> str:
        t = sq.val(b, key) or ""
        return t[len(sq.CS):] if t.startswith(sq.CS) else t

    ancestors = [
        {"name": sq.val(b, "ancestorName"), "type": _type_short(b, "ancestorType")}
        for b in anc_bindings
    ]
    children = [
        {
            "name": sq.val(b, "childName"),
            "type": _type_short(b, "childType"),
            "page_reference": sq.val(b, "page"),
        }
        for b in child_bindings
    ]

    return {
        "location": input.location,
        "ancestors": ancestors,
        "children": children,
    }


@mcp.tool()
async def search_by_property(input: SearchByPropertyInput) -> dict:
    """Find entities matching a property value.

    e.g. entity_type='NPC', property_name='nationality', value='Breland'.
    Always returns page_reference and source_book.
    """
    if input.property_name not in sq.ALLOWED_PROPERTY_NAMES:
        return {
            "error": (
                f"property_name {input.property_name!r} is not a known cs: predicate. "
                f"Allowed: {sorted(sq.ALLOWED_PROPERTY_NAMES)}"
            )
        }

    query = sq.build_search_by_property_query(
        input.entity_type,
        input.property_name,
        input.value,
        input.edition,
        input.canon_type,
    )
    bindings = await sq.sparql_select(query)

    results = [
        {
            "name": sq.val(b, "name"),
            "type": input.entity_type,
            "page_reference": sq.val(b, "page"),
            "source_book": sq.val(b, "bookTitle"),
            "edition": sq.val(b, "edition"),
            "canon_type": sq.val(b, "canonType"),
        }
        for b in bindings
    ]

    return {
        "results": results,
        "count": len(results),
        "applied_filters": {
            "entity_type": input.entity_type,
            "property_name": input.property_name,
            "value": input.value,
        },
    }


@mcp.tool()
async def get_ingestion_status(input: GetIngestionStatusInput) -> dict:
    """Return pipeline status for one or all documents.

    States: PENDING | CONVERTING_PDF | MARKDOWN_READY |
            CLASSIFYING_SECTIONS | EXTRACTING_ENTITIES |
            MAPPING_TO_ONTOLOGY | LOADING_GRAPH | COMPLETED | FAILED.
    In-progress documents include current_page/total_pages or
    current_chunk/total_chunks. COMPLETED includes entity_count and
    triple_count. FAILED includes error and last_successful_stage.
    """
    if input.document_id:
        doc = await st.get_doc_status(input.document_id)
        if doc is None:
            return {"error": f"Unknown document_id: {input.document_id!r}"}
        return doc

    return await st.list_doc_statuses(page=1)


# ── Admin HTTP endpoints ───────────────────────────────────────────────────
# These are served alongside /mcp via FastMCP's custom_route mechanism.
# Agents use /mcp; operators and the dashboard use these endpoints.


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe — checks Fuseki and Redis connectivity."""
    fuseki_ok = await sq.fuseki_reachable()
    redis_ok = await st.redis_reachable()
    healthy = fuseki_ok and redis_ok
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "fuseki": fuseki_ok,
            "redis": redis_ok,
        },
        status_code=200 if healthy else 503,
    )


@mcp.custom_route("/status", methods=["GET"])
async def status_list(request: Request) -> JSONResponse:
    """Paginated list of all document pipeline states."""
    try:
        page = int(request.query_params.get("page", 1))
    except ValueError:
        page = 1
    data = await st.list_doc_statuses(page=max(1, page))
    return JSONResponse(data)


@mcp.custom_route("/status/{document_id}", methods=["GET"])
async def status_single(request: Request) -> JSONResponse:
    """Full pipeline state for one document, including in-progress counters."""
    document_id = request.path_params["document_id"]
    doc = await st.get_doc_status(document_id)
    if doc is None:
        return JSONResponse(
            {"error": f"Unknown document_id: {document_id!r}"}, status_code=404
        )
    return JSONResponse(doc)


@mcp.custom_route("/ingest", methods=["POST"])
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
            {"error": "edition must be one of: 3e, 4e, 5e, any"}, status_code=422
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

    # Upload PDF and YAML sidecar to MinIO
    try:
        from minio import Minio

        minio_client = Minio(
            _MINIO_ENDPOINT.replace("http://", "").replace("https://", ""),
            access_key=_MINIO_ACCESS_KEY,
            secret_key=_MINIO_SECRET_KEY,
            secure=_MINIO_ENDPOINT.startswith("https://"),
        )

        pdf_bytes: bytes = await pdf_file.read()
        pdf_stream = io.BytesIO(pdf_bytes)
        minio_client.put_object(
            _RAW_PDFS_BUCKET,
            f"{document_id}.pdf",
            pdf_stream,
            length=len(pdf_bytes),
            content_type="application/pdf",
        )

        yaml_bytes = yaml.dump(metadata, allow_unicode=True).encode()
        yaml_stream = io.BytesIO(yaml_bytes)
        minio_client.put_object(
            _RAW_PDFS_BUCKET,
            f"{document_id}.yaml",
            yaml_stream,
            length=len(yaml_bytes),
            content_type="application/yaml",
        )
    except Exception as exc:
        return JSONResponse(
            {"error": f"MinIO upload failed: {exc}"}, status_code=500
        )

    await st.set_doc_pending(
        document_id,
        title=str(metadata["title"]),
        edition=str(metadata["edition"]),
        canon_type=str(metadata["canon_type"]),
    )

    return JSONResponse({"document_id": document_id, "status": "PENDING"}, status_code=202)


@mcp.custom_route("/admin/requeue/{document_id}", methods=["POST"])
async def requeue(request: Request) -> JSONResponse:
    """Reset a FAILED document back to PENDING for re-processing."""
    document_id = request.path_params["document_id"]
    ok = await st.requeue_doc(document_id)
    if not ok:
        doc = await st.get_doc_status(document_id)
        if doc is None:
            return JSONResponse(
                {"error": f"Unknown document_id: {document_id!r}"}, status_code=404
            )
        return JSONResponse(
            {"error": f"Document is in state {doc['status']!r}, not FAILED."},
            status_code=409,
        )
    return JSONResponse({"document_id": document_id, "status": "PENDING"})


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
