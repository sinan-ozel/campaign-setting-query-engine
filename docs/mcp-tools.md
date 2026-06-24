# MCP Tools

The server exposes 10 tools at the `/mcp` endpoint (streamable-http transport). Connect any MCP-compatible agent or client to `http://<host>:8000/mcp`.

All tools filter results by `edition` (`3e`, `4e`, `5e`, `any`) and `canon_type` (`canon`, `kanon`, `community`, `any`). Defaults are `any` for both.

Every result that references a sourcebook includes `page_reference` and `source_book`.

---

## Enumeration tools

### `list_entities`

List all entities of a given type. The primary enumeration tool.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `entity_type` | string | yes | One of the [queryable types](ontology.md#entity-types) (e.g. `River`, `NPC`, `Faction`) |
| `edition` | string | no | `3e`, `4e`, `5e`, or `any` (default) |
| `canon_type` | string | no | `canon`, `kanon`, `community`, or `any` (default) |
| `filters` | object | no | Property-value pairs, e.g. `{"nationality": "Breland"}` |

**Returns** — array of `{name, type, source_refs, editions, canon_types}`

**Examples**

> "List all rivers in Eberron."
> → `entity_type: River`

> "List canon NPCs from 3e sourcebooks."
> → `entity_type: NPC, edition: 3e, canon_type: canon`

---

### `list_completed_documents`

List every sourcebook that has been fully ingested. Use this before querying to confirm a book is ready.

**Input** — none

**Returns** — array of `{document_id, title, entity_count, triple_count, completed_at}`

---

## Entity detail tools

### `get_entity`

Return all known facts about a named entity.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Canonical name as it appears in the graph |
| `edition` | string | no | Edition filter |
| `canon_type` | string | no | Canon filter |
| `depth` | string | no | `summary` (default) — core properties; `full` — all relationships |

**Returns** — entity URI, source book, edition, canon type, and all extracted properties as key-value pairs

**Example**

> "Tell me everything about Lady Vol."
> → `name: "Lady Vol", depth: full`

---

### `get_entity_edges`

Return all outgoing edges from an entity to other named entities, grouped by predicate. One call to see every relationship at once.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Canonical entity name |

**Returns** — `{entity, edges: {predicate: [names]}, edge_count}`

**Example**

> "What are all the connections for House Cannith?"
> → `name: "House Cannith"`

---

### `get_relationships`

Traverse a single named relationship from an entity.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `entity_name` | string | yes | Source entity name |
| `relationship` | string | yes | One of the [traversable relationships](ontology.md#relationships) |
| `edition` | string | no | Edition filter |
| `canon_type` | string | no | Canon filter |

**Returns** — `{entity, relationship, results: [{name, type, source_refs}], count}`

**Example**

> "Who are the enemies of the Emerald Claw?"
> → `entity_name: "Order of the Emerald Claw", relationship: enemies`

---

### `get_location_hierarchy`

Return the full spatial containment chain for a location — ancestors up to the continent and direct children.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `location` | string | yes | Location name |
| `edition` | string | no | Edition filter |
| `canon_type` | string | no | Canon filter |

**Returns** — `{location, ancestors: [{name, type}], children: [{name, type, page_reference}]}`

The containment is traversed via SPARQL property path (`cs:contains+`) — no OWL reasoner needed. "List everything in Xen'drik" returns all descendants automatically by querying children of Xen'drik.

**Example**

> "What regions and cities are inside Breland?"
> → `location: Breland`

---

### `search_by_property`

Find entities of a given type that match a property value.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `entity_type` | string | yes | Queryable type |
| `property_name` | string | yes | A known `cs:` predicate (see [searchable properties](ontology.md#searchable-properties)) |
| `value` | string | yes | Value to match (case-insensitive) |
| `edition` | string | no | Edition filter |
| `canon_type` | string | no | Canon filter |

**Returns** — array of matching entities with source refs

**Example**

> "Find all NPCs with nationality Karrnath."
> → `entity_type: NPC, property_name: nationality, value: Karrnath`

---

## Pipeline status tools

### `get_ingestion_status`

Return pipeline state for one or all documents.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `document_id` | string | no | Specific document ID, or omit to list all |

**Returns** — Full status object including current stage, progress counters, entity/triple counts on completion, and error details on failure.

Pipeline states: `PENDING → CONVERTING_PDF → MARKDOWN_READY → CLASSIFYING_SECTIONS → EXTRACTING_ENTITIES → MAPPING_TO_ONTOLOGY → LOADING_GRAPH → COMPLETED | FAILED`

---

## Graph review tools

### `list_entity_type_assignments`

List the entity name → type assignments stored in Redis. Use this to audit how the pipeline classified entities and spot misclassifications.

**Input**

| Field | Type | Required | Description |
|---|---|---|---|
| `type_filter` | string | no | Show only assignments for this type, e.g. `Language` or `Faction` |
| `limit` | int | no | Max results (default 200) |

**Returns** — `{assignments: [{name, type}], count}`

---

### `list_type_conflicts`

List type conflicts flagged during ingestion — cases where the same entity name was classified as two unrelated types across different chunks.

**Input** — none

**Returns** — `{conflicts: [{name, types, chunks}], count}`

Use this after ingestion to identify misclassified entities before querying. A conflict means the first-seen type won; the others were skipped.
