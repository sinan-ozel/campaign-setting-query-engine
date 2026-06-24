# Helm Deployment

The Helm chart deploys the full stack — mcp-server, pdf-worker, graph-worker, dashboard, Fuseki, Redis, and MinIO — to any Kubernetes cluster.

## Prerequisites

- Kubernetes 1.24+
- Helm 3.8+
- A Docker Hub account with the published images (or your own registry)
- A default StorageClass that supports `ReadWriteOnce` PVCs (for Fuseki, Redis, MinIO)
- A StorageClass supporting `ReadWriteMany` for the `input` and `chunks` volumes (NFS, EFS, CephFS, etc.)

## Install

```bash
helm install my-csqe oci://ghcr.io/sinan-ozel/charts/campaign-setting-query-engine \
  --version 0.1.0 \
  --set image.organization=sinan-ozel \
  --set fuseki.adminPassword=changeme \
  --set minio.rootPassword=changeme
```

`image.organization` is the only required value — it is your Docker Hub username or org where the four images were published.

## Configuration reference

All tunables live in `values.yaml`. Pass overrides with `--set` or `-f my-values.yaml`.

### Images

| Value | Default | Description |
|---|---|---|
| `image.registry` | `docker.io` | Container registry |
| `image.organization` | *(required)* | Docker Hub org or username |
| `image.tag` | chart `appVersion` | Image tag; defaults to `0.1.0` |
| `image.pullPolicy` | `IfNotPresent` | Kubernetes pull policy |

### MCP server

| Value | Default | Description |
|---|---|---|
| `server.replicaCount` | `1` | Number of replicas |
| `server.logLevel` | `info` | Log level (`debug`, `info`, `warning`, `error`) |
| `server.resources` | `{}` | CPU/memory requests and limits |

### PDF worker

| Value | Default | Description |
|---|---|---|
| `pdfWorker.replicaCount` | `1` | Number of workers |
| `pdfWorker.logLevel` | `info` | Log level |
| `pdfWorker.lockTtlSeconds` | `120` | Redis lock TTL per document |
| `pdfWorker.pollIntervalSeconds` | `5` | MinIO poll interval |
| `pdfWorker.resources` | `{}` | CPU/memory requests and limits |

### Graph worker

| Value | Default | Description |
|---|---|---|
| `graphWorker.replicaCount` | `1` | Number of workers |
| `graphWorker.logLevel` | `info` | Log level |
| `graphWorker.llamaCppHost` | `http://host.docker.internal:8080/v1` | LLM endpoint (override with your provider) |
| `graphWorker.contextWindow` | `8192` | Token budget for chunking and LLM calls |
| `graphWorker.lockTtlSeconds` | `300` | Redis lock TTL per document |
| `graphWorker.pollIntervalSeconds` | `10` | MinIO poll interval |
| `graphWorker.resources` | `{}` | CPU/memory requests and limits |

### Dashboard

| Value | Default | Description |
|---|---|---|
| `dashboard.replicaCount` | `1` | Number of replicas |
| `dashboard.service.type` | `ClusterIP` | Kubernetes service type |
| `dashboard.service.port` | `8501` | Service port |
| `dashboard.ingress.enabled` | `false` | Enable Ingress |
| `dashboard.ingress.host` | `""` | Hostname for Ingress |
| `dashboard.resources` | `{}` | CPU/memory requests and limits |

### Fuseki

| Value | Default | Description |
|---|---|---|
| `fuseki.enabled` | `true` | Deploy bundled Fuseki; set `false` to use an external instance |
| `fuseki.adminUser` | `admin` | Fuseki admin username |
| `fuseki.adminPassword` | `changeme` | Fuseki admin password |
| `fuseki.jvmArgs` | `-Xmx4g` | JVM heap for Fuseki |
| `fuseki.storage.size` | `50Gi` | PVC size for TDB2 data |
| `fuseki.storage.storageClass` | `""` | StorageClass; blank uses cluster default |
| `fuseki.external.endpoint` | `""` | External Fuseki URL when `fuseki.enabled=false` |

### Redis

| Value | Default | Description |
|---|---|---|
| `redis.enabled` | `true` | Deploy bundled Redis; set `false` to use an external instance |
| `redis.storage.size` | `10Gi` | PVC size |
| `redis.external.url` | `""` | External Redis URL when `redis.enabled=false` |

### MinIO

| Value | Default | Description |
|---|---|---|
| `minio.enabled` | `true` | Deploy bundled MinIO; set `false` to use an external S3-compatible store |
| `minio.rootUser` | `minioadmin` | MinIO root user |
| `minio.rootPassword` | `changeme` | MinIO root password |
| `minio.storage.size` | `200Gi` | PVC size |
| `minio.external.endpoint` | `""` | External S3 endpoint when `minio.enabled=false` |
| `minio.external.accessKey` | `""` | Access key when `minio.enabled=false` |
| `minio.external.secretKey` | `""` | Secret key when `minio.enabled=false` |

### Storage for pipeline data

| Value | Default | Description |
|---|---|---|
| `input.storage.size` | `100Gi` | PVC size for the PDF drop folder (ReadWriteMany) |
| `chunks.storage.size` | `50Gi` | PVC size for Markdown scratch space (ReadWriteMany) |

### Config file overrides

The chart ships with default config files baked in. Override any of them with the full file content as a string value:

```bash
helm install my-csqe ... \
  --set-file llmConfig=my-llm.yaml \
  --set-file ontologySchema=my-ontology.yaml
```

| Value | Config file replaced |
|---|---|
| `fusekiConfig` | `config/fuseki-config.ttl` |
| `ontologySchema` | `config/ontology_schema.yaml` |
| `ingestionConfig` | `config/ingestion_config.yaml` |
| `llmConfig` | `config/llm.yaml` |

## Using an external LLM

The graph-worker needs an OpenAI-compatible chat endpoint. Override `graphWorker.llamaCppHost` with your provider:

```bash
# Ollama running in the same cluster
--set graphWorker.llamaCppHost=http://ollama:11434

# OpenAI API (set model in llmConfig override)
--set graphWorker.llamaCppHost=https://api.openai.com/v1
```

For cloud providers (Mistral, Anthropic, OpenAI) you will also need to override `llmConfig` to set the correct model and API key env var substitution.

## Exposing the dashboard

Enable Ingress with a hostname:

```yaml
dashboard:
  ingress:
    enabled: true
    className: nginx
    host: csqe.example.com
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt
    tls:
      - hosts: [csqe.example.com]
        secretName: csqe-tls
```

## Upgrade

```bash
helm upgrade my-csqe oci://ghcr.io/sinan-ozel/charts/campaign-setting-query-engine \
  --version 0.2.0 \
  -f my-values.yaml
```

Fuseki, Redis, and MinIO use `ReadWriteOnce` PVCs that survive upgrades. The knowledge graph is preserved across restarts.

## Uninstall

```bash
helm uninstall my-csqe

# Also delete PVCs (removes all graph data)
kubectl delete pvc -l app.kubernetes.io/instance=my-csqe
```
