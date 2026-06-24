# Campaign Setting Query Engine

A knowledge graph–backed MCP server for precision lore retrieval from tabletop RPG campaign settings. Built for Eberron. Zero hallucinations — every answer cites a sourcebook page.

> **Canonical evaluation query**: *"List me the rivers of Eberron."*
> Scored on precision + recall + F1 against ground truth extracted from the sourcebooks.

---

## What it does

Drop in your PDF sourcebooks. The pipeline ingests them, extracts entities, and builds a SPARQL knowledge graph. Your AI agent queries it through 10 MCP tools and gets structured answers with page references — no LLM in the query path, no hallucinations.

```
Client / Agent
     │  MCP tools (/mcp)
     ▼
mcp-server  ──SPARQL──▶  Fuseki (OWL graph)
     │
MinIO (/raw-pdfs, /markdown)
     │
     ├── pdf-worker   (PDF → Markdown)
     └── graph-worker (Markdown → triples, LLM)

Redis  (pipeline state + entity dedup index)
```

---

## Quick links

- [Quickstart](quickstart.md) — run locally with Docker in five minutes
- [Helm deployment](helm.md) — deploy to Kubernetes
- [MCP Tools](mcp-tools.md) — all 10 tools your agent can call
- [Admin API](admin-api.md) — ingest, status, and management endpoints
- [Configuration](configuration.md) — every config file and environment variable
- [Ontology](ontology.md) — entity types, properties, and SPARQL namespace
