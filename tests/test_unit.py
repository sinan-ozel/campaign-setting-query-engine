"""Unit tests that run without any containers.

Place fast, import-level tests here. Integration and service tests live in
tests/mcp_server/, tests/pdf_worker/, tests/graph_worker/, tests/integration/.
"""

import pytest

pytestmark = pytest.mark.anyio


async def test_placeholder():
    assert True
