# MCP Inspector

This directory contains a Docker Compose configuration for running the MCP server alongside the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) for visual testing and debugging.

## Services

- **mcp-server**: Your MCP server, built from the root `Dockerfile`, serving at `http://localhost:8000/mcp`
- **mcp-inspector**: Visual tool for testing and inspecting MCP tools in a browser UI

## Usage

### Start both services

```bash
docker compose -f inspector/docker-compose.yaml up --build
```

Or use the VS Code task: **Terminal → Run Task → inspector**

### Open the Inspector UI

Check the docker logs for the full URL with the auth token:

```
http://localhost:6274/?MCP_PROXY_AUTH_TOKEN=<token>
```

- **MCP Inspector UI**: http://localhost:6274 (copy full URL with token from logs)
- **MCP Server**: http://localhost:8000
- **MCP Endpoint**: http://localhost:8000/mcp

### Stop the services

```bash
docker compose -f inspector/docker-compose.yaml down
```

## MCP Transport

The server uses the **Streamable HTTP** transport (the current MCP standard). The MCP endpoint is at `/mcp` (POST for requests). The inspector is pre-configured to connect to `http://mcp-server:8000/mcp` within the Docker network.

## Testing the MCP Endpoint Directly

```bash
# Initialize the MCP session
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "curl", "version": "0.1"}}, "id": 1}'
```
