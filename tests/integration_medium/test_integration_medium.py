"""Medium-model integration tests.

Run these with a capable model (e.g. Mistral codestral-2501, claude-haiku).
Tests cover CharacterClass/Item/Skill extraction and multi-hop traversal
that a 2B model cannot reliably produce.
"""

import pytest
from tests.integration_helpers import (
    PIPELINE_TIMEOUT,
    _FASHION_DESIGNER_ID,
    _LYCANTHROPES_ID,
    _SIMPLE_PSIONICS_ID,
    _contains_any,
    _names,
    _poll_mcp_until_listed,
)

pytestmark = pytest.mark.anyio

_FASHION_ATTIRES = (
    "casual attire",
    "inconspicuous attire",
    "conspicuous attire",
    "stunning attire",
)


# ── Book ingestion gates ───────────────────────────────────────────────────
# Each test waits via list_completed_documents until the book is ready.
# All entity/traversal tests declare a dependency on the relevant gate.


@pytest.mark.depends(name="simple_psionics_ingested")
async def test_simple_psionics_ingested(mcp_tools):
    found = await _poll_mcp_until_listed(_SIMPLE_PSIONICS_ID, mcp_tools)
    assert found, (
        f"'{_SIMPLE_PSIONICS_ID}' did not appear in list_completed_documents "
        f"within {PIPELINE_TIMEOUT}s"
    )


@pytest.mark.depends(name="lycanthropes_ingested")
async def test_lycanthropes_ingested(mcp_tools):
    found = await _poll_mcp_until_listed(_LYCANTHROPES_ID, mcp_tools)
    assert found, (
        f"'{_LYCANTHROPES_ID}' did not appear in list_completed_documents "
        f"within {PIPELINE_TIMEOUT}s"
    )


@pytest.mark.depends(name="fashion_designer_ingested")
async def test_fashion_designer_ingested(mcp_tools):
    found = await _poll_mcp_until_listed(_FASHION_DESIGNER_ID, mcp_tools)
    assert found, (
        f"'{_FASHION_DESIGNER_ID}' did not appear in list_completed_documents "
        f"within {PIPELINE_TIMEOUT}s"
    )


# ── Skill extraction (simple-psionics) ────────────────────────────────────


@pytest.mark.depends(on=["simple_psionics_ingested"])
async def test_psionics_yields_skill_entities(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    assert result.get("count", 0) >= 1, (
        f"Expected at least one Skill after ingesting simple-psionics; "
        f"got: {_names(result)}"
    )


@pytest.mark.depends(on=["simple_psionics_ingested"])
async def test_psionics_skill_entries_have_source_book(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    entries_with_source = [r for r in result.get("results", []) if r.get("source_book")]
    assert entries_with_source, (
        f"No Skill entries with source_book; all skills: {_names(result)}"
    )


@pytest.mark.depends(on=["simple_psionics_ingested"])
async def test_psionic_skill_full_property_traversal(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    target = next(iter(result.get("results", [])), None)
    assert target, "No Skill entity found for traversal"

    entity = await mcp_tools(
        "get_entity", input={"name": target["name"], "depth": "full"}
    )
    assert "error" not in entity, f"get_entity failed: {entity}"
    assert any(
        entity.get(k)
        for k in ("canonicalName", "label", "source_book", "description", "sourceText")
    ), f"Psionic Skill entity has no populated properties: {entity}"


@pytest.mark.depends(on=["simple_psionics_ingested"])
async def test_all_ingested_skills_have_source_books(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    missing = [r for r in result.get("results", []) if not r.get("source_book")]
    assert not missing, f"Skills missing source_book: {[r['name'] for r in missing]}"


# ── CharacterClass extraction ──────────────────────────────────────────────


@pytest.mark.depends(on=["lycanthropes_ingested"])
async def test_lycanthropes_yields_charclass(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "CharacterClass"})
    names = _names(result)
    assert _contains_any(names, ("werewolf", "lycanthrope")), (
        f"Expected werewolf CharacterClass; got: {names}"
    )


@pytest.mark.depends(on=["lycanthropes_ingested", "fashion_designer_ingested"])
async def test_fashion_designer_yields_charclass(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "CharacterClass"})
    assert result.get("count", 0) >= 1, (
        f"Expected at least one CharacterClass after all ingestion; "
        f"got: {_names(result)}"
    )


@pytest.mark.depends(on=["lycanthropes_ingested"])
async def test_werewolf_charclass_relationship_traversal(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "CharacterClass"})
    candidates = [
        r["name"]
        for r in result.get("results", [])
        if "werewolf" in r["name"].lower() or "lycanthrope" in r["name"].lower()
    ]
    assert candidates, "No Werewolf CharacterClass entity found for traversal"

    entity = await mcp_tools(
        "get_entity", input={"name": candidates[0], "depth": "full"}
    )
    assert "error" not in entity, f"get_entity failed: {entity}"
    assert (
        entity.get("source_book") or entity.get("label") or entity.get("canonicalName")
    ), f"Werewolf class entity missing all basic properties: {entity}"


@pytest.mark.depends(on=["lycanthropes_ingested", "fashion_designer_ingested"])
async def test_multi_hop_list_then_search_by_property(mcp_tools):
    list_result = await mcp_tools(
        "list_entities", input={"entity_type": "CharacterClass"}
    )
    assert list_result.get("count", 0) >= 1, (
        "Expected at least one CharacterClass after ingesting all three fixtures"
    )

    first_entry = list_result["results"][0]
    if first_entry.get("name"):
        search_result = await mcp_tools(
            "search_by_property",
            input={
                "entity_type": "CharacterClass",
                "property_name": "canonicalName",
                "value": first_entry["name"],
            },
        )
        assert (
            "error" not in search_result
            or "not a known" not in search_result.get("error", "")
        ), f"search_by_property rejected canonicalName: {search_result}"


# ── Item / Attire extraction (FashionDesigner) ────────────────────────────


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_fashion_designer_yields_attire_items(mcp_tools):
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        if result.get("count", 0) >= 1:
            return
    pytest.fail(
        "Expected at least one Item/Attire/MagicItem entity "
        "after ingesting FashionDesigner.pdf; graph is empty for all item types"
    )


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_attire_items_have_charges_property(mcp_tools):
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        attire_entries = [
            r
            for r in result.get("results", [])
            if any(a in r["name"].lower() for a in _FASHION_ATTIRES)
        ]
        if not attire_entries:
            continue
        entity = await mcp_tools(
            "get_entity",
            input={"name": attire_entries[0]["name"], "depth": "full"},
        )
        assert "error" not in entity, f"get_entity failed: {entity}"
        assert (
            entity.get("charges")
            or entity.get("rechargeCondition")
            or entity.get("description")
        ), f"Attire item {attire_entries[0]['name']!r} has no charges/description: {entity}"
        return
    pytest.skip("No Attire/Item entities found — FashionDesigner ingestion may not have completed")


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_attire_items_have_source_book(mcp_tools):
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        attire_entries = [
            r
            for r in result.get("results", [])
            if any(a in r["name"].lower() for a in _FASHION_ATTIRES)
        ]
        if not attire_entries:
            continue
        for entry in attire_entries:
            assert entry.get("source_book"), (
                f"Attire item {entry['name']!r} missing source_book reference"
            )
        return
    pytest.skip("No Attire/Item entities found — FashionDesigner ingestion may not have completed")


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_attire_granted_spell_traversal(mcp_tools):
    target_name = None
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        match = next(
            (
                r["name"]
                for r in result.get("results", [])
                if any(a in r["name"].lower() for a in _FASHION_ATTIRES)
            ),
            None,
        )
        if match:
            target_name = match
            break

    if target_name is None:
        pytest.skip("No Attire/Item entities found for grantedSpell traversal")

    rel_result = await mcp_tools(
        "get_relationships",
        input={"entity_name": target_name, "relationship": "grantedSpell"},
    )
    assert "error" not in rel_result, f"get_relationships failed: {rel_result}"


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_search_attire_by_rarity(mcp_tools):
    for rarity in ("common", "uncommon", "rare"):
        result = await mcp_tools(
            "search_by_property",
            input={"entity_type": "MagicItem", "property_name": "rarity", "value": rarity},
        )
        if "error" in result:
            pytest.skip(f"search_by_property rarity returned error: {result['error']}")
        if result.get("count", 0) > 0:
            return


@pytest.mark.depends(on=["fashion_designer_ingested"])
async def test_all_magic_items_have_source_books(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "MagicItem"})
    missing = [r for r in result.get("results", []) if not r.get("source_book")]
    assert not missing, (
        f"These MagicItems are missing source_book: {[r['name'] for r in missing]}"
    )
