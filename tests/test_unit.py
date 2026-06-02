import pytest

pytestmark = pytest.mark.anyio


async def test_unit():
    assert True

async def test_returns_open_status(mcp_tools):
    result = await mcp_tools("is_open_now")
    assert "open" in result
