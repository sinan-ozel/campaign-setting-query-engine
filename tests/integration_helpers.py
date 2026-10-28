"""Shared helpers for integration test suites."""

import asyncio
import os
import time

import httpx

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000")
PIPELINE_TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "300"))
POLL_INTERVAL = 5

_SIMPLE_PSIONICS_ID = "simple-psionics"
_LYCANTHROPES_ID = "lycanthropes-in-eberron"
_FASHION_DESIGNER_ID = "fashiondesigner"


async def _poll_until_done(
    document_id: str, timeout: int = PIPELINE_TIMEOUT
) -> dict:
    """Poll /status/{document_id} until COMPLETED or FAILED, or raise
    TimeoutError."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            resp = await client.get(
                f"{MCP_SERVER_URL}/status/{document_id}", timeout=10
            )
            if resp.status_code == 200:
                doc = resp.json()
                if doc.get("status") in ("COMPLETED", "FAILED"):
                    return doc
            await asyncio.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"Document {document_id!r} did not reach COMPLETED|FAILED in {timeout}s"
    )


async def _poll_until_completed(
    document_id: str, timeout: int = PIPELINE_TIMEOUT
) -> bool:
    """Poll /status/{document_id} until COMPLETED.

    Returns True when COMPLETED, False on FAILED or timeout.
    """
    try:
        doc = await _poll_until_done(document_id, timeout)
    except TimeoutError:
        return False
    return doc.get("status") == "COMPLETED"


def _names(result: dict) -> list[str]:
    """Lowercased names from a list_names/search_by_property result."""
    return [n.lower() for n in result.get("names", [])]


def _contains_any(haystack: list[str], needles: tuple[str, ...]) -> bool:
    return any(needle in name for name in haystack for needle in needles)


async def _all_names(mcp_tools, entity_type: str) -> list[str]:
    """Every name of a type, following list_names paging."""
    names: list[str] = []
    offset = 0
    while True:
        page = await mcp_tools(
            "list_names",
            input={"entity_type": entity_type, "limit": 200, "offset": offset},
        )
        names.extend(page.get("names", []))
        if "next_offset" not in page:
            return names
        offset = page["next_offset"]


async def _sources_by_name(mcp_tools, names: list[str]) -> dict[str, list]:
    """{name: sources} from get_sources, 10 names per call."""
    out: dict[str, list] = {}
    for i in range(0, len(names), 10):
        result = await mcp_tools(
            "get_sources", input={"names": names[i : i + 10]}
        )
        for row in result.get("results", []):
            if "name" in row:
                out[row["name"]] = row.get("sources", [])
    return out
