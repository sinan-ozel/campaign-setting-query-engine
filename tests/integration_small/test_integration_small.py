"""Small-model integration tests.

Run these with a 2B model (e.g. llama.cpp / gemma4:e2b).
Tests cover pipeline completion and basic entity/traversal correctness.
"""

import pytest
from tests.integration_helpers import (
    _FASHION_DESIGNER_ID,
    _LYCANTHROPES_ID,
    _contains_any,
    _names,
    _poll_until_done,
)

pytestmark = pytest.mark.anyio


# ── Pipeline completion ────────────────────────────────────────────────────


async def test_lycanthropes_pipeline_completes():
    doc = await _poll_until_done(_LYCANTHROPES_ID)
    assert doc["status"] == "COMPLETED", (
        f"status={doc.get('status')}, error={doc.get('error')}, "
        f"last_stage={doc.get('last_successful_stage')}"
    )


async def test_fashion_designer_pipeline_completes():
    doc = await _poll_until_done(_FASHION_DESIGNER_ID)
    assert doc["status"] == "COMPLETED", (
        f"status={doc.get('status')}, error={doc.get('error')}, "
        f"last_stage={doc.get('last_successful_stage')}"
    )


# ── Entity extraction ──────────────────────────────────────────────────────


async def test_lycanthropes_yields_race_entity(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "Race"})
    names = _names(result)
    assert _contains_any(names, ("werewolf", "lycanthrope")), (
        f"Expected werewolf/lycanthrope Race; got: {names}"
    )


# ── Graph traversal ────────────────────────────────────────────────────────


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
