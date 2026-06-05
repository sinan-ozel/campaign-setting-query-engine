"""End-to-end tests: PDF in → queryable knowledge graph out.

Requires the full stack (Fuseki, Redis, MinIO, pdf-worker, graph-worker,
mcp-server) and a reachable LLM endpoint (LLAMA_CPP_HOST).
"""

import pytest

pytestmark = pytest.mark.anyio


async def test_placeholder():
    """Replace with real tests once fixture PDFs are available."""
    assert True
