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


async def _poll_until_done(document_id: str, timeout: int = PIPELINE_TIMEOUT) -> dict:
    """Poll /status/{document_id} until COMPLETED or FAILED, or raise TimeoutError."""
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


def _names(result: dict) -> list[str]:
    return [r["name"].lower() for r in result.get("results", [])]


def _contains_any(haystack: list[str], needles: tuple[str, ...]) -> bool:
    return any(needle in name for name in haystack for needle in needles)
