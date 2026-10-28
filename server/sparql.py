"""SPARQL query helpers for the Fuseki endpoint.

Everything that depends on the ontology (types, relationships, filterable
properties) is derived from ontology_schema.yaml at import time, so adding a
type or relationship needs only a YAML edit.
"""

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

# Plumbing predicates that link entities to provenance nodes rather than to
# other entities; never shown as relationships.
_STRUCTURAL = ("rdf:type", "rdfs:label", "cs:mentionedIn", "cs:hasMention")


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


def _build_type_parents(schema: dict) -> dict[str, str]:
    """Return {class: direct_parent_class} from entity_types subtypes."""
    parents: dict[str, str] = {}
    for type_def in schema["entity_types"].values():
        root = type_def["uri_suffix"]
        specs = list((type_def.get("subtypes") or {}).values())
        if type_def.get("default_subtype"):
            specs.append(type_def["default_subtype"])
        for spec in specs:
            if isinstance(spec, str):
                if spec != root:
                    parents[spec] = root
            else:
                chain = [spec["class"], *spec.get("parents", [root])]
                for child, parent in zip(chain, chain[1:]):
                    parents.setdefault(child, parent)
    return parents


_ONTOLOGY = _load_ontology_schema()

ENTITY_CLASS: dict[str, str] = {
    k: f"cs:{v}" for k, v in _ONTOLOGY["queryable_types"].items()
}

REL_SPEC: dict[str, dict] = _ONTOLOGY["relationship_properties"]

# (predicate suffix, direction) → API relationship name, for labelling the
# edges get_entity returns. First name in YAML order wins.
REL_NAME_BY_EDGE: dict[tuple[str, str], str] = {}
for _name, _spec in REL_SPEC.items():
    if "property" in _spec:
        REL_NAME_BY_EDGE.setdefault(
            (_spec["property"], _spec["direction"]), _name
        )

ALLOWED_PROPERTY_NAMES: tuple[str, ...] = tuple(
    _ONTOLOGY["allowed_filter_properties"]
)

TYPE_PARENT: dict[str, str] = _build_type_parents(_ONTOLOGY)


def type_ancestors(cls: str) -> list[str]:
    """Return every ancestor class of cls, nearest first."""
    chain = []
    while cls in TYPE_PARENT:
        cls = TYPE_PARENT[cls]
        chain.append(cls)
    return chain


def most_specific(types: list[str]) -> list[str]:
    """Drop types that are ancestors of another type in the list."""
    ancestors = {a for t in types for a in type_ancestors(t)}
    return sorted(t for t in set(types) if t not in ancestors)


def type_tree() -> dict[str, list[str]]:
    """Return {root_type: [all queryable descendant types]} for queryable
    types."""
    queryable = set(_ONTOLOGY["queryable_types"].values())
    tree: dict[str, list[str]] = {}
    for cls in sorted(queryable):
        chain = type_ancestors(cls)
        if not chain:
            tree.setdefault(cls, [])
            continue
        tree.setdefault(chain[-1], []).append(cls)
    return {root: sorted(children) for root, children in sorted(tree.items())}


def enum_values() -> dict[str, list[str]]:
    """Return {property: allowed literal values} for enumerated properties."""
    values: dict[str, set[str]] = {}
    for type_def in _ONTOLOGY["entity_types"].values():
        for spec in (type_def.get("enum_datatype_properties") or {}).values():
            values.setdefault(spec["property"], set()).update(spec["values"])
    return {k: sorted(v) for k, v in sorted(values.items())}


# ── Low-level helpers ─────────────────────────────────────────────────────


def _sparql_escape(value: str) -> str:
    """Escape a string for safe embedding in a SPARQL literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def short(uri: str | None) -> str:
    """Strip the cs: namespace from a URI."""
    uri = uri or ""
    return uri[len(CS) :] if uri.startswith(CS) else uri


def val(binding: dict, key: str) -> str | None:
    """Extract a binding value or return None."""
    item = binding.get(key)
    return item["value"] if item else None


def split_agg(binding: dict, key: str) -> list[str]:
    """Split a pipe-separated GROUP_CONCAT result into a list, filtering
    blanks."""
    raw = val(binding, key) or ""
    return [x for x in raw.split("|") if x]


def _name_match(var: str, name: str) -> str:
    """Graph pattern binding ?{var} to entities whose label or alias equals
    name."""
    escaped = _sparql_escape(name.strip().lower())
    return f"""\
    {{ ?{var} rdfs:label ?{var}Key . }} UNION {{ ?{var} cs:alias ?{var}Key . }}
    FILTER(LCASE(STR(?{var}Key)) = "{escaped}")
    FILTER EXISTS {{ ?{var} rdf:type ?{var}AnyType . }}"""


def _provenance_filter(
    edition: str, canon_type: str, var: str = "entity"
) -> str:
    """FILTER EXISTS clause keeping entities mentioned in a matching book."""
    conds = []
    if edition != "any":
        conds.append(f'?{var}Src cs:edition "{_sparql_escape(edition)}" .')
    if canon_type != "any":
        conds.append(f'?{var}Src cs:canonType "{_sparql_escape(canon_type)}" .')
    if not conds:
        return ""
    return f"FILTER EXISTS {{ ?{var} cs:mentionedIn ?{var}Src . {' '.join(conds)} }}"


def _property_filter(
    prop: str, value: str, i: int, match: str = "exact"
) -> str:
    """Clause matching ?entity cs:{prop} against value.

    Literal objects are compared by value; entity objects by their label, so
    nationality = "Breland" matches the Breland entity.
    """
    escaped = _sparql_escape(value.strip().lower())
    target = f"LCASE(COALESCE(STR(?fl{i}), STR(?fv{i})))"
    test = (
        f'CONTAINS({target}, "{escaped}")'
        if match == "contains"
        else f'{target} = "{escaped}"'
    )
    return f"""\
    ?entity cs:{prop} ?fv{i} .
    OPTIONAL {{ ?fv{i} rdfs:label ?fl{i} . }}
    FILTER({test})"""


async def sparql_select(query: str) -> list[dict]:
    """Execute a SPARQL SELECT against Fuseki; return the bindings list."""

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{_FUSEKI_ENDPOINT}/sparql",
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()
    return response.json()["results"]["bindings"]


async def fuseki_reachable() -> bool:
    """Return True if Fuseki responds to a ping."""
    base = _FUSEKI_ENDPOINT.rsplit("/", 1)[0]
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/$/ping")
    except httpx.HTTPError:
        return False
    return r.status_code == 200


# ── Query builders ────────────────────────────────────────────────────────


def build_list_names_query(
    entity_type: str,
    edition: str,
    canon_type: str,
    filters: dict[str, str],
    limit: int,
    offset: int,
) -> str:
    """Names of entities of a type, one page, alphabetical."""
    clauses = "\n".join(
        _property_filter(prop, value, i)
        for i, (prop, value) in enumerate(filters.items())
    )
    return f"""\
{PREFIXES}
SELECT ?name WHERE {{
  {{ SELECT DISTINCT ?entity ?name WHERE {{
    ?entity rdf:type {ENTITY_CLASS[entity_type]} ;
            rdfs:label ?name .
{clauses}
    {_provenance_filter(edition, canon_type)}
  }} }}
}}
ORDER BY LCASE(STR(?name))
LIMIT {limit} OFFSET {offset}
"""


def build_count_query(
    entity_type: str,
    edition: str,
    canon_type: str,
    filters: dict[str, str],
    match: str = "exact",
) -> str:
    """Total number of entities build_list_names_query would page over."""
    clauses = "\n".join(
        _property_filter(prop, value, i, match)
        for i, (prop, value) in enumerate(filters.items())
    )
    return f"""\
{PREFIXES}
SELECT (COUNT(DISTINCT ?entity) AS ?total) WHERE {{
    ?entity rdf:type {ENTITY_CLASS[entity_type]} .
{clauses}
    {_provenance_filter(edition, canon_type)}
}}
"""


def build_search_by_property_query(
    entity_type: str,
    property_name: str,
    value: str,
    match: str,
    edition: str,
    canon_type: str,
    limit: int,
    offset: int,
) -> str:
    """Names of entities whose property matches value."""
    return f"""\
{PREFIXES}
SELECT ?name WHERE {{
  {{ SELECT DISTINCT ?entity ?name WHERE {{
    ?entity rdf:type {ENTITY_CLASS[entity_type]} ;
            rdfs:label ?name .
{_property_filter(property_name, value, 0, match)}
    {_provenance_filter(edition, canon_type)}
  }} }}
}}
ORDER BY LCASE(STR(?name))
LIMIT {limit} OFFSET {offset}
"""


def build_search_entities_query(
    text: str, entity_type: str | None, limit: int
) -> str:
    """Entities whose label or alias contains text; exact and prefix matches
    first."""
    escaped = _sparql_escape(text.strip().lower())
    type_clause = (
        f"?entity rdf:type {ENTITY_CLASS[entity_type]} ." if entity_type else ""
    )
    return f"""\
{PREFIXES}
SELECT ?name (MIN(?score) AS ?best) (SAMPLE(?hitText) AS ?matched)
       (GROUP_CONCAT(DISTINCT STR(?type); SEPARATOR="|") AS ?types)
WHERE {{
    ?entity rdfs:label ?name .
    {type_clause}
    {{ ?entity rdfs:label ?hit . }} UNION {{ ?entity cs:alias ?hit . }}
    BIND(LCASE(STR(?hit)) AS ?h)
    FILTER(CONTAINS(?h, "{escaped}"))
    BIND(IF(?h = "{escaped}", 0, IF(STRSTARTS(?h, "{escaped}"), 1, 2)) AS ?score)
    BIND(STR(?hit) AS ?hitText)
    ?entity rdf:type ?type .
}}
GROUP BY ?name
ORDER BY ?best STRLEN(STR(?name)) LCASE(STR(?name))
LIMIT {limit}
"""


def build_search_text_query(
    words: list[str], entity_type: str | None, limit: int
) -> str:
    """Entities whose name + description contain the most query words."""
    type_clause = (
        f"?entity rdf:type {ENTITY_CLASS[entity_type]} ." if entity_type else ""
    )
    score = " + ".join(
        f'IF(CONTAINS(?hay, "{_sparql_escape(w)}"), 1, 0)' for w in words
    )
    return f"""\
{PREFIXES}
SELECT ?entity ?name ?desc ?score WHERE {{
    ?entity cs:description ?desc ;
            rdfs:label ?name .
    {type_clause}
    BIND(LCASE(CONCAT(STR(?name), " ", STR(?desc))) AS ?hay)
    BIND(({score}) AS ?score)
    FILTER(?score > 0)
}}
ORDER BY DESC(?score) STRLEN(STR(?desc))
LIMIT {limit}
"""


def build_resolve_query(name: str) -> str:
    """Entities whose label or alias equals name, with their types."""
    return f"""\
{PREFIXES}
SELECT ?e ?label (GROUP_CONCAT(DISTINCT STR(?type); SEPARATOR="|") AS ?types)
WHERE {{
{_name_match("e", name)}
    ?e rdfs:label ?label ;
       rdf:type ?type .
}}
GROUP BY ?e ?label
"""


def build_literals_query(uri: str) -> str:
    """Every literal property of one entity."""
    return f"""\
{PREFIXES}
SELECT ?p ?o WHERE {{
    <{uri}> ?p ?o .
    FILTER(isLiteral(?o))
}}
"""


def build_edges_query(uri: str) -> str:
    """Every edge between one entity and other named entities, both
    directions."""
    structural = ", ".join(_STRUCTURAL)
    return f"""\
{PREFIXES}
SELECT ?p ?dir ?otherName (GROUP_CONCAT(DISTINCT STR(?type); SEPARATOR="|") AS ?types)
WHERE {{
    {{ <{uri}> ?p ?other . BIND("out" AS ?dir) }}
    UNION
    {{ ?other ?p <{uri}> . BIND("in" AS ?dir) }}
    FILTER(?p NOT IN ({structural}))
    ?other rdfs:label ?otherName .
    OPTIONAL {{ ?other rdf:type ?type . }}
}}
GROUP BY ?p ?dir ?otherName
ORDER BY ?p ?dir LCASE(STR(?otherName))
"""


def build_sources_query(uri: str) -> str:
    """Books and pages that mention one entity."""
    return f"""\
{PREFIXES}
SELECT ?book ?edition ?canonType
       (GROUP_CONCAT(DISTINCT STR(?page); SEPARATOR="|") AS ?pages)
WHERE {{
    <{uri}> cs:hasMention ?m .
    ?m cs:inBook ?src .
    OPTIONAL {{ ?m cs:atPage ?page . }}
    ?src rdfs:label ?book .
    OPTIONAL {{ ?src cs:edition ?edition . }}
    OPTIONAL {{ ?src cs:canonType ?canonType . }}
}}
GROUP BY ?book ?edition ?canonType
ORDER BY ?book
"""


def build_relationship_query(
    uri: str, relationship: str, limit: int, offset: int, count: bool = False
) -> str:
    """Entities reached from the entity at uri over one API relationship."""
    spec = REL_SPEC[relationship]
    if spec.get("derived") == "capital":
        pattern = _capital_pattern(uri, spec["status"])
    else:
        edge = (
            f"?entity cs:{spec['property']} ?related ."
            if spec["direction"] == "out"
            else f"?related cs:{spec['property']} ?entity ."
        )
        pattern = f"    VALUES ?entity {{ <{uri}> }}\n    {edge}"
    if count:
        return f"""\
{PREFIXES}
SELECT (COUNT(DISTINCT ?related) AS ?total) WHERE {{
{pattern}
    ?related rdfs:label ?relName .
}}
"""
    return f"""\
{PREFIXES}
SELECT ?relName (GROUP_CONCAT(DISTINCT STR(?type); SEPARATOR="|") AS ?types)
WHERE {{
{pattern}
    ?related rdfs:label ?relName .
    OPTIONAL {{ ?related rdf:type ?type . }}
}}
GROUP BY ?relName
ORDER BY LCASE(STR(?relName))
LIMIT {limit} OFFSET {offset}
"""


def _capital_pattern(uri: str, status: str) -> str:
    """Cities that are capitals of the nation, with the given capital status.

    A city counts when it names the nation in cs:capitalOf (status defaults to
    current if the text gave none), or, when it names no nation, when it
    carries a cs:capitalStatus and the nation is its nearest Nation ancestor in
    the containment hierarchy.
    """
    escaped = _sparql_escape(status)
    return f"""\
    VALUES ?entity {{ <{uri}> }}
    {{
        ?related cs:capitalOf ?entity .
        OPTIONAL {{ ?related cs:capitalStatus ?st0 . }}
        BIND(COALESCE(?st0, "current") AS ?st)
    }} UNION {{
        ?related cs:capitalStatus ?st .
        FILTER NOT EXISTS {{ ?related cs:capitalOf ?anyNation . }}
        ?entity cs:contains+ ?related .
        FILTER NOT EXISTS {{
            ?mid rdf:type cs:Nation .
            ?entity cs:contains+ ?mid .
            ?mid cs:contains+ ?related .
        }}
    }}
    FILTER(?st = "{escaped}")"""


def build_location_ancestors_query(uri: str) -> str:
    """All ancestors of a location (walks cs:contains upwards), unordered."""
    return f"""\
{PREFIXES}
SELECT ?ancestor ?ancestorName (GROUP_CONCAT(DISTINCT STR(?type); SEPARATOR="|") AS ?types)
WHERE {{
    ?ancestor cs:contains+ <{uri}> ;
              rdfs:label ?ancestorName .
    OPTIONAL {{ ?ancestor rdf:type ?type . }}
}}
GROUP BY ?ancestor ?ancestorName
"""


def build_contains_edges_query(uris: list[str]) -> str:
    """Direct cs:contains edges among the given nodes (parent, child)."""
    values = " ".join(f"<{u}>" for u in uris)
    return f"""\
{PREFIXES}
SELECT ?parent ?child WHERE {{
    VALUES ?parent {{ {values} }}
    VALUES ?child {{ {values} }}
    ?parent cs:contains ?child .
}}
"""


def build_location_children_query(
    uri: str, depth: str, limit: int, offset: int, count: bool = False
) -> str:
    """Direct children (depth=direct) or all descendants (depth=all) of a
    location."""
    path = "cs:contains" if depth == "direct" else "cs:contains+"
    head = (
        "SELECT (COUNT(DISTINCT ?child) AS ?total)"
        if count
        else 'SELECT ?childName (GROUP_CONCAT(DISTINCT STR(?type); SEPARATOR="|") AS ?types)'
    )
    tail = (
        ""
        if count
        else f"GROUP BY ?childName\nORDER BY LCASE(STR(?childName))\nLIMIT {limit} OFFSET {offset}"
    )
    return f"""\
{PREFIXES}
{head}
WHERE {{
    VALUES ?loc {{ <{uri}> }}
    ?loc {path} ?child .
    ?child rdfs:label ?childName .
    OPTIONAL {{ ?child rdf:type ?type . }}
}}
{tail}
"""


def build_list_sources_query() -> str:
    """Every ingested source book with its entity count."""
    return f"""\
{PREFIXES}
SELECT ?title ?edition ?canonType (COUNT(DISTINCT ?e) AS ?entities) WHERE {{
    ?book rdf:type cs:SourceBook ;
          rdfs:label ?title .
    OPTIONAL {{ ?book cs:edition ?edition . }}
    OPTIONAL {{ ?book cs:canonType ?canonType . }}
    OPTIONAL {{ ?e cs:mentionedIn ?book . }}
}}
GROUP BY ?title ?edition ?canonType
ORDER BY ?title
"""
