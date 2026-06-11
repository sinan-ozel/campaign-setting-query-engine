"""Black-box tests for the pdf-worker service.

Tests seed MinIO /raw-pdfs/ directly and assert on MinIO /markdown/ and Redis
state. No Fuseki or LLM dependency.
"""

import pytest

pytestmark = pytest.mark.anyio


async def test_placeholder():
    """Replace with real tests once fixture PDFs are available."""
    assert True
