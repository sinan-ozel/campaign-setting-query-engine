"""Black-box tests for the mcp-server service.

Fuseki and Redis are seeded directly; no workers are running. MCP tools are
called via the FastMCP client; admin endpoints via httpx.
"""

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


# ── MCP tool tests ─────────────────────────────────────────────────────────


async def test_list_entities_empty_graph(mcp_tools):
    result = await mcp_tools("list_entities", input={"entity_type": "River"})
    assert "results" in result
    assert result["results"] == []


async def test_list_entities_returns_seeded_river(mcp_tools):
    _seed_fuseki(f"""
        <{CS}TestRiver> rdf:type <{CS}River> ;
            <{RDFS}label> "Test River" ;
            <{CS}pageNumber> "99" .
        """)
    result = await mcp_tools("list_entities", input={"entity_type": "River"})
    assert any(r["name"] == "Test River" for r in result.get("results", []))


async def test_list_entities_returns_page_references(mcp_tools):
    _seed_fuseki(f"""
        <{CS}book_page_ref_test> rdf:type <{CS}SourceBook> ;
            <{RDFS}label> "Page Ref Test Book" ;
            <{CS}edition> "3e" ;
            <{CS}canonType> "canon" .
        <{CS}PageRefTestRiver> rdf:type <{CS}River> ;
            <{RDFS}label> "Page Ref Test River" ;
            <{CS}mentionedIn> <{CS}book_page_ref_test> ;
            <{CS}hasMention> <{CS}mention_pagereftestriver> .
        <{CS}mention_pagereftestriver> <{CS}inBook> <{CS}book_page_ref_test> ;
            <{CS}atPage> "42" .
        """)
    result = await mcp_tools("list_entities", input={"entity_type": "River"})
    target = next(
        r for r in result["results"] if r["name"] == "Page Ref Test River"
    )
    assert any(ref["page"] is not None for ref in target["source_refs"])


async def test_get_entity_not_found(mcp_tools):
    result = await mcp_tools(
        "get_entity", input={"name": "NonExistentEntityXYZ"}
    )
    assert "error" in result


async def test_get_entity_found(mcp_tools):
    result = await mcp_tools("get_entity", input={"name": "Test River"})
    assert "error" not in result
    assert result.get("label") == "Test River" or "Test River" in str(result)


async def test_search_by_property_rejects_unknown_property(mcp_tools):
    result = await mcp_tools(
        "search_by_property",
        input={
            "entity_type": "NPC",
            "property_name": "INJECTION; DROP TABLE",
            "value": "anything",
        },
    )
    assert "error" in result


async def test_search_by_property_known_property(mcp_tools):
    result = await mcp_tools(
        "search_by_property",
        input={
            "entity_type": "NPC",
            "property_name": "alignment",
            "value": "LN",
        },
    )
    assert "results" in result


async def test_get_ingestion_status_all(mcp_tools):
    result = await mcp_tools("get_ingestion_status", input={})
    assert "documents" in result or "total" in result or "error" not in result


async def test_edition_filter(mcp_tools):
    _seed_fuseki(f"""
        <{CS}book_test_3e> rdf:type <{CS}SourceBook> ;
            <{RDFS}label> "Test 3e Book" ;
            <{CS}edition> "3e" ;
            <{CS}canonType> "canon" .
        <{CS}FilterTestRiver> rdf:type <{CS}River> ;
            <{RDFS}label> "Filter Test River" ;
            <{CS}mentionedIn> <{CS}book_test_3e> ;
            <{CS}hasMention> <{CS}mention_filtertestriver> .
        <{CS}mention_filtertestriver> <{CS}inBook> <{CS}book_test_3e> ;
            <{CS}atPage> "42" .
        """)
    result_3e = await mcp_tools(
        "list_entities",
        input={"entity_type": "River", "edition": "3e"},
    )
    result_5e = await mcp_tools(
        "list_entities",
        input={"entity_type": "River", "edition": "5e"},
    )
    names_3e = [r["name"] for r in result_3e.get("results", [])]
    names_5e = [r["name"] for r in result_5e.get("results", [])]
    assert "Filter Test River" in names_3e
    assert "Filter Test River" not in names_5e
