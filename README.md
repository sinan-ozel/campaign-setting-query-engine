![CI/CD](https://github.com/sinan-ozel/campaign-setting-query-engine/actions/workflows/ci.yaml/badge.svg?branch=main)
![License](https://img.shields.io/github/license/sinan-ozel/campaign-setting-query-engine.svg)

# Campaign Setting Query Engine

A knowledge graph–backed MCP server for precision lore retrieval from tabletop RPG campaign settings. Built for Eberron. Zero hallucinations — every answer cites a sourcebook page.

> **Canonical evaluation query**: *"List me the rivers of Eberron."*
> Scored on precision + recall + F1 against ground truth extracted from the sourcebooks.

---

## What it does

1. **Ingests PDF sourcebooks** — submits a PDF + metadata YAML; a pdf-worker converts it to Markdown page-by-page (with OCR fallback and JPX error handling).
2. **Extracts entities** — a graph-worker chunks the Markdown, classifies sections, and runs a single combined LLM call per chunk to extract NPCs, locations, factions, religions, deities, races, classes, and skills.
3. **Builds a knowledge graph** — extracted entities are mapped to an OWL ontology and written to Apache Jena Fuseki as named-graph triples. OWL transitivity on `cs:contains` means "list everything in Xen'drik" returns all descendants automatically.
4. **Serves MCP tools** — a FastMCP server exposes 6 tools that translate natural-language lore queries into SPARQL. Agents get structured answers with page references. No LLM at query time.

---

## Architecture

```
Client / Agent
     │  MCP tools (/mcp)
     ▼
mcp-server  ──SPARQL──▶  Fuseki (OWL graph)
     │
     │  Admin HTTP (/ingest, /status, /admin/*)
     ▼
Streamlit dashboard

MinIO (/raw-pdfs, /markdown)
     │
     ├── pdf-worker   (PDF → Markdown, no LLM)
     └── graph-worker (Markdown → triples, LLM via LiteLLM)

Redis  (pipeline state, entity dedup index)
LLM    (external: llama.cpp / Ollama / OpenAI / Anthropic via config/llm.yaml)
```

**Services:**

| Service | Image | Description |
|---|---|---|
| `mcp-server` | `Dockerfile` | FastMCP tools + admin HTTP API |
| `pdf-worker` | `pdf_worker/Dockerfile` | PDF → Markdown conversion |
| `graph-worker` | `graph_worker/Dockerfile` | Markdown → knowledge graph triples |
| `dashboard` | `dashboard/Dockerfile` | Streamlit ingestion monitor |
| Fuseki 5.1.0 | `stain/jena-fuseki:5.1.0` | SPARQL 1.1 graph store, OWL inference |
| Redis 7 | `redis:7-alpine` | Pipeline state, persistent (AOF) |
| MinIO | `minio/minio` | PDF and Markdown object storage |

---

## Quickstart

The only requirement is **Docker**.

### 1. Configure the LLM

Copy `.env.example` to `.env` and set your LLM endpoint:

```bash
cp .env.example .env
# Edit .env:
LLAMA_CPP_HOST=http://your-gpu-host:8080/v1
```

The LLM provider config lives in `config/llm.yaml`. The default is llama.cpp (OpenAI-compatible). Swap to Ollama, Mistral, OpenAI, or Anthropic by editing that file — no code changes needed:

```yaml
# config/llm.yaml — llama.cpp (default)
api_base: ${LLAMA_CPP_HOST}
model: openai/gemma4:e2b
api_key: dummy
timeout: 150
```

```yaml
# Ollama example
api_base: http://ollama:11434
model: ollama/gemma3:27b
```

```yaml
# OpenAI example
model: gpt-4o
api_key: ${OPENAI_API_KEY}
```

### 2. Start the full stack

```bash
docker compose up --build
```

Services start in dependency order. Fuseki takes ~30 seconds on first boot (OWL reasoner init).

### 3. Submit a sourcebook

Via the dashboard at **http://localhost:8501**, or via the API:

```bash
curl -X POST http://localhost:8000/ingest \
  -F "pdf=@eberron_campaign_setting_3e.pdf" \
  -F 'metadata=document_id: eberron_campaign_setting_3e
title: "Eberron Campaign Setting (3.5e)"
edition: 3e
canon_type: canon
publisher: "Wizards of the Coast"
tags: [core-rulebook, setting-lore]'
```

Poll for progress:

```bash
curl http://localhost:8000/status/eberron_campaign_setting_3e
```

### 4. Query the graph

Connect any MCP client to `http://localhost:8000/mcp`. Available tools:

| Tool | Example |
|---|---|
| `list_entities` | List all rivers, all factions, all NPCs in Breland |
| `get_entity` | Full profile for "Lady Vol" |
| `get_relationships` | Allies of "The Emerald Claw" |
| `get_location_hierarchy` | Everything Xen'drik contains (transitive) |
| `search_by_property` | NPCs with nationality "Karrnath" |
| `get_ingestion_status` | Current pipeline state per document |

---

## Configuration files

All live in `config/` — version-controlled, mounted as ConfigMaps in Kubernetes:

| File | Purpose |
|---|---|
| `config/ontology.ttl` | OWL schema: classes, properties, `cs:contains owl:TransitiveProperty` |
| `config/ingestion_config.yaml` | Coreference thresholds, chunking parameters |
| `config/llm.yaml` | LiteLLM provider config — swap providers here |
| `config/fuseki-config.ttl` | Fuseki Assembler: wires OWL reasoner to TDB2 store |

---

## Development

### VS Code tasks

Open **Terminal → Run Task** (`Ctrl+Shift+P → Tasks: Run Task`):

| Task | What it runs |
|---|---|
| `test: mcp-server` | mcp-server black-box tests (default) |
| `test: pdf-worker` | pdf-worker black-box tests |
| `test: graph-worker` | graph-worker black-box tests (needs LLM) |
| `test: integration` | Full pipeline: PDF in → graph query out |
| `test: all` | All four suites in series, stops on first failure |
| `dev: start` | Full stack via `docker-compose.yaml` |
| `dev: stop` | Tear down the full stack |
| `lint` | ruff check on `server/` and `tests/` |
| `reformat` | black + docformatter + isort |
| `inspector` | MCP Inspector at http://localhost:6274 |

### Run a specific test suite

```bash
# mcp-server (no LLM needed)
docker compose -f tests/mcp_server/docker-compose.yml up --build --abort-on-container-exit --exit-code-from test-runner

# pdf-worker (no LLM needed)
docker compose -f tests/pdf_worker/docker-compose.yml up --build --abort-on-container-exit --exit-code-from test-runner

# graph-worker (needs LLAMA_CPP_HOST in .env)
docker compose -f tests/graph_worker/docker-compose.yml up --build --abort-on-container-exit --exit-code-from test-runner

# Full integration (needs LLAMA_CPP_HOST in .env)
docker compose -f tests/integration/docker-compose.yml up --build --abort-on-container-exit --exit-code-from test-runner
```

### Lint and reformat

```bash
docker compose -f lint/docker-compose.yaml up --build --abort-on-container-exit
docker compose -f reformat/docker-compose.yaml up --build --abort-on-container-exit
```

---

## Project structure

```
.
├── Dockerfile                   # mcp-server image
├── docker-compose.yaml          # full local dev stack
├── .env.example                 # copy to .env, set LLAMA_CPP_HOST
│
├── config/
│   ├── ontology.ttl             # OWL schema
│   ├── ingestion_config.yaml    # pipeline parameters
│   ├── llm.yaml                 # LiteLLM provider config ← edit this to swap LLMs
│   └── fuseki-config.ttl        # Fuseki Assembler (OWL inference)
│
├── server/                      # mcp-server: FastMCP tools + admin HTTP
│   ├── main.py                  # 6 MCP tools + /ingest /status /admin/* /health
│   ├── sparql.py                # SPARQL query builders
│   └── status.py                # Redis pipeline-state helpers
│
├── pdf_worker/
│   ├── Dockerfile
│   └── src/main.py              # PDF → Markdown, page-by-page, JPX fallback
│
├── graph_worker/
│   ├── Dockerfile
│   └── src/
│       ├── main.py              # poll loop, Redis lock, per-chunk progress
│       ├── chunker.py           # semantic heading-based Markdown chunker
│       ├── extractor.py         # LiteLLM classifier + combined entity extractor
│       └── mapper.py            # ontology mapper, coreference, triple writer
│
├── dashboard/
│   ├── Dockerfile
│   └── src/app.py               # Streamlit status monitor + submission form
│
├── tests/
│   ├── mcp_server/              # black-box: MCP tools, admin endpoints
│   ├── pdf_worker/              # black-box: PDF → Markdown pipeline
│   ├── graph_worker/            # black-box: Markdown → triples
│   └── integration/             # end-to-end: PDF in → query out
│
├── design/
│   ├── DESIGN.md                # architecture, ontology, pipeline design
│   └── EVALUATION.md            # evaluation queries and scoring
│
└── .github/workflows/ci.yaml    # CI/CD pipeline
```

---

## Ontology

Namespace: `http://campaignsetting.io/ontology#` (prefix: `cs:`)

Key classes: `NPC`, `Location` (→ `City`, `River`, `Nation`, `Region`, `Dungeon`, `Sea`, `Mountain`, `Forest`, `Ruin`, `Plane`), `Faction`, `Religion`, `Deity`, `Race`, `CharacterClass`, `Skill`, `PotentialMotive`, `SourceBook`.

Key properties: `cs:contains` (`owl:TransitiveProperty`) for spatial containment, `cs:mentionedIn` linking entities to `SourceBook` nodes that carry `cs:edition` and `cs:canonType`.

Every result includes `page_reference` and `source_book`. Filtering by edition (`3e`, `4e`, `5e`) and canonicity (`canon`, `kanon`, `community`) works on all tools.

---

## CI/CD

The workflow in `.github/workflows/ci.yaml` runs on every push:

```
reformat → lint
reformat → test (mcp-server suite)
         → detect-changes
              └── publish (main only)
                   └── publish-docs
```

Versioning is driven by `server/__init__.py`. Bump `__version__` above the last git tag to trigger a stable Docker Hub release on the next main push.
