"""Campaign Setting Query Engine — MCP server.

MCP tools (served at /mcp) are for knowledge-graph queries by agents. Admin
HTTP routes (/health, /status, /ingest, /admin/*) are for operators and the
Streamlit dashboard.
"""

import io
import logging
import os
from typing import Literal

import yaml
from fastmcp import FastMCP
from minio import Minio, S3Error
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

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

# Derived from YAML so adding a type or relationship requires only a YAML edit.
EntityType = Literal[*tuple(sq.ENTITY_CLASS.keys())]  # type: ignore[misc]
CanonFilter = Literal["canon", "kanon", "community", "any"]
EditionFilter = Literal["3e", "4e", "5e", "any"]
RelType = Literal[*tuple(sq.REL_PROPERTY.keys())]  # type: ignore[misc]

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
        json_schema_extra={"type": "object"},
    )


class GetEntityInput(BaseModel):
    """Input for get_entity."""

    name: str = Field(
        description="The canonical name of the entity to retrieve."
    )
    edition: EditionFilter = Field(
        default="any", description="Filter by edition: 3e, 4e, 5e, or any."
    )
    canon_type: CanonFilter = Field(
        default="any",
        description="Filter by canonicity: canon, kanon, community, or any.",
    )
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
    edition: EditionFilter = Field(
        default="any", description="Filter by edition: 3e, 4e, 5e, or any."
    )
    canon_type: CanonFilter = Field(
        default="any",
        description="Filter by canonicity: canon, kanon, community, or any.",
    )


class GetLocationHierarchyInput(BaseModel):
    """Input for get_location_hierarchy."""

    location: str = Field(
        description="Name of the location to look up in the containment hierarchy."
    )
    edition: EditionFilter = Field(
        default="any", description="Filter by edition: 3e, 4e, 5e, or any."
    )
    canon_type: CanonFilter = Field(
        default="any",
        description="Filter by canonicity: canon, kanon, community, or any.",
    )


class SearchByPropertyInput(BaseModel):
    """Input for search_by_property."""

    entity_type: EntityType = Field(description="RDF class to search within.")
    property_name: str = Field(
        description=(
            "A known cs: predicate name (without prefix), e.g. 'nationality'."
        ),
        json_schema_extra={"not": {"type": "null"}},
    )
    value: str = Field(description="Value to match (case-insensitive).")
    edition: EditionFilter = Field(
        default="any", description="Filter by edition: 3e, 4e, 5e, or any."
    )
    canon_type: CanonFilter = Field(
        default="any",
        description="Filter by canonicity: canon, kanon, community, or any.",
    )


class GetIngestionStatusInput(BaseModel):
    """Input for get_ingestion_status."""

    document_id: str | None = Field(
        default=None,
        description="A specific document_id, or null to list all (first page).",
        json_schema_extra={"type": "string"},
    )


class ListCompletedDocumentsInput(BaseModel):
    """Input for list_completed_documents."""


class GetEntityEdgesInput(BaseModel):
    """Input for get_entity_edges."""

    name: str = Field(description="The canonical name of the entity.")


class ListEntityTypeAssignmentsInput(BaseModel):
    """Input for list_entity_type_assignments."""

    type_filter: str | None = Field(
        default=None,
        description=(
            "Show only entities assigned this type, e.g. 'Language' or 'Faction'. "
            "Omit to see all assignments."
        ),
        json_schema_extra={"type": "string"},
    )
    limit: int = Field(
        default=200,
        description="Maximum number of entries to return (default 200).",
    )


class ListTypeConflictsInput(BaseModel):
    """Input for list_type_conflicts."""


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
                "source_refs": sq.parse_source_refs(b),
                "editions": sq.split_agg(b, "editions"),
                "canon_types": sq.split_agg(b, "canonTypes"),
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
            short = p[len(cs_prefix) :]
        elif p == rdfs_label:
            short = "label"
        else:
            continue
        props.setdefault(short, [])
        if o and o not in props[short]:
            props[short].append(o)

    result = {
        "entity_uri": entity_uri,
        "source_book": source_book,
        "edition": edition,
        "canon_type": canon_type,
    }
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
        rel_types = sq.split_agg(b, "relTypes")
        rel_type = rel_types[0] if rel_types else ""
        if rel_type.startswith(sq.CS):
            rel_type = rel_type[len(sq.CS) :]
        results.append(
            {
                "name": sq.val(b, "relName"),
                "type": rel_type,
                "source_refs": sq.parse_source_refs(b),
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

    Ancestors (up) and direct children (down). Transitive via SPARQL property
    path (cs:contains+); no OWL reasoner required.
    """
    anc_query = sq.build_location_ancestors_query(input.location)
    child_query = sq.build_location_children_query(input.location)

    anc_bindings, child_bindings = (
        await sq.sparql_select(anc_query),
        await sq.sparql_select(child_query),
    )

    def _type_short(b: dict, key: str) -> str:
        t = sq.val(b, key) or ""
        return t[len(sq.CS) :] if t.startswith(sq.CS) else t

    ancestors = [
        {
            "name": sq.val(b, "ancestorName"),
            "type": _type_short(b, "ancestorType"),
        }
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
            "source_refs": sq.parse_source_refs(b),
            "editions": sq.split_agg(b, "editions"),
            "canon_types": sq.split_agg(b, "canonTypes"),
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
async def list_completed_documents(input: ListCompletedDocumentsInput) -> dict:
    """List every document that has been fully ingested into the knowledge
    graph.

    Returns document_id, title, entity_count, triple_count, and completed_at
    for each COMPLETED document. Use this to confirm a source book is ready
    before querying its entities via list_entities or get_entity.
    """
    docs = await st.list_all_completed()
    return {"documents": docs, "count": len(docs)}


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


@mcp.tool()
async def get_entity_edges(input: GetEntityEdgesInput) -> dict:
    """Return all outgoing edges from an entity to other named entities.

    Each edge is {predicate, related_name}. Results are grouped by predicate
    for readability. Excludes structural triples (rdf:type, source book links).
    Use this to see every relationship an entity has in one call.
    """
    query = sq.build_get_entity_edges_query(input.name)
    bindings = await sq.sparql_select(query)

    if not bindings:
        return {"error": f"Entity not found or has no edges: {input.name!r}"}

    cs_prefix = sq.CS
    edges_by_predicate: dict[str, list[str]] = {}
    for b in bindings:
        p = sq.val(b, "p") or ""
        short = p[len(cs_prefix) :] if p.startswith(cs_prefix) else p
        label = sq.val(b, "relatedLabel") or ""
        edges_by_predicate.setdefault(short, [])
        if label not in edges_by_predicate[short]:
            edges_by_predicate[short].append(label)

    return {
        "entity": input.name,
        "edges": edges_by_predicate,
        "edge_count": sum(len(v) for v in edges_by_predicate.values()),
    }


@mcp.tool()
async def list_entity_type_assignments(
    input: ListEntityTypeAssignmentsInput,
) -> dict:
    """List entity name → canonical type assignments stored in Redis.

    Use this to audit how the pipeline classified entities and spot
    misclassifications. Filter by type_filter to see, e.g., everything
    currently classified as 'Language'. Useful for identifying first-match
    errors before querying the graph.
    """
    return await st.list_entity_type_assignments(
        type_filter=input.type_filter,
        limit=input.limit,
    )


@mcp.tool()
async def list_type_conflicts(_: ListTypeConflictsInput) -> dict:
    """List type conflicts flagged for review during graph ingestion.

    A conflict is recorded when the same entity name was classified as two
    unrelated types across different chunks (neither is a subclass of the
    other). Review these to identify first-match misclassifications and decide
    which type should be canonical.
    """
    conflicts = await st.list_type_conflicts()
    return {"conflicts": conflicts, "count": len(conflicts)}


# ── Admin HTTP endpoints ───────────────────────────────────────────────────
# These are served alongside /mcp via FastMCP's custom_route mechanism.
# Agents use /mcp; operators and the dashboard use these endpoints.


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
