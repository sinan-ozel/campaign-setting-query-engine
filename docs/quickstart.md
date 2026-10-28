# Quickstart

The only local requirement is **Docker**. No Python, no Node, no Fuseki installation needed.

## 1. Clone and configure

```bash
git clone https://github.com/sinan-ozel/campaign-setting-query-engine.git
cd campaign-setting-query-engine
cp .env.example .env
```

Edit `.env` and point it at your LLM:

```bash
LLAMA_CPP_HOST=http://your-gpu-host:8080/v1
```

The engine uses any OpenAI-compatible endpoint. See [Configuration → LLM provider](configuration.md#llm-provider) for Ollama, Mistral, and Anthropic examples.

## 2. Start the stack

```bash
docker compose up --build
```

Services start in dependency order. Fuseki takes ~30 seconds on first boot (TDB2 initialisation). When all services are healthy you'll see log lines from `graph-worker` polling for documents.

| Service | URL |
|---|---|
| Dashboard | http://localhost:8501 |
| MCP server | http://localhost:8000/mcp |
| Fuseki admin | http://localhost:3030 |
| MinIO console | http://localhost:9001 |

## 3. Ingest a sourcebook

Drop a PDF into the dashboard at **http://localhost:8501**, or use the API directly:

```bash
curl -X POST http://localhost:8000/ingest \
  -F "pdf=@eberron_campaign_setting_3e.pdf" \
  -F 'metadata=document_id: eberron_3e
title: "Eberron Campaign Setting (3.5e)"
edition: 3e
canon_type: canon
publisher: "Wizards of the Coast"
tags: [core-rulebook, setting-lore]'
```

The required metadata fields are:

| Field | Values |
|---|---|
| `document_id` | unique slug, e.g. `eberron_3e` |
| `title` | display name |
| `edition` | `3e`, `4e`, `5e`, or `any` |
| `canon_type` | `canon`, `kanon`, or `community` |

Poll for progress:

```bash
curl http://localhost:8000/status/eberron_3e
```

Pipeline states: `PENDING → CONVERTING_PDF → MARKDOWN_READY → CLASSIFYING_SECTIONS → EXTRACTING_ENTITIES → MAPPING_TO_ONTOLOGY → LOADING_GRAPH → COMPLETED`.

A large sourcebook (300+ pages) takes 30–90 minutes depending on LLM speed.

## 4. Connect your agent

Point any MCP client at `http://localhost:8000/mcp`. The server uses streamable-http transport (MCP standard).

Example — Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "campaign-lore": {
      "transport": "streamable-http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Your agent now has access to all [10 MCP tools](mcp-tools.md). Try:

> "List all rivers in Eberron from canon 3e sourcebooks."

> "What is the capital of Breland?"

> "List me d'Cannith family members."

> "Tell me about fashion in Breland."

## Serve a finished graph (read-only)

Ingestion runs in the cloud with a GPU and a larger model. Once it's done, download the graph and serve it locally with no LLM, Redis, MinIO or workers. Your small client model talks to the MCP server; the server never calls an LLM.

1. Download a backup with the `k3s: Download Fuseki backup` task. It writes `backups/campaign_v<major>.<minor>_<timestamp>.nq.gz`, with the version of the images that built the graph.
2. Start serve mode:

    ```bash
    docker compose -f serve/docker-compose.yaml up --build
    ```

    A one-shot loader puts the newest `campaign_v<major>.<minor>_*.nq.gz` into an empty Fuseki, then the MCP server starts with `CSQE_MODE=serve` at `http://localhost:8000/mcp`. Set `BACKUP_FILE=<name>` to pick a specific backup.

3. To switch to another backup, wipe the loaded graph first: `docker compose -f serve/docker-compose.yaml down -v`.

The loader only accepts a backup whose major.minor matches `server/__init__.py`. The MCP tools are built from the ontology, and a minor version bump means the ontology changed, so an older graph would answer questions with the wrong schema. Check out the release that matches your backup instead.

## Teardown

```bash
# Stop without losing data
docker compose down

# Stop and wipe all data (Fuseki graph, Redis state, MinIO objects)
docker compose down -v
```
