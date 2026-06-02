import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


@pytest.fixture(scope="session")
def mcp_tools(request):
    try:
        base_url = request.config.getoption("--mcp-tools")
    except ValueError:
        base_url = "http://mcp-server:8000"
    url = base_url.rstrip("/") + "/mcp"

    async def call(tool_name: str, **kwargs) -> dict:
        try:
            async with Client(url) as client:
                result = await client.call_tool(tool_name, kwargs)
        except ToolError as e:
            return {"error": str(e)}
        if result.structured_content is not None:
            return result.structured_content
        for item in result.content:
            if hasattr(item, "text"):
                try:
                    return json.loads(item.text)
                except json.JSONDecodeError:
                    return {"text": item.text}
        return {}

    return call
