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
should be swappable by replacing a config file, so DMs running homebrew
campaigns or organisations with internal knowledge bases can adapt the
same stack without touching pipeline code.

**Non-goals**: Real-time ingestion, multi-user auth, fine-tuned models,
multiple campaign settings.

---

## 2. Infrastructure

### Component stack

| Component | Choice | Rationale |
|---|---|---|
| Graph database | Apache Jena Fuseki (containerised) | SPARQL 1.1, OWL inference, transitive property paths native |
| Vector store | LanceDB or Qdrant | Existing pipeline — unchanged |
| Object storage | MinIO (S3-compatible) | PDF staging, pipeline state, portable across clouds |
| MCP server | FastAPI (Python) | Thin stateless layer between LLM tools and SPARQL |
| LLM (inference) | gemma4:e2b via llama.cpp server | External, on dedicated GPU machine — not in cluster |
| Embeddings | nomic-ai/nomic-embed-text-v1.5 via fastembed | 8192-token context, Matryoshka dims, baked into container |
| Orchestration | Kubernetes + Helm | StatefulSet for Fuseki, Deployment for MCP server |
| Status store | Redis | Pipeline state per document, entity name→URI index |

### Why Jena Fuseki over Oxigraph

The previous design used Oxigraph and noted that `owl:TransitiveProperty`
on `cs:contains` was a declaration of intent only — Oxigraph does not
reason. Every transitive SPARQL query needed manual `cs:contains+` property
paths, which is a discipline requirement that silently produces wrong
results when forgotten.

Jena Fuseki with OWL inference enabled handles transitivity automatically.
`cs:contains` declared `owl:TransitiveProperty` in the ontology means a
SPARQL query for direct children automatically returns all descendants.
The MCP server query code is simpler and less error-prone. Fuseki runs
in a container, is Kubernetes-native, and is SPARQL 1.1 compliant — the
same queries work against any other compliant store if Fuseki is ever
replaced.

### Embedding model: nomic-embed-text-v1.5

`all-MiniLM-L6-v2` (as you specified) is used via fastembed and baked
into the container image. However, note that as of 2025–2026 the model
is considered legacy: it has a 256-token context window, 2022-era
architecture, and around 5–8% lower retrieval accuracy than current
alternatives on MTEB benchmarks.

**Recommendation**: use `nomic-ai/nomic-embed-text-v1.5` instead.
It has an 8192-token context window (matching your LLM), supports
Matryoshka variable-dimension embeddings (trade size for speed at
inference time), outperforms MiniLM-L6-v2 on retrieval benchmarks,
and is natively supported by fastembed. The bake-in pattern is identical.

```dockerfile
# Bake nomic-embed-text-v1.5 into the container image.
# Containers without internet access can start immediately.
RUN python -c "
from fastembed import TextEmbedding
TextEmbedding('nomic-ai/nomic-embed-text-v1.5')
"
```

If `all-MiniLM-L6-v2` is required for compatibility with the existing
vector pipeline, use it for that pipeline and nomic for the graph
coreference linking step. The two tasks use separate model instances.

### Ontology hackability

The ontology lives as `ontology.ttl` in the repository, loaded into
Kubernetes as a ConfigMap and mounted read-only into the Fuseki pod.

**Why a mounted volume and not an inline ConfigMap value?**
ConfigMaps have a 1MB size limit and handle multiline strings poorly.
A ConfigMap-backed volume stores the file cleanly in etcd and mounts it
as a proper file on disk. Fuseki loads it via `--file` on startup.

To update the ontology:
1. Edit `ontology.ttl` in the repository
2. IaC (Terraform / Pulumi / Flux) applies the updated ConfigMap
3. Fuseki pod restarts and reloads the schema
4. Ingestion workers re-run affected documents if schema changed materially

The ontology is a **first-class versioned artifact**: full Git history,
PR review, rollback via GitOps. The pipeline code never changes when the
ontology changes.

---

## 3. Deployment Architecture

### Principle: separate pods for separate concerns

There are four microservices and two stateful backing stores. Each has
a distinct operational profile and deploys independently.

| | Fuseki | mcp-server | pdf-worker | graph-worker | dashboard |
|---|---|---|---|---|---|
| Kind | StatefulSet | Deployment | Deployment | Deployment | Deployment |
| Replicas | 1 | 1–N | 0–N (KEDA) | 0–N (KEDA) | 1 |
| State | PVC (TDB2) | None | None | None | None |
| Scales on | Never | Traffic | PDF queue depth | Markdown queue depth | Never |
| LLM needed | No | No | No | Yes | No |
| Dockerfile | (upstream image) | `Dockerfile.mcp` | `Dockerfile.pdf` | `Dockerfile.graph` | `Dockerfile.dashboard` |

**pdf-worker** — converts PDFs to Markdown. No LLM. No ML dependencies.
Pure document processing (pdfplumber / marker-pdf). Reads from MinIO
`/raw-pdfs/`, writes to MinIO `/markdown/`. Horizontally scalable —
multiple replicas can process different books simultaneously. Redis
locking with timestamps prevents two pods from claiming the same document.

**graph-worker** — chunks Markdown, classifies sections, extracts entities
via LLM, maps to ontology, writes triples to Fuseki and vectors to the
vector store. LLM-heavy. Needs fastembed baked in. Reads from MinIO
`/markdown/`, writes to Fuseki and the vector store.

**mcp-server** — stateless HTTP server. Handles MCP tool calls (SPARQL →
Fuseki), the `/ingest` intake endpoint (drops PDF+metadata into MinIO),
`/status/{document_id}` for pipeline state, and `/health`. No LLM at
query time unless the optional query-intent classifier is enabled.

**dashboard** — Streamlit app. Status monitor for ingested documents.
Submission form for new PDFs with metadata. Communicates exclusively
through the mcp-server HTTP API — it never touches Redis, MinIO, or
Fuseki directly. Polls `/status` endpoints on a configurable interval.

### LLM is outside the cluster

The LLM (gemma4:e2b via llama.cpp) runs on a dedicated GPU machine and
is accessed over the network. It is not part of the Kubernetes cluster
and is not in the docker-compose network.

```dotenv
LLAMA_CPP_HOST=http://sinan.msi-nvidia-server.ts.ozel.network:8080/v1
```

```yaml
# litellm / openai-compatible model string
model: openai/gemma4:e2b
```

All pods that need LLM access (ingestion workers, MCP server for
query-intent classification) receive `LLAMA_CPP_HOST` as an environment
variable. The endpoint is treated as an OpenAI-compatible API — standard
`/v1/chat/completions` interface.

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
  ├── FastAPI, stateless, port 8000
  ├── No PVC, no LLM dependency
  ├── Env: FUSEKI_ENDPOINT, VECTOR_STORE_ENDPOINT,
  │        REDIS_URL, MINIO_ENDPOINT
  └── Redeploys freely on code change
        │
        │ HTTP  (SPARQL SELECT via /query)
        ▼
  fuseki-svc (ClusterIP Service → StatefulSet)
  ├── Apache Jena Fuseki, port 3030
  ├── Dataset: /campaign
  ├── OWL inference enabled
  ├── PVC: fuseki-data (ReadWriteOnce, 10Gi)  [TDB2 store]
  └── Volume: ontology-config → /ontology/ontology.ttl (read-only)

  pdf-worker (Deployment, replicas: 0–N via KEDA)
  ├── PDF → Markdown conversion only. No LLM.
  ├── Horizontally scalable — Redis locking prevents double-processing
  ├── Reads from MinIO /raw-pdfs
  ├── Writes to MinIO /markdown
  ├── Updates Redis pipeline state with timestamps
  └── Env: REDIS_URL, MINIO_ENDPOINT, LOCK_TIMEOUT_SECONDS

  graph-worker (Deployment, replicas: 0–N via KEDA)
  ├── Markdown → triples + vectors. LLM-heavy.
  ├── fastembed model baked into image
  ├── Reads from MinIO /markdown
  ├── Writes triples to Fuseki via SPARQL UPDATE
  ├── Writes vectors to vector store
  ├── Updates Redis pipeline state with timestamps
  └── Env: FUSEKI_ENDPOINT, REDIS_URL, MINIO_ENDPOINT,
           LLAMA_CPP_HOST, VECTOR_STORE_ENDPOINT, LOCK_TIMEOUT_SECONDS

  vector-store (StatefulSet)
  └── LanceDB or Qdrant, PVC: vector-data

  minio (StatefulSet)
  └── Buckets: /raw-pdfs  /markdown  /processing  /completed  /failed

  redis (Deployment)
  ├── Distributed lock per document_id (string with TTL)
  └── Entity name→URI deduplication index during ingestion

  ontology-config (ConfigMap)
  └── ontology.ttl → mounted into fuseki pod

  ──────────────────────────────────────────── (cluster boundary)

  [EXTERNAL] llama.cpp server
  └── GPU machine: sinan.msi-nvidia-server.ts.ozel.network:8080
      gemma4:e2b, OpenAI-compatible /v1 API
      NOT in cluster. NOT in docker-compose network.
      Accessed only by graph-worker via LLAMA_CPP_HOST env var.
```

### Fuseki StatefulSet manifest

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
      initContainers:
      - name: load-ontology
        image: stain/jena-fuseki:latest
        command:
          - /jena-fuseki/tdbloader
          - --loc=/fuseki-base/databases/campaign
          - /ontology/ontology.ttl
        volumeMounts:
        - name: fuseki-data
          mountPath: /fuseki-base
        - name: ontology
          mountPath: /ontology
      containers:
      - name: fuseki
        image: stain/jena-fuseki:latest
        env:
        - name: ADMIN_PASSWORD
          valueFrom:
            secretKeyRef:
              name: fuseki-secret
              key: admin-password
        - name: JVM_ARGS
          value: "-Xmx2g"
        - name: FUSEKI_DATASET_1
          value: "/campaign"
        ports:
        - containerPort: 3030
        volumeMounts:
        - name: fuseki-data
          mountPath: /fuseki-base
        - name: ontology
          mountPath: /ontology
          readOnly: true
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "3Gi"
            cpu: "2000m"
      volumes:
      - name: ontology
        configMap:
          name: ontology-config
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
        - name: VECTOR_STORE_ENDPOINT
          value: "http://vector-store-svc:6333"
        - name: REDIS_URL
          value: "redis://redis-svc:6379"
        - name: LLAMA_CPP_HOST
          valueFrom:
            secretKeyRef:
              name: llm-endpoint
              key: host
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
graph store. To replace Fuseki with another SPARQL 1.1 endpoint (Oxigraph,
Amazon Neptune, Stardog), update one env var. No code changes.

### Write concurrency

Fuseki's TDB2 store supports concurrent reads but serialises writes via
an internal write lock. Multiple ingestion worker pods writing
simultaneously is safe — Fuseki queues the transactions. This is an
improvement over the Oxigraph embedded store which required `replicas: 1`
on workers. Ingestion worker replicas can scale freely.

---

## 4. Ontology Design

Namespace: `http://campaignsetting.io/ontology#`
Prefix: `cs:`

### 4.1 Full ontology (`ontology.ttl`)

```turtle
@prefix cs:   <http://campaignsetting.io/ontology#> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

# ── Classes ────────────────────────────────────────────────────────────────

cs:Entity         rdf:type owl:Class ;
    rdfs:comment  "Root class for all named things in the campaign setting." .

cs:NPC            rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "A named non-player character." .

cs:Location       rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "Any named place: city, river, dungeon, plane, etc." .

# Location subclasses — enables "list all rivers" without string matching
cs:City           rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:River          rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Region         rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Nation         rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Dungeon        rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Sea            rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Mountain       rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Forest         rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Ruin           rdf:type owl:Class ; rdfs:subClassOf cs:Location .
cs:Plane          rdf:type owl:Class ; rdfs:subClassOf cs:Location .

cs:Faction        rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "An organisation, guild, army, cult, or political body." .

cs:Religion       rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "A faith, pantheon, or religious tradition." .

cs:Deity          rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "A god, divine being, or object of worship." .

cs:Race           rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "A playable or non-playable species or ancestry." .

cs:CharacterClass rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "An RPG character class: Wizard, Fighter, Artificer, etc." .

cs:Skill          rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "A named skill, proficiency, or ability." .

cs:PotentialMotive rdf:type owl:Class ;
    rdfs:subClassOf cs:Entity ;
    rdfs:comment  "A plausible goal or drive attributed to an NPC or Faction.
                   Multiple instances per entity are expected and normal.
                   Never asserted as ground truth — always sourced." .

cs:SourceBook     rdf:type owl:Class ;
    rdfs:comment  "A published sourcebook, module, or document." .

# ── Datatype Properties ────────────────────────────────────────────────────

cs:canonicalName  rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:Entity ;
    rdfs:range    xsd:string .

cs:alias          rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:Entity ;
    rdfs:range    xsd:string ;
    rdfs:comment  "Multiple values allowed." .

cs:description    rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:Entity ;
    rdfs:range    xsd:string ;
    rdfs:comment  "When an entity appears in multiple editions with different
                   descriptions, store each with inline provenance:
                   'Young Keeper of the Flame [3e, p.42]'
                   Both are stored as separate cs:description triples on the
                   same entity node. Querying by edition uses cs:edition on
                   the associated SourceBook, not on the description itself." .

cs:alignment      rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:NPC ;
    rdfs:range    xsd:string .

cs:factionType    rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:Faction ;
    rdfs:range    xsd:string ;
    rdfs:comment  "Military, Criminal, Religious, Arcane, Mercantile, etc." .

cs:motiveSummary  rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:PotentialMotive ;
    rdfs:range    xsd:string .

cs:motiveSource   rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:PotentialMotive ;
    rdfs:range    xsd:string ;
    rdfs:comment  "Verbatim or near-verbatim quote supporting this reading." .

# ── Provenance ─────────────────────────────────────────────────────────────

cs:sourceText     rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:Entity ;
    rdfs:range    xsd:string ;
    rdfs:comment  "Verbatim or near-verbatim passage from source." .

cs:pageNumber     rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:Entity ;
    rdfs:range    xsd:string ;
    rdfs:comment  "Page number(s). String allows ranges: '42', '42-43'." .

# ── SourceBook metadata (supplied via PDF metadata interface) ──────────────

cs:edition        rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:SourceBook ;
    rdfs:range    xsd:string ;
    rdfs:comment  "Edition string: '3e', '4e', '5e', 'any'." .

cs:canonType      rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:SourceBook ;
    rdfs:range    xsd:string ;
    rdfs:comment  "One of: canon, kanon, community." .

cs:publicationYear rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:SourceBook ;
    rdfs:range    xsd:string .

cs:publisher      rdf:type owl:DatatypeProperty ;
    rdfs:domain   cs:SourceBook ;
    rdfs:range    xsd:string .

# ── Object Properties ──────────────────────────────────────────────────────

cs:mentionedIn    rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Entity ;
    rdfs:range    cs:SourceBook .

# cs:contains is TransitiveProperty.
# With Fuseki OWL inference enabled, this is automatic —
# no need for cs:contains+ property paths in queries.
cs:contains       rdf:type owl:ObjectProperty, owl:TransitiveProperty ;
    rdfs:domain   cs:Location ;
    rdfs:range    cs:Location ;
    rdfs:comment  "Spatial containment. continent → nation → city → district.
                   Transitive inference handled by Jena OWL reasoner." .

cs:borderedBy     rdf:type owl:ObjectProperty, owl:SymmetricProperty ;
    rdfs:domain   cs:Location ;
    rdfs:range    cs:Location .

cs:controlledBy   rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Location ;
    rdfs:range    cs:Faction .

cs:locatedIn      rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Entity ;
    rdfs:range    cs:Location .

cs:hasRace        rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:Race .

cs:hasClass       rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:CharacterClass .

cs:hasSkill       rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:Skill .

cs:hasPotentialMotive rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:PotentialMotive .

cs:nationality    rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:Nation .

cs:memberOf       rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:Faction .

cs:leaderOf       rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:Faction .

cs:worships       rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:Deity .

cs:alliedWith     rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:NPC .

cs:enemyOf        rdf:type owl:ObjectProperty, owl:SymmetricProperty ;
    rdfs:domain   cs:NPC ; rdfs:range cs:NPC .

cs:factionLocatedIn rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Faction ; rdfs:range cs:Location .

cs:operatesIn     rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Faction ; rdfs:range cs:Location .

cs:factionAlly    rdf:type owl:ObjectProperty, owl:SymmetricProperty ;
    rdfs:domain   cs:Faction ; rdfs:range cs:Faction .

cs:factionEnemy   rdf:type owl:ObjectProperty, owl:SymmetricProperty ;
    rdfs:domain   cs:Faction ; rdfs:range cs:Faction .

cs:factionPotentialMotive rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Faction ; rdfs:range cs:PotentialMotive .

cs:deityOf        rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Deity ; rdfs:range cs:Religion .

cs:primaryDeity   rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Religion ; rdfs:range cs:Deity .

cs:worshippedBy   rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Deity ; rdfs:range cs:Faction .

cs:dominantReligion rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Nation ; rdfs:range cs:Religion .

cs:typicalClass   rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Race ; rdfs:range cs:CharacterClass .

cs:nativeRegion   rdf:type owl:ObjectProperty ;
    rdfs:domain   cs:Race ; rdfs:range cs:Location .
```

---

## 5. Editions and Canonicity

Edition and canonicity are **metadata on the SourceBook node**, not a
separate `cs:Shelf` class. Every entity carries `cs:mentionedIn` pointing
to a `cs:SourceBook`, and every `cs:SourceBook` carries `cs:edition` and
`cs:canonType` literals. Filtering by edition or canonicity is done by
joining through the SourceBook in SPARQL.

### Canon types

| Value | Meaning |
|---|---|
| `canon` | Official Hasbro / Wizards of the Coast publications |
| `kanon` | Publications by Keith Baker, original Eberron designer |
| `community` | Fan-made, third-party, or unofficial content |

### How filtering works in SPARQL

```sparql
# "List rivers — 5e canon only"
PREFIX cs: <http://campaignsetting.io/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

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

An entity published across editions has multiple `cs:mentionedIn` triples
pointing to different SourceBook nodes. The entity node is not duplicated.
The edition metadata lives on the SourceBook, supplied at ingestion time
via the PDF metadata interface (see section 6).

### SourceBook bootstrap data (loaded at startup, separate from ontology.ttl)

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
metadata. This is how the pipeline knows which SourceBook to associate
extracted entities with, what edition they belong to, and whether they
are canon or kanon.

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
```

`source_book_uri` is optional. If omitted, the ingestion worker creates
a new SourceBook URI from a slug of `title`. If provided and the URI
already exists in the graph, the ingestion worker reuses it (idempotent
re-ingestion of additional pages from the same book).

### Submission methods

**MinIO sidecar** (primary method for bulk upload, bypassing the
mcp-server): drop the PDF into `/raw-pdfs/` and a matching `.yaml`
sidecar with the same base filename. The pdf-worker picks up pairs
automatically.

```
/raw-pdfs/eberron_campaign_setting_3e.pdf
/raw-pdfs/eberron_campaign_setting_3e.yaml
```

**HTTP API** (primary method for automation and batch scripts).
The metadata field accepts YAML:

```
POST /ingest
Content-Type: multipart/form-data

pdf:      <file>
metadata: <yaml string matching schema above>
```

**CLI** (wraps the HTTP API, for scripting a known library):

```bash
campaign-ingest \
  --pdf      ./books/eberron_cs_3e.pdf \
  --title    "Eberron Campaign Setting (3.5e)" \
  --edition  3e \
  --canon    canon \
  --publisher "Wizards of the Coast" \
  --year     2004
```

All methods produce the same result: a PDF + metadata `.yaml` pair in
MinIO `/raw-pdfs/` and a `PENDING` status record in Redis.

### Validation

Validated by the mcp-server (for HTTP/CLI submissions) and re-validated
by the pdf-worker before processing begins:

- `document_id`: required, must be unique (checked against Redis)
- `edition`: required, must be one of `3e`, `4e`, `5e`, `any`
- `canon_type`: required, must be one of `canon`, `kanon`, `community`
- `title`: required

Missing or invalid metadata → `FAILED` status immediately, no processing
attempted, clear error message in the status record.

---

## 7. Ingestion Pipelines

### Reference implementations

`pdf_pipeline.py` and `chunking_pipeline.py` in the repository root are
working starting points that demonstrate the pipeline mechanics. They are
**not authoritative**. DESIGN.md is the source of truth for intended
behaviour, data shapes, prompt design, and service boundaries. Where the
reference files and this document conflict, this document wins. The
reference files will be refactored to match as the services are built out.

### Three microservices, two pipelines

The full ingestion flow involves three microservices and produces two
outputs from the same source PDF: a vector index (Pipeline A) and a
knowledge graph (Pipeline B). Both share the PDF→Markdown conversion step
performed by the pdf-worker.

```
Client submits PDF + metadata JSON
         │
         ▼
    mcp-server  ──writes──▶  MinIO /raw-pdfs/
                                    │
                              pdf-worker polls
                                    │
                         PDF → Markdown (no LLM)
                                    │
                         MinIO /markdown/
                                    │
                          graph-worker polls
                                    │
                 ┌──────────────────┴──────────────────┐
                 ▼                                     ▼
         Pipeline A                           Pipeline B
     chunk → embed → vectors            classify → extract →
       (vector store)                   map → triples (Fuseki)
```

### Redis state schema

Each document has two Redis entries: a state hash and a distributed lock.

#### State hash — `doc:{document_id}:state`

```
HSET doc:eberron_cs_3e:state
  status               CONVERTING_PDF
  title                "Eberron Campaign Setting (3.5e)"
  edition              3e
  canon_type           canon
  worker_id            pdf-worker-7d9f4b
  created_at           2025-11-01T14:00:00Z
  updated_at           2025-11-01T14:00:05Z
  started_at           2025-11-01T14:00:05Z
  completed_at         (empty until COMPLETED)
  last_successful_stage  (empty until first stage completes)
  error                (empty unless FAILED)
  entity_count         (empty until COMPLETED)
  triple_count         (empty until COMPLETED)
```

All timestamps are ISO 8601 UTC strings. `worker_id` is the pod name
from the `HOSTNAME` environment variable — useful for debugging which
pod processed a given document.

`updated_at` is written on every state transition. The dashboard and
mcp-server use it to show elapsed time and to detect stale locks.

#### Distributed lock — `doc:{document_id}:lock`

A plain Redis string key set with `SET NX PX {ttl_ms}`. The value is
the claiming worker's `worker_id`.

```
SET doc:eberron_cs_3e:lock pdf-worker-7d9f4b NX PX 300000
```

This is a standard Redis distributed lock pattern. `NX` means set only
if the key does not exist. `PX 300000` sets a 300-second TTL — if the
worker crashes mid-conversion, the lock expires automatically and another
pod can claim the document.

**Lock acquisition flow** (pdf-worker, same pattern for graph-worker):

```python
import redis, os, time
from datetime import datetime, timezone

LOCK_TTL_MS  = int(os.environ.get("LOCK_TIMEOUT_SECONDS", 300)) * 1000
WORKER_ID    = os.environ["HOSTNAME"]

def try_claim(r: redis.Redis, document_id: str) -> bool:
    """
    Attempt to claim a document for processing.
    Returns True if this worker successfully claimed it, False otherwise.
    """
    lock_key  = f"doc:{document_id}:lock"
    state_key = f"doc:{document_id}:state"

    # Atomic claim: only succeeds if no lock exists
    claimed = r.set(lock_key, WORKER_ID, nx=True, px=LOCK_TTL_MS)
    if not claimed:
        return False

    # Update state to show this worker has started
    now = datetime.now(timezone.utc).isoformat()
    r.hset(state_key, mapping={
        "status":     "CONVERTING_PDF",
        "worker_id":  WORKER_ID,
        "started_at": now,
        "updated_at": now,
    })
    return True


def refresh_lock(r: redis.Redis, document_id: str):
    """
    Extend the lock TTL. Call this periodically during long operations
    to prevent expiry on a slow but healthy conversion.
    """
    lock_key = f"doc:{document_id}:lock"
    r.pexpire(lock_key, LOCK_TTL_MS)


def release_lock(r: redis.Redis, document_id: str):
    """Release the lock only if this worker owns it."""
    lock_key = f"doc:{document_id}:lock"
    # Lua script for atomic check-and-delete
    script = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
        return redis.call("DEL", KEYS[1])
    else
        return 0
    end
    """
    r.eval(script, 1, lock_key, WORKER_ID)
```

**Timeout and recovery**: the `updated_at` timestamp allows the
dashboard and the mcp-server to surface stalled documents. A document
that has been in `CONVERTING_PDF` for longer than `LOCK_TIMEOUT_SECONDS`
with an expired lock can be safely re-queued by setting its status back
to `PENDING`. This is a manual operator action visible in the dashboard,
not an automatic retry — automatic retries on unknown failures risk
data corruption.

#### Poll loop (pdf-worker)

```python
def poll_loop(r: redis.Redis, minio_client):
    """
    Continuously scan MinIO /raw-pdfs/ for unclaimed documents.
    Multiple replicas run this loop simultaneously — the Redis lock
    ensures each document is processed by exactly one pod.
    """
    while True:
        for obj in minio_client.list_objects("raw-pdfs"):
            if not obj.object_name.endswith(".pdf"):
                continue

            document_id = obj.object_name.removesuffix(".pdf")
            state_key   = f"doc:{document_id}:state"
            status      = r.hget(state_key, "status")

            # Skip if already claimed or completed
            if status and status not in (None, b"PENDING", b"FAILED"):
                continue

            if try_claim(r, document_id):
                try:
                    convert_pdf(r, minio_client, document_id)
                except Exception as e:
                    set_failed(r, document_id, str(e))
                finally:
                    release_lock(r, document_id)

        time.sleep(5)
```

### Pipeline states

Every PDF moves through explicit states stored in Redis keyed by
`document_id`. The MCP server returns HTTP 503 with a status payload
until `COMPLETED`. All transitions must be covered by blackbox tests.

```
PENDING
  → CONVERTING_PDF              (pdf-worker)
    → MARKDOWN_READY
      → CLASSIFYING_SECTIONS    (graph-worker)
        → EXTRACTING_ENTITIES
          → MAPPING_TO_ONTOLOGY
            → LOADING_GRAPH
              → LOADING_VECTORS
                → COMPLETED
                → FAILED  { error, last_successful_stage }
```

The two-stage split means a failure in `EXTRACTING_ENTITIES` does not
require re-running PDF conversion. Retry resumes from `MARKDOWN_READY`.

### Named graphs for atomic rollback

Each document's triples live in a named graph:
`<http://campaignsetting.io/doc/{document_id}>`

On failure in the graph-worker, one SPARQL statement removes all traces:

```sparql
DROP GRAPH <http://campaignsetting.io/doc/eberron_campaign_setting_3e>
```

### MinIO bucket layout

```
/raw-pdfs       ← mcp-server or user writes PDF + .yaml sidecar here
/markdown       ← pdf-worker writes converted .md files here
/processing     ← files currently being worked on (either worker)
/completed      ← successfully processed
/failed         ← failed, error artifact stored alongside
```

### Pipeline A — Vector search (pdf-worker + graph-worker)

```
MinIO /markdown/{document_id}.md
 └─ chunk (512 tokens, 64 token overlap)       [graph-worker]
     └─ embed (nomic-embed-text-v1.5)
         └─ store with metadata payload:
            { edition, canon_type, source_book,
              page_number, document_id }
         └─ LanceDB / Qdrant
```

Metadata from the PDF sidecar is stored as vector payload fields to
enable filtered vector search by edition and canonicity.

### Pipeline B — Knowledge graph (graph-worker)

```
MinIO /markdown/{document_id}.md
 └─ chunk (512 tokens, 64 token overlap)
     └─ section classifier  (LLM, 1-token output)
         └─ entity extractor  (LLM, JSON, one prompt per type)
             └─ ontology mapper  (deterministic: slug + Redis dedup)
                 └─ triple writer  (SPARQL UPDATE → Fuseki named graph)
```

### pdf-worker responsibilities

Converts a PDF to clean Markdown preserving page boundaries. Page numbers
are embedded as Markdown headers so the graph-worker can extract them
per-chunk without re-processing the PDF.

```markdown
<!-- page: 42 -->
## The Dagger River

The Dagger River is the longest river in Khorvaire...

<!-- page: 43 -->
...
```

This is the mechanism that populates `cs:pageNumber` on every extracted
entity — the graph-worker reads the nearest preceding `<!-- page: N -->`
comment when writing each triple.

### Step B1 — Section classification

One LLM call per chunk. One-token output. Discards HISTORY and FLAVOUR
before the expensive extraction step. ~30–40% of sourcebook pages contain
extractable structured entities.

```
SYSTEM:
You classify sections of a fantasy RPG sourcebook.
Return exactly ONE label:
LOCATION | NPC | FACTION | RELIGION | RACE | CLASS_SKILL |
HISTORY | FLAVOUR | OTHER

USER:
{chunk_text}
```

LLM config:
```python
model = "openai/gemma4:e2b"
base_url = os.environ["LLAMA_CPP_HOST"]
max_tokens = 5
temperature = 0.0
```

**Classifier label routing:**

| Label | Extraction prompt used | Notes |
|---|---|---|
| `LOCATION` | Location prompt | Covers cities, rivers, regions, planes, etc. |
| `NPC` | NPC prompt | |
| `FACTION` | Faction prompt | |
| `RELIGION` | Religion/Deity prompt | |
| `RACE` | Race prompt | |
| `CLASS_SKILL` | Class/Skill prompt | Combined — chunks rarely describe one without the other |
| `HISTORY` | None — vector pipeline only | Narrative, no structured entities |
| `FLAVOUR` | None — vector pipeline only | Prose, no structured entities |
| `OTHER` | None | Discarded |

Note: `NATION` is not a separate classifier label. Nations are a subclass
of Location (`cs:Nation rdfs:subClassOf cs:Location`) and are extracted
by the Location prompt with `type: "Nation"`. The classifier does not need
to distinguish them — the LLM extraction prompt handles the type assignment.

### Step B2 — Entity extraction prompts

One prompt per section type. Generic prompts produce worse results.
Every prompt appends a known-entity hint to help coreference resolution:

```
Known entities already in graph (use these exact names if referring to them):
{top_20_relevant_entity_names_from_redis}
```

#### NPC

```
SYSTEM:
Extract structured NPC data from RPG sourcebook text.
Return ONLY valid JSON. No preamble, no explanation.

{
  "npcs": [{
    "name": "canonical name as written in source",
    "aliases": ["other names, titles, epithets"],
    "race": "race name or null",
    "character_class": "class name or null",
    "alignment": "alignment string or null",
    "nationality": "nation name or null",
    "location": "primary location or null",
    "factions": ["faction names"],
    "worships": "deity name or null",
    "potential_motives": [
      {
        "summary": "one sentence describing a possible goal or drive",
        "source_quote": "verbatim or near-verbatim supporting text or null"
      }
    ],
    "description": "brief physical or personality description",
    "relationships": [
      {"target": "entity name", "type": "ally|enemy|rival|mentor|subordinate|other"}
    ],
    "page_reference": "page number(s): '42' or '42-43'"
  }]
}

If no NPCs are described, return {"npcs": []}.

Known entities already in graph:
{known_entities}

USER:
{chunk_text}
```

#### Location

```
SYSTEM:
Extract structured location data from RPG sourcebook text.
Return ONLY valid JSON. No preamble.

{
  "locations": [{
    "name": "canonical name",
    "aliases": ["other names"],
    "type": "City|River|Region|Nation|Dungeon|Sea|Mountain|Forest|Ruin|Plane|Other",
    "parent_location": "containing region or nation or null",
    "controlling_faction": "faction name or null",
    "nation": "nation name or null",
    "description": "brief description",
    "notable_npcs": ["NPC names associated with this location"],
    "connected_locations": ["adjacent or linked location names"],
    "page_reference": "page number(s)"
  }]
}

If no locations are described, return {"locations": []}.

Known entities already in graph:
{known_entities}

USER:
{chunk_text}
```

#### Faction

```
SYSTEM:
Extract structured faction data from RPG sourcebook text.
Return ONLY valid JSON. No preamble.

{
  "factions": [{
    "name": "canonical name",
    "aliases": ["other names"],
    "type": "Military|Criminal|Religious|Political|Mercantile|Arcane|Druidic|Other",
    "alignment": "general alignment tendency or null",
    "headquarters": "location name or null",
    "nation": "primary nation or null",
    "leader": "NPC name or null",
    "members": ["notable NPC names"],
    "potential_motives": [
      {
        "summary": "one sentence describing a plausible faction goal",
        "source_quote": "supporting text or null"
      }
    ],
    "allies": ["faction names"],
    "enemies": ["faction names"],
    "operates_in": ["location names"],
    "worships": "deity name or null",
    "page_reference": "page number(s)"
  }]
}

Known entities already in graph:
{known_entities}

USER:
{chunk_text}
```

#### Religion and Deity

```
SYSTEM:
Extract structured religion and deity data from RPG sourcebook text.
Return ONLY valid JSON. No preamble.

{
  "religions": [{
    "name": "canonical religion or pantheon name",
    "aliases": ["other names"],
    "primary_deity": "main deity name or null",
    "deities": ["all associated deity names"],
    "worshipping_factions": ["faction names"],
    "dominant_in": ["nation names"],
    "description": "brief description of beliefs or practices",
    "page_reference": "page number(s)"
  }],
  "deities": [{
    "name": "canonical deity name",
    "aliases": ["titles, epithets"],
    "religion": "parent religion or pantheon name or null",
    "alignment": "alignment string or null",
    "domains": ["divine domain names"],
    "description": "brief description",
    "page_reference": "page number(s)"
  }]
}

Known entities already in graph:
{known_entities}

USER:
{chunk_text}
```

#### Race

```
SYSTEM:
Extract structured race/ancestry data from RPG sourcebook text.
Return ONLY valid JSON. No preamble.

{
  "races": [{
    "name": "canonical race name",
    "aliases": ["other names or subtypes"],
    "description": "brief description of the race",
    "typical_classes": ["character class names commonly associated"],
    "native_regions": ["location names where this race originates"],
    "notable_npcs": ["named individuals of this race mentioned in text"],
    "page_reference": "page number(s)"
  }]
}

If no races are described, return {"races": []}.

Known entities already in graph:
{known_entities}

USER:
{chunk_text}
```

#### Class and Skill

```
SYSTEM:
Extract structured character class and skill data from RPG sourcebook text.
Return ONLY valid JSON. No preamble.

{
  "classes": [{
    "name": "canonical class name",
    "aliases": ["other names or variants"],
    "description": "brief description",
    "associated_skills": ["skill names associated with this class"],
    "page_reference": "page number(s)"
  }],
  "skills": [{
    "name": "canonical skill name",
    "aliases": ["other names"],
    "description": "brief description of what this skill represents",
    "page_reference": "page number(s)"
  }]
}

If no classes or skills are described, return {"classes": [], "skills": []}.

Known entities already in graph:
{known_entities}

USER:
{chunk_text}
```

### Step B3 — Ontology mapping and deduplication

Extracted JSON is converted to RDF triples deterministically. Entity
names are normalised to URI slugs and deduplicated against a Redis
name→URI index maintained throughout ingestion.

```python
import re

def uri_slug(name: str) -> str:
    """'Sharn, City of Towers' → 'Sharn_City_of_Towers'"""
    slug = re.sub(r"[^\w\s-]", "", name)
    slug = re.sub(r"[\s-]+", "_", slug.strip())
    return slug

# Redis key: "entity:Silver_Flame"
# Value:     "http://campaignsetting.io/ontology#Silver_Flame"
#
# On first encounter: create URI, store in Redis.
# On subsequent encounter: return existing URI.
# "the Silver Flame", "Silver Flame", "The Flame" all resolve to
# cs:Silver_Flame because the known-entity hint in the extraction
# prompt instructs the LLM to use the canonical name.
```

Coreference resolution via fastembed: if the known-entity hint fails
(the LLM coins a new name anyway), the mapper checks the new slug
against existing entity labels using cosine similarity with
`nomic-ai/nomic-embed-text-v1.5`. Similarity above 0.92 → reuse
existing URI and log the alias. Between 0.80 and 0.92 → flag for
manual review, proceed with new URI. Below 0.80 → new entity.

```python
from fastembed import TextEmbedding

# Model is pre-downloaded and baked into the container image.
# No internet access required at runtime.
embedder = TextEmbedding("nomic-ai/nomic-embed-text-v1.5")
```

Container bake-in:

```dockerfile
# Pre-download nomic-embed-text-v1.5 so containers without internet
# access can start immediately.
RUN python -c "
from fastembed import TextEmbedding
TextEmbedding('nomic-ai/nomic-embed-text-v1.5')
"
```

### Step B4 — Triple writing

Batch SPARQL UPDATE INSERT DATA into the document's named graph via
Fuseki's `/update` endpoint. One transaction per document. On any
exception: `DROP GRAPH`, set Redis status to `FAILED`.

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

The LLM never sees SPARQL. Every tool takes human-readable parameters and
returns structured data with provenance. The server builds, validates,
and executes all SPARQL internally.

### Context window discipline

Six tools in the manifest. Descriptions are terse. Total manifest target:
under 800 tokens.

If context pressure grows, a query-intent classifier (one LLM call,
one-word output: LIST | GET | RELATE | HIERARCHY | SEARCH | STATUS)
selects a 2–3 tool subset before the main agent call.

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
    Transitive via OWL inference — no property path gymnastics needed.
    """

def search_by_property(
    entity_type: EntityType,
    property_name: str,
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
            MAPPING_TO_ONTOLOGY | LOADING_GRAPH |
            LOADING_VECTORS | COMPLETED | FAILED
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

---

## 9. Evaluation Harness

### Test infrastructure

Tests run via:

```bash
cd tests && docker compose up --abort-on-container-exit
```

Every service is tested as a black box — tests make HTTP or MinIO API
calls against running containers. They do not import application code
or inspect internal state directly. Test dependencies live in
`pyproject.toml` under `[project.optional-dependencies] test`. The
test runner container uses the existing project Dockerfile template.

### Test folder structure

Each microservice has its own subfolder with its own docker-compose
file. This keeps test environments isolated — testing the pdf-worker
does not require the LLM to be reachable; testing the graph-worker
does not require the mcp-server to be running.

```
tests/
├── conftest.py               # shared fixtures: MinIO client, Redis client,
│                             # HTTP clients, fixture PDF paths
├── fixtures/
│   ├── sample_eberron.pdf    # small hand-crafted PDF, known ground truth
│   ├── sample_eberron.yaml   # matching metadata sidecar
│   └── expected/
│       ├── rivers_3e.json    # ground truth for eval queries
│       └── factions_5e.json
│
├── pdf_worker/
│   ├── docker-compose.yml    # fuseki + redis + minio + pdf-worker + runner
│   └── test_pdf_worker.py
│
├── graph_worker/
│   ├── docker-compose.yml    # fuseki + redis + minio + graph-worker + runner
│   └── test_graph_worker.py  # seeds MinIO /markdown/ directly
│
├── mcp_server/
│   ├── docker-compose.yml    # fuseki + redis + minio + mcp-server + runner
│   └── test_mcp_server.py    # seeds Fuseki directly for query tests
│
└── integration/
    ├── docker-compose.yml    # full stack: all three services + runner
    └── test_integration.py   # end-to-end: PDF in → query out
```

### Per-service docker-compose files

#### tests/pdf_worker/docker-compose.yml

```yaml
version: "3.9"
services:

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  minio:
    image: minio/minio:latest
    command: server /data
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      retries: 10

  pdf-worker:
    build:
      context: ../..
      dockerfile: Dockerfile.pdf
    environment:
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
    depends_on:
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy

  test-runner:
    build:
      context: ../..
      dockerfile: Dockerfile   # existing project template
    command: pytest tests/pdf_worker/ -v --tb=short
    environment:
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
    depends_on:
      pdf-worker:
        condition: service_started
```

#### tests/graph_worker/docker-compose.yml

```yaml
version: "3.9"
services:

  fuseki:
    image: stain/jena-fuseki:latest
    environment:
      ADMIN_PASSWORD: testpassword
      FUSEKI_DATASET_1: /campaign
    volumes:
      - ../../ontology.ttl:/ontology/ontology.ttl:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3030/$/ping"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  minio:
    image: minio/minio:latest
    command: server /data
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      retries: 10

  graph-worker:
    build:
      context: ../..
      dockerfile: Dockerfile.graph
    environment:
      FUSEKI_ENDPOINT: http://fuseki:3030/campaign
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
      LLAMA_CPP_HOST: ${LLAMA_CPP_HOST:-http://host.docker.internal:8080/v1}
    depends_on:
      fuseki:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy

  test-runner:
    build:
      context: ../..
      dockerfile: Dockerfile
    command: pytest tests/graph_worker/ -v --tb=short
    environment:
      FUSEKI_ENDPOINT: http://fuseki:3030/campaign
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
      LLAMA_CPP_HOST: ${LLAMA_CPP_HOST:-http://host.docker.internal:8080/v1}
    depends_on:
      graph-worker:
        condition: service_started
```

#### tests/mcp_server/docker-compose.yml

```yaml
version: "3.9"
services:

  fuseki:
    image: stain/jena-fuseki:latest
    environment:
      ADMIN_PASSWORD: testpassword
      FUSEKI_DATASET_1: /campaign
    volumes:
      - ../../ontology.ttl:/ontology/ontology.ttl:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3030/$/ping"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  minio:
    image: minio/minio:latest
    command: server /data
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      retries: 10

  mcp-server:
    build:
      context: ../..
      dockerfile: Dockerfile.mcp
    environment:
      FUSEKI_ENDPOINT: http://fuseki:3030/campaign
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
    depends_on:
      fuseki:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 5s
      retries: 10

  test-runner:
    build:
      context: ../..
      dockerfile: Dockerfile
    command: pytest tests/mcp_server/ -v --tb=short
    environment:
      MCP_SERVER_URL: http://mcp-server:8000
      FUSEKI_ENDPOINT: http://fuseki:3030/campaign
      REDIS_URL: redis://redis:6379
    depends_on:
      mcp-server:
        condition: service_healthy
```

#### tests/integration/docker-compose.yml

Full stack. This is the only test that exercises the complete
PDF → Markdown → triples + vectors → query path.

```yaml
version: "3.9"
services:

  fuseki:
    image: stain/jena-fuseki:latest
    environment:
      ADMIN_PASSWORD: testpassword
      FUSEKI_DATASET_1: /campaign
    volumes:
      - ../../ontology.ttl:/ontology/ontology.ttl:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3030/$/ping"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  minio:
    image: minio/minio:latest
    command: server /data
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 5s
      retries: 10

  pdf-worker:
    build:
      context: ../..
      dockerfile: Dockerfile.pdf
    environment:
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
    depends_on:
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy

  graph-worker:
    build:
      context: ../..
      dockerfile: Dockerfile.graph
    environment:
      FUSEKI_ENDPOINT: http://fuseki:3030/campaign
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
      LLAMA_CPP_HOST: ${LLAMA_CPP_HOST:-http://host.docker.internal:8080/v1}
    depends_on:
      fuseki:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy

  mcp-server:
    build:
      context: ../..
      dockerfile: Dockerfile.mcp
    environment:
      FUSEKI_ENDPOINT: http://fuseki:3030/campaign
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
    depends_on:
      fuseki:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 5s
      retries: 10

  test-runner:
    build:
      context: ../..
      dockerfile: Dockerfile
    command: pytest tests/integration/ -v --tb=short
    environment:
      MCP_SERVER_URL: http://mcp-server:8000
      REDIS_URL: redis://redis:6379
      MINIO_ENDPOINT: http://minio:9000
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
      LLAMA_CPP_HOST: ${LLAMA_CPP_HOST:-http://host.docker.internal:8080/v1}
    depends_on:
      mcp-server:
        condition: service_healthy
```

### What each test module covers

#### tests/pdf_worker/test_pdf_worker.py

Black-box tests against the pdf-worker. Tests seed MinIO `/raw-pdfs/`
directly and assert on MinIO `/markdown/` and Redis state.

```python
def test_pdf_produces_markdown_in_minio():
    # Upload fixture PDF + metadata to /raw-pdfs/
    # Poll Redis until MARKDOWN_READY
    # Assert /markdown/{document_id}.md exists in MinIO

def test_markdown_contains_page_markers():
    # Assert <!-- page: N --> comments present in output

def test_page_numbers_are_accurate():
    # Assert known content appears after correct page marker

def test_invalid_metadata_sets_failed_status():
    # Upload PDF with missing edition field
    # Assert Redis status → FAILED immediately

def test_duplicate_document_id_rejected():
    # Upload same document_id twice
    # Assert second upload → FAILED with clear error
```

#### tests/graph_worker/test_graph_worker.py

Black-box tests against the graph-worker. Tests seed MinIO `/markdown/`
directly (bypassing the pdf-worker) and assert on Fuseki and Redis.

```python
def test_markdown_produces_triples_in_fuseki():
    # Write fixture .md to /markdown/
    # Set Redis status to MARKDOWN_READY
    # Poll until COMPLETED
    # SPARQL query Fuseki → assert expected triples present

def test_named_graph_created_per_document():
    # Assert graph URI exists after ingestion

def test_failed_extraction_drops_named_graph():
    # Inject malformed Markdown that causes extraction failure
    # Assert named graph removed, status → FAILED

def test_retry_resumes_from_markdown_ready():
    # Force failure at EXTRACTING_ENTITIES
    # Re-trigger from MARKDOWN_READY
    # Assert no re-conversion, completes successfully

def test_page_numbers_present_on_triples():
    # After ingestion, SPARQL query for cs:pageNumber
    # Assert all entities have non-null page references

def test_coreference_deduplication():
    # Ingest two chunks referring to same entity by different names
    # Assert single URI in Fuseki, both names as cs:alias
```

#### tests/mcp_server/test_mcp_server.py

Black-box tests against the mcp-server HTTP API. Tests seed Fuseki
directly via SPARQL UPDATE (no workers needed) to test query behaviour
in isolation from ingestion.

```python
def test_health_endpoint_returns_200():

def test_empty_graph_returns_empty_list():
    r = client.get("/tools/list_entities?entity_type=River")
    assert r.status_code == 200
    assert r.json()["results"] == []

def test_unknown_document_status_returns_404():

def test_list_entities_returns_page_references():
    # Seed Fuseki with known triples including pageNumber
    # Assert all results contain page_reference

def test_edition_filter_applied_correctly():

def test_canon_type_filter_applied_correctly():

def test_ingest_endpoint_writes_to_minio():
    # POST /ingest with fixture PDF
    # Assert MinIO /raw-pdfs/ contains the file
    # Assert Redis status → PENDING

def test_ingest_without_metadata_returns_422():
```

#### tests/integration/test_integration.py

End-to-end tests. Submit a fixture PDF, wait for full pipeline
completion, assert on query results.

```python
def test_full_pipeline_pdf_to_queryable_graph():
    # POST fixture PDF + metadata to /ingest
    # Poll get_ingestion_status until COMPLETED (with timeout)
    # list_entities(entity_type="River") → assert known rivers present

def test_rivers_precision_and_recall():
    names = [r["name"] for r in results]
    s = score(names, KNOWN_FIXTURE_RIVERS)
    assert s["recall"]    >= 0.80
    assert s["precision"] >= 0.90

def test_all_results_have_page_references():
    assert all(r.get("page_reference") for r in results)

def test_shelf_filter_works_end_to_end():
    # Submit canon PDF and kanon PDF
    # Assert edition filter returns only appropriate results
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

## 11. Hardware and Testbed Strategy

### Local development machine (6GB VRAM)

The LLM (gemma4:e2b via llama.cpp) runs here. This machine is external
to the Kubernetes cluster and docker-compose network. It serves the
OpenAI-compatible `/v1` API at:

```
http://sinan.msi-nvidia-server.ts.ozel.network:8080/v1
```

The rest of the stack (Fuseki, MCP server, ingestion workers, Redis,
MinIO) runs in Docker or Kubernetes on the development machine's CPU.
The 6GB VRAM is dedicated entirely to the LLM.

**Constraints on the 6GB VRAM machine**:
- gemma4:e2b at Q4: fits comfortably, leaves headroom
- 8k context window: adequate for chunk extraction (512 tokens in,
  ~400 out, ~200 system prompt)
- Ingestion is LLM-bound, not GPU-memory-bound — expect slow throughput
  on large corpora. Acceptable for development.

### Ephemeral cloud testbed (evaluation and CI)

Full-corpus evaluation via IaC-managed ephemeral GPU instances:

| Cloud | Instance | GPU | VRAM |
|---|---|---|---|
| AWS | g5.xlarge | A10G | 24GB |
| GCP | g2-standard-4 | L4 | 24GB |

Workflow: provision → ingest full corpus → run test suite →
export JSON report to MinIO/S3 → destroy. Estimated cost: $2–4 per run.

The evaluation JSON is compared against a stored baseline on each CI run.
Regression in F1 score blocks the pipeline like a failing test.

### Upgrade path

A 24GB VRAM local machine (RTX 3090 or 4090) allows 13B unquantised or
34B Q4 models. Larger models improve extraction quality most on hard
cases: coreference resolution, ambiguous faction relationships, entities
described obliquely. For a project that may become a product, this is
the highest-leverage hardware investment.

---

## 10. Streamlit Dashboard

The dashboard is a lightweight status monitor and submission form. It is
a microservice that communicates exclusively with the mcp-server HTTP API.
It has no direct access to Redis, MinIO, Fuseki, or any worker. This is
a hard architectural constraint — if the dashboard ever needs information
that the mcp-server does not expose, the correct fix is to add an endpoint
to the mcp-server, not to give the dashboard direct database access.

### What the dashboard does

**Status view** — a table of all documents with their current pipeline
state, timestamps, elapsed time, entity count, triple count, and error
message if failed. Refreshes via polling on a configurable interval
(default 10 seconds). Shows a clear visual distinction between
PENDING / IN PROGRESS / COMPLETED / FAILED states.

**Submission form** — fields for all required and optional metadata,
plus a file uploader for the PDF. On submit, POSTs to `/ingest` and
immediately switches to the status view filtered to the new document.

**Re-queue action** — for documents stuck in a stale IN PROGRESS state
(lock expired, worker crashed), an operator button calls a
`/admin/requeue/{document_id}` endpoint on the mcp-server to reset the
status to `PENDING`.

### mcp-server endpoints required by the dashboard

The dashboard only calls these endpoints. No others.

```
GET  /status                          → list all documents with full state
GET  /status/{document_id}            → single document state
POST /ingest                          → submit PDF + metadata
POST /admin/requeue/{document_id}     → reset FAILED/stale to PENDING
GET  /health                          → used to show connection status
```

### Polling design

Streamlit's `st.rerun()` with `time.sleep()` is sufficient for polling
at this scale. No WebSockets needed. The dashboard does not hold open
connections to the mcp-server between polls.

```python
import streamlit as st
import httpx
import time

MCP_URL      = os.environ["MCP_SERVER_URL"]
POLL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 10))

def status_page():
    st.title("Ingestion Status")

    # Fetch all document states from mcp-server
    try:
        resp = httpx.get(f"{MCP_URL}/status", timeout=5)
        docs = resp.json()["documents"]
    except Exception as e:
        st.error(f"Cannot reach mcp-server: {e}")
        docs = []

    # Render status table
    for doc in docs:
        col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
        col1.write(doc["title"])
        col2.write(doc["status"])
        col3.write(doc.get("updated_at", "—"))
        if doc["status"] in ("FAILED",) or _is_stale(doc):
            if col4.button("Re-queue", key=doc["document_id"]):
                httpx.post(f"{MCP_URL}/admin/requeue/{doc['document_id']}")
                st.rerun()

    # Auto-refresh
    time.sleep(POLL_SECONDS)
    st.rerun()


def _is_stale(doc: dict) -> bool:
    """True if the document has been IN PROGRESS for too long."""
    if doc["status"] not in ("CONVERTING_PDF", "CLASSIFYING_SECTIONS",
                              "EXTRACTING_ENTITIES", "MAPPING_TO_ONTOLOGY",
                              "LOADING_GRAPH", "LOADING_VECTORS"):
        return False
    updated = datetime.fromisoformat(doc["updated_at"])
    return (datetime.now(timezone.utc) - updated).seconds > STALE_THRESHOLD_SECONDS


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
            "document_id":       slugify(title),
            "title":             title,
            "edition":           edition,
            "canon_type":        canon,
            "publisher":         publisher,
            "publication_year":  year,
            "authors":           [a.strip() for a in authors.split(",") if a.strip()],
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

### Dashboard in docker-compose (tests)

The dashboard is not included in the per-service test docker-compose
files. It is included in `tests/integration/docker-compose.yml` only,
since integration tests may want to verify the status endpoint surfaces
correctly through the UI flow.

```yaml
  dashboard:
    build:
      context: ../..
      dockerfile: Dockerfile.dashboard
    environment:
      MCP_SERVER_URL: http://mcp-server:8000
      POLL_INTERVAL_SECONDS: 2   # faster polling in tests
    depends_on:
      mcp-server:
        condition: service_healthy
    ports:
      - "8501:8501"
```

### Dashboard in Helm values

```yaml
dashboard:
  image: your-registry/campaign-dashboard:latest
  dockerfile: Dockerfile.dashboard
  replicas: 1
  port: 8501
  pollIntervalSeconds: 10
```

---

## 12. Helm Chart

The project ships a Helm chart for one-command installation on any
Kubernetes cluster. All tuneable values are in `values.yaml`.

### Chart structure

```
chart/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── _helpers.tpl
    ├── namespace.yaml
    ├── configmap-ontology.yaml
    ├── secret-fuseki.yaml
    ├── secret-llm.yaml
    ├── statefulset-fuseki.yaml
    ├── service-fuseki.yaml
    ├── statefulset-redis.yaml
    ├── service-redis.yaml
    ├── statefulset-minio.yaml
    ├── service-minio.yaml
    ├── statefulset-vectorstore.yaml
    ├── service-vectorstore.yaml
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
keywords:
  - mcp
  - rag
  - knowledge-graph
  - sparql
  - ttrpg
maintainers:
  - name: your-name
    email: your@email.com
dependencies: []
```

### values.yaml

```yaml
# values.yaml — override any of these at install time

namespace: campaign-query

fuseki:
  image: stain/jena-fuseki:latest
  dataset: campaign
  storage: 10Gi
  jvmArgs: "-Xmx2g"
  adminPassword: ""   # set via --set or secret

llm:
  # External LLM endpoint — NOT deployed by this chart.
  # Required only by graph-worker. Must be set at install time.
  host: ""            # e.g. http://your-gpu-host:8080/v1
  model: openai/gemma4:e2b

embeddings:
  model: nomic-ai/nomic-embed-text-v1.5

mcpServer:
  image: your-registry/campaign-mcp-server:latest
  dockerfile: Dockerfile.mcp
  replicas: 1
  port: 8000

pdfWorker:
  image: your-registry/campaign-pdf-worker:latest
  dockerfile: Dockerfile.pdf
  replicas: 1
  # No LLM access needed

graphWorker:
  image: your-registry/campaign-graph-worker:latest
  dockerfile: Dockerfile.graph
  replicas: 1
  # Requires llm.host to be set

dashboard:
  image: your-registry/campaign-dashboard:latest
  dockerfile: Dockerfile.dashboard
  replicas: 1
  port: 8501
  pollIntervalSeconds: 10

vectorStore:
  type: qdrant          # qdrant | lancedb
  storage: 20Gi

minio:
  storage: 50Gi
  rootUser: minioadmin
  rootPassword: ""      # set via --set or secret
  buckets:
    - raw-pdfs
    - markdown
    - processing
    - completed
    - failed

redis:
  image: redis:7-alpine

ingress:
  enabled: false
  host: ""
  tls: false
```

### Installation

```bash
# Add the chart repo (once published)
helm repo add campaign-query https://your-registry/charts
helm repo update

# Install with required overrides
helm install eberron-query campaign-query/campaign-query \
  --namespace campaign-query \
  --create-namespace \
  --set fuseki.adminPassword=changeme \
  --set llm.host=http://your-gpu-host:8080/v1 \
  --set minio.rootPassword=changeme

# Upgrade after config change
helm upgrade eberron-query campaign-query/campaign-query \
  --reuse-values \
  --set mcpServer.replicas=2

# Uninstall
helm uninstall eberron-query --namespace campaign-query
```

### Ontology as Helm value

The ontology file is rendered into a ConfigMap by the chart. To use a
custom ontology:

```bash
helm install my-query campaign-query/campaign-query \
  --set-file ontologyFile=./my_custom_ontology.ttl \
  --set llm.host=http://your-gpu-host:8080/v1 \
  --set fuseki.adminPassword=changeme \
  --set minio.rootPassword=changeme
```

If `ontologyFile` is not set, the chart uses the bundled
`ontology.ttl` from the repository.

---

## 13. Known Risks and Mitigations

### Coreference resolution

**Problem**: "the Silver Flame" in chapter 3, "the Flame" in chapter 7,
and "Church of the Silver Flame" in a Keith Baker post all refer to the
same entity. Without resolution the pipeline creates three separate URI
nodes and queries return partial results.

**Mitigation**: Two-layer approach.

Layer 1 — known-entity hint in every extraction prompt. The LLM is shown
the 20 most relevant entity names already in the graph and instructed to
use these exact names. Resolves most coreferences before they reach the
mapper.

Layer 2 — fastembed similarity fallback. If the LLM coins a new name
anyway, the ontology mapper embeds the new name using
`nomic-ai/nomic-embed-text-v1.5` (pre-baked into the container) and
checks cosine similarity against existing entity labels:

- ≥ 0.92: reuse existing URI, store new name as `cs:alias`
- 0.80–0.92: flag for review, proceed with new URI
- < 0.80: new entity

```dockerfile
RUN python -c "
from fastembed import TextEmbedding
TextEmbedding('nomic-ai/nomic-embed-text-v1.5')
"
```

### Cross-edition entity descriptions

**Problem**: Jaela Daran is described differently in 3e and 5e. A single
`cs:description` triple cannot hold both.

**Solution**: Store edition-tagged description literals inline.

```turtle
cs:Jaela_Daran
    cs:description "Young Keeper of the Flame [3e, p.42]" ;
    cs:description "Keeper of the Silver Flame [5e, p.18]" .
```

The entity node is shared. The page reference in the literal makes
provenance unambiguous. This is queryable and readable. A formal RDF
reification solution is cleaner but significantly more complex —
defer unless this causes measurable problems.

### OWL transitivity

With Jena Fuseki and OWL inference enabled, `cs:contains` declared
`owl:TransitiveProperty` behaves automatically. A query for
"everything Breland contains" returns all descendants at any depth
without explicit `cs:contains+` property paths. This is the primary
reason Fuseki was chosen over Oxigraph.

Verify the reasoner is active at startup:

```sparql
# Smoke test: if Breland contains Sharn, and Sharn contains the Cogs,
# this query must return the Cogs without an explicit triple
# (Breland cs:contains Cogs).
SELECT ?child WHERE {
    cs:Breland cs:contains ?child .
}
```

### Write concurrency

Fuseki's TDB2 store serialises concurrent writes internally via a
write lock. Multiple ingestion worker replicas are safe. This is an
improvement over the Oxigraph embedded store. No special concurrency
management required in application code.

### Embedding model quality

`nomic-embed-text-v1.5` is chosen over `all-MiniLM-L6-v2` for the
coreference similarity step: 8192-token context vs 256, better MTEB
retrieval scores, and active maintenance as of 2025–2026. MiniLM-L6-v2
is considered a legacy model for new projects. If the existing vector
pipeline requires MiniLM for compatibility, run both models — MiniLM
for vector chunks, nomic for entity linking. They are separate instances
with separate responsibilities.