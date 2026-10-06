# MCP Tools

The server exposes 10 tools at the `/mcp` endpoint (streamable-http transport). Connect any MCP-compatible agent or client to `http://<host>:8000/mcp`.

The tools are built for small local models with small context windows (e.g. a 2B model with 32k context). They form a ladder, so that no single call can flood the client:

1. **Find names**: `list_names` to enumerate a type, `search_entities` for a partial name.
2. **Follow links**: `get_relationships`.
3. **Read one entity**: `get_entity`.
4. **Cite**: `get_sources` for books and pages.
5. **Fallback**: `search_text` when no relationship or property fits.

`describe_schema` tells the model which types, relationships and properties exist.

**Size limits.** List tools page their results (`limit` / `offset`, at most 200 per page) and report `total`, `truncated` and `next_offset`. On top of that, every response is capped at `MAX_RESPONSE_CHARS` characters (default 12000, roughly 3k tokens). A response over the cap is trimmed and marked `truncated: true` with a `note`.

**Errors** come back as `{"error", "did_you_mean", "hint"}`. An unknown type, relationship or property gets its closest valid names; an unknown entity name gets the closest entity names.

---

## `describe_schema`

What the graph can answer: entity types grouped under their root type, every relationship with a one-line meaning, filterable properties, enumerated values, and the workflow above. No input. Its output is a fixed ~2k tokens.

---

## Finding names

### `list_names`

Names of every entity of a type, alphabetical, one page at a time. Subtypes are included.

| Field | Type | Default | Description |
|---|---|---|---|
| `entity_type` | string | required | One of the [entity types](ontology.md#entity-types) |
| `filters` | object | `{}` | Property → value, all must match; entity-valued properties match the linked entity's name |
| `edition` | string | `any` | `3e`, `4e`, `5e`, `any` |
| `canon_type` | string | `any` | `canon`, `kanon`, `community`, `any` |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | For paging |

```json
list_names(entity_type="River")
→ {"entity_type": "River", "total": 23, "offset": 0, "count": 23, "names": ["Brey River", "Dagger River", ...]}

list_names(entity_type="Tradition", filters={"category": "fashion", "practicedBy": "Breland"})
```

### `search_entities`

Entities whose name or alias contains the query, best first (exact, then prefix, then substring; shorter names first).

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Part of a name or alias (2+ characters) |
| `entity_type` | string | none | Restrict to a type |
| `limit` | int | 10 | 1–50 |

```json
search_entities(query="Cannith")
→ {"matches": [{"name": "House Cannith", "types": ["DragonmarkedHouse"]}, {"name": "Merrix d'Cannith", "types": ["NPC"]}, ...]}
```

---

## Following links

### `get_relationships`

Entities reached from one entity over one [relationship](ontology.md#relationships): names and types, paged.

| Field | Type | Default | Description |
|---|---|---|---|
| `entity_name` | string | required | Exact name or alias (case-insensitive) |
| `relationship` | string | required | e.g. `members`, `capital`, `ruler`, `traditions`, `produces` |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | For paging |

```json
get_relationships(entity_name="House Cannith", relationship="members")
get_relationships(entity_name="Breland", relationship="capital")   → Wroat
```

An empty result comes with a hint to read the entity's description or use `search_text`.

### `get_location_hierarchy`

Every place containing a location (nearest first), and the places inside it.

| Field | Type | Default | Description |
|---|---|---|---|
| `location` | string | required | Exact location name |
| `depth` | string | `direct` | `direct`: children only; `all`: every level below |
| `limit` / `offset` | int | 50 / 0 | Paging over children |

---

## Reading one entity

### `get_entity`

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Exact name or alias (case-insensitive) |
| `section` | string | `facts` | `facts` or `relationships` |
| `page` | int | 1 | Page of relationships (60 links each) |

- `facts`: types plus every literal property (description, aliases, status, years, climate, ...).
- `relationships`: every link to another entity, in both directions, grouped by relationship name.

When several entities carry the name, the one whose label matches exactly wins, and the rest are listed in `other_matches`.

### `get_sources`

Books (with edition and canonicity) and pages for up to 10 entities at once. Use it to cite answers.

```json
get_sources(names=["Brey River"])
→ {"results": [{"name": "Brey River", "sources": [{"book": "Eberron: Rising from the Last War", "edition": "5e", "canon_type": "canon", "pages": ["142", "160"]}]}]}
```

---

## Searching by value or text

### `search_by_property`

Names of entities of a type whose property matches a value.

| Field | Type | Default | Description |
|---|---|---|---|
| `entity_type` | string | required | Type to search in |
| `property_name` | string | required | One of the [filterable properties](ontology.md#filterable-properties) |
| `value` | string | required | Case-insensitive |
| `match` | string | `exact` | `exact` or `contains` |
| `edition`, `canon_type`, `limit`, `offset` | | | As in `list_names` |

### `search_text`

The fallback when no relationship or property fits: searches entity names and descriptions for the query's words, ranks entities by how many words they contain, and returns a snippet plus up to three sources for each.

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Words to look for (stopwords and words under 3 letters are ignored) |
| `entity_type` | string | none | Restrict to a type |
| `limit` | int | 10 | 1–25 |

```json
search_text(query="capital of Breland")
→ {"matches": [{"name": "Wroat", "matched_words": 2, "snippet": "The capital of Breland.", "sources": ["Politics of Breland p.2", ...]}]}
```

---

## Sources

### `list_sources`

Every source book in the graph with edition, canonicity and the number of entities it mentions. No input.

---

## Moved out of MCP in 0.2.0

These were MCP tools before and are now admin HTTP endpoints, because they are for operators rather than client models (see [Admin API](admin-api.md)):

| Old tool | Now |
|---|---|
| `get_ingestion_status` | `GET /status`, `GET /status/{document_id}` |
| `list_entity_type_assignments` | `GET /admin/type-assignments` |
| `list_type_conflicts` | `GET /admin/type-conflicts` |
| `list_completed_documents` | `list_sources` (reads the graph, works in serve mode) |
| `get_entity_edges` | `get_entity(section="relationships")` |
| `list_entities` | `list_names` + `get_sources` |
