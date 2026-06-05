"""Shared fixtures available to all test subfolders."""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


@pytest.fixture(scope="session")
def mcp_server_url(request) -> str:
    """Base URL of the running mcp-server (e.g. http://mcp-server:8000)."""
    try:
        url = request.config.getoption("--mcp-tools")
    except ValueError:
        url = "http://mcp-server:8000"
    return url.rstrip("/")


@pytest.fixture(scope="session")
def mcp_tools(mcp_server_url):
    """Async callable: call an MCP tool by name and return parsed result."""
    url = mcp_server_url + "/mcp"

    async def call(tool_name: str, **kwargs) -> dict:
        try:
            async with Client(url) as client:
                result = await client.call_tool(tool_name, kwargs)
        except ToolError as exc:
            return {"error": str(exc)}
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
