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

### Ingestion pipeline

| Value | Default | Description |
|---|---|---|
| `ingest.enabled` | `true` | Deploy `pdf-worker` and `graph-worker`. Set `false` for a query-only stack (see [Deploying on AWS with a GPU node](#deploying-on-aws-with-a-gpu-node)) |

### MCP server

| Value | Default | Description |
|---|---|---|
| `server.replicaCount` | `1` | Number of replicas |
| `server.logLevel` | `info` | Log level (`debug`, `info`, `warning`, `error`) |
| `server.resources` | `{}` | CPU/memory requests and limits |
| `server.ingress.enabled` | `false` | Enable Ingress |
| `server.ingress.host` | `""` | Hostname; blank defaults to `mcp.<domain>` when `domain` is set |
| `server.ingress.className` / `.annotations` / `.tls` | | Same semantics as `dashboard.ingress` below |

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

### LLM server (GPU node)

Self-hosted OpenAI-compatible llama.cpp server, replacing an external
`graphWorker.llamaCppHost`. Scheduled onto whichever node k3s-anywhere's GPU
cloud-init labels `k3s-anywhere.io/gpu-node=true`.

| Value | Default | Description |
|---|---|---|
| `llmServer.enabled` | `false` | Deploy the in-cluster LLM server (and the NVIDIA device plugin DaemonSet) |
| `llmServer.image.repository` / `.tag` | `sinanozel/llama.cuda` / `qwen3.5-9b-q4km` | Image with the model baked in — swap the tag to try a different model |
| `llmServer.port` | `8080` | Container/service port |
| `llmServer.extraArgs` | `[]` | Extra CLI args passed to the server |
| `llmServer.nodeSelector` | `{k3s-anywhere.io/gpu-node: "true"}` | Where the LLM server (and device plugin) get scheduled |
| `llmServer.resources.limits` | `{nvidia.com/gpu: 1}` | GPU allocation |

### Dashboard

| Value | Default | Description |
|---|---|---|
| `dashboard.replicaCount` | `1` | Number of replicas |
| `dashboard.service.type` | `ClusterIP` | Kubernetes service type |
| `dashboard.service.port` | `8501` | Service port |
| `dashboard.ingress.enabled` | `false` | Enable Ingress |
| `dashboard.ingress.host` | `""` | Hostname; blank defaults to `domain` when set |
| `dashboard.resources` | `{}` | CPU/memory requests and limits |

### MCP Inspector

| Value | Default | Description |
|---|---|---|
| `inspector.enabled` | `false` | Deploy the [MCP Inspector](https://github.com/modelcontextprotocol/inspector), pointed at this release's mcp-server |
| `inspector.ingress.enabled` | `false` | Enable Ingress |
| `inspector.ingress.host` | `""` | Hostname; blank defaults to `inspector.<domain>` |

Auth is always on (no `DANGEROUSLY_OMIT_AUTH` — that's a local-docker-compose
convenience only). Get the session token with:
```bash
kubectl logs deploy/<release>-inspector
```

### Public domain + TLS

| Value | Default | Description |
|---|---|---|
| `domain` | `""` | Base hostname for server/dashboard/inspector Ingress resources when their own `.host` is blank |
| `certManager.enabled` | `false` | Auto-annotate Ingress resources for cert-manager and create a `ClusterIssuer` |
| `certManager.issuer.email` | `""` | ACME account email — required when `certManager.enabled=true` |
| `certManager.issuer.server` | Let's Encrypt **staging** | ACME server URL. Staging by default; switch to `https://acme-v02.api.letsencrypt.org/directory` for a trusted cert on a long-lived deployment |

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

## Deploying on AWS with a GPU node

`infrastructure/`, `deploy/`, `restore/`, and `.github/workflows/{provision,teardown,deploy,delete}.yaml`
stand up this chart on a k3s cluster provisioned by
[k3s-anywhere](https://github.com/sinan-ozel/k3s-anywhere), with one GPU node
running `llmServer` in place of an external LLM. Every step has a local
`.vscode/tasks.json` task (`k3s: ...`) and a matching GitHub Actions
workflow, so local testing and CI automation stay identical.

There's no external DNS — reachability is via the AWS Elastic IP through a
[nip.io](https://nip.io) hostname (`<ip-with-dashes>.nip.io`, which resolves
any subdomain back to the embedded IP with no DNS records needed), so
cert-manager can still issue real Let's Encrypt certs over HTTP-01 through
Traefik (k3s's built-in ingress controller).

Rough order of operations (see each workflow/task for details):

1. `k3s: First-time setup (AWS)` — once per AWS account.
2. `k3s: Provision (AWS)` (or the `provision.yaml` workflow) — creates the
   cluster, including the GPU node.
3. `k3s: Deploy` (or the `deploy.yaml` workflow) — restores any existing
   backup, installs cert-manager, computes the nip.io domain from the
   cluster's Elastic IP, and `helm upgrade --install`s this chart with
   `llmServer.enabled=true`, `inspector.enabled=true`, and Ingress enabled
   on all three of dashboard/mcp-server/inspector.
4. `k3s: Teardown (AWS)` (or the `teardown.yaml` workflow) — backs up
   `fuseki-data`/`redis-data`/`minio-data` to S3, then destroys the cluster.

### Moving the finished graph to cheap infrastructure

No export/import file format — the same Longhorn backup taken at teardown
(step 4 above) is the portability mechanism, since it snapshots
`redis-data` right alongside `fuseki-data` (so ingestion-status/dedup/type
state comes along with the graph, not just the TDB2 data). To run a
finished graph on cheap CPU-only infrastructure once ingestion is done:

1. Provision a second cluster with `GPU_NODE_COUNT=0` pointed at the
   *same* `STATE_BUCKET_NAME` (so it shares the backup bucket).
2. Run `restore/` (`k3s: Restore from backup` task, or `deploy.yaml`'s
   `restore` job) against it — this rehydrates the three PVCs from the
   last backup.
3. `helm upgrade --install` with `--set ingest.enabled=false --set
   llmServer.enabled=false` — `pdf-worker`, `graph-worker`, and the GPU LLM
   server never get scheduled; Fuseki/mcp-server/dashboard come up already
   populated.

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
