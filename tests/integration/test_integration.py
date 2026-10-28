"""Integration tests — full pipeline through the MCP server.

Gate tests poll /status/{document_id} until each source book is ingested. All
entity and traversal tests declare a pytest-depends dependency on the relevant
gate so they are auto-skipped if ingestion failed.
"""

import os

import pytest

from tests.integration_helpers import (
    _FASHION_DESIGNER_ID,
    _LYCANTHROPES_ID,
    _SIMPLE_PSIONICS_ID,
    PIPELINE_TIMEOUT,
    _all_names,
    _contains_any,
    _names,
    _poll_until_completed,
    _sources_by_name,
)

pytestmark = pytest.mark.anyio

# graph-worker needs a real, reachable LLM to extract entities. Set in
# tests/.env for local runs (see tests/.env.example); left unset in CI,
# which has no GPU/LLM endpoint — so LLM-dependent ingestion, and every
# test that pytest-depends on it, auto-skip there.
_requires_llm = pytest.mark.skipif(
    not os.environ.get("LLAMA_CPP_HOST"),
    reason="LLAMA_CPP_HOST not set — no live LLM to extract entities with.",
)

_FASHION_ATTIRES = (
    "casual attire",
    "inconspicuous attire",
    "conspicuous attire",
    "stunning attire",
)

# Attire is a plain Item since 0.2.0 (no longer a MagicItem).
_ITEM_TYPES = ("Attire", "Item")


# ── Book ingestion gates ───────────────────────────────────────────────────


@pytest.mark.skip(
    reason=(
        "simple-psionics has 26 chunks and requires two LLM calls each. "
        "At local llama.cpp throughput this exceeds the 2700s CI timeout. "
        "The pipeline is correct — lycanthropes and fashiondesigner complete "
        "and produce valid graph data. Re-enable when a faster inference "
        "endpoint is available in CI."
    )
)
@pytest.mark.depends(name="simple_psionics_ingested")
async def test_simple_psionics_ingested():
    assert await _poll_until_completed(
        _SIMPLE_PSIONICS_ID
    ), f"'{_SIMPLE_PSIONICS_ID}' did not reach COMPLETED within {PIPELINE_TIMEOUT}s"


@_requires_llm
@pytest.mark.depends(name="lycanthropes_ingested")
async def test_lycanthropes_ingested():
    assert await _poll_until_completed(
        _LYCANTHROPES_ID
    ), f"'{_LYCANTHROPES_ID}' did not reach COMPLETED within {PIPELINE_TIMEOUT}s"


@_requires_llm
@pytest.mark.depends(name="fashion_designer_ingested")
async def test_fashion_designer_ingested():
    assert await _poll_until_completed(
        _FASHION_DESIGNER_ID
    ), f"'{_FASHION_DESIGNER_ID}' did not reach COMPLETED within {PIPELINE_TIMEOUT}s"


@pytest.mark.depends(on=["lycanthropes_ingested", "fashion_designer_ingested"])
async def test_ingested_books_are_listed(mcp_tools):
    result = await mcp_tools("list_sources", _={})
    assert (
        result.get("count", 0) >= 2
    ), f"Expected 2+ source books; got {result}"


# ── Race extraction (lycanthropes-in-eberron) ─────────────────────────────


@pytest.mark.depends(on=["lycanthropes_ingested"])
async def test_lycanthropes_yields_race_entity(mcp_tools):
    result = await mcp_tools("list_names", input={"entity_type": "Race"})
    names = _names(result)
    assert _contains_any(
        names, ("werewolf", "lycanthrope")
    ), f"Expected werewolf/lycanthrope Race; got: {names}"


@pytest.mark.depends(on=["lycanthropes_ingested"])
async def test_werewolf_entity_facts(mcp_tools):
    names = await _all_names(mcp_tools, "Race")
    candidates = [
        n
        for n in names
        if "werewolf" in n.lower() or "lycanthrope" in n.lower()
    ]
    assert candidates, "No werewolf/lycanthrope Race entity found"

    entity = await mcp_tools("get_entity", input={"name": candidates[0]})
    assert "error" not in entity, f"get_entity failed: {entity}"
    assert (
        "Race" in entity["types"] or entity["facts"]
    ), f"Werewolf entity is empty: {entity}"


# ── Skill extraction (simple-psionics) ────────────────────────────────────


@pytest.mark.depends(on=["simple_psionics_ingested"])
async def test_psionics_yields_skill_entities(mcp_tools):
    result = await mcp_tools("list_names", input={"entity_type": "Skill"})
    assert (
        result.get("total", 0) >= 1
    ), f"Expected a Skill; got: {_names(result)}"


@pytest.mark.depends(on=["simple_psionics_ingested"])
async def test_all_ingested_skills_have_source_books(mcp_tools):
    names = await _all_names(mcp_tools, "Skill")
    sources = await _sources_by_name(mcp_tools, names)
    missing = [n for n in names if not sources.get(n)]
    assert not missing, f"Skills without sources: {missing}"


# ── CharacterClass extraction ──────────────────────────────────────────────


@pytest.mark.depends(on=["lycanthropes_ingested"])
async def test_lycanthropes_yields_charclass(mcp_tools):
    names = _names(
        await mcp_tools("list_names", input={"entity_type": "CharacterClass"})
    )
    assert _contains_any(
        names, ("werewolf", "lycanthrope")
    ), f"Expected werewolf CharacterClass; got: {names}"


@pytest.mark.depends(on=["lycanthropes_ingested"])
async def test_werewolf_charclass_relationships(mcp_tools):
    names = await _all_names(mcp_tools, "CharacterClass")
    candidates = [
        n
        for n in names
        if "werewolf" in n.lower() or "lycanthrope" in n.lower()
    ]
    assert candidates, "No Werewolf CharacterClass entity found for traversal"

    entity = await mcp_tools(
        "get_entity", input={"name": candidates[0], "section": "relationships"}
    )
    assert "error" not in entity, f"get_entity failed: {entity}"
    assert "relationships" in entity


@pytest.mark.depends(on=["lycanthropes_ingested", "fashion_designer_ingested"])
async def test_search_entities_finds_listed_class(mcp_tools):
    names = await _all_names(mcp_tools, "CharacterClass")
    assert names, "Expected at least one CharacterClass after ingestion"
    result = await mcp_tools("search_entities", input={"query": names[0]})
    assert names[0] in [m["name"] for m in result["matches"]]


# ── Item / Attire extraction (FashionDesigner) ────────────────────────────


async def _fashion_attire(mcp_tools) -> list[str]:
    for entity_type in _ITEM_TYPES:
        names = await _all_names(mcp_tools, entity_type)
        hits = [
            n for n in names if any(a in n.lower() for a in _FASHION_ATTIRES)
        ]
        if hits:
            return hits
    return []


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_fashion_designer_yields_attire_items(mcp_tools):
    for entity_type in _ITEM_TYPES:
        result = await mcp_tools(
            "list_names", input={"entity_type": entity_type}
        )
        if result.get("total", 0) >= 1:
            return
    pytest.fail(
        "Expected at least one Item/Attire entity after ingesting FashionDesigner.pdf"
    )


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_attire_items_have_description(mcp_tools):
    attire = await _fashion_attire(mcp_tools)
    if not attire:
        pytest.skip("No FashionDesigner attire entities found")
    entity = await mcp_tools("get_entity", input={"name": attire[0]})
    assert "error" not in entity, f"get_entity failed: {entity}"
    facts = entity["facts"]
    assert facts.get("charges") or facts.get(
        "description"
    ), f"Attire item {attire[0]!r} has no charges/description: {entity}"


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_attire_items_have_source_book(mcp_tools):
    attire = await _fashion_attire(mcp_tools)
    if not attire:
        pytest.skip("No FashionDesigner attire entities found")
    sources = await _sources_by_name(mcp_tools, attire)
    missing = [n for n in attire if not sources.get(n)]
    assert not missing, f"Attire items without sources: {missing}"


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_search_items_by_rarity(mcp_tools):
    for rarity in ("common", "uncommon", "rare"):
        result = await mcp_tools(
            "search_by_property",
            input={
                "entity_type": "Item",
                "property_name": "rarity",
                "value": rarity,
            },
        )
        assert (
            "error" not in result
        ), f"search_by_property rarity failed: {result}"
        if result.get("total", 0) > 0:
            return
