"""Black-box tests for the graph-worker service.

Tests seed MinIO /markdown/ directly (bypassing the pdf-worker) and assert on
Fuseki and Redis. Requires LLAMA_CPP_HOST to be set.
"""

import pytest

pytestmark = pytest.mark.anyio


async def test_placeholder():
    """Replace with real tests once fixture Markdown files are available."""
    assert True
