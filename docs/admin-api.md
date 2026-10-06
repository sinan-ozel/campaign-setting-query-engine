# Admin API

The mcp-server exposes HTTP endpoints alongside the `/mcp` MCP endpoint. Operators and the Streamlit dashboard use these directly. Agents should use the [MCP tools](mcp-tools.md) instead.

Base URL: `http://<host>:8000`

---

## Health

### `GET /health`

Liveness probe. Checks Fuseki, and Redis in ingest mode. Also reports the server mode and version.

**Response**

```json
{"status": "ok", "mode": "ingest", "version": "0.2.0", "fuseki": true, "redis": true}
```

In serve mode `redis` is `null`. Returns `200` when healthy, `503` when degraded.

!!! note "Serve mode"
    With `CSQE_MODE=serve` (read-only, Fuseki only) every endpoint below answers `503`: there is no Redis or MinIO to back them. Only `/health`, `/openapi.json` and `/mcp` work.

---

## Document status

### `GET /status`

Paginated list of all document pipeline states.

**Query parameters**

| Parameter | Default | Description |
|---|---|---|
| `page` | `1` | Page number (20 results per page) |

**Response**

```json
{
  "documents": [
    {
      "document_id": "eberron_3e",
      "title": "Eberron Campaign Setting (3.5e)",
      "status": "COMPLETED",
      "entity_count": 1247,
      "triple_count": 8931,
      "completed_at": "2026-06-21T14:32:00Z"
    }
  ],
  "page": 1
}
```

### `GET /status/{document_id}`

Full pipeline state for one document, including in-progress counters.

**Response — in progress**

```json
{
  "document_id": "eberron_3e",
  "status": "EXTRACTING_ENTITIES",
  "current_chunk": 47,
  "total_chunks": 312
}
```

**Response — failed**

```json
{
  "document_id": "eberron_3e",
  "status": "FAILED",
  "error": "LLM connection timeout",
  "last_successful_stage": "CLASSIFYING_SECTIONS"
}
```

---

## Ingestion

### `POST /ingest`

Accept a PDF + metadata YAML and queue the document for processing.

**Content-Type**: `multipart/form-data`

**Fields**

| Field | Description |
|---|---|
| `pdf` | The PDF file |
| `metadata` | YAML string with document metadata |

**Metadata fields**

| Field | Required | Values |
|---|---|---|
| `document_id` | yes | Unique slug (letters, numbers, underscores, hyphens) |
| `title` | yes | Display name |
| `edition` | yes | `3e`, `4e`, `5e`, or `any` |
| `canon_type` | yes | `canon`, `kanon`, or `community` |
| `publisher` | no | Publisher name |
| `tags` | no | List of strings |

**Example**

```bash
curl -X POST http://localhost:8000/ingest \
  -F "pdf=@eberron_campaign_setting.pdf" \
  -F 'metadata=document_id: eberron_3e
title: "Eberron Campaign Setting (3.5e)"
edition: 3e
canon_type: canon
publisher: "Wizards of the Coast"'
```

**Response** — `202 Accepted`

```json
{"document_id": "eberron_3e", "status": "PENDING"}
```

**Error responses**

| Code | Cause |
|---|---|
| `409` | `document_id` already exists |
| `422` | Missing or invalid fields |
| `502` | MinIO upload failure |

---

## Document management

### `POST /admin/requeue/{document_id}`

Reset a `FAILED` document back to `PENDING`. Only works when the document is in `FAILED` state.

```bash
curl -X POST http://localhost:8000/admin/requeue/eberron_3e
```

### `POST /admin/restart/{document_id}`

Force any document back to `PENDING`, releasing any stale lock. Works regardless of current state. Use this for documents stuck in an in-progress state (e.g. after a worker crash) or to re-trigger ingestion after a content update.

```bash
curl -X POST http://localhost:8000/admin/restart/eberron_3e
```

---

## Graph review

These were MCP tools before 0.2.0. They read the graph-worker's Redis bookkeeping and are for operators auditing a run.

### `GET /admin/type-assignments`

Entity name → canonical type, as stored by the graph-worker.

| Query parameter | Default | Description |
|---|---|---|
| `type_filter` | none | Only entities assigned this class, e.g. `Language` |
| `limit` | 200 | 1–1000; anything else is a `400` |

```bash
curl "http://localhost:8000/admin/type-assignments?type_filter=Language&limit=100"
```

```json
{"assignments": [{"name": "Breland", "type": "Nation"}], "total": 1}
```

### `GET /admin/type-conflicts`

Entities classified as two unrelated types in different chunks; the first type was kept.

```json
{"conflicts": [{"name": "Thrane", "existing_type": "Nation", "conflicting_type": "Language"}], "count": 1}
```
