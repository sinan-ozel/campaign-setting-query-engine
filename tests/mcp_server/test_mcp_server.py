"""Black-box tests for the mcp-server service.

Fuseki and Redis are seeded directly; no workers are running. MCP tools are
called via the FastMCP client; admin endpoints via httpx.
"""

import json
import os

import httpx
import pytest

pytestmark = pytest.mark.anyio

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000")
FUSEKI_ENDPOINT = os.environ.get(
    "FUSEKI_ENDPOINT", "http://fuseki:3030/campaign"
)
FUSEKI_ADMIN_PASSWORD = os.environ.get("FUSEKI_ADMIN_PASSWORD", "testpassword")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

CS = "http://campaignsetting.io/ontology#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


# ── Helpers ────────────────────────────────────────────────────────────────


def _seed_fuseki(triples_ttl: str) -> None:
    """INSERT DATA into the default graph via SPARQL UPDATE."""
    query = (
        f"PREFIX cs: <{CS}> PREFIX rdfs: <{RDFS}> PREFIX rdf: <{RDF}>\n"
        f"INSERT DATA {{ {triples_ttl} }}"
    )
    r = httpx.post(
        f"{FUSEKI_ENDPOINT}/update",
        content=query.encode("utf-8"),
        headers={"Content-Type": "application/sparql-update; charset=utf-8"},
        auth=("admin", FUSEKI_ADMIN_PASSWORD),
        timeout=10,
    )
    r.raise_for_status()


def _seed_redis_failed(document_id: str) -> None:
    import redis

    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.hset(
        f"doc:{document_id}:state",
        mapping={
            "status": "FAILED",
            "title": "Test Book",
            "edition": "3e",
            "canon_type": "canon",
            "error": "simulated failure",
        },
    )


# ── Admin endpoint tests ───────────────────────────────────────────────────


async def test_health_returns_200():
    r = httpx.get(f"{MCP_SERVER_URL}/health")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "status" in body
    assert "fuseki" in body
    assert "redis" in body


async def test_empty_status_list():
    r = httpx.get(f"{MCP_SERVER_URL}/status")
    assert r.status_code == 200
    body = r.json()
    assert "documents" in body
    assert "total" in body


async def test_unknown_document_returns_404():
    r = httpx.get(f"{MCP_SERVER_URL}/status/no_such_document_xyz")
    assert r.status_code == 404


async def test_status_pagination():
    import redis as redislib

    r = redislib.from_url(REDIS_URL, decode_responses=True)
    # Seed 12 fake documents
    for i in range(12):
        r.hset(
            f"doc:pagination_test_{i:02d}:state",
            mapping={"status": "COMPLETED", "title": f"Book {i}"},
        )

    resp1 = httpx.get(f"{MCP_SERVER_URL}/status", params={"page": 1})
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert len(data1["documents"]) <= 10
    assert data1["total"] >= 12

    resp2 = httpx.get(f"{MCP_SERVER_URL}/status", params={"page": 2})
    assert resp2.status_code == 200
    data2 = resp2.json()
    # Page 2 has the remainder
    assert len(data2["documents"]) >= 2

    # Cleanup
    for i in range(12):
        r.delete(f"doc:pagination_test_{i:02d}:state")


async def test_requeue_failed_document():
    _seed_redis_failed("requeue_test_doc")
    r = httpx.post(f"{MCP_SERVER_URL}/admin/requeue/requeue_test_doc")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PENDING"

    # Check Redis
    import redis as redislib

    rc = redislib.from_url(REDIS_URL, decode_responses=True)
    assert rc.hget("doc:requeue_test_doc:state", "status") == "PENDING"
    rc.delete("doc:requeue_test_doc:state")


async def test_requeue_non_failed_returns_409():
    import redis as redislib

    rc = redislib.from_url(REDIS_URL, decode_responses=True)
    rc.hset(
        "doc:not_failed_doc:state",
        mapping={"status": "COMPLETED", "title": "Test"},
    )
    r = httpx.post(f"{MCP_SERVER_URL}/admin/requeue/not_failed_doc")
    assert r.status_code == 409
    rc.delete("doc:not_failed_doc:state")


async def test_health_reports_mode_and_version():
    body = httpx.get(f"{MCP_SERVER_URL}/health").json()
    assert body["mode"] == "ingest"
    assert body["version"].count(".") >= 2


async def test_type_conflicts_endpoint():
    import redis as redislib

    rc = redislib.from_url(REDIS_URL, decode_responses=True)
    rc.sadd("review:type_conflicts", "Thrane|Nation|Language")
    r = httpx.get(f"{MCP_SERVER_URL}/admin/type-conflicts")
    assert r.status_code == 200
    assert {
        "name": "Thrane",
        "existing_type": "Nation",
        "conflicting_type": "Language",
    } in r.json()["conflicts"]
    rc.srem("review:type_conflicts", "Thrane|Nation|Language")


async def test_type_assignments_rejects_bad_limit():
    r = httpx.get(
        f"{MCP_SERVER_URL}/admin/type-assignments", params={"limit": "0"}
    )
    assert r.status_code == 400
    assert "Example" in r.json()["error"]


# ── MCP tool tests ─────────────────────────────────────────────────────────
# One small, coherent graph is seeded once: Breland with its capital Wroat
# (capital only by containment + capitalStatus), Cyre with a former capital
# named explicitly, a river and a lake, House Cannith with a member, a
# fashion tradition, and 300 filler NPCs to exercise paging and the
# response-size budget (MAX_RESPONSE_CHARS=4000 in docker-compose.yml).

BOOK = f"<{CS}book_test_eberron>"


def _ent(
    slug: str, label: str, *types: str, page: str = "1", extra: str = ""
) -> str:
    type_triples = " ".join(f"<{CS}{slug}> rdf:type <{CS}{t}> ." for t in types)
    return f"""
        {type_triples}
        <{CS}{slug}> rdfs:label "{label}" ;
            cs:mentionedIn {BOOK} ;
            cs:hasMention <{CS}mention_{slug}> .
        <{CS}mention_{slug}> cs:inBook {BOOK} ; cs:atPage "{page}" .
        {extra}
    """


@pytest.fixture(scope="module", autouse=True)
def seeded_graph():
    fillers = "".join(
        _ent(
            f"Filler_{i:03d}",
            f"Filler Person Number {i:03d} of the Long Name Club",
            "NPC",
        )
        for i in range(300)
    )
    _seed_fuseki(f"""
        {BOOK} rdf:type cs:SourceBook ; rdfs:label "Test Eberron Book" ;
            cs:edition "5e" ; cs:canonType "canon" .
        {_ent("Khorvaire", "Khorvaire", "Continent", "Location")}
        {_ent("Breland", "Breland", "Nation", "Location", page="12",
              extra=f'<{CS}Khorvaire> cs:contains <{CS}Breland> . <{CS}Breland> cs:ruledBy <{CS}Boranel> .')}
        {_ent("Wroat", "Wroat", "City", "Location", page="14",
              extra=f'''<{CS}Breland> cs:contains <{CS}Wroat> .
                        <{CS}Wroat> cs:capitalStatus "current" ;
                            cs:description "Wroat is the capital of Breland and seat of King Boranel." .''')}
        {_ent("Sharn", "Sharn", "City", "Location", extra=f'<{CS}Breland> cs:contains <{CS}Sharn> .')}
        {_ent("Cyre", "Cyre", "Nation", "Location", extra=f'<{CS}Cyre> cs:status "past" .')}
        {_ent("Metrol", "Metrol", "City", "Location",
              extra=f'<{CS}Metrol> cs:capitalOf <{CS}Cyre> ; cs:capitalStatus "former" .')}
        {_ent("Brey_River", "Brey River", "River", "Location", page="142",
              extra=f'<{CS}mention_Brey_River_b> cs:inBook {BOOK} ; cs:atPage "9" . <{CS}Brey_River> cs:hasMention <{CS}mention_Brey_River_b> .')}
        {_ent("Lake_Galifar", "Lake Galifar", "Lake", "Location")}
        {_ent("Boranel", "Boranel ir'Wynarn", "NPC", extra=f'<{CS}Boranel> cs:alias "King Boranel" ; cs:nationality <{CS}Breland> .')}
        {_ent("House_Cannith", "House Cannith", "DragonmarkedHouse", "Faction")}
        {_ent("Merrix", "Merrix d'Cannith", "NPC", extra=f'<{CS}Merrix> cs:memberOf <{CS}House_Cannith> .')}
        {_ent("Brelish_fashion", "Brelish fashion", "Tradition",
              extra=f'''<{CS}Brelish_fashion> cs:category "fashion" ; cs:practicedBy <{CS}Breland> ;
                        cs:description "Brelish nobles favor practical cuts and long coats." .''')}
        {_ent("Stormreach", "Stormreach", "City", "Location")}
        {_ent("Shawarma", "Brelish Shawarma with Goblin sauce", "Dish",
              extra=f'<{CS}Shawarma> cs:originatesIn <{CS}Breland> ; cs:servedIn <{CS}Stormreach> .')}
        {_ent("Petal_and_Thorn", "Petal and Thorn", "Shop", "Location",
              extra=f'<{CS}Petal_and_Thorn> cs:shopType "florist" . <{CS}Sharn> cs:contains <{CS}Petal_and_Thorn> .')}
        {fillers}
    """)


async def test_describe_schema_lists_types_relationships_properties(mcp_tools):
    result = await mcp_tools("describe_schema", _={})
    assert "Lake" in result["entity_types"]["Location"]
    assert "Cocktail" in result["entity_types"]["Drink"]
    assert "capital" in result["relationships"]
    assert "members" in result["relationships"]
    assert "category" in result["filter_properties"]
    assert "fashion" in result["property_values"]["category"]


async def test_list_names_rivers_excludes_lakes(mcp_tools):
    result = await mcp_tools("list_names", input={"entity_type": "River"})
    assert result["names"] == ["Brey River"]
    assert result["total"] == 1
    lakes = await mcp_tools("list_names", input={"entity_type": "Lake"})
    assert lakes["names"] == ["Lake Galifar"]


async def test_list_names_pages_with_total(mcp_tools):
    result = await mcp_tools(
        "list_names", input={"entity_type": "NPC", "limit": 10, "offset": 10}
    )
    assert result["total"] == 302
    assert result["count"] == 10
    assert result["next_offset"] == 20
    assert result["truncated"] is True


async def test_list_names_respects_size_budget(mcp_tools):
    result = await mcp_tools(
        "list_names", input={"entity_type": "NPC", "limit": 200}
    )
    assert len(json.dumps(result)) <= 4000 + 400
    assert result["truncated"] is True
    assert result["total"] == 302


async def test_list_names_unknown_type_suggests(mcp_tools):
    result = await mcp_tools("list_names", input={"entity_type": "Rivers"})
    assert "River" in result["did_you_mean"]
    assert "describe_schema" in result["hint"]


async def test_list_names_filters_by_linked_entity_name(mcp_tools):
    result = await mcp_tools(
        "list_names",
        input={"entity_type": "NPC", "filters": {"nationality": "breland"}},
    )
    assert result["names"] == ["Boranel ir'Wynarn"]


async def test_search_entities_partial_name(mcp_tools):
    result = await mcp_tools("search_entities", input={"query": "cannith"})
    names = [m["name"] for m in result["matches"]]
    assert names[:2] == ["House Cannith", "Merrix d'Cannith"]
    assert result["matches"][0]["types"] == ["DragonmarkedHouse"]


async def test_search_entities_matches_alias(mcp_tools):
    result = await mcp_tools("search_entities", input={"query": "King Boranel"})
    assert result["matches"][0]["name"] == "Boranel ir'Wynarn"
    assert result["matches"][0]["matched_alias"] == "King Boranel"


async def test_members_of_house(mcp_tools):
    result = await mcp_tools(
        "get_relationships",
        input={"entity_name": "House Cannith", "relationship": "members"},
    )
    assert [r["name"] for r in result["results"]] == ["Merrix d'Cannith"]


async def test_capital_derived_from_containment(mcp_tools):
    result = await mcp_tools(
        "get_relationships",
        input={"entity_name": "Breland", "relationship": "capital"},
    )
    assert [r["name"] for r in result["results"]] == ["Wroat"]


async def test_former_capital_named_explicitly(mcp_tools):
    former = await mcp_tools(
        "get_relationships",
        input={"entity_name": "Cyre", "relationship": "formerCapitals"},
    )
    current = await mcp_tools(
        "get_relationships",
        input={"entity_name": "Cyre", "relationship": "capital"},
    )
    assert [r["name"] for r in former["results"]] == ["Metrol"]
    assert current["results"] == []


async def test_ruler(mcp_tools):
    result = await mcp_tools(
        "get_relationships",
        input={"entity_name": "breland", "relationship": "ruler"},
    )
    assert [r["name"] for r in result["results"]] == ["Boranel ir'Wynarn"]


async def test_unknown_relationship_suggests(mcp_tools):
    result = await mcp_tools(
        "get_relationships",
        input={"entity_name": "Breland", "relationship": "capitals"},
    )
    assert "capital" in result["did_you_mean"]


async def test_unknown_entity_suggests(mcp_tools):
    result = await mcp_tools(
        "get_relationships",
        input={"entity_name": "Cannith", "relationship": "members"},
    )
    assert "House Cannith" in result["did_you_mean"]


async def test_get_sources_groups_pages_by_book(mcp_tools):
    result = await mcp_tools(
        "get_sources", input={"names": ["Brey River", "Nowhere"]}
    )
    brey, missing = result["results"]
    assert brey["sources"] == [
        {
            "book": "Test Eberron Book",
            "edition": "5e",
            "canon_type": "canon",
            "pages": ["9", "142"],
        }
    ]
    assert "error" in missing


async def test_get_entity_facts(mcp_tools):
    result = await mcp_tools("get_entity", input={"name": "Wroat"})
    assert result["types"] == ["City"]
    assert result["facts"]["capitalStatus"] == "current"
    assert "capital of Breland" in result["facts"]["description"]


async def test_get_entity_relationships_section(mcp_tools):
    result = await mcp_tools(
        "get_entity", input={"name": "Breland", "section": "relationships"}
    )
    rels = result["relationships"]
    assert {"name": "Wroat", "types": ["City"]} in rels["contains"]
    assert {"name": "Khorvaire", "types": ["Continent"]} in rels["containedIn"]
    assert {"name": "Brelish fashion", "types": ["Tradition"]} in rels[
        "traditions"
    ]


async def test_location_hierarchy(mcp_tools):
    result = await mcp_tools(
        "get_location_hierarchy", input={"location": "Wroat"}
    )
    assert [a["name"] for a in result["ancestors"]] == ["Breland", "Khorvaire"]
    down = await mcp_tools(
        "get_location_hierarchy",
        input={"location": "Khorvaire", "depth": "all"},
    )
    assert {"Breland", "Wroat", "Sharn"} <= {
        c["name"] for c in down["children"]
    }


async def test_search_by_property_tradition_category(mcp_tools):
    result = await mcp_tools(
        "search_by_property",
        input={
            "entity_type": "Tradition",
            "property_name": "category",
            "value": "Fashion",
        },
    )
    assert result["names"] == ["Brelish fashion"]


async def test_search_by_property_rejects_unknown_property(mcp_tools):
    result = await mcp_tools(
        "search_by_property",
        input={
            "entity_type": "NPC",
            "property_name": "INJECTION; DROP TABLE",
            "value": "x",
        },
    )
    assert "error" in result


async def test_search_text_finds_capital_in_description(mcp_tools):
    result = await mcp_tools(
        "search_text", input={"query": "What is the capital of Breland?"}
    )
    top = result["matches"][0]
    assert top["name"] == "Wroat"
    assert "capital of Breland" in top["snippet"]
    assert top["sources"] == ["Test Eberron Book p.14"]


async def test_search_text_fashion(mcp_tools):
    result = await mcp_tools(
        "search_text", input={"query": "fashion", "entity_type": "Tradition"}
    )
    assert result["matches"][0]["name"] == "Brelish fashion"


async def test_list_sources(mcp_tools):
    result = await mcp_tools("list_sources", _={})
    book = next(b for b in result["books"] if b["title"] == "Test Eberron Book")
    assert book["edition"] == "5e"
    assert book["entities"] >= 300


async def test_edition_filter(mcp_tools):
    in_5e = await mcp_tools(
        "list_names", input={"entity_type": "River", "edition": "5e"}
    )
    in_3e = await mcp_tools(
        "list_names", input={"entity_type": "River", "edition": "3e"}
    )
    assert in_5e["names"] == ["Brey River"]
    assert in_3e["names"] == []


async def test_menu_of_city_and_dishes_of_nation(mcp_tools):
    menu = await mcp_tools(
        "get_relationships",
        input={"entity_name": "Stormreach", "relationship": "menu"},
    )
    local = await mcp_tools(
        "get_relationships",
        input={"entity_name": "Breland", "relationship": "localDishes"},
    )
    assert [r["name"] for r in menu["results"]] == [
        "Brelish Shawarma with Goblin sauce"
    ]
    assert [r["name"] for r in local["results"]] == [
        "Brelish Shawarma with Goblin sauce"
    ]


async def test_shops_by_type(mcp_tools):
    result = await mcp_tools(
        "list_names",
        input={"entity_type": "Shop", "filters": {"shopType": "florist"}},
    )
    assert result["names"] == ["Petal and Thorn"]
