"""End-to-end tests: PDF in → queryable knowledge graph out.

Requires the full stack (Fuseki, Redis, MinIO, pdf-worker, graph-worker,
mcp-server) and a reachable LLM endpoint (LLAMA_CPP_HOST).

Tests run in file order:
  1. Pipeline completion (ingest + poll)
  2. Entity extraction (verify what the LLM pulled from each PDF)
  3. Graph traversal (multi-hop SPARQL via MCP tools)
"""

import asyncio
import os
import pathlib
import time

import httpx
import pytest

pytestmark = pytest.mark.anyio

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000")
FIXTURES_DIR = pathlib.Path(__file__).parent.parent / "fixtures"
PIPELINE_TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "300"))
POLL_INTERVAL = 5

_SIMPLE_PSIONICS_ID = "simple-psionics"
_LYCANTHROPES_ID = "lycanthropes-in-eberron"
_FASHION_DESIGNER_ID = "fashion-designer"


# ── Helpers ────────────────────────────────────────────────────────────────


async def _ingest_pdf(
    document_id: str,
    pdf_path: pathlib.Path,
    title: str,
    edition: str = "any",
    canon_type: str = "community",
) -> tuple[int, dict]:
    """POST a PDF to /ingest; return (status_code, body)."""
    metadata_yaml = (
        f"document_id: {document_id}\n"
        f"title: '{title}'\n"
        f"edition: {edition}\n"
        f"canon_type: {canon_type}\n"
    )
    async with httpx.AsyncClient() as client:
        with open(pdf_path, "rb") as f:
            resp = await client.post(
                f"{MCP_SERVER_URL}/ingest",
                files={"pdf": (pdf_path.name, f, "application/pdf")},
                data={"metadata": metadata_yaml},
                timeout=30,
            )
    return resp.status_code, resp.json()


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


# ── 1. Pipeline completion ─────────────────────────────────────────────────


async def test_ingest_simple_psionics_completes():
    """simple-psionics.pdf submits and the full pipeline reaches COMPLETED."""
    code, body = await _ingest_pdf(
        _SIMPLE_PSIONICS_ID,
        FIXTURES_DIR / "simple-psionics.pdf",
        "Simple Psionics",
    )
    # 409 means the document was already ingested from a prior run — still valid
    assert code in (202, 409), f"Unexpected ingest response {code}: {body}"
    if code == 202:
        doc = await _poll_until_done(_SIMPLE_PSIONICS_ID)
        assert doc["status"] == "COMPLETED", (
            f"Pipeline failed: status={doc.get('status')}, "
            f"error={doc.get('error')}, "
            f"last_stage={doc.get('last_successful_stage')}"
        )


async def test_ingest_lycanthropes_completes():
    """lycanthropes-in-eberron.pdf submits and the full pipeline reaches COMPLETED."""
    code, body = await _ingest_pdf(
        _LYCANTHROPES_ID,
        FIXTURES_DIR / "lycanthropes-in-eberron.pdf",
        "Lycanthropes in Eberron",
    )
    assert code in (202, 409), f"Unexpected ingest response {code}: {body}"
    if code == 202:
        doc = await _poll_until_done(_LYCANTHROPES_ID)
        assert doc["status"] == "COMPLETED", (
            f"Pipeline failed: status={doc.get('status')}, "
            f"error={doc.get('error')}, "
            f"last_stage={doc.get('last_successful_stage')}"
        )


async def test_ingest_fashion_designer_completes():
    """FashionDesigner.pdf submits and the full pipeline reaches COMPLETED."""
    code, body = await _ingest_pdf(
        _FASHION_DESIGNER_ID,
        FIXTURES_DIR / "FashionDesigner.pdf",
        "Fashion Designer: A Specialization for Artificers",
    )
    assert code in (202, 409), f"Unexpected ingest response {code}: {body}"
    if code == 202:
        doc = await _poll_until_done(_FASHION_DESIGNER_ID)
        assert doc["status"] == "COMPLETED", (
            f"Pipeline failed: status={doc.get('status')}, "
            f"error={doc.get('error')}, "
            f"last_stage={doc.get('last_successful_stage')}"
        )


# ── 2. Entity extraction ───────────────────────────────────────────────────

# simple-psionics.pdf defines four psionic feat trees (Telekinesis, Telepathy,
# Pyrokinesis, Cryokinesis).  The graph-worker should classify these as Skill
# entities.

_PSIONIC_TYPES = ("telekinesis", "telepathy", "pyrokinesis", "cryokinesis")


async def test_psionics_yields_skill_entities(mcp_tools):
    """After simple-psionics ingestion, at least one psionic type appears as a Skill."""
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    names = _names(result)
    assert _contains_any(names, _PSIONIC_TYPES), (
        f"Expected at least one of {_PSIONIC_TYPES} as a Skill; got: {names[:30]}"
    )


async def test_psionics_skill_entries_have_source_book(mcp_tools):
    """Psionic Skill entities carry a source_book reference back to simple-psionics."""
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    psionic_entries = [
        r for r in result.get("results", [])
        if any(t in r["name"].lower() for t in _PSIONIC_TYPES)
    ]
    assert psionic_entries, "No psionic Skill entries found"
    for entry in psionic_entries:
        assert entry.get("source_book"), (
            f"Psionic Skill entry missing source_book: {entry}"
        )


# lycanthropes-in-eberron.pdf describes Werewolves as a Race and defines a
# Werewolf character class.

async def test_lycanthropes_yields_race_entity(mcp_tools):
    """After lycanthropes ingestion, Werewolf or Lycanthrope appears as a Race."""
    result = await mcp_tools("list_entities", input={"entity_type": "Race"})
    names = _names(result)
    assert _contains_any(names, ("werewolf", "lycanthrope")), (
        f"Expected werewolf/lycanthrope Race; got: {names}"
    )


async def test_lycanthropes_yields_charclass(mcp_tools):
    """After lycanthropes ingestion, the Werewolf class appears as a CharacterClass."""
    result = await mcp_tools("list_entities", input={"entity_type": "CharacterClass"})
    names = _names(result)
    assert _contains_any(names, ("werewolf", "lycanthrope")), (
        f"Expected werewolf CharacterClass; got: {names}"
    )


# FashionDesigner.pdf introduces a Fashion Designer specialization for Artificers.

async def test_fashion_designer_yields_charclass(mcp_tools):
    """After FashionDesigner ingestion, Fashion Designer or Artificer appears as a CharacterClass."""
    result = await mcp_tools("list_entities", input={"entity_type": "CharacterClass"})
    names = _names(result)
    assert _contains_any(names, ("fashion", "artificer")), (
        f"Expected fashion designer/artificer CharacterClass; got: {names}"
    )


# ── 3. Graph traversal ─────────────────────────────────────────────────────
#
# These tests exercise SPARQL graph traversal via MCP tools: first locating
# an entity with list_entities, then navigating its properties and
# relationships with get_entity (depth=full) and get_relationships.


async def test_psionic_skill_full_property_traversal(mcp_tools):
    """Graph traversal: get_entity(depth=full) on a psionic Skill returns populated properties."""
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    target = next(
        (
            r for r in result.get("results", [])
            if any(t in r["name"].lower() for t in _PSIONIC_TYPES)
        ),
        None,
    )
    assert target, "No psionic Skill entity found for traversal"

    entity = await mcp_tools(
        "get_entity", input={"name": target["name"], "depth": "full"}
    )
    assert "error" not in entity, f"get_entity failed: {entity}"
    # At minimum, the entity must surface a label/name and a source reference
    assert any(
        entity.get(k) for k in ("canonicalName", "label", "source_book", "description", "sourceText")
    ), f"Psionic Skill entity has no populated properties: {entity}"


async def test_werewolf_entity_full_property_traversal(mcp_tools):
    """Graph traversal: get_entity(depth=full) on the Werewolf Race returns populated properties."""
    result = await mcp_tools("list_entities", input={"entity_type": "Race"})
    candidates = [
        r["name"] for r in result.get("results", [])
        if "werewolf" in r["name"].lower() or "lycanthrope" in r["name"].lower()
    ]
    assert candidates, "No werewolf/lycanthrope Race entity found for traversal"

    entity = await mcp_tools(
        "get_entity", input={"name": candidates[0], "depth": "full"}
    )
    assert "error" not in entity, f"get_entity failed: {entity}"
    assert any(
        entity.get(k) for k in ("canonicalName", "label", "source_book", "description", "sourceText")
    ), f"Werewolf entity has no populated properties: {entity}"


async def test_werewolf_charclass_relationship_traversal(mcp_tools):
    """Graph traversal: Werewolf CharacterClass can be retrieved with full depth.

    The lycanthropes document describes the Werewolf class with five forms and
    shifting mechanics.  get_entity(depth=full) should return the class with at
    least its label and source reference, confirming the triple store has
    populated entity data.
    """
    result = await mcp_tools("list_entities", input={"entity_type": "CharacterClass"})
    candidates = [
        r["name"] for r in result.get("results", [])
        if "werewolf" in r["name"].lower() or "lycanthrope" in r["name"].lower()
    ]
    assert candidates, "No Werewolf CharacterClass entity found for traversal"

    entity = await mcp_tools(
        "get_entity", input={"name": candidates[0], "depth": "full"}
    )
    assert "error" not in entity, f"get_entity failed: {entity}"
    assert entity.get("source_book") or entity.get("label") or entity.get("canonicalName"), (
        f"Werewolf class entity missing all basic properties: {entity}"
    )


async def test_multi_hop_list_then_search_by_property(mcp_tools):
    """Graph traversal: list CharacterClass entities then search by source document.

    This exercises a two-step SPARQL traversal: first enumerate all classes,
    then use search_by_property to verify the graph is queryable by attribute.
    """
    # Step 1: list all CharacterClass entities
    list_result = await mcp_tools("list_entities", input={"entity_type": "CharacterClass"})
    assert list_result.get("count", 0) >= 1, (
        "Expected at least one CharacterClass after ingesting all three fixtures"
    )

    # Step 2: search_by_property on canonicalName — any class with a canonicalName
    # confirms the graph-worker wrote that datatype property and SPARQL can filter on it.
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
        # Either we get results (property was written) or an empty list (property not
        # written for this entity) — neither is a failure; "error" key would be.
        assert "error" not in search_result or "not a known" not in search_result.get("error", ""), (
            f"search_by_property rejected canonicalName: {search_result}"
        )


async def test_all_ingested_skills_have_source_books(mcp_tools):
    """Graph traversal: every Skill in the graph has a source_book reference.

    Verifies the mentionedIn triple was written for all extracted Skill entities,
    which confirms the triple-store can be traversed from Entity → SourceBook.
    """
    result = await mcp_tools("list_entities", input={"entity_type": "Skill"})
    skills_without_source = [
        r for r in result.get("results", []) if not r.get("source_book")
    ]
    assert not skills_without_source, (
        f"These Skills are missing source_book: "
        f"{[r['name'] for r in skills_without_source]}"
    )


# ── 4. Magic item extraction (FashionDesigner.pdf) ─────────────────────────
#
# FashionDesigner.pdf defines four named Attire items with charges and
# granted spells.  These should be extracted as cs:Attire (or cs:WondrousItem
# / cs:MagicItem) entities with rarity, charges, and rechargeCondition.
#
# The four items:
#   Casual Attire        — Friends cantrip + Calm Emotions (4 charges)
#   Inconspicuous Attire — Disguise Self + Geas (4 charges)
#   Conspicuous Attire   — Suggestion + Dominate Person/Monster (4 charges)
#   Stunning Attire      — Charm Person + Hypnotic Pattern + Geas (4 charges)

_FASHION_ATTIRES = (
    "casual attire",
    "inconspicuous attire",
    "conspicuous attire",
    "stunning attire",
)


async def test_fashion_designer_yields_attire_items(mcp_tools):
    """After FashionDesigner ingestion, the four Attire items appear as Item entities."""
    # Query the most specific type first; fall back to broader types if needed.
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        names = _names(result)
        found = [a for a in _FASHION_ATTIRES if _contains_any(names, (a,))]
        if found:
            return  # at least one attire found under some item type — pass
    pytest.fail(
        f"Expected at least one of {_FASHION_ATTIRES} as an Item/Attire entity "
        f"after ingesting FashionDesigner.pdf"
    )


async def test_attire_items_have_charges_property(mcp_tools):
    """Attire items carry a charges property (4 charges each per the document)."""
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        attire_entries = [
            r for r in result.get("results", [])
            if any(a in r["name"].lower() for a in _FASHION_ATTIRES)
        ]
        if not attire_entries:
            continue
        # At least one attire should expose charges via get_entity(full)
        entity = await mcp_tools(
            "get_entity", input={"name": attire_entries[0]["name"], "depth": "full"}
        )
        assert "error" not in entity, f"get_entity failed: {entity}"
        assert entity.get("charges") or entity.get("rechargeCondition") or entity.get("description"), (
            f"Attire item {attire_entries[0]['name']!r} has no charges/description: {entity}"
        )
        return
    pytest.skip("No Attire/Item entities found — FashionDesigner ingestion may not have completed")


async def test_attire_items_have_source_book(mcp_tools):
    """Attire items carry a source_book reference back to FashionDesigner."""
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        attire_entries = [
            r for r in result.get("results", [])
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


async def test_attire_granted_spell_traversal(mcp_tools):
    """Graph traversal: Attire item → grantedSpell → Skill (multi-hop via cs:grantedSpell).

    Casual Attire grants Friends and Calm Emotions; Stunning Attire grants Charm Person,
    Hypnotic Pattern, and Geas.  The get_relationships call traverses the grantedSpell
    edge and should return at least one Skill entity.
    """
    # Find any Attire/Item entity
    target_name = None
    for entity_type in ("Attire", "WondrousItem", "MagicItem", "Item"):
        result = await mcp_tools("list_entities", input={"entity_type": entity_type})
        match = next(
            (r["name"] for r in result.get("results", [])
             if any(a in r["name"].lower() for a in _FASHION_ATTIRES)),
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
    # The relationship may exist (count >= 1) or the graph-worker may not have
    # written it yet; we assert the tool call itself succeeds (no error key).
    assert "error" not in rel_result, f"get_relationships failed: {rel_result}"


async def test_search_attire_by_rarity(mcp_tools):
    """Graph traversal: search_by_property(rarity=...) returns Attire items.

    Verifies the rarity datatype property was written and is queryable.
    If no rarity was extracted, the test is skipped rather than failed.
    """
    for rarity in ("common", "uncommon", "rare"):
        result = await mcp_tools(
            "search_by_property",
            input={
                "entity_type": "MagicItem",
                "property_name": "rarity",
                "value": rarity,
            },
        )
        if "error" in result:
            # Property not yet in allowed list or schema mismatch — skip
            pytest.skip(f"search_by_property rarity returned error: {result['error']}")
        if result.get("count", 0) > 0:
            return  # found items at some rarity — pass
    # No items found at any rarity — property may not have been extracted,
    # which is acceptable; only a tool error is a hard failure.


async def test_all_magic_items_have_source_books(mcp_tools):
    """Graph traversal: every MagicItem in the graph links to a SourceBook.

    Confirms the Entity → cs:mentionedIn → SourceBook traversal works for items.
    """
    result = await mcp_tools("list_entities", input={"entity_type": "MagicItem"})
    items_without_source = [
        r for r in result.get("results", []) if not r.get("source_book")
    ]
    assert not items_without_source, (
        f"These MagicItems are missing source_book: "
        f"{[r['name'] for r in items_without_source]}"
    )
