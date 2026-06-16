"""Small-model integration tests.

Run these with a 2B model (e.g. llama.cpp / gemma4:e2b).
Tests cover pipeline completion and basic entity/traversal correctness.
"""

import pytest
from tests.integration_helpers import (
    PIPELINE_TIMEOUT,
    _FASHION_DESIGNER_ID,
    _LYCANTHROPES_ID,
    _contains_any,
    _names,
    _poll_mcp_until_listed,
)

pytestmark = pytest.mark.anyio


# ── Book ingestion gates ───────────────────────────────────────────────────
# Each test waits via list_completed_documents until the book is ready.
# All entity/traversal tests declare a dependency on the relevant gate.


@pytest.mark.depends(name="lycanthropes_ingested_small")
async def test_lycanthropes_ingested(mcp_tools):
    found = await _poll_mcp_until_listed(_LYCANTHROPES_ID, mcp_tools)
    assert found, (
        f"'{_LYCANTHROPES_ID}' did not appear in list_completed_documents "
        f"within {PIPELINE_TIMEOUT}s"
    )


@pytest.mark.depends(name="fashion_designer_ingested_small")
async def test_fashion_designer_ingested(mcp_tools):
    found = await _poll_mcp_until_listed(_FASHION_DESIGNER_ID, mcp_tools)
    assert found, (
        f"'{_FASHION_DESIGNER_ID}' did not appear in list_completed_documents "
        f"within {PIPELINE_TIMEOUT}s"
    )


# ── Entity extraction ──────────────────────────────────────────────────────


@pytest.mark.depends(on=["lycanthropes_ingested_small"])
async def test_lycanthropes_yields_race_entity(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "Race"})
    names = _names(result)
    assert _contains_any(names, ("werewolf", "lycanthrope")), (
        f"Expected werewolf/lycanthrope Race; got: {names}"
    )


# ── Graph traversal ────────────────────────────────────────────────────────


@pytest.mark.depends(on=["lycanthropes_ingested_small"])
async def test_werewolf_entity_full_property_traversal(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "Race"})
    candidates = [
        r["name"]
        for r in result.get("results", [])
        if "werewolf" in r["name"].lower() or "lycanthrope" in r["name"].lower()
    ]
    assert candidates, "No werewolf/lycanthrope Race entity found for traversal"

    entity = await mcp_tools(
        "get_entity", input={"name": candidates[0], "depth": "full"}
    )
    assert "error" not in entity, f"get_entity failed: {entity}"
    assert any(
        entity.get(k)
        for k in ("canonicalName", "label", "source_book", "description", "sourceText")
    ), f"Werewolf entity has no populated properties: {entity}"
