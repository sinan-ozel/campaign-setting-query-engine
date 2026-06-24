# Configuration

All configuration lives in `config/` and is version-controlled. In Kubernetes, these files are mounted as a ConfigMap. In Docker Compose, they are volume-mounted read-only.

---

## LLM provider

**File**: `config/llm.yaml`

Configures the LiteLLM provider used by the graph-worker for entity extraction. All keys except `model` are passed directly to `litellm.completion()`.

```yaml
# llama.cpp / any OpenAI-compatible endpoint (default)
api_base: ${LLAMA_CPP_HOST}
model: openai/gemma4:e2b
api_key: dummy
timeout: 150
```

Swap providers by editing this file — no code changes needed:

=== "Ollama"

    ```yaml
    api_base: http://ollama:11434
    model: ollama/gemma3:27b
    ```

=== "Mistral API"

    ```yaml
    api_base: https://api.mistral.ai
    model: mistral/codestral-2501
    api_key: ${MISTRAL_API_KEY}
    ```

=== "OpenAI"

    ```yaml
    model: gpt-4o
    api_key: ${OPENAI_API_KEY}
    ```

=== "Anthropic"

    ```yaml
    model: anthropic/claude-haiku-4-5-20251001
    api_key: ${ANTHROPIC_API_KEY}
    ```

Values matching `${VAR_NAME}` are substituted from environment variables at startup. In Docker Compose set them in `.env`. In Kubernetes, inject them as env vars on the graph-worker pod.

`max_tokens` is controlled per-call by the pipeline (`contextWindow / 4`) and is not set here.

---

## Ontology schema

**File**: `config/ontology_schema.yaml`

Defines every entity type the pipeline can extract and how properties map to RDF triples. Adding a new entity type or a new property field requires only a YAML edit — no Python changes.

See [Ontology](ontology.md) for the full class and property reference.

---

## Ingestion parameters

**File**: `config/ingestion_config.yaml`

Controls chunking and extraction behaviour. The defaults are tuned for 8 192-token context windows.

---

## Fuseki dataset

**File**: `config/fuseki-config.ttl`

Apache Jena Fuseki Assembler configuration. Defines the TDB2 persistent dataset and enables `tdb2:unionDefaultGraph` so SPARQL queries see triples across all named graphs.

---

## Environment variables

### graph-worker

| Variable | Default | Description |
|---|---|---|
| `FUSEKI_ENDPOINT` | `http://localhost:3030/campaign` | Fuseki SPARQL endpoint |
| `FUSEKI_USER` | `admin` | Fuseki admin username |
| `FUSEKI_PASSWORD` | *(none)* | Fuseki admin password |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `MINIO_ENDPOINT` | `http://localhost:9000` | MinIO endpoint |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `LLAMA_CPP_HOST` | `http://host.docker.internal:8080/v1` | LLM base URL (also substituted into `llm.yaml`) |
| `CONTEXT_WINDOW` | `4096` | Token budget for chunking and LLM calls |
| `LOCK_TTL_SECONDS` | `300` | Redis lock TTL per document |
| `POLL_INTERVAL_SECONDS` | `10` | Seconds between MinIO polls |
| `INGESTION_CONFIG_PATH` | `/config/ingestion_config.yaml` | Path to ingestion config |
| `LLM_CONFIG_PATH` | `/config/llm.yaml` | Path to LLM config |
| `ONTOLOGY_SCHEMA_PATH` | `/config/ontology_schema.yaml` | Path to ontology schema |
| `LOG_LEVEL` | `INFO` | Log level |

### pdf-worker

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `MINIO_ENDPOINT` | `http://localhost:9000` | MinIO endpoint |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `INPUT_DIR` | `/input` | Directory to watch for PDFs |
| `LOCK_TTL_SECONDS` | `120` | Redis lock TTL per document |
| `POLL_INTERVAL_SECONDS` | `5` | Seconds between input directory polls |
| `LOG_LEVEL` | `INFO` | Log level |

### mcp-server

| Variable | Default | Description |
|---|---|---|
| `FUSEKI_ENDPOINT` | `http://localhost:3030/campaign` | Fuseki SPARQL endpoint |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `MINIO_ENDPOINT` | `http://localhost:9000` | MinIO endpoint |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `ONTOLOGY_SCHEMA_PATH` | `/config/ontology_schema.yaml` | Path to ontology schema |
| `LOG_LEVEL` | `info` | Log level |

---

## Docker Compose vs Kubernetes

In Docker Compose, `LLAMA_CPP_HOST` is the only variable you typically need to set — put it in `.env` and copy from `.env.example`.

In Kubernetes (Helm), use `--set graphWorker.llamaCppHost=...` or override `llmConfig` with a full provider config file. Passwords for Fuseki and MinIO are stored in Kubernetes Secrets and injected automatically.
