# Campaign Setting Query Engine — DESIGN.md

> A knowledge graph–backed query engine for tabletop RPG campaign settings,
> exposed as an MCP server. Precision lore retrieval for Dungeon Masters.
> Target: Eberron.

---

## Table of Contents

1. [Project Goals](#1-project-goals)
2. [Infrastructure](#2-infrastructure)
3. [Deployment Architecture](#3-deployment-architecture)
4. [Ontology Design](#4-ontology-design)
5. [Editions and Canonicity](#5-editions-and-canonicity)
6. [PDF Metadata Interface](#6-pdf-metadata-interface)
7. [Ingestion Pipelines](#7-ingestion-pipelines)
8. [MCP Server and Tool Design](#8-mcp-server-and-tool-design)
9. [Evaluation Harness](#9-evaluation-harness)
10. [Streamlit Dashboard](#10-streamlit-dashboard)
11. [Hardware and Testbed Strategy](#11-hardware-and-testbed-strategy)
12. [Helm Chart](#12-helm-chart)
13. [Known Risks and Mitigations](#13-known-risks-and-mitigations)

---

## 1. Project Goals

**Primary**: Given a corpus of Eberron sourcebooks (PDF), answer structured
lore queries with high precision and zero hallucination. The canonical
evaluation query is:

> "List me the rivers of Eberron."

Scoring: precision (penalise hallucinations) + recall (penalise misses) + F1.

**Secondary**: The architecture — ontology, ingestion prompts, MCP tools —
should be swappable by replacing config files, so DMs running homebrew
campaigns or organisations with internal knowledge bases can adapt the
same stack without touching pipeline code.

**Non-goals**: Real-time ingestion, multi-user auth, fine-tuned models,
multiple campaign settings in parallel, vector/embedding-based retrieval.

---

## 2. Infrastructure

### Component stack

| Component | Choice | Rationale |
|---|---|---|
| Graph database | See graph DB selection below | SPARQL 1.1, named graphs, property paths |
| Object storage | MinIO (S3-compatible) | PDF staging, pipeline state, portable |
| MCP server | FastMCP + FastAPI (Python) | Thin stateless layer over SPARQL |
| LLM (inference) | gemma4:e2b via llama.cpp server | External GPU machine — not in cluster |
| Embeddings | nomic-ai/nomic-embed-text-v1.5 via fastembed | Coreference resolution only — not stored |
| Orchestration | Kubernetes + Helm | StatefulSet for graph DB, Deployment for workers |
| Status store | Redis (persistent, AOF) | Pipeline state per document, entity dedup index |

### Graph database selection

The original design used `stain/jena-fuseki:latest` with a plain
`FUSEKI_DATASET_1` environment variable. This creates a TDB2 dataset with
no OWL inference. An Assembler TTL configuration file was trialled with
`OWLFBRuleReasoner`, but the volume mount at `/ontology/ontology.ttl` was
unreliable, and the memory/startup cost was significant for a 2B parameter
model setup.

**Current approach: no OWL reasoner.** OWL inference was trialled but
dropped — the Assembler config and schema file mount add operational overhead
that outweighs the benefit in a development environment. The three features
OWL would have provided are emulated at write time instead:

- *Subclass inference* — the mapper writes explicit `rdf:type` assertions for
  every parent class (e.g. a `cs:City` entity also gets `rdf:type cs:Location`)
- *Symmetric properties* — the mapper writes both directions explicitly at
  ingestion time (e.g. both `A cs:siblingOf B` and `B cs:siblingOf A`)
- *Transitive containment* — SPARQL property path `cs:contains+` is used in
  `get_location_hierarchy` queries; no runtime inference required

Before committing to Fuseki, here is the full comparison:

| | **Jena Fuseki 4.x** | Oxigraph | RDF4J 4.x | Virtuoso OSE 7 |
|---|---|---|---|---|
| OWL transitivity | ✅ with Assembler | ❌ | ✅ | ⚠️ limited |
| License | **Apache 2.0** | MIT/Apache | EDL (BSD) | GPL-2 |
| Named graphs | ✅ | ✅ | ✅ | ✅ |
| SPARQL 1.1 Update | ✅ | ✅ | ✅ | ✅ |
| k8s support | Container / StatefulSet | Container | Container | Container |
| k8s operator | ❌ | ❌ | ❌ | ❌ |
| Enterprise adoption | Widespread | Growing | Eclipse ecosystem | DBpedia, etc. |
| Write concurrency | Single writer (TDB2) | Single process | Multi-writer | Multi-writer |
| Persistent store | TDB2 on PVC | RocksDB on PVC | Native persistence | Native persistence |

**Decision: Apache Jena Fuseki 5.1.0.**

- Apache 2.0 license — no GPL contamination, no proprietary lock-in.
- Single-writer TDB2 is not a limitation — ingestion is serialised by the
  Redis distributed lock; the graph DB never sees concurrent writes.
- `stain/jena-fuseki` is well-maintained and widely deployed.

Image pinned to `stain/jena-fuseki:5.1.0`. Do not use `:latest` for a
stateful service.

### Embedding model: for coreference only

`nomic-ai/nomic-embed-text-v1.5` via fastembed is used **only** for the
coreference resolution step in the ontology mapper (section 7). Embeddings
are computed in-process and compared with cosine similarity — they are
**not stored** in any vector database. There is no vector store in this
project.

Bake into the graph-worker container image so no internet access is needed
at runtime:

```dockerfile
RUN python -c "
from fastembed import TextEmbedding
TextEmbedding('nomic-ai/nomic-embed-text-v1.5')
"
```

### Python dependencies (pyproject.toml)

The following are added to the base `dependencies` in `pyproject.toml`:

```toml
"pyyaml>=6.0.0",
"pymupdf-layout==1.27.1",
"pymupdf4llm==0.3.4",
"fastembed==0.8.0",
```

### PDF pipeline patterns (from prior implementation)

The `pdf_pipeline.py` and `chunking_pipeline.py` reference implementations
have been deleted. The following patterns from those files are retained in
this design:

**JPX error handling** — `pymupdf4llm.to_markdown` raises
`pymupdf.mupdf.FzErrorLibrary` with `"Failed to decode JPX image"` on some
PDFs. The fallback chain is:

1. Full conversion with `to_markdown(path)`
2. On `FzErrorLibrary` ("JPX"): retry with `ignore_images=True`
3. Still failing: page-by-page conversion, skipping pages that raise JPX
   errors and replacing them with a placeholder comment

**OCR fallback** — If `words_per_page < OCR_WORDS_PER_PAGE_THRESHOLD`
(default 50) after text extraction, retry with
`to_markdown(path, use_ocr=True, ocr_language="eng")`. The threshold is
configurable via env var.

**YAML front matter** — Every converted Markdown file begins with a YAML
front matter block:

```yaml
---
filename: eberron_campaign_setting_3e
tags: [canon, 3e]                     # from folder path under /raw-pdfs/
pdf_title: "Eberron Campaign Setting"  # from PDF metadata
pdf_author: "Keith Baker et al."
pages: 324
ocr: true                             # present only if OCR was used
body_title: "Eberron Campaign Setting" # unique top-level heading, if found
---
```

`tags` is derived from the relative folder path under `LIBRARY_DIR`:
a file at `/raw-pdfs/canon/3e/book.pdf` gets `tags: [canon, 3e]`.

**Semantic chunking** — The `MarkdownChunker` class from `chunking_pipeline.py`
provides heading-based semantic chunking with:
- TOC detection and stripping (prevents TOC lines from becoming chunks)
- ATX, bold (`**Title**`), and italic (`_Title_`) heading detection
- Footer/page-number stripping with strictly-increasing page validation
- Drop-cap and prose-lead-in guards to avoid false heading detection
- Parent index and breadcrumb hierarchy metadata per chunk

Semantic heading-based chunks are preferred over fixed-token chunks for
this use case: entity descriptions align naturally with section headings,
and page numbers can be tracked accurately per section.

**Chunk metadata schema** — Adapted from `chunking_pipeline.py`. Extra
front-matter fields are **not** spread into the `book` struct — only the
five known fields are included. This is a strict contract: the graph-worker
reads exactly these keys, and nothing else leaks in from arbitrary YAML.

```python
chunk_metadata = {
    "id":                   deterministic_uuid(source_key, chunk_index),
    "file_path":            str(relative_path),      # e.g. "canon/3e/book.pdf"
    "text":                 body_text,               # the section body, stripped
    "section_title":        str | None,              # heading text for this chunk
    "section_title_in_toc": str | None,              # matched TOC entry title
    "chapter_label_in_toc": str | None,              # e.g. "Chapter 3: Sharn"
    "page_number":          int | None,              # from nearest <!-- page: N -->
    "token_count":          int,                     # cl100k_base token count
    "parent_index":         int | None,              # index of nearest ancestor chunk
    "section_hierarchy":    list[str],               # [grandparent, parent] titles
    "book": {
        # title: unique top-level heading found in the Markdown body, if exactly
        #        one exists. Comes from _unique_top_level_header() on the body text.
        #        More reliable than pdf_title for RPG books whose PDF metadata is
        #        often wrong ("Untitled", blank, or publisher boilerplate).
        "title":               book_meta.get("body_title"),

        # title_from_pdf: the "Title" field embedded in the PDF's XMP/DocInfo
        #                 metadata. Included for traceability but not used as
        #                 the canonical name — often inaccurate.
        "title_from_pdf":      book_meta.get("pdf_title"),

        # author_from_pdf: the "Author" field from PDF metadata. Same caveat —
        #                  present for traceability, may be blank or wrong.
        "author_from_pdf":     book_meta.get("pdf_author"),

        # page_count_from_pdf: total pages reported by pymupdf. Used as the
        #                      upper bound for page-number validation in
        #                      MarkdownChunker._is_footer_line().
        "page_count_from_pdf": book_meta.get("pages"),

        # tags: free-form list supplied by the submitter in the .yaml sidecar.
        #       Optional — defaults to []. No convention enforced; examples:
        #       ["core-rulebook", "setting-lore"], ["adventure-module"], etc.
        #       edition and canon_type are the authoritative filter fields;
        #       tags are for anything those structured fields don't cover.
        "tags":                book_meta.get("tags", []),
    },
    "chunking_started_at":  str | None,   # ISO 8601 UTC, wall-clock
    "chunking_completed_at": str | None,  # ISO 8601 UTC, wall-clock
}
```

No `vector` or `embedding_model` fields — there is no vector store.

### Ingestion configuration — `ingestion_config.yaml`

Configurable parameters are stored in `ingestion_config.yaml`, mounted as
a ConfigMap alongside `ontology_schema.yaml`. Pipeline code reads this file
at startup. Changing thresholds requires updating the ConfigMap and restarting
the graph-worker — no code changes.

```yaml
# ingestion_config.yaml
coreference:
  auto_merge_threshold: 0.92   # cosine similarity ≥ this → reuse URI
  review_threshold: 0.80       # ≥ this and < auto_merge → flag, new URI

chunking:
  min_section_tokens: 10       # sections shorter than this are merged up

classifier:
  # Token budget for the 1-token classification call
  max_tokens: 5
  temperature: 0.0
```

Both `ontology_schema.yaml` and `ingestion_config.yaml` are version-controlled
artifacts with full Git history and PR review.

---

## 3. Deployment Architecture

### Principle: separate pods for separate concerns

| | Fuseki | mcp-server | pdf-worker | graph-worker | dashboard |
|---|---|---|---|---|---|
| Kind | StatefulSet | Deployment | Deployment | Deployment | Deployment |
| Replicas | 1 | 1–N | 0–N (KEDA) | 0–N (KEDA) | 1 |
| State | PVC (TDB2) | None | None | None | None |
| Scales on | Never | Traffic | PDF queue depth | Markdown queue depth | Never |
| LLM needed | No | No | No | Yes | No |

**pdf-worker** — converts PDFs to Markdown page by page (pymupdf4llm).
No LLM. No ML dependencies. Reads PDF + `.yaml` sidecar from MinIO
`/raw-pdfs/`, writes `.md` to MinIO `/markdown/`. Updates Redis state with
per-page progress. Redis distributed lock prevents two pods from claiming
the same document. The lock TTL is extended after each page is written.

**graph-worker** — chunks Markdown, classifies sections, extracts all
entity types via a single combined LLM call per chunk, maps to ontology,
writes triples to Fuseki. fastembed loaded in-process for coreference
resolution. Reads from MinIO `/markdown/`, writes to Fuseki. Updates Redis
state with per-chunk progress. Lock TTL extended after each chunk.

**mcp-server** — stateless FastMCP + FastAPI server on port 8000. Serves:
- `/mcp` — MCP protocol endpoint (FastMCP), for agents and MCP clients
- `/ingest` — submit PDF + metadata (drops into MinIO)
- `/status` — pipeline state for all documents (paginated)
- `/status/{document_id}` — single document state including progress
- `/admin/requeue/{document_id}` — reset FAILED to PENDING
- `/health` — liveness probe

MCP tools query Fuseki via SPARQL. Admin endpoints are for operators.
No LLM at query time (optional query-intent classifier is out of scope).

**dashboard** — Streamlit app. Communicates only via mcp-server HTTP API.
Never accesses Redis, MinIO, or Fuseki directly.

### LLM is outside the cluster

```dotenv
LLAMA_CPP_HOST=http://sinan.msi-nvidia-server.ts.ozel.network:8080/v1
```

```yaml
model: openai/gemma4:e2b
```

LLM parallelism is handled at the infrastructure level: multiple llama.cpp
instances behind a load balancer, with multiple graph-worker replicas each
calling the LB endpoint. The pipeline code treats the LLM as a standard
OpenAI-compatible API; scaling is a deployment concern, not a code concern.

### Kubernetes layout

```
Namespace: campaign-query
─────────────────────────────────────────────────────────────────

  dashboard (Deployment, replicas: 1)
  ├── Streamlit, port 8501
  ├── No direct access to Redis, MinIO, or Fuseki
  ├── Communicates only via mcp-server HTTP API
  └── Env: MCP_SERVER_URL, POLL_INTERVAL_SECONDS

  mcp-server (Deployment, replicas: 1–N)
  ├── FastMCP + FastAPI, stateless, port 8000
  ├── No PVC, no LLM dependency
  ├── Env: FUSEKI_ENDPOINT, REDIS_URL, MINIO_ENDPOINT
  └── Redeploys freely on code change
        │
        │ HTTP  (SPARQL SELECT via /sparql, UPDATE via /update)
        ▼
  fuseki-svc (ClusterIP → StatefulSet)
  ├── Apache Jena Fuseki 5.1.0, port 3030
  ├── Dataset: /campaign (configured via Assembler TTL)
  ├── No OWL reasoner — transitivity via SPARQL cs:contains+
  ├── PVC: fuseki-data (ReadWriteOnce, 10Gi)  [TDB2 store]
  └── ConfigMap: fuseki-config    → /etc/fuseki/configuration/config.ttl

  pdf-worker (Deployment, replicas: 0–N via KEDA)
  ├── PDF → Markdown. No LLM.
  ├── Page-by-page conversion; Redis state updated per page
  ├── Reads from MinIO /raw-pdfs (PDF + .yaml sidecar)
  ├── Writes to MinIO /markdown
  └── Env: REDIS_URL, MINIO_ENDPOINT, LOCK_TTL_SECONDS

  graph-worker (Deployment, replicas: 0–N via KEDA)
  ├── Markdown → triples. LLM-heavy.
  ├── fastembed baked into image (coreference resolution)
  ├── Reads from MinIO /markdown
  ├── Writes triples to Fuseki via SPARQL UPDATE
  ├── Redis state updated per chunk
  └── Env: FUSEKI_ENDPOINT, REDIS_URL, MINIO_ENDPOINT,
           LLAMA_CPP_HOST, LOCK_TTL_SECONDS

  minio (StatefulSet)
  └── Buckets: /raw-pdfs  /markdown  /processing  /completed  /failed

  redis (StatefulSet)                      ← persistent, AOF enabled
  ├── PVC: redis-data (1Gi)
  ├── appendonly yes
  ├── Distributed lock per document_id
  └── Entity name→URI deduplication index

  app-config (ConfigMap)
  ├── ontology_schema.yaml → /config/ontology_schema.yaml
  └── ingestion_config.yaml → /config/ingestion_config.yaml

  fuseki-config (ConfigMap)
  └── config.ttl → /etc/fuseki/configuration/config.ttl

  ────────────────────────────────────────────── (cluster boundary)

  [EXTERNAL] llama.cpp server
  └── GPU machine: sinan.msi-nvidia-server.ts.ozel.network:8080
      gemma4:e2b, OpenAI-compatible /v1 API
```

### Fuseki StatefulSet manifest

No `initContainer`. The Assembler config handles ontology loading.
See section 4 for the `fuseki-config.ttl` content.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: fuseki
  namespace: campaign-query
spec:
  serviceName: fuseki-svc
  replicas: 1
  selector:
    matchLabels:
      app: fuseki
  template:
    metadata:
      labels:
        app: fuseki
    spec:
      containers:
      - name: fuseki
        image: stain/jena-fuseki:5.1.0
        env:
        - name: ADMIN_PASSWORD
          valueFrom:
            secretKeyRef:
              name: fuseki-secret
              key: admin-password
        - name: JVM_ARGS
          value: "-Xmx2g"
        # Do NOT set FUSEKI_DATASET_1 — the Assembler config takes over.
        ports:
        - containerPort: 3030
        volumeMounts:
        - name: fuseki-data
          mountPath: /fuseki-base
        - name: fuseki-config
          mountPath: /etc/fuseki/configuration
          readOnly: true
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "3Gi"
            cpu: "2000m"
      volumes:
      - name: fuseki-config
        configMap:
          name: fuseki-config
  volumeClaimTemplates:
  - metadata:
      name: fuseki-data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 10Gi
---
apiVersion: v1
kind: Service
metadata:
  name: fuseki-svc
  namespace: campaign-query
spec:
  selector:
    app: fuseki
  ports:
  - port: 3030
    targetPort: 3030
  clusterIP: None
```

### Redis StatefulSet manifest

Redis is **persistent**. AOF (Append-Only File) mode ensures state survives
pod restarts. Loss of Redis state makes it impossible to know what has been
ingested, breaks `document_id` uniqueness enforcement, and destroys the
entity deduplication index.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
  namespace: campaign-query
spec:
  serviceName: redis-svc
  replicas: 1
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        command: ["redis-server", "--appendonly", "yes"]
        ports:
        - containerPort: 6379
        volumeMounts:
        - name: redis-data
          mountPath: /data
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        readinessProbe:
          exec:
            command: ["redis-cli", "ping"]
          initialDelaySeconds: 5
          periodSeconds: 5
  volumeClaimTemplates:
  - metadata:
      name: redis-data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 1Gi
```

### MCP server Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-server
  namespace: campaign-query
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mcp-server
  template:
    metadata:
      labels:
        app: mcp-server
    spec:
      containers:
      - name: mcp-server
        image: your-registry/campaign-mcp-server:latest
        ports:
        - containerPort: 8000
        env:
        - name: FUSEKI_ENDPOINT
          value: "http://fuseki-svc:3030/campaign"
        - name: REDIS_URL
          value: "redis://redis-svc:6379"
        - name: MINIO_ENDPOINT
          value: "http://minio-svc:9000"
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
```

### Interface boundary

`FUSEKI_ENDPOINT` is the only coupling between application code and the
graph store. To replace Fuseki with another SPARQL 1.1 endpoint, update
one env var. No code changes.

---

## 4. Ontology Design

Namespace: `http://campaignsetting.io/ontology#`
Prefix: `cs:`

### 4.1 Fuseki Assembler configuration — `fuseki-config.ttl`

Stored at `config/fuseki-config.ttl` in the repository, mounted via ConfigMap
into `/etc/fuseki/configuration/config.ttl`. Fuseki loads all `.ttl` files
from `/etc/fuseki/configuration/` automatically on startup.

No OWL reasoner is configured. The dataset is a plain TDB2 store with
`tdb2:unionDefaultGraph true`, which allows SPARQL queries against the union
of all named graphs without specifying a `FROM` clause.

**Smoke test** — verify `cs:contains+` transitivity (this uses only SPARQL,
no reasoner required):

```sparql
# Seed two direct cs:contains triples
INSERT DATA {
  <http://campaignsetting.io/ontology#Breland>
      <http://campaignsetting.io/ontology#contains>
      <http://campaignsetting.io/ontology#Sharn> .
  <http://campaignsetting.io/ontology#Sharn>
      <http://campaignsetting.io/ontology#contains>
      <http://campaignsetting.io/ontology#TheCogs> .
}

PREFIX cs: <http://campaignsetting.io/ontology#>
# cs:contains+ traverses transitively via SPARQL property path
SELECT ?child WHERE { cs:Breland cs:contains+ ?child . }
# Returns both cs:Sharn and cs:TheCogs
```

### 4.2 Named graphs

Per-document entity triples live in named graphs:

```
<http://campaignsetting.io/doc/{document_id}>
```

This allows atomic rollback on failure: one `DROP GRAPH` removes all traces
of a failed ingestion run. `tdb2:unionDefaultGraph true` means the default
graph is the union of all named graphs, so queries without `FROM` clauses
see all entities automatically.

### 4.3 Ontology schema (`ontology_schema.yaml`)

The authoritative schema is `config/ontology_schema.yaml`. It defines:

- `queryable_types` — all `cs:` class names exposed by the MCP tools
- `relationship_properties` — traversable edge names for `get_relationships`
- `allowed_filter_properties` — property names accepted by `search_by_property`
- `entity_types` — per-type LLM extraction schema and property maps

Property maps drive the generic mapper (`mapper.py`) — seven map types control
how LLM-extracted fields become RDF triples. No Python changes are needed to
add fields or entity types; only a YAML edit is required.

**Subclass encoding** — instead of OWL subclass axioms, the mapper writes
explicit `rdf:type` assertions. A `cs:City` entity gets both
`rdf:type cs:City` and `rdf:type cs:Location`, resolved via the `subtypes`
section of the relevant entity type definition in the YAML.

**Symmetric properties** — `siblingOf`, `spouseOf`, `alliedWith`, `enemyOf`,
`factionAlly`, `factionEnemy` are written in both directions at ingestion time.
The `symmetric_object_properties` and `relationship_types[*].symmetric: true`
YAML keys control this.

Below is the namespace and prefix used throughout:

```turtle
@prefix cs:   <http://campaignsetting.io/ontology#> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
```

Key classes (all written as explicit `rdf:type` triples by the mapper):

```
cs:Region         rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Nation         rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Dungeon        rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Sea            rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Mountain       rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Forest         rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Ruin           rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Plane          rdf:type owl:Class ; rdfs:subClassOf cs:Location .

NPC, Location, City, Ward, Neighborhood, Tavern, Shop,
River, Sea, Mountain, MountainRange, Forest, Jungle, Desert, Plain, Island,
Continent, Nation, Region, Dungeon, Ruin, Plane, Moon,
Faction, Newspaper, DragonmarkedHouse, Family,
Religion, Deity, Race, CharacterClass, Skill, Feat, Dish,
Item, MagicItem, WondrousItem, Attire, MagicArmor, MagicWeapon,
Potion, Ring, Rod, Scroll, Staff, Wand,
PotentialMotive, SourceBook
```

See `config/ontology_schema.yaml` for the full class hierarchy (via `subtypes`),
all property names, and the LLM extraction schemas.

---

## 5. Editions and Canonicity

Edition and canonicity are metadata on the `cs:SourceBook` node. Every
entity carries `cs:mentionedIn` pointing to a `cs:SourceBook`, which
carries `cs:edition` and `cs:canonType`. Filtering is done by joining
through the SourceBook in SPARQL.

### Canon types

| Value | Meaning |
|---|---|
| `canon` | Official Hasbro / Wizards of the Coast publications |
| `kanon` | Publications by Keith Baker, original Eberron designer |
| `community` | Fan-made, third-party, or unofficial content |

### Filtering in SPARQL

```sparql
PREFIX cs:   <http://campaignsetting.io/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

SELECT ?name ?page ?bookTitle WHERE {
    ?loc  rdf:type       cs:River ;
          rdfs:label     ?name ;
          cs:mentionedIn ?src ;
          cs:pageNumber  ?page .
    ?src  rdfs:label     ?bookTitle ;
          cs:edition     "5e" ;
          cs:canonType   "canon" .
}
ORDER BY ?name
```

An entity published across editions has multiple `cs:mentionedIn` triples.
The entity node is not duplicated; the SourceBook node carries the edition.

### SourceBook bootstrap

SourceBook nodes are created by the graph-worker the first time a document
with that URI is ingested (idempotent: if the URI already exists, it is
reused). Well-known Eberron books can also be pre-loaded via SPARQL UPDATE
on first deployment:

```turtle
cs:book_eberron_campaign_setting_3e
    rdf:type         cs:SourceBook ;
    rdfs:label       "Eberron Campaign Setting (3.5e)" ;
    cs:edition       "3e" ;
    cs:canonType     "canon" ;
    cs:publisher     "Wizards of the Coast" ;
    cs:publicationYear "2004" .

cs:book_rising_from_the_last_war
    rdf:type         cs:SourceBook ;
    rdfs:label       "Eberron: Rising from the Last War" ;
    cs:edition       "5e" ;
    cs:canonType     "canon" ;
    cs:publisher     "Wizards of the Coast" ;
    cs:publicationYear "2019" .

cs:book_wayfinders_guide
    rdf:type         cs:SourceBook ;
    rdfs:label       "Wayfinder's Guide to Eberron" ;
    cs:edition       "5e" ;
    cs:canonType     "kanon" ;
    cs:publisher     "Dungeon Masters Guild" ;
    cs:publicationYear "2018" .
```

---

## 6. PDF Metadata Interface

Every PDF submitted to the ingestion pipeline must be accompanied by
metadata. This is how the pipeline associates extracted entities with the
correct SourceBook, edition, and canonicity.

### Metadata schema

```yaml
document_id: eberron_campaign_setting_3e
title: "Eberron Campaign Setting (3.5e)"
edition: 3e
canon_type: canon
publisher: Wizards of the Coast
publication_year: "2004"
authors:
  - Keith Baker
  - Bill Slavicsek
  - James Wyatt
source_book_uri: cs:book_eberron_campaign_setting_3e  # optional
tags:                                                  # optional, free-form
  - core-rulebook
  - setting-lore
```

`source_book_uri` is optional. If omitted the ingestion worker creates a
new SourceBook URI from a slug of `title`. If provided and the URI exists,
the worker reuses it (idempotent re-ingestion).

### Submission methods

**MinIO sidecar** — drop PDF + `.yaml` sidecar with the same base filename:

```
/raw-pdfs/eberron_campaign_setting_3e.pdf
/raw-pdfs/eberron_campaign_setting_3e.yaml
```

**HTTP API** — multipart POST to `/ingest`:

```
POST /ingest
Content-Type: multipart/form-data

pdf:      <file>
metadata: <yaml string>
```

**CLI** — wraps the HTTP API:

```bash
campaign-ingest \
  --pdf      ./books/eberron_cs_3e.pdf \
  --title    "Eberron Campaign Setting (3.5e)" \
  --edition  3e \
  --canon    canon \
  --publisher "Wizards of the Coast" \
  --year     2004
```

### Validation

- `document_id`: required, unique (checked against Redis). Duplicate →
  `FAILED` immediately with clear error.
- `edition`: required, one of `3e`, `4e`, `5e`, `any`.
- `canon_type`: required, one of `canon`, `kanon`, `community`.
- `title`: required.

Missing or invalid metadata → `FAILED` status, no processing attempted.

### Retry endpoint

Failed documents are **not** automatically retried. Automatic retries on
unknown failures risk data corruption (partial triples in named graph,
corrupted dedup index). Re-queue is a manual operator action:

```
POST /admin/requeue/{document_id}
```

Resets the document status from `FAILED` to `PENDING`. The pdf-worker or
graph-worker will pick it up on the next poll cycle. The Streamlit dashboard
exposes a "Retry" button for each FAILED document.

---

## 7. Ingestion Pipelines

### Pipeline overview

One pipeline: PDF → Markdown → knowledge graph triples.
There is no vector pipeline and no vector store.

```
Client submits PDF + metadata
         │
         ▼
    mcp-server  ──writes──▶  MinIO /raw-pdfs/
                                    │
                              pdf-worker polls
                                    │
                     PDF → Markdown, page by page
                     (progress + lock refresh per page)
                                    │
                         MinIO /markdown/
                                    │
                          graph-worker polls
                                    │
              classify chunk → extract all entity types → map → triples
              (progress + lock refresh per chunk)
                                    │
                               Fuseki (named graph)
```

### Redis state schema

#### State hash — `doc:{document_id}:state`

```
HSET doc:eberron_cs_3e:state
  status                CONVERTING_PDF
  title                 "Eberron Campaign Setting (3.5e)"
  edition               3e
  canon_type            canon
  worker_id             pdf-worker-7d9f4b
  created_at            2025-11-01T14:00:00Z
  updated_at            2025-11-01T14:00:05Z
  started_at            2025-11-01T14:00:05Z
  completed_at          (empty until COMPLETED)
  last_successful_stage (empty until first stage completes)
  error                 (empty unless FAILED)
  current_page          12           ← updated by pdf-worker per page
  total_pages           324          ← set at start of conversion
  current_chunk         47           ← updated by graph-worker per chunk
  total_chunks          891          ← set at start of graph processing
  entity_count          (empty until COMPLETED)
  triple_count          (empty until COMPLETED)
```

`updated_at` changes on every state write, including per-page and per-chunk
progress updates.

#### Distributed lock — `doc:{document_id}:lock`

```python
SET doc:eberron_cs_3e:lock pdf-worker-7d9f4b NX PX {ttl_ms}
```

`ttl_ms` defaults to `LOCK_TTL_SECONDS × 1000`. Set conservatively —
**the lock is explicitly extended after each page (pdf-worker) or chunk
(graph-worker)**, so it only needs to survive the processing of one unit:

- pdf-worker: `LOCK_TTL_SECONDS` default 120 (conversion of one page is
  typically 2–10 seconds; 120s gives 12× headroom)
- graph-worker: `LOCK_TTL_SECONDS` default 300 (one chunk with two LLM
  calls may take 30–120 seconds on a 2B model; 300s gives 2.5× headroom)

**Per-unit lock refresh** — after processing each page or chunk:

```python
r.hset(state_key, mapping={
    "current_page": pno + 1,    # or current_chunk
    "updated_at":   now_utc(),
})
r.pexpire(lock_key, LOCK_TTL_MS)   # extend TTL — prevents expiry mid-run
```

This replaces the previous `refresh_lock()` design that was never actually
called. No background thread is needed. The processing loop itself is the
heartbeat.

**Release** — Lua script ensures only the owner can release:

```python
script = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""
result = r.eval(script, 1, lock_key, WORKER_ID)
# result == 0 means we lost the lock — log a warning, do NOT continue writing
```

If the lock was stolen (expired + reclaimed), `result == 0`. The worker
must detect this and **abort** the current write rather than continuing.

#### Poll loop

```python
def poll_loop(r: redis.Redis, minio_client):
    while True:
        for obj in minio_client.list_objects("raw-pdfs"):
            if not obj.object_name.endswith(".pdf"):
                continue

            document_id = obj.object_name.removesuffix(".pdf")
            state_key   = f"doc:{document_id}:state"
            status      = r.hget(state_key, "status")

            # Only claim PENDING documents.
            # FAILED documents require explicit operator requeue via
            # POST /admin/requeue/{document_id} — never auto-retry.
            if status not in (None, b"PENDING"):
                continue

            if try_claim(r, document_id):
                try:
                    convert_pdf(r, minio_client, document_id)
                except Exception as e:
                    set_failed(r, document_id, str(e))
                finally:
                    release_lock(r, document_id)

        time.sleep(POLL_INTERVAL_SECONDS)
```

### Pipeline states

```
PENDING
  → CONVERTING_PDF              (pdf-worker claims)
    → MARKDOWN_READY
      → CLASSIFYING_SECTIONS    (graph-worker)
        → EXTRACTING_ENTITIES
          → MAPPING_TO_ONTOLOGY
            → LOADING_GRAPH
              → COMPLETED
              → FAILED  { error, last_successful_stage }
```

A failure in `EXTRACTING_ENTITIES` does not require re-running PDF
conversion. Retry resumes from `MARKDOWN_READY`.

### Named graphs for atomic rollback

```sparql
DROP GRAPH <http://campaignsetting.io/doc/eberron_campaign_setting_3e>
```

Called on any exception in the graph-worker before setting status to
`FAILED`. One statement removes all partial work.

### MinIO bucket layout

```
/raw-pdfs       ← PDF + .yaml sidecar written here by mcp-server / operator
/markdown       ← pdf-worker writes converted .md files here
/processing     ← files currently being worked on
/completed      ← successfully processed
/failed         ← failed documents, error artifact stored alongside
```

### pdf-worker: page-by-page conversion

Converts a PDF to Markdown with page boundary markers. Processing one
page at a time (1) enables per-page progress reporting, (2) extends the
Redis lock naturally, and (3) applies the JPX fallback per page rather
than failing the entire document.

**Page markers** are injected between pages so the graph-worker can track
provenance without re-parsing the PDF:

```markdown
<!-- page: 42 -->
## The Dagger River

The Dagger River is the longest river in Khorvaire...

<!-- page: 43 -->
...
```

**Conversion function:**

```python
def convert_pdf(r, minio_client, document_id):
    pdf_bytes = download_from_minio(minio_client, "raw-pdfs", document_id)
    yaml_meta = load_yaml_sidecar(minio_client, "raw-pdfs", document_id)

    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        pdf_path = Path(tmp.name)

        doc = pymupdf.open(str(pdf_path))
        total_pages = doc.page_count
        doc.close()

        r.hset(state_key, {"total_pages": total_pages, "current_page": 0})

        pages_md = [build_front_matter(pdf_path, yaml_meta, total_pages)]

        for pno in range(total_pages):
            page_md = convert_page_safe(pdf_path, pno)   # JPX fallback
            pages_md.append(f"\n\n<!-- page: {pno + 1} -->\n\n{page_md}")

            r.hset(state_key, {
                "current_page": pno + 1,
                "updated_at":   now_utc(),
            })
            r.pexpire(lock_key, LOCK_TTL_MS)

    full_md = "".join(pages_md)
    upload_to_minio(minio_client, "markdown", document_id, full_md)
    set_status(r, document_id, "MARKDOWN_READY")
```

**`convert_page_safe`** — the three-level JPX fallback from the reference
implementation:

```python
def convert_page_safe(pdf_path: Path, pno: int) -> str:
    try:
        return pymupdf4llm.to_markdown(str(pdf_path), pages=[pno])
    except FzErrorLibrary as e:
        if "Failed to decode JPX image" not in str(e):
            raise
    try:
        return pymupdf4llm.to_markdown(str(pdf_path), pages=[pno],
                                        ignore_images=True)
    except FzErrorLibrary as e:
        if "Failed to decode JPX image" not in str(e):
            raise
    logger.warning("PDF pipeline: skipping page %d — JPX decode error.", pno)
    return f"\n[Page {pno + 1} skipped — JPX image decode error]\n"
```

**OCR fallback** — after full conversion, if
`words_per_page < OCR_WORDS_PER_PAGE_THRESHOLD` (env var, default 50),
redo the full conversion with `use_ocr=True, ocr_language=OCR_LANGUAGE`.

**Front matter** — prepended once at the start of the Markdown file:

```python
def build_front_matter(pdf_path, yaml_meta, page_count, ocr_used=False):
    doc = pymupdf.open(str(pdf_path))
    pdf_meta = doc.metadata
    doc.close()
    data = {
        "document_id":       yaml_meta["document_id"],
        "title":             yaml_meta["title"],
        "edition":           yaml_meta["edition"],
        "canon_type":        yaml_meta["canon_type"],
        "tags":              yaml_meta.get("tags", []),
        "pdf_title":         pdf_meta.get("title") or "",
        "pdf_author":        pdf_meta.get("author") or "",
        "pages":             page_count,
    }
    if ocr_used:
        data["ocr"] = True
    return "---\n" + yaml.dump(data, allow_unicode=True) + "---\n\n"
```

### graph-worker: classify → extract → map → write

The graph-worker reads the Markdown from MinIO, parses the front matter,
strips the TOC, and semantically chunks the body using the `MarkdownChunker`
logic (heading-based, preserving page numbers from `<!-- page: N -->`
markers). It then processes each chunk in sequence.

#### Step 1 — Binary section classifier

One LLM call per chunk. Returns `ENTITIES` or `SKIP`.

```
SYSTEM:
You classify sections of a fantasy RPG sourcebook.
Return exactly ONE label:
  ENTITIES — the text describes named places, characters, factions,
             religions, races, or character classes worth extracting
  SKIP     — narrative history, atmospheric prose, rules text,
             tables, or appendix material with no extractable named entities

USER:
{chunk_text}
```

LLM config: `max_tokens=5, temperature=0.0`

`SKIP` chunks are not sent to the extraction step — they are not discarded
from the Markdown. The page numbers and section titles they carry remain
available for the `<!-- page: N -->` provenance chain.

#### Step 2 — Combined entity extraction

One LLM call per `ENTITIES` chunk. Extracts **all entity types in a single
call**. A chunk may contain multiple NPCs, multiple locations, and a faction
simultaneously — all are extracted together.

**Token budget on 6GB VRAM / gemma4:e2b**: The combined JSON schema in the
system prompt is ~600 tokens. A typical chunk is ~300–500 tokens. Combined
output (all entity arrays) runs 200–800 tokens depending on content density.
Total round-trip stays well within the 8k context window. Set
`max_tokens=1024` for extraction (vs. `max_tokens=5` for the classifier).
If the model truncates output on a particularly dense chunk, the JSON parser
will raise on invalid JSON — treat this as a soft failure, log it, and
continue to the next chunk rather than failing the entire document.

```
SYSTEM:
Extract ALL named entities from this RPG sourcebook excerpt.
Return ONLY valid JSON. No preamble, no explanation.

{
  "npcs": [{
    "name": "canonical name as written",
    "aliases": ["titles, other names"],
    "race": "race name or null",
    "character_class": "class name or null",
    "alignment": "alignment string or null",
    "nationality": "nation name or null",
    "location": "primary location or null",
    "factions": ["faction names"],
    "worships": "deity name or null",
    "potential_motives": [
      {"summary": "one sentence", "source_quote": "verbatim text or null"}
    ],
    "description": "brief description",
    "relationships": [
      {"target": "entity name", "type": "ally|enemy|rival|mentor|subordinate|other"}
    ],
    "page_reference": "42 or 42-43"
  }],
  "locations": [{
    "name": "canonical name",
    "aliases": ["other names"],
    "type": "City|River|Region|Nation|Dungeon|Sea|Mountain|Forest|Ruin|Plane|Other",
    "parent_location": "containing region or null",
    "controlling_faction": "faction name or null",
    "description": "brief description",
    "notable_npcs": ["NPC names"],
    "connected_locations": ["location names"],
    "page_reference": "page number(s)"
  }],
  "factions": [{
    "name": "canonical name",
    "aliases": ["other names"],
    "type": "Military|Criminal|Religious|Political|Mercantile|Arcane|Druidic|Other",
    "headquarters": "location name or null",
    "leader": "NPC name or null",
    "members": ["notable NPC names"],
    "potential_motives": [
      {"summary": "one sentence", "source_quote": "text or null"}
    ],
    "allies": ["faction names"],
    "enemies": ["faction names"],
    "operates_in": ["location names"],
    "worships": "deity name or null",
    "page_reference": "page number(s)"
  }],
  "religions": [{
    "name": "canonical religion name",
    "aliases": ["other names"],
    "primary_deity": "deity name or null",
    "deities": ["all associated deity names"],
    "worshipping_factions": ["faction names"],
    "dominant_in": ["nation names"],
    "description": "brief description",
    "page_reference": "page number(s)"
  }],
  "deities": [{
    "name": "canonical deity name",
    "aliases": ["titles, epithets"],
    "religion": "parent religion or null",
    "alignment": "alignment or null",
    "domains": ["divine domain names"],
    "description": "brief description",
    "page_reference": "page number(s)"
  }],
  "races": [{
    "name": "canonical race name",
    "aliases": ["other names or subtypes"],
    "description": "brief description",
    "typical_classes": ["class names"],
    "native_regions": ["location names"],
    "notable_npcs": ["named individuals"],
    "page_reference": "page number(s)"
  }],
  "classes": [{
    "name": "canonical class name",
    "aliases": ["variants"],
    "description": "brief description",
    "associated_skills": ["skill names"],
    "page_reference": "page number(s)"
  }],
  "skills": [{
    "name": "canonical skill name",
    "aliases": ["other names"],
    "description": "brief description",
    "page_reference": "page number(s)"
  }]
}

Return empty arrays for types not present in the text.

Known entities already in graph (use these exact names when referring to them):
{top_20_relevant_entity_names_from_redis}

USER:
{chunk_text}
```

**Progress update after each chunk:**

```python
r.hset(state_key, {
    "current_chunk": chunk_index + 1,
    "status":        "EXTRACTING_ENTITIES",
    "updated_at":    now_utc(),
})
r.pexpire(lock_key, LOCK_TTL_MS)
```

#### Step 3 — Ontology mapping and deduplication

Extracted JSON is converted to RDF triples deterministically. Entity names
are normalised to URI slugs and deduplicated via a Redis name→URI index.

```python
def uri_slug(name: str) -> str:
    """'Sharn, City of Towers' → 'Sharn_City_of_Towers'"""
    slug = re.sub(r"[^\w\s-]", "", name)
    return re.sub(r"[\s-]+", "_", slug.strip())
```

**Coreference thresholds** are read from `ingestion_config.yaml`:

```python
cfg = load_ingestion_config()  # /config/ingestion_config.yaml
AUTO_MERGE  = cfg["coreference"]["auto_merge_threshold"]   # default 0.92
REVIEW      = cfg["coreference"]["review_threshold"]        # default 0.80
```

Similarity scoring via fastembed (nomic-embed-text-v1.5, in-process):

- ≥ `AUTO_MERGE`: reuse existing URI, store new name as `cs:alias`
- `REVIEW` ≤ sim < `AUTO_MERGE`: flag in Redis for manual review,
  proceed with new URI
- < `REVIEW`: new entity

Thresholds can be tuned per-deployment by updating `ingestion_config.yaml`
without changing code.

#### Step 4 — Triple writing

Batch SPARQL UPDATE INSERT DATA into the document's named graph. One
transaction per document. On any exception: `DROP GRAPH` → set FAILED.

```sparql
INSERT DATA {
  GRAPH <http://campaignsetting.io/doc/eberron_campaign_setting_3e> {
    cs:Dagger_River
        rdf:type        cs:River ;
        rdfs:label      "Dagger River" ;
        cs:mentionedIn  cs:book_eberron_campaign_setting_3e ;
        cs:pageNumber   "208" ;
        cs:description  "The longest river in Khorvaire... [3e, p.208]" .
  }
}
```

---

## 8. MCP Server and Tool Design

### Design principle

**MCP tools are for knowledge graph queries by agents and MCP clients.**
They query Fuseki and return structured lore with provenance.

**Admin HTTP endpoints are for operators and the dashboard.** They handle
ingestion submission, status checking, and pipeline management.

Both are served by the same FastMCP + FastAPI process on port 8000:
- `/mcp` — MCP protocol endpoint (FastMCP), for agents / MCP clients
- `/ingest`, `/status`, `/admin/*`, `/health` — FastAPI routes

The MCP tools never return HTTP 503 for "document not yet ingested" — they
query whatever is currently in the graph. An un-ingested document simply
produces empty results, which is correct behaviour.

### SPARQL injection protection

`search_by_property` accepts a `property_name` parameter that is used to
construct a SPARQL query. This is a potential injection vector: an LLM or
prompt-injected content could pass a crafted string that alters the query
logic.

`property_name` **must** be validated against an explicit whitelist before
use. Any value not in the whitelist returns an error immediately — no SPARQL
is executed:

```python
ALLOWED_PROPERTY_NAMES: frozenset[str] = frozenset({
    "nationality", "alignment", "worships", "memberOf", "leaderOf",
    "locatedIn", "controlledBy", "factionType", "description", "alias",
    "canonicalName", "pageNumber", "edition", "canonType", "publisher",
    "factionLocatedIn", "operatesIn", "dominantReligion", "typicalClass",
    "nativeRegion", "hasRace", "hasClass", "hasSkill",
})

def search_by_property(entity_type, property_name, value, ...):
    if property_name not in ALLOWED_PROPERTY_NAMES:
        raise ValueError(
            f"property_name {property_name!r} is not a known cs: predicate."
        )
    # build and execute SPARQL safely
```

The same principle applies to all parameters that influence SPARQL
construction. String values are passed as SPARQL literals (bound via
parameterised query or escaped), never interpolated raw.

### Context window discipline

Six tools in the manifest. Total manifest target: under 800 tokens.

### Tool manifest

```python
from typing import Literal

EntityType = Literal[
    "NPC", "Faction", "Religion", "Deity", "Race", "CharacterClass", "Skill",
    "Location", "River", "City", "Region", "Nation", "Dungeon",
    "Sea", "Mountain", "Forest", "Ruin", "Plane"
]
CanonFilter   = Literal["canon", "kanon", "community", "any"]
EditionFilter = Literal["3e", "4e", "5e", "any"]
RelType = Literal[
    "allies", "enemies", "members", "operatesIn", "contains",
    "worships", "hasPotentialMotive", "controlledBy", "locatedIn", "nationality"
]


def list_entities(
    entity_type: EntityType,
    edition: EditionFilter = "any",
    canon_type: CanonFilter = "any",
    filters: dict | None = None,
) -> list[dict]:
    """
    List all entities of a given type.
    filters: optional e.g. {"nationality": "Breland"}.
    Returns name, type, page_reference, source_book, edition, canon_type.
    Primary tool for enumeration: 'list rivers', 'list factions'.
    """


def get_entity(
    name: str,
    edition: EditionFilter = "any",
    canon_type: CanonFilter = "any",
    depth: Literal["summary", "full"] = "summary",
) -> dict:
    """
    All known facts about a named entity.
    summary: core properties. full: all relationships.
    Always returns page_reference and source_book.
    """


def get_relationships(
    entity_name: str,
    relationship: RelType,
    edition: EditionFilter = "any",
    canon_type: CanonFilter = "any",
) -> list[dict]:
    """
    Traverse a named relationship from an entity.
    Returns related names, types, page references.
    """


def get_location_hierarchy(
    location: str,
    edition: EditionFilter = "any",
    canon_type: CanonFilter = "any",
) -> dict:
    """
    Full spatial containment chain for a location.
    Ancestors (up) and direct children (down).
    Transitive via SPARQL property path cs:contains+.
    """


def search_by_property(
    entity_type: EntityType,
    property_name: str,       # validated against ALLOWED_PROPERTY_NAMES
    value: str,
    edition: EditionFilter = "any",
    canon_type: CanonFilter = "any",
) -> list[dict]:
    """
    Find entities matching a property value.
    e.g. ("NPC", "nationality", "Breland")
    Always returns page_reference and source_book.
    """


def get_ingestion_status(
    document_id: str | None = None,
) -> dict:
    """
    Pipeline status for one or all documents.
    States: PENDING | CONVERTING_PDF | MARKDOWN_READY |
            CLASSIFYING_SECTIONS | EXTRACTING_ENTITIES |
            MAPPING_TO_ONTOLOGY | LOADING_GRAPH | COMPLETED | FAILED
    Includes current_page/total_pages and current_chunk/total_chunks
    for in-progress documents.
    COMPLETED includes entity_count, triple_count.
    FAILED includes error, last_successful_stage.
    """
```

### Response shape

```json
{
  "results": [
    {
      "name": "Dagger River",
      "type": "River",
      "edition": "3e",
      "canon_type": "canon",
      "source_book": "Eberron Campaign Setting (3.5e)",
      "page_reference": "208",
      "description": "The longest river in Khorvaire... [3e, p.208]"
    }
  ],
  "count": 1,
  "applied_filters": { "edition": "3e", "canon_type": "canon" }
}
```

### Admin endpoint summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/status` | All documents, paginated (`?page=1`) |
| `GET` | `/status/{document_id}` | Single document state + progress |
| `POST` | `/ingest` | Submit PDF + metadata |
| `POST` | `/admin/requeue/{document_id}` | Reset FAILED to PENDING |
| `GET` | `/health` | Liveness probe |

---

## 9. Evaluation Harness

### Project structure and template alignment

This repository is built on the MCP server template. The template's
`server/` directory contains the mcp-server (FastMCP) code. The template
provides one CI/CD pipeline, one `tests/` folder, and one set of
lint/reformat/validate-docs containers — all of which apply to the
mcp-server service.

The additional microservices (pdf-worker, graph-worker, dashboard) require
their own Dockerfiles (`Dockerfile.pdf`, `Dockerfile.graph`,
`Dockerfile.dashboard`) and their own test environments. The CI pipeline
must be extended to build and test all services.

Each service is tested as a black box. Tests make HTTP, MinIO, Redis, or
SPARQL calls against running containers. They never import application code.

### Test folder structure

```
tests/
├── conftest.py               # shared fixtures: MinIO, Redis, HTTP clients,
│                             # fixture PDF paths
├── fixtures/
│   ├── sample_eberron.pdf    # small hand-crafted PDF, known ground truth
│   ├── sample_eberron.yaml   # matching metadata sidecar
│   └── expected/
│       ├── rivers_3e.json    # ground truth for eval queries
│       └── factions_5e.json
│
├── pdf_worker/
│   ├── docker-compose.yml    # redis + minio + pdf-worker + runner
│   └── test_pdf_worker.py
│
├── graph_worker/
│   ├── docker-compose.yml    # fuseki + redis + minio + graph-worker + runner
│   └── test_graph_worker.py  # seeds MinIO /markdown/ directly
│
├── mcp_server/               # replaces the template's tests/
│   ├── docker-compose.yml    # fuseki + redis + minio + mcp-server + runner
│   └── test_mcp_server.py    # seeds Fuseki directly for query tests
│
└── integration/
    ├── docker-compose.yml    # full stack
    └── test_integration.py   # end-to-end: PDF in → query out
```

The template's existing `tests/docker-compose.yaml` and `tests/test_unit.py`
are migrated into `tests/mcp_server/`.

### pdf-worker tests

```python
def test_pdf_produces_markdown_in_minio():
    # Upload fixture PDF + metadata to /raw-pdfs/
    # Poll Redis until MARKDOWN_READY
    # Assert /markdown/{document_id}.md exists in MinIO

def test_markdown_contains_page_markers():
    # Assert <!-- page: N --> comments present in output

def test_page_numbers_are_accurate():
    # Assert known content appears after correct page marker

def test_progress_updates_during_conversion():
    # Poll /status/{document_id} during conversion
    # Assert current_page increases monotonically

def test_invalid_metadata_sets_failed_status():
    # Upload PDF with missing edition field
    # Assert Redis status → FAILED immediately

def test_duplicate_document_id_rejected():
    # Upload same document_id twice
    # Assert second upload → FAILED with clear error

def test_failed_document_not_auto_retried():
    # Assert FAILED document stays FAILED without explicit requeue
```

### graph-worker tests

```python
def test_markdown_produces_triples_in_fuseki():
    # Write fixture .md to /markdown/, set Redis MARKDOWN_READY
    # Poll until COMPLETED
    # SPARQL query → assert expected triples present

def test_named_graph_created_per_document():
    # Assert named graph URI exists after ingestion

def test_multiple_entities_extracted_from_one_chunk():
    # Fixture chunk contains 3 NPCs and 2 locations
    # Assert all 5 entities appear in Fuseki

def test_failed_extraction_drops_named_graph():
    # Inject malformed Markdown → extraction failure
    # Assert named graph removed, status → FAILED

def test_retry_resumes_from_markdown_ready():
    # Force failure at EXTRACTING_ENTITIES
    # POST /admin/requeue → assert status → PENDING
    # Assert completion without re-converting PDF

def test_page_numbers_present_on_triples():
    # Assert all entities have non-null cs:pageNumber

def test_coreference_deduplication():
    # Two chunks referring to same entity by different names
    # Assert single URI, both names as cs:alias
```

### mcp-server tests

```python
def test_health_endpoint_returns_200():

def test_empty_graph_returns_empty_list():
    r = client.get("/tools/list_entities?entity_type=River")
    assert r.status_code == 200
    assert r.json()["results"] == []

def test_list_entities_returns_page_references():

def test_edition_filter_applied_correctly():

def test_canon_type_filter_applied_correctly():

def test_ingest_endpoint_writes_to_minio():
    # POST /ingest → assert MinIO /raw-pdfs/ and Redis PENDING

def test_requeue_endpoint_resets_failed_to_pending():
    # Set Redis status FAILED
    # POST /admin/requeue → assert PENDING

def test_search_by_property_rejects_unknown_property():
    # Pass property_name not in whitelist → assert 400/422

def test_status_pagination():
    # Insert 25 documents in Redis
    # GET /status?page=1 → assert 10 results
    # GET /status?page=3 → assert 5 results
```

### Integration tests

```python
def test_full_pipeline_pdf_to_queryable_graph():
    # POST fixture PDF → poll until COMPLETED
    # list_entities(entity_type="River") → assert known rivers present

def test_rivers_precision_and_recall():
    names = [r["name"] for r in results]
    s = score(names, KNOWN_FIXTURE_RIVERS)
    assert s["recall"]    >= 0.80
    assert s["precision"] >= 0.90

def test_all_results_have_page_references():
    assert all(r.get("page_reference") for r in results)

def test_shelf_filter_works_end_to_end():

def test_requeue_after_failure_completes_successfully():
```

### Scoring function

```python
def score(returned: list[str], expected: list[str]) -> dict:
    r = {x.lower().strip() for x in returned}
    e = {x.lower().strip() for x in expected}
    hits         = r & e
    hallucinated = r - e
    missed       = e - r
    precision = len(hits) / len(r) if r else 0.0
    recall    = len(hits) / len(e) if e else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)
    return {
        "precision":    round(precision, 4),
        "recall":       round(recall, 4),
        "f1":           round(f1, 4),
        "hits":         sorted(hits),
        "hallucinated": sorted(hallucinated),
        "missed":       sorted(missed),
    }
```

---

## 10. Streamlit Dashboard

The dashboard is a lightweight status monitor and submission form that
communicates exclusively with the mcp-server HTTP API. It never touches
Redis, MinIO, or Fuseki directly. This is a hard architectural constraint.

### What the dashboard does

**Status view** — paginated table of all documents with pipeline state,
timestamps, elapsed time, progress (current_page/total_pages or
current_chunk/total_chunks for in-progress documents), entity count,
triple count, and error message if failed. Refreshes on a configurable
interval (default 10 seconds).

**Submission form** — all required and optional metadata fields plus a
PDF uploader. On submit, POSTs to `/ingest`.

**Retry action** — for FAILED documents, a "Retry" button calls
`POST /admin/requeue/{document_id}`. The document moves back to PENDING
and is re-processed on the next worker poll cycle.

### mcp-server endpoints used by the dashboard

```
GET  /status                          → paginated list of all documents
GET  /status/{document_id}            → single document state + progress
POST /ingest                          → submit PDF + metadata
POST /admin/requeue/{document_id}     → reset FAILED to PENDING
GET  /health                          → connection indicator
```

### Polling design and pagination

```python
import streamlit as st
import httpx
import time
from datetime import datetime, timezone

MCP_URL      = os.environ["MCP_SERVER_URL"]
POLL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 10))
PAGE_SIZE    = 10   # hardcoded; change here to adjust all paginated views

STALE_THRESHOLD_SECONDS = 600


def status_page():
    st.title("Ingestion Status")

    page = st.session_state.get("status_page", 1)
    col_prev, col_info, col_next = st.columns([1, 2, 1])
    if col_prev.button("← Prev") and page > 1:
        st.session_state["status_page"] = page - 1
        st.rerun()

    try:
        resp = httpx.get(
            f"{MCP_URL}/status",
            params={"page": page, "page_size": PAGE_SIZE},
            timeout=5,
        )
        data = resp.json()
        docs  = data.get("documents", [])
        total = data.get("total", 0)
        pages = max(1, -(-total // PAGE_SIZE))  # ceiling division
    except Exception as e:
        st.error(f"Cannot reach mcp-server: {e}")
        docs, total, pages = [], 0, 1

    col_info.write(f"Page {page} of {pages}  ({total} documents)")
    if col_next.button("Next →") and page < pages:
        st.session_state["status_page"] = page + 1
        st.rerun()

    for doc in docs:
        col1, col2, col3, col4, col5 = st.columns([3, 2, 2, 2, 1])
        col1.write(doc["title"])
        col2.write(doc["status"])
        col3.write(_progress_str(doc))
        col4.write(doc.get("updated_at", "—"))
        if doc["status"] == "FAILED" or _is_stale(doc):
            if col5.button("Retry", key=doc["document_id"]):
                httpx.post(f"{MCP_URL}/admin/requeue/{doc['document_id']}")
                st.rerun()

    time.sleep(POLL_SECONDS)
    st.rerun()


def _progress_str(doc: dict) -> str:
    status = doc.get("status", "")
    if status == "CONVERTING_PDF":
        curr = doc.get("current_page", "?")
        total = doc.get("total_pages", "?")
        return f"Page {curr}/{total}"
    if status in ("CLASSIFYING_SECTIONS", "EXTRACTING_ENTITIES",
                  "MAPPING_TO_ONTOLOGY", "LOADING_GRAPH"):
        curr = doc.get("current_chunk", "?")
        total = doc.get("total_chunks", "?")
        return f"Chunk {curr}/{total}"
    if status == "COMPLETED":
        return f"{doc.get('entity_count', '?')} entities"
    return "—"


def _is_stale(doc: dict) -> bool:
    if doc["status"] not in (
        "CONVERTING_PDF", "CLASSIFYING_SECTIONS", "EXTRACTING_ENTITIES",
        "MAPPING_TO_ONTOLOGY", "LOADING_GRAPH",
    ):
        return False
    try:
        updated = datetime.fromisoformat(doc["updated_at"])
        # .total_seconds() — NOT .seconds (which is only the seconds component)
        return (datetime.now(timezone.utc) - updated).total_seconds() \
               > STALE_THRESHOLD_SECONDS
    except (KeyError, ValueError):
        return False
```

### Submission form

```python
def submission_form():
    st.title("Ingest a New Book")

    with st.form("ingest_form"):
        title    = st.text_input("Title *")
        edition  = st.selectbox("Edition *", ["3e", "4e", "5e", "any"])
        canon    = st.selectbox("Canon type *", ["canon", "kanon", "community"])
        publisher = st.text_input("Publisher")
        year     = st.text_input("Publication year")
        authors  = st.text_input("Authors (comma-separated)")
        pdf_file = st.file_uploader("PDF file *", type=["pdf"])
        submitted = st.form_submit_button("Submit")

    if submitted:
        if not title or not pdf_file:
            st.error("Title and PDF are required.")
            return

        metadata = {
            "document_id":      slugify(title),
            "title":            title,
            "edition":          edition,
            "canon_type":       canon,
            "publisher":        publisher,
            "publication_year": year,
            "authors":          [a.strip() for a in authors.split(",") if a.strip()],
        }

        resp = httpx.post(
            f"{MCP_URL}/ingest",
            files={"pdf": (pdf_file.name, pdf_file.getvalue(), "application/pdf")},
            data={"metadata": json.dumps(metadata)},
            timeout=30,
        )

        if resp.status_code == 202:
            st.success(f"Submitted: {metadata['document_id']}")
            st.session_state["view"] = "status"
            st.rerun()
        else:
            st.error(f"Submission failed: {resp.status_code} — {resp.text}")
```

---

## 11. Hardware and Testbed Strategy

### Local development machine

The LLM (gemma4:e2b via llama.cpp) runs on a dedicated GPU machine external
to the cluster:

```
http://sinan.msi-nvidia-server.ts.ozel.network:8080/v1
```

The rest of the stack (Fuseki, mcp-server, ingestion workers, Redis, MinIO)
runs in Docker or Kubernetes on CPU. The GPU is dedicated entirely to the LLM.

**Constraints:**
- gemma4:e2b at Q4 fits comfortably in 6GB VRAM
- 8k context: adequate for chunk extraction (chunk in, ~400 tokens of JSON out)
- Ingestion is LLM-bound. Expect slow throughput on large corpora.
  For development, test with 2–3 short books to verify the pipeline works
  end-to-end. Once the stack is confirmed correct, ingest the full library
  and wait — the database persists, so the investment accumulates.
- LLM parallelism is an infrastructure concern: deploy multiple llama.cpp
  instances behind a load balancer, run multiple graph-worker replicas.
  The pipeline code makes standard OpenAI-compatible API calls and is
  unaware of the deployment topology.

### Ephemeral cloud testbed (evaluation and CI)

Full-corpus evaluation via IaC-managed ephemeral GPU instances:

| Cloud | Instance | GPU | VRAM |
|---|---|---|---|
| AWS | g5.xlarge | A10G | 24GB |
| GCP | g2-standard-4 | L4 | 24GB |

Workflow: provision → ingest full corpus → run test suite →
export JSON report → destroy. Estimated cost: $2–4 per run.

---

## 12. Helm Chart

### Chart structure

```
chart/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── _helpers.tpl
    ├── namespace.yaml
    ├── configmap-ontology.yaml       # ontology_schema.yaml + ingestion_config.yaml
    ├── configmap-fuseki.yaml         # fuseki-config.ttl (Assembler config)
    ├── secret-fuseki.yaml
    ├── secret-llm.yaml
    ├── statefulset-fuseki.yaml
    ├── service-fuseki.yaml
    ├── statefulset-redis.yaml        # persistent, AOF enabled
    ├── service-redis.yaml
    ├── statefulset-minio.yaml
    ├── service-minio.yaml
    ├── deployment-mcp-server.yaml
    ├── service-mcp-server.yaml
    ├── deployment-pdf-worker.yaml
    ├── deployment-graph-worker.yaml
    ├── deployment-dashboard.yaml
    ├── service-dashboard.yaml
    └── ingress.yaml
```

### Chart.yaml

```yaml
apiVersion: v2
name: campaign-query
description: >
  Knowledge graph–backed query engine for tabletop RPG campaign settings.
  Exposes an MCP server for precision lore retrieval.
type: application
version: 0.1.0
appVersion: "0.1.0"
```

### values.yaml

```yaml
namespace: campaign-query

fuseki:
  image: stain/jena-fuseki
  tag: "5.1.0"    # pin explicitly — never use latest
  dataset: campaign
  storage: 10Gi
  jvmArgs: "-Xmx2g"
  adminPassword: ""          # set via --set or secret

llm:
  host: ""                   # e.g. http://your-gpu-host:8080/v1
  model: openai/gemma4:e2b

mcpServer:
  image: your-registry/campaign-mcp-server:latest
  replicas: 1
  port: 8000

pdfWorker:
  image: your-registry/campaign-pdf-worker:latest
  replicas: 1
  lockTtlSeconds: 120
  pollIntervalSeconds: 5
  ocrThreshold: 50

graphWorker:
  image: your-registry/campaign-graph-worker:latest
  replicas: 1
  lockTtlSeconds: 300
  pollIntervalSeconds: 10

dashboard:
  image: your-registry/campaign-dashboard:latest
  replicas: 1
  port: 8501
  pollIntervalSeconds: 10

minio:
  storage: 50Gi
  rootUser: minioadmin
  rootPassword: ""
  buckets:
    - raw-pdfs
    - markdown
    - processing
    - completed
    - failed

redis:
  image: redis:7-alpine
  storage: 1Gi              # persistent, AOF mode

ingress:
  enabled: false
  host: ""
  tls: false
```

### Installation

```bash
helm install eberron-query ./chart \
  --namespace campaign-query \
  --create-namespace \
  --set fuseki.adminPassword=changeme \
  --set llm.host=http://your-gpu-host:8080/v1 \
  --set minio.rootPassword=changeme
```

### Custom ontology schema

```bash
helm install my-query ./chart \
  --set-file ontologySchema=./my_ontology_schema.yaml \
  --set-file ingestionConfigFile=./my_ingestion_config.yaml \
  --set llm.host=http://your-gpu-host:8080/v1 \
  --set fuseki.adminPassword=changeme \
  --set minio.rootPassword=changeme
```

---

## 13. Known Risks and Mitigations

### Coreference resolution

**Problem**: "the Silver Flame" in chapter 3 and "Church of the Silver Flame"
in a Keith Baker post may or may not refer to the same entity. Without
resolution the pipeline creates separate URI nodes and queries return
partial results.

**Mitigation**: Two-layer approach.

Layer 1 — known-entity hint in every extraction prompt. The LLM is shown
the 20 most relevant entity names and instructed to use these exact names.
Resolves most coreferences before they reach the mapper.

Layer 2 — fastembed cosine similarity (nomic-embed-text-v1.5, in-process).
Thresholds are **configurable** in `ingestion_config.yaml` and can be tuned
per-deployment without code changes. The defaults (0.92 / 0.80) are
reasonable starting points; calibrate against a labeled sample of Eberron
entity variant names if precision matters.

### SPARQL injection

**Problem**: `property_name` in `search_by_property` is used to construct
SPARQL queries. An LLM or prompt injection from sourcebook content could
pass a crafted value to alter query logic, exfiltrate data, or execute
arbitrary SPARQL updates.

**Mitigation**: Validate `property_name` against `ALLOWED_PROPERTY_NAMES`
(a frozenset of known `cs:` predicates) before any SPARQL is constructed.
String values used as literals are parameterised or escaped. Fuseki's
SPARQL Update endpoint is not reachable from the mcp-server's tool code —
only SELECT queries are issued by tools.

### Cross-edition entity descriptions

**Problem**: Jaela Daran is described differently in 3e and 5e.

**Solution**: Store edition-tagged description literals inline:

```turtle
cs:Jaela_Daran
    cs:description "Young Keeper of the Flame [3e, p.42]" ;
    cs:description "Keeper of the Silver Flame [5e, p.18]" .
```

The entity node is shared. Edition metadata lives on the SourceBook.

### SPARQL property path transitivity — verification

`cs:contains+` traversal requires no reasoner. Run the smoke test from
section 4.1 after every Fuseki pod restart to verify the TDB2 store and
dataset configuration loaded correctly.

### Write concurrency

Fuseki's TDB2 store serialises concurrent writes internally. Multiple
graph-worker replicas writing simultaneously is safe — Fuseki queues
transactions. No application-level write locking is needed beyond the
per-document Redis lock (which prevents two workers from writing to the
same named graph simultaneously).

### Ingestion throughput

Ingestion is LLM-bound. A 350-page sourcebook may take hours on a single
2B-parameter model. This is acceptable for development: test with 2–3 short
books first, confirm correctness, then let the full corpus run. The database
persists — the investment accumulates, and you do not need to re-ingest.

For faster ingestion: deploy multiple llama.cpp instances behind a load
balancer and run multiple graph-worker replicas. Each replica processes a
different document in parallel. The pipeline code requires no changes.
