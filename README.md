![CI/CD](https://github.com/<ORGANIZATION>/<SERVER-NAME>/actions/workflows/ci.yaml/badge.svg?branch=main)
![Docker Hub](https://img.shields.io/docker/v/<ORGANIZATION>/<SERVER-NAME>?label=Docker%20Hub)
![License](https://img.shields.io/github/license/<ORGANIZATION>/<SERVER-NAME>.svg)

# MCP Server Template

A production-ready template for building [Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers in Python. Everything runs through Docker Compose — no local Python installation required beyond Docker itself.

## What's Included

- **MCP server skeleton** — `server/main.py` using [FastMCP](https://github.com/jlowin/fastmcp) with **Streamable HTTP + SSE** transport on port 8000
- **Containerized tooling** — reformat, lint, validate-docs, and test all run via `docker compose`
- **MCP Inspector** — visual browser UI for testing and debugging your tools
- **Automated CI/CD** — GitHub Actions that reformat on branches, lint + test on every push, publish to Docker Hub on main
- **SemVer releases** — stable vs. dev version logic driven by `server/__init__.py`
- **VS Code tasks** — one-click access to every workflow from the Command Palette

---

## Quickstart

### 1. Create a repo from this template

Click **"Use this template"** on GitHub.

### 2. Replace placeholders

Find and replace these strings across the entire repo:

| Placeholder | Replace with | Used in |
|---|---|---|
| `<SERVER-NAME>` | `my-mcp-server` (hyphenated) | Docker image names, GitHub URLs, workflow env |
| `<SERVER_NAME>` | `my_mcp_server` (underscored) | Not currently used — reserved if you rename `server/` |
| `<ORGANIZATION>` | Your GitHub username or org | URLs, badges |

Run this to find all occurrences:
```bash
grep -r "<SERVER-NAME>\|<ORGANIZATION>" --include="*.yaml" --include="*.toml" --include="*.md" --include="*.py" .
```

### 3. Rename `server/` to your module name (optional but recommended)

```bash
mv server/ my_mcp_server/
# Then update every reference to `server/` in:
#   Dockerfile, reformat/Dockerfile, lint/Dockerfile, docs-validate/Dockerfile,
#   tests/Dockerfile, tests/docker-compose.yaml, reformat/reformat.sh,
#   lint/lint.sh, pyproject.toml ([tool.setuptools.packages.find] and
#   [tool.setuptools.dynamic]), .github/workflows/ci.yaml (Get Current Version step)
```

### 4. Write your version into `server/__init__.py`

```python
__version__ = "0.1.0"
```

### 5. Fill in `pyproject.toml`

- Set `name`, `description`, `authors`
- Update `[project.urls]`

### 6. Add your MCP tools in `server/main.py`

```python
from fastmcp import FastMCP

mcp = FastMCP("my-mcp-server")

@mcp.tool()
def my_tool(param: str) -> str:
    """Do something useful."""
    return f"Result: {param}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
```

### 7. Set up Docker Hub secrets in GitHub

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | A Docker Hub access token (not your password) |

### 8. (Optional) Set up GitHub Pages for docs

Go to **Settings → Pages → Deploy from a branch**: `gh-pages`, `/` (root).

---

## MCP Transport

The server uses **Streamable HTTP** transport (the current MCP standard). The endpoint is at:

```
http://localhost:8000/mcp
```

Compatible with all MCP clients that support HTTP transport (Claude Desktop, Claude.ai, Cursor, etc.).

---

## Development

The only requirement is **Docker**.

### Run the tests

```bash
docker compose -f tests/docker-compose.yaml up --build --abort-on-container-exit --exit-code-from test
```

This starts the MCP server, waits for it to be healthy, then runs pytest against it with `pytest-mcp-tools`.

### Lint

```bash
docker compose -f lint/docker-compose.yaml up --build --abort-on-container-exit
```

### Reformat

```bash
docker compose -f reformat/docker-compose.yaml up --build --abort-on-container-exit
```

Reformatting runs `black`, `docformatter`, and `isort` on `server/` and `tests/`, then writes changes back to disk (via volume mount). On non-main branches CI commits these changes automatically.

### Validate docs

```bash
docker compose -f docs-validate/docker-compose.yaml up --build --abort-on-container-exit
```

### Run the MCP Inspector

```bash
docker compose -f inspector/docker-compose.yaml up --build
```

Then open the URL printed in the logs (includes the auth token):
```
http://localhost:6274/?MCP_PROXY_AUTH_TOKEN=<token>
```

---

## VS Code Tasks

Open **Terminal → Run Task** (or `Ctrl+Shift+P → Tasks: Run Task`):

| Task | What it does |
|---|---|
| `test` | Runs the full test suite (server + test runner containers) |
| `lint` | Runs ruff against `server/` and `tests/` |
| `reformat` | Formats code with black + docformatter + isort |
| `validate-docs` | Builds MkDocs site with `--strict` |
| `inspector` | Starts the server + MCP Inspector (leaves running in background) |

---

## CI/CD Pipeline

The workflow at [.github/workflows/ci.yaml](.github/workflows/ci.yaml) runs on every push and pull request:

```
push (any branch)
│
├── reformat        ← runs black/isort/docformatter
│   └── (commits reformatted code back, non-main branches only)
│
├── lint            ← ruff check (needs: reformat)
├── test            ← pytest --mcp-tools (needs: reformat)
├── validate-docs   ← mkdocs build --strict
└── detect-changes  ← checks if server/ or README changed
    │
    └── publish (main only, when changed)
        ├── Build & push Docker image to Docker Hub
        │   ├── stable:  <org>/<server>:1.2.3  +  :latest
        │   └── dev:     <org>/<server>:1.2.4.dev202401011200
        ├── Tag stable release in git
        ├── Create GitHub Release
        └── publish-docs (stable + docs exist)
            └── mike deploy to GitHub Pages
```

### Versioning

Version is read from `server/__init__.py`. The logic:

- If `__version__` > last git tag → **stable release** (tags git, pushes `:latest`)
- If `__version__` == last git tag → **dev release** (appends `.devYYYYMMDDHHMM`, no `:latest`)

Bump `__version__` in `server/__init__.py` to trigger a stable release on the next main push.

---

## Project Structure

```
.
├── Dockerfile                   # Builds and runs the MCP server
├── pyproject.toml               # Project metadata, dependencies, tool config
├── server/
│   ├── __init__.py              # __version__ = "x.y.z"
│   └── main.py                  # FastMCP server — add your tools here
├── tests/
│   ├── Dockerfile               # Test runner image
│   ├── docker-compose.yaml      # mcp-server + test-runner services
│   └── test_unit.py             # Placeholder — add your tests here
├── reformat/                    # black + docformatter + isort container
├── lint/                        # ruff container
├── docs-validate/               # mkdocs build container
├── inspector/                   # MCP Inspector + server for visual testing
├── scripts/
│   └── semver_compare.py        # Used by CI to compare versions
└── .github/workflows/ci.yaml    # Full CI/CD pipeline
```
