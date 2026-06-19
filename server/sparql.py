"""SPARQL query helpers for the Fuseki endpoint."""

import logging
import os
import sys

import httpx
import yaml

logger = logging.getLogger(__name__)

CS = "http://campaignsetting.io/ontology#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
OWL = "http://www.w3.org/2002/07/owl#"

PREFIXES = f"""\
PREFIX cs:   <{CS}>
PREFIX rdfs: <{RDFS}>
PREFIX rdf:  <{RDF}>
PREFIX owl:  <{OWL}>
"""

_FUSEKI_ENDPOINT = os.environ.get(
    "FUSEKI_ENDPOINT", "http://localhost:3030/campaign"
)

_ONTOLOGY_SCHEMA_PATH = os.environ.get(
    "ONTOLOGY_SCHEMA_PATH", "/config/ontology_schema.yaml"
)


def _load_ontology_schema() -> dict:
    try:
        with open(_ONTOLOGY_SCHEMA_PATH) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(
            "Ontology schema not found at %r. "
            "Set ONTOLOGY_SCHEMA_PATH or mount config/ontology_schema.yaml "
            "to /config/ontology_schema.yaml.",
            _ONTOLOGY_SCHEMA_PATH,
        )
        sys.exit(1)


_ONTOLOGY = _load_ontology_schema()

ENTITY_CLASS: dict[str, str] = {
    k: f"cs:{v}" for k, v in _ONTOLOGY["queryable_types"].items()
}

REL_PROPERTY: dict[str, str] = {
    k: f"cs:{v}" for k, v in _ONTOLOGY["relationship_properties"].items()
}

ALLOWED_PROPERTY_NAMES: frozenset[str] = frozenset(
    _ONTOLOGY["allowed_filter_properties"]
)


def _sparql_escape(value: str) -> str:
    """Escape a string for safe embedding in a SPARQL literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def val(binding: dict, key: str) -> str | None:
    """Extract a binding value or return None."""
    item = binding.get(key)
    return item["value"] if item else None


def split_agg(binding: dict, key: str) -> list[str]:
    """Split a pipe-separated GROUP_CONCAT result into a list, filtering blanks."""
    raw = val(binding, key) or ""
    return [x for x in raw.split("|") if x]


def parse_source_refs(binding: dict, key: str = "sourceRefs") -> list[dict]:
    """Parse pipe-separated 'page~~book' pairs into [{page, book}] objects."""
    raw = val(binding, key) or ""
    result = []
    for item in (x for x in raw.split("|") if x):
        page, _, book = item.partition("~~")
        result.append({"page": page or None, "book": book or None})
    return result


def _edition_filter(edition: str, var: str = "?edition") -> str:
    if edition == "any":
        return ""
    return f'FILTER({var} = "{_sparql_escape(edition)}")'


def _canon_filter(canon_type: str, var: str = "?canonType") -> str:
    if canon_type == "any":
        return ""
    return f'FILTER({var} = "{_sparql_escape(canon_type)}")'


async def sparql_select(query: str) -> list[dict]:
    """Execute a SPARQL SELECT against Fuseki; return the bindings list."""

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{_FUSEKI_ENDPOINT}/sparql",
            params={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()
    return response.json()["results"]["bindings"]


async def sparql_update(query: str) -> None:
    """Execute a SPARQL UPDATE against Fuseki."""

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{_FUSEKI_ENDPOINT}/update",
            content=query.encode("utf-8"),
            headers={
                "Content-Type": "application/sparql-update; charset=utf-8"
            },
        )
        response.raise_for_status()


async def fuseki_reachable() -> bool:
    """Return True if Fuseki responds to a ping."""
    base = _FUSEKI_ENDPOINT.rsplit("/", 1)[0]
    async with httpx.AsyncClient(timeout=3.0) as client:
        r = await client.get(f"{base}/$/ping")
        return r.status_code == 200


def build_list_entities_query(
    entity_type: str,
    edition: str = "any",
    canon_type: str = "any",
    filters: dict | None = None,
) -> str:
    """Build a SPARQL SELECT that lists entities of the given type."""
    cls = ENTITY_CLASS[entity_type]
    edition_f = _edition_filter(edition)
    canon_f = _canon_filter(canon_type)

    extra_clauses = ""
    if filters:
        for prop, val_str in filters.items():
            if prop not in ALLOWED_PROPERTY_NAMES:
                continue
            escaped = _sparql_escape(str(val_str))
            extra_clauses += (
                f"    ?entity cs:{prop} ?fv_{prop} .\n"
                f'    FILTER(LCASE(STR(?fv_{prop})) = LCASE("{escaped}"))\n'
            )

    return f"""\
{PREFIXES}
SELECT ?entity ?name
  (GROUP_CONCAT(DISTINCT CONCAT(COALESCE(STR(?page),""), "~~", COALESCE(STR(?bookTitle),"")); SEPARATOR="|") AS ?sourceRefs)
  (GROUP_CONCAT(DISTINCT ?edition; SEPARATOR="|") AS ?editions)
  (GROUP_CONCAT(DISTINCT ?canonType; SEPARATOR="|") AS ?canonTypes)
WHERE {{
    ?entity rdf:type {cls} ;
            rdfs:label ?name .
    OPTIONAL {{
        ?entity cs:hasMention ?mention .
        ?mention cs:inBook ?src .
        OPTIONAL {{ ?mention cs:atPage ?page . }}
        ?src rdfs:label ?bookTitle ;
             cs:edition ?edition ;
             cs:canonType ?canonType .
    }}
    {edition_f}
    {canon_f}
    {extra_clauses}
}}
GROUP BY ?entity ?name
ORDER BY ?name
"""


def build_get_entity_query(
    name: str,
    edition: str = "any",
    canon_type: str = "any",
    depth: str = "summary",
) -> str:
    """Build a SPARQL query returning all triples for a named entity."""
    escaped = _sparql_escape(name)
    edition_f = _edition_filter(edition)
    canon_f = _canon_filter(canon_type)

    if depth == "summary":
        prop_filter = (
            "FILTER(?p IN ("
            "rdfs:label, cs:description, cs:alias, cs:alignment,"
            "cs:factionType, cs:pageNumber, cs:canonicalName,"
            "cs:motiveSummary, cs:edition, cs:canonType"
            "))"
        )
    else:
        prop_filter = ""

    return f"""\
{PREFIXES}
SELECT ?entity ?p ?o ?bookTitle ?edition ?canonType WHERE {{
    ?entity rdfs:label ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{escaped}"))
    ?entity ?p ?o .
    {prop_filter}
    OPTIONAL {{
        ?entity cs:mentionedIn ?src .
        ?src rdfs:label ?bookTitle ;
             cs:edition ?edition ;
             cs:canonType ?canonType .
    }}
    {edition_f}
    {canon_f}
}}
"""


def build_get_relationships_query(
    entity_name: str,
    relationship: str,
    edition: str = "any",
    canon_type: str = "any",
) -> str:
    """Build a SPARQL query for a named relationship from an entity."""
    prop = REL_PROPERTY[relationship]
    escaped = _sparql_escape(entity_name)
    edition_f = _edition_filter(edition)
    canon_f = _canon_filter(canon_type)

    return f"""\
{PREFIXES}
SELECT ?related ?relName
  (GROUP_CONCAT(DISTINCT ?relType; SEPARATOR="|") AS ?relTypes)
  (GROUP_CONCAT(DISTINCT CONCAT(COALESCE(STR(?page),""), "~~", COALESCE(STR(?bookTitle),"")); SEPARATOR="|") AS ?sourceRefs)
WHERE {{
    ?entity rdfs:label ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{escaped}"))
    ?entity {prop} ?related .
    ?related rdfs:label ?relName .
    OPTIONAL {{ ?related rdf:type ?relType . }}
    OPTIONAL {{
        ?related cs:hasMention ?mention .
        ?mention cs:inBook ?src .
        OPTIONAL {{ ?mention cs:atPage ?page . }}
        ?src rdfs:label ?bookTitle .
    }}
    {edition_f}
    {canon_f}
}}
GROUP BY ?related ?relName
ORDER BY ?relName
"""


def build_get_entity_edges_query(name: str) -> str:
    """Build a SPARQL query returning all outgoing edges to named entities.

    Returns every (predicate, related_label) pair where the object has an
    rdfs:label — i.e. all semantic edges, excluding structural plumbing
    (rdf:type, rdfs:label, cs:mentionedIn, cs:hasMention, cs:inBook, cs:atPage).
    """
    escaped = _sparql_escape(name)
    return f"""\
{PREFIXES}
SELECT ?p ?relatedLabel WHERE {{
    ?entity rdfs:label ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{escaped}"))
    ?entity ?p ?related .
    ?related rdfs:label ?relatedLabel .
    FILTER(?p NOT IN (
        rdf:type,
        rdfs:label,
        cs:mentionedIn,
        cs:hasMention,
        cs:inBook,
        cs:atPage
    ))
}}
ORDER BY ?p ?relatedLabel
"""


def build_location_ancestors_query(location_name: str) -> str:
    """Return all ancestors of a location (OWL transitivity walks up)."""
    escaped = _sparql_escape(location_name)
    return f"""\
{PREFIXES}
SELECT DISTINCT ?ancestor ?ancestorName ?ancestorType WHERE {{
    ?loc rdfs:label ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{escaped}"))
    ?ancestor cs:contains+ ?loc .
    ?ancestor rdfs:label ?ancestorName .
    OPTIONAL {{ ?ancestor rdf:type ?ancestorType . }}
}}
ORDER BY ?ancestorName
"""


def build_location_children_query(location_name: str) -> str:
    """Return all descendants of a location (OWL transitivity walks down)."""
    escaped = _sparql_escape(location_name)
    return f"""\
{PREFIXES}
SELECT DISTINCT ?child ?childName ?childType ?page WHERE {{
    ?loc rdfs:label ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{escaped}"))
    ?loc cs:contains+ ?child .
    ?child rdfs:label ?childName .
    OPTIONAL {{ ?child rdf:type ?childType . }}
    OPTIONAL {{ ?child cs:pageNumber ?page . }}
}}
ORDER BY ?childName
"""


def build_search_by_property_query(
    entity_type: str,
    property_name: str,
    value: str,
    edition: str = "any",
    canon_type: str = "any",
) -> str:
    """Build a SPARQL SELECT filtering by a specific property value."""
    cls = ENTITY_CLASS[entity_type]
    escaped_val = _sparql_escape(value)
    edition_f = _edition_filter(edition)
    canon_f = _canon_filter(canon_type)

    return f"""\
{PREFIXES}
SELECT ?entity ?name
  (GROUP_CONCAT(DISTINCT CONCAT(COALESCE(STR(?page),""), "~~", COALESCE(STR(?bookTitle),"")); SEPARATOR="|") AS ?sourceRefs)
  (GROUP_CONCAT(DISTINCT ?edition; SEPARATOR="|") AS ?editions)
  (GROUP_CONCAT(DISTINCT ?canonType; SEPARATOR="|") AS ?canonTypes)
WHERE {{
    ?entity rdf:type {cls} ;
            rdfs:label ?name ;
            cs:{property_name} ?propVal .
    FILTER(LCASE(STR(?propVal)) = LCASE("{escaped_val}"))
    OPTIONAL {{
        ?entity cs:hasMention ?mention .
        ?mention cs:inBook ?src .
        OPTIONAL {{ ?mention cs:atPage ?page . }}
        ?src rdfs:label ?bookTitle ;
             cs:edition ?edition ;
             cs:canonType ?canonType .
    }}
    {edition_f}
    {canon_f}
}}
GROUP BY ?entity ?name
ORDER BY ?name
"""
