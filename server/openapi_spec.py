"""Hand-maintained OpenAPI 3.1 spec for the admin HTTP endpoints.

Served at GET /openapi.json and validated by pytest-openapi (see
tests/mcp_server/docker-compose.yml). This is the contract: every documented
status code must be one the server can genuinely return, every response and
request body needs a description + example, and no endpoint should ever return
an undocumented 5xx.

The MCP tools (list_names, get_entity, ...) are exposed over the separate /mcp
JSON-RPC endpoint, not as individual REST paths, so they are intentionally not
documented here.

/status/{document_id}, /ingest, /admin/requeue/{document_id}, and
/admin/restart/{document_id} all need either pre-existing document state or a
real file upload to exercise their 200 path, which pytest-openapi's JSON-only,
stateless test generation can't provide — they're excluded from live contract
testing via --openapi-ignore in the test command, but stay fully documented
here.
"""

_ERROR_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "string",
            "description": "Human-readable explanation of what went wrong and, where applicable, what the caller should do differently.",
        }
    },
}

_DOCUMENT_STATE_SCHEMA = {
    "type": "object",
    "description": "Pipeline state for one ingested document. All fields are strings because they are stored as a Redis hash.",
    "required": ["document_id", "status"],
    "properties": {
        "document_id": {
            "type": "string",
            "description": "Unique identifier of the document, as submitted to /ingest.",
        },
        "status": {
            "type": "string",
            "description": "Current pipeline stage.",
            "enum": [
                "PENDING",
                "CONVERTING_PDF",
                "MARKDOWN_READY",
                "CLASSIFYING_SECTIONS",
                "EXTRACTING_ENTITIES",
                "MAPPING_TO_ONTOLOGY",
                "LOADING_GRAPH",
                "COMPLETED",
                "FAILED",
            ],
        },
        "title": {
            "type": "string",
            "description": "Human-readable title of the sourcebook, as submitted to /ingest.",
        },
        "edition": {
            "type": "string",
            "description": "Edition tag submitted with the document (3e, 4e, 5e, or any).",
        },
        "canon_type": {
            "type": "string",
            "description": "Canonicity tag submitted with the document (canon, kanon, or community).",
        },
        "created_at": {
            "type": "string",
            "description": "ISO-8601 UTC timestamp of when the document was first submitted.",
        },
        "updated_at": {
            "type": "string",
            "description": "ISO-8601 UTC timestamp of the most recent state change.",
        },
        "current_page": {
            "type": "string",
            "description": "Current page number during CONVERTING_PDF, as a string.",
        },
        "total_pages": {
            "type": "string",
            "description": "Total page count during CONVERTING_PDF, as a string.",
        },
        "current_chunk": {
            "type": "string",
            "description": "Current chunk number during the classification/extraction/mapping/loading stages, as a string.",
        },
        "total_chunks": {
            "type": "string",
            "description": "Total chunk count during the classification/extraction/mapping/loading stages, as a string.",
        },
        "entity_count": {
            "type": "string",
            "description": "Number of entities extracted, populated once COMPLETED, as a string.",
        },
        "triple_count": {
            "type": "string",
            "description": "Number of RDF triples written, populated once COMPLETED, as a string.",
        },
        "error": {
            "type": "string",
            "description": "Error detail, populated once FAILED.",
        },
        "last_successful_stage": {
            "type": "string",
            "description": "The last pipeline stage that completed successfully before FAILED.",
        },
    },
}

OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "Campaign Setting Query Engine — Admin API",
        "version": "1",
        "description": (
            "Operator and dashboard-facing HTTP endpoints served alongside the "
            "/mcp JSON-RPC endpoint. Agents use /mcp for knowledge-graph "
            "queries; these endpoints are for ingestion pipeline management."
        ),
    },
    "paths": {
        "/health": {
            "get": {
                "summary": "Liveness probe",
                "description": "Checks Fuseki and Redis connectivity. Used by Kubernetes liveness/readiness probes.",
                "responses": {
                    "200": {
                        "description": "Both Fuseki and Redis are reachable.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["status", "fuseki", "redis"],
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "description": "Overall health: 'ok' if both dependencies are reachable, 'degraded' otherwise.",
                                            "enum": ["ok", "degraded"],
                                        },
                                        "fuseki": {
                                            "type": "boolean",
                                            "description": "Whether the Fuseki SPARQL endpoint responded successfully.",
                                        },
                                        "redis": {
                                            "type": "boolean",
                                            "description": "Whether Redis responded to PING.",
                                        },
                                    },
                                },
                                "example": {
                                    "status": "ok",
                                    "fuseki": True,
                                    "redis": True,
                                },
                            }
                        },
                    },
                    "503": {
                        "description": "Fuseki and/or Redis is unreachable.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["status", "fuseki", "redis"],
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "description": "Overall health: 'ok' if both dependencies are reachable, 'degraded' otherwise.",
                                            "enum": ["ok", "degraded"],
                                        },
                                        "fuseki": {
                                            "type": "boolean",
                                            "description": "Whether the Fuseki SPARQL endpoint responded successfully.",
                                        },
                                        "redis": {
                                            "type": "boolean",
                                            "description": "Whether Redis responded to PING.",
                                        },
                                    },
                                },
                                "example": {
                                    "status": "degraded",
                                    "fuseki": False,
                                    "redis": True,
                                },
                            }
                        },
                    },
                },
            }
        },
        "/status": {
            "get": {
                "summary": "Paginated pipeline status for every document",
                "description": "Returns one page of document pipeline states, most recently created first.",
                "parameters": [
                    {
                        "name": "page",
                        "in": "query",
                        "required": False,
                        "description": "1-indexed page number. Defaults to 1; invalid values are treated as 1.",
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                        },
                    }
                ],
                "responses": {
                    "200": {
                        "description": "A page of document states.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": [
                                        "documents",
                                        "total",
                                        "page",
                                        "page_size",
                                    ],
                                    "properties": {
                                        "documents": {
                                            "type": "array",
                                            "description": "Document states on this page.",
                                            "items": _DOCUMENT_STATE_SCHEMA,
                                        },
                                        "total": {
                                            "type": "integer",
                                            "description": "Total number of documents across all pages.",
                                        },
                                        "page": {
                                            "type": "integer",
                                            "description": "The page number this response corresponds to.",
                                        },
                                        "page_size": {
                                            "type": "integer",
                                            "description": "Number of documents per page.",
                                        },
                                    },
                                },
                                "example": {
                                    "documents": [],
                                    "total": 0,
                                    "page": 1,
                                    "page_size": 10,
                                },
                            }
                        },
                    }
                },
            }
        },
        "/status/{document_id}": {
            "get": {
                "summary": "Full pipeline state for one document",
                "description": "Includes in-progress counters (current_page/total_pages or current_chunk/total_chunks) while running, and entity_count/triple_count once COMPLETED.",
                "parameters": [
                    {
                        "name": "document_id",
                        "in": "path",
                        "required": True,
                        "description": "The document_id submitted to /ingest.",
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "The document's current pipeline state.",
                        "content": {
                            "application/json": {
                                "schema": _DOCUMENT_STATE_SCHEMA,
                                "example": {
                                    "document_id": "eberron-campaign-setting-3e",
                                    "status": "COMPLETED",
                                    "title": "Eberron Campaign Setting (3.5e)",
                                    "edition": "3e",
                                    "canon_type": "canon",
                                    "created_at": "2026-01-01T00:00:00+00:00",
                                    "updated_at": "2026-01-01T00:05:00+00:00",
                                    "entity_count": "412",
                                    "triple_count": "3190",
                                },
                            }
                        },
                    },
                    "404": {
                        "description": "No document with this document_id has ever been submitted.",
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {
                                    "error": "Unknown document_id: 'no-such-document'"
                                },
                            }
                        },
                    },
                },
            }
        },
        "/ingest": {
            "post": {
                "summary": "Submit a PDF sourcebook for ingestion",
                "description": "Uploads a PDF and its metadata; writes both to MinIO and marks the document PENDING for the pipeline to pick up.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["pdf", "metadata"],
                                "properties": {
                                    "pdf": {
                                        "type": "string",
                                        "format": "binary",
                                        "description": "The PDF sourcebook file.",
                                    },
                                    "metadata": {
                                        "type": "string",
                                        "description": (
                                            "YAML document with required keys document_id, title, "
                                            "edition (one of 3e, 4e, 5e, any), and canon_type (one "
                                            "of canon, kanon, community); optional keys publisher "
                                            "and tags."
                                        ),
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "202": {
                        "description": "Accepted — the document is queued as PENDING for the pdf-worker.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["document_id", "status"],
                                    "properties": {
                                        "document_id": {
                                            "type": "string",
                                            "description": "Echoes the submitted document_id.",
                                        },
                                        "status": {
                                            "type": "string",
                                            "description": "Always 'PENDING' on success.",
                                            "enum": ["PENDING"],
                                        },
                                    },
                                },
                                "example": {
                                    "document_id": "eberron-campaign-setting-3e",
                                    "status": "PENDING",
                                },
                            }
                        },
                    },
                    "422": {
                        "description": (
                            "The request is malformed: missing pdf/metadata fields, "
                            "invalid YAML, a missing required metadata key, an "
                            "unrecognised edition/canon_type value, or the uploaded "
                            "file could not be read."
                        ),
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {
                                    "error": "edition must be one of: 3e, 4e, 5e, any"
                                },
                            }
                        },
                    },
                    "409": {
                        "description": "A document with this document_id has already been submitted.",
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {
                                    "error": "document_id 'eberron-campaign-setting-3e' already exists."
                                },
                            }
                        },
                    },
                    "502": {
                        "description": "MinIO rejected the upload (bucket/connectivity issue).",
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {"error": "MinIO error: <details>"},
                            }
                        },
                    },
                    "500": {
                        "description": (
                            "Unexpected failure while uploading to MinIO. Documented "
                            "for completeness; not expected under normal operation — "
                            "see server/main.py's ingest() catch-all."
                        ),
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {
                                    "error": "MinIO upload failed: <details>"
                                },
                            }
                        },
                    },
                },
            }
        },
        "/admin/requeue/{document_id}": {
            "post": {
                "summary": "Reset a FAILED document back to PENDING",
                "description": "Only valid for documents currently in the FAILED state.",
                "parameters": [
                    {
                        "name": "document_id",
                        "in": "path",
                        "required": True,
                        "description": "The document_id to requeue.",
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "The document was reset to PENDING.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["document_id", "status"],
                                    "properties": {
                                        "document_id": {
                                            "type": "string",
                                            "description": "Echoes the requeued document_id.",
                                        },
                                        "status": {
                                            "type": "string",
                                            "description": "Always 'PENDING' on success.",
                                            "enum": ["PENDING"],
                                        },
                                    },
                                },
                                "example": {
                                    "document_id": "eberron-campaign-setting-3e",
                                    "status": "PENDING",
                                },
                            }
                        },
                    },
                    "404": {
                        "description": "No document with this document_id has ever been submitted.",
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {
                                    "error": "Unknown document_id: 'no-such-document'"
                                },
                            }
                        },
                    },
                    "409": {
                        "description": "The document exists but is not currently FAILED.",
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {
                                    "error": "Document is in state 'COMPLETED', not FAILED."
                                },
                            }
                        },
                    },
                },
            }
        },
        "/admin/restart/{document_id}": {
            "post": {
                "summary": "Force any document back to PENDING",
                "description": "Works regardless of current status and releases any stale processing lock. Use for stuck in-progress documents or to re-trigger ingestion after a content update.",
                "parameters": [
                    {
                        "name": "document_id",
                        "in": "path",
                        "required": True,
                        "description": "The document_id to restart.",
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "The document was reset to PENDING and its lock released.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["document_id", "status"],
                                    "properties": {
                                        "document_id": {
                                            "type": "string",
                                            "description": "Echoes the restarted document_id.",
                                        },
                                        "status": {
                                            "type": "string",
                                            "description": "Always 'PENDING' on success.",
                                            "enum": ["PENDING"],
                                        },
                                    },
                                },
                                "example": {
                                    "document_id": "eberron-campaign-setting-3e",
                                    "status": "PENDING",
                                },
                            }
                        },
                    },
                    "404": {
                        "description": "No document with this document_id has ever been submitted.",
                        "content": {
                            "application/json": {
                                "schema": _ERROR_SCHEMA,
                                "example": {
                                    "error": "Unknown document_id: 'no-such-document'"
                                },
                            }
                        },
                    },
                },
            }
        },
    },
}

# ── Additions kept separate from the literal above ─────────────────────────

_HEALTH_SCHEMA = {
    "type": "object",
    "required": ["status", "mode", "version", "fuseki", "redis"],
    "properties": {
        "status": {
            "type": "string",
            "description": "Overall health: 'ok' if every dependency of this mode is reachable, 'degraded' otherwise.",
            "enum": ["ok", "degraded"],
        },
        "mode": {
            "type": "string",
            "description": "CSQE_MODE: 'ingest' (full stack) or 'serve' (read-only, Fuseki only).",
            "enum": ["ingest", "serve"],
        },
        "version": {
            "type": "string",
            "description": "Server version (server/__init__.py). Its major.minor must match the graph backup it serves.",
        },
        "fuseki": {
            "type": "boolean",
            "description": "Whether the Fuseki SPARQL endpoint responded successfully.",
        },
        "redis": {
            "type": ["boolean", "null"],
            "description": "Whether Redis responded to PING; null in serve mode, where Redis is not used.",
        },
    },
}

for _code, _example in (
    (
        "200",
        {
            "status": "ok",
            "mode": "ingest",
            "version": "0.2.0",
            "fuseki": True,
            "redis": True,
        },
    ),
    (
        "503",
        {
            "status": "degraded",
            "mode": "ingest",
            "version": "0.2.0",
            "fuseki": False,
            "redis": True,
        },
    ),
):
    _content = OPENAPI_SPEC["paths"]["/health"]["get"]["responses"][_code][
        "content"
    ]["application/json"]
    _content["schema"] = _HEALTH_SCHEMA
    _content["example"] = _example

OPENAPI_SPEC["paths"]["/admin/type-assignments"] = {
    "get": {
        "summary": "Entity type assignments",
        "description": "Entity name → canonical type stored by the graph-worker, for auditing misclassifications.",
        "parameters": [
            {
                "name": "type_filter",
                "in": "query",
                "required": False,
                "description": "Only show entities assigned this class, e.g. 'Language'.",
                "schema": {"type": "string"},
                "example": "Language",
            },
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "description": "Maximum assignments to return, 1–1000 (default 200).",
                "schema": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 200,
                },
                "example": 100,
            },
        ],
        "responses": {
            "200": {
                "description": "One page of assignments plus the total matching.",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["assignments", "total"],
                            "properties": {
                                "assignments": {
                                    "type": "array",
                                    "description": "Assignments, alphabetical by name.",
                                    "items": {
                                        "type": "object",
                                        "required": ["name", "type"],
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "description": "Entity name.",
                                            },
                                            "type": {
                                                "type": "string",
                                                "description": "Assigned class.",
                                            },
                                        },
                                    },
                                },
                                "total": {
                                    "type": "integer",
                                    "description": "Assignments matching the filter.",
                                },
                            },
                        },
                        "example": {
                            "assignments": [
                                {"name": "Breland", "type": "Nation"}
                            ],
                            "total": 1,
                        },
                    }
                },
            },
            "400": {
                "description": "limit is not an integer from 1 to 1000.",
                "content": {
                    "application/json": {
                        "schema": _ERROR_SCHEMA,
                        "example": {
                            "error": "limit must be an integer from 1 to 1000, got '0'. Example: /admin/type-assignments?type_filter=Language&limit=100"
                        },
                    }
                },
            },
        },
    }
}

OPENAPI_SPEC["paths"]["/admin/type-conflicts"] = {
    "get": {
        "summary": "Type conflicts flagged during ingestion",
        "description": "Entities classified as two unrelated types across chunks; the first type was kept.",
        "responses": {
            "200": {
                "description": "Every flagged conflict.",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["conflicts", "count"],
                            "properties": {
                                "conflicts": {
                                    "type": "array",
                                    "description": "Flagged conflicts, alphabetical.",
                                    "items": {
                                        "type": "object",
                                        "required": [
                                            "name",
                                            "existing_type",
                                            "conflicting_type",
                                        ],
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "description": "Entity name.",
                                            },
                                            "existing_type": {
                                                "type": "string",
                                                "description": "Type kept.",
                                            },
                                            "conflicting_type": {
                                                "type": "string",
                                                "description": "Type rejected.",
                                            },
                                        },
                                    },
                                },
                                "count": {
                                    "type": "integer",
                                    "description": "Number of conflicts.",
                                },
                            },
                        },
                        "example": {
                            "conflicts": [
                                {
                                    "name": "Thrane",
                                    "existing_type": "Nation",
                                    "conflicting_type": "Language",
                                }
                            ],
                            "count": 1,
                        },
                    }
                },
            }
        },
    }
}

# Every route except /health and /openapi.json needs Redis or MinIO, so a
# serve-mode deployment (CSQE_MODE=serve) answers them with 503.
_SERVE_MODE_503 = {
    "description": "The server runs in read-only serve mode; ingestion and pipeline endpoints are disabled.",
    "content": {
        "application/json": {
            "schema": _ERROR_SCHEMA,
            "example": {
                "error": "This server runs in read-only serve mode (CSQE_MODE=serve): ingestion and pipeline endpoints are disabled. Use an ingest-mode deployment for this."
            },
        }
    },
}
for _path, _ops in OPENAPI_SPEC["paths"].items():
    if _path == "/health":
        continue
    for _op in _ops.values():
        _op["responses"].setdefault("503", _SERVE_MODE_503)
