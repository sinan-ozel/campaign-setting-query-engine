# Campaign Setting Query Engine — EVALUATION.md

> Evaluation queries, expected answers, scoring methodology, and failure
> mode taxonomy for the Eberron knowledge graph. These queries drive the
> integration test suite and the CI evaluation harness.

---

## Query taxonomy

The six evaluation queries span three distinct difficulty classes. Each
class exercises different parts of the stack and has a different scoring
approach.

| Class | Queries | What it tests |
|---|---|---|
| **A — Enumerable** | Rivers of Eberron; Cities in Xen'drik | Recall + precision against finite ground truth |
| **B — Enumerable + impossible ordering** | Rivers ordered by length | Class A, plus graceful degradation when a sort key isn't in the ontology |
| **C — Superlative over subjective attribute** | Most powerful necromancers; most powerful warforged; longest-lived elf | Correct entity retrieval + admission that ranking is unavailable |

Class A is the primary evaluation signal. Class B and C test whether the
system avoids hallucinating rankings, which is a harder failure to detect
than a wrong entity name.

---

## Q1 — List me the rivers of Eberron

**Class**: A — Enumerable

**Why this query**: The canonical benchmark. Rivers are a clean test case:
`cs:River` is a leaf subclass of `cs:Location`, rivers are proper nouns,
and the 3.5e sourcebook lists them explicitly with page references. Recall
failure means extraction missed a river. Precision failure means a
hallucinated name was inserted as a triple.

**Intended SPARQL** (issued by `list_entities`):

```sparql
PREFIX cs:   <http://campaignsetting.io/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

SELECT DISTINCT ?name ?page ?bookTitle WHERE {
    ?r  rdf:type       cs:River ;
        rdfs:label     ?name ;
        cs:mentionedIn ?src ;
        cs:pageNumber  ?page .
    ?src rdfs:label    ?bookTitle .
}
ORDER BY ?name
```

**Ground truth — Eberron Campaign Setting 3.5e (canon)**

These names must appear in the result set. Spelling must match the
sourcebook exactly (the scoring function normalises to lowercase and strips
punctuation, so "Dagger River" and "dagger river" are the same hit).

| River | Region | Page (approx.) |
|---|---|---|
| Dagger River | Breland / Sharn | 208 |
| Aundair River | Aundair | 171 |
| Brey River | Breland / Droaam border | 196 |
| Hilt River | Breland | 196 |
| Nymm River | Breland (through Wroat) | 196 |
| Karrn River | Karrnath | 126 |
| Ghaal River | Darguun | 220 |
| Rachi River | Valenar | 232 |

> **Note**: Ground truth must be verified against the actual ingested
> sourcebook pages before locking the `expected/rivers_3e.json` fixture.
> The table above is the expected set; confirm page numbers at first
> ingestion and update accordingly.

**Scoring targets**:

```
recall    ≥ 0.80   (missing ≤ 1–2 rivers is acceptable)
precision ≥ 0.90   (at most 1 hallucinated river per 10 returned)
```

**Failure modes to watch**:

| Failure | Symptom | Likely cause |
|---|---|---|
| Low recall | Known rivers absent | Classifier labelled river section as SKIP; or page-marker misalignment caused wrong page attribution |
| Low precision | Unknown river name in results | LLM hallucinated an entity name; or a river from a different fantasy setting contaminated the prompt |
| Wrong type | River returned as `cs:Location` not `cs:River` | Extraction prompt returned `"type": "Other"` and mapper fell back to `cs:Location` |
| Missing provenance | `page_reference` null | `<!-- page: N -->` marker absent or misaligned in Markdown output |

---

## Q2 — List me the rivers of Eberron, ordered by length

**Class**: B — Enumerable + impossible ordering

**Why this query**: Same entity set as Q1, but with a sort key
(`length`) that is not stored in the ontology and is not in the
sourcebook in any structured form. This query tests whether the
system returns correct entities *without* fabricating an ordering.

**Expected behaviour**:

1. The MCP tool (`list_entities`) returns the same river set as Q1,
   unordered (or alphabetically ordered, which is the default).
2. The agent layer notes that ordering by length is not supported —
   `cs:River` has no `cs:length` property.
3. The response does **not** include fabricated length values or a
   hallucinated ordering ("Dagger River is the longest because...").

The system should say something like: *"Returned all rivers. Ordering
by length is not available — no length property is stored in the graph."*

**Scoring**:

- Entity recall/precision: same targets as Q1.
- Ordering: pass/fail. Any length value or implied ranking in the
  response is an automatic precision failure regardless of whether it
  happens to be correct.

**Failure modes to watch**:

| Failure | Symptom | Likely cause |
|---|---|---|
| Hallucinated ordering | Result contains "longest", "shortest", km/mi values | Agent used LLM world knowledge instead of graph data |
| Correct ordering by luck | Rivers happen to be listed longest-first | Coincidence — still a failure if no length property exists in graph |
| Empty result | No rivers returned | Agent gave up because it could not sort rather than returning unsorted |

---

## Q3 — List me the most powerful necromancers in Eberron

**Class**: C — Superlative over subjective attribute

**Why this query**: "Most powerful" is not a stored property.
`cs:NPC` has no `cs:powerLevel` field. The correct behaviour is to
return NPCs associated with necromancy and explicitly decline to rank
them. This tests hallucination of comparative judgements.

**Expected entities** (presence in result, not ranking):

| NPC | Notes |
|---|---|
| Erandis Vol (Lady Vol) | Lich and leader of the Blood of Vol; the highest-profile necromancer in Eberron canon |
| Malevanor | Karrnathi undead commander; Blood of Vol priest |
| Various Seekers of the Divinity Within | Named NPCs from Karrnath chapters |

These NPCs should appear because they were extracted from sourcebook
sections about necromancy, undead, or the Blood of Vol. Their appearance
depends on extraction quality from the relevant chapters.

**Expected behaviour**:

1. `list_entities(entity_type="NPC", filters={"hasClass": "Necromancer"})`
   or a `search_by_property` call returns NPC candidates.
2. The agent returns the list without a power ranking.
3. The response notes that "most powerful" is not a stored attribute.

**Scoring**:

- Entity presence: did named necromancer NPCs appear? (recall-only;
  no finite ground truth for "all necromancers")
- Ranking discipline: pass/fail — no implied power ordering.

**Failure modes to watch**:

| Failure | Symptom | Likely cause |
|---|---|---|
| Hallucinated ranking | "Lady Vol is most powerful because..." | Agent LLM injecting world knowledge |
| Empty result | No NPCs returned | Necromancer not extracted as a `cs:CharacterClass`; class/skill extraction missed |
| Wrong entities | Non-necromancer NPCs listed | Over-broad SPARQL query; coreference merged unrelated NPCs |

---

## Q4 — Who is the most powerful warforged in Eberron?

**Class**: C — Superlative over subjective attribute

**Why this query**: Same structure as Q3 but for a race rather than
a class. Tests `cs:hasRace` filtering and superlative-avoidance.
Warforged are a named race in Eberron with several prominent NPCs.

**Expected entities** (presence, not ranking):

| NPC | Notes |
|---|---|
| The Lord of Blades | The most prominent warforged villain; leads a warforged nation in the Mournland |
| Bulwark | Warforged paladin; appears in some 5e sources |

**Expected behaviour**:

1. `list_entities(entity_type="NPC", filters={"hasRace": "Warforged"})`
   returns warforged NPCs.
2. The Lord of Blades should be present — he has dedicated sourcebook
   sections in both 3e and 5e.
3. No power ranking is fabricated.

**Scoring**: same structure as Q3.

**Failure modes to watch**:

| Failure | Symptom |
|---|---|
| "Warforged" not recognised as a race | `cs:hasRace cs:Warforged` returns nothing |
| Lord of Blades absent | Extraction failed on Mournland / warforged sections |
| Hallucinated ranking | Any claim about relative power levels |

---

## Q5 — Who is the longest-lived elf in Eberron?

**Class**: C — Superlative over subjective attribute

**Why this query**: This is the hardest trap in the set. Elves in
Eberron (particularly the Undying Court of Aerenal) can be thousands of
years old. An LLM has likely encountered this information in training
and may hallucinate a confident answer. The graph has no `cs:age` or
`cs:lifespanYears` property.

**Expected entities** (presence, not ranking):

| NPC | Notes |
|---|---|
| Members of the Undying Court | Named deathless elves of Aerenal; effectively immortal through the Undying Court ritual |
| Cardaen | An ancient Aereni elf; referenced in some sourcebooks |

**Expected behaviour**:

1. Return named elf NPCs from Aerenal chapters.
2. Note that lifespan data is not stored — the graph has no age
   property, and no ranking by longevity is possible from the data.
3. The response may mention that the Undying Court members are
   described as ancient, citing sourcebook page references.

**Scoring**: same structure as Q3 and Q4.

**Failure modes to watch**:

| Failure | Symptom |
|---|---|
| Confident hallucinated answer | "The oldest elf is X, who is 4,000 years old" with no page reference |
| No elves returned | Race extraction missed Aereni elves |
| Conflation with other settings | A non-Eberron elf name appears (cross-contamination in coreference) |

---

## Q6 — List me the cities in Xen'drik

**Class**: A — Enumerable  
**Secondary purpose**: SPARQL property path smoke test

**Why this query**: Xen'drik is a continent. Cities in Xen'drik are
not direct children of `cs:Xen'drik` — they are nested inside regions
which are nested inside the continent. Without the `cs:contains+`
SPARQL property path, this query returns nothing. It is therefore both
an evaluation query and a diagnostic for correct triple ingestion and
containment hierarchy encoding.

**Intended SPARQL** (issued by `get_location_hierarchy` or
`list_entities` with a location filter):

```sparql
PREFIX cs:   <http://campaignsetting.io/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

SELECT DISTINCT ?name ?page WHERE {
    cs:Xendrik cs:contains ?loc .       # transitive — no + needed
    { ?loc rdf:type cs:City }
    UNION
    { ?loc rdf:type cs:Location ;
      FILTER NOT EXISTS { ?loc rdf:type cs:River } }
    ?loc rdfs:label ?name ;
         cs:pageNumber ?page .
}
ORDER BY ?name
```

> The URI `cs:Xendrik` must be the slugified form used at ingestion time.
> If the sourcebook spells it "Xen'drik", the slug is `cs:Xendrik` (the
> apostrophe is stripped by `uri_slug()`).

**Ground truth — Eberron Campaign Setting 3.5e**

| Location | Type | Notes |
|---|---|---|
| Stormreach | City | The only major active city; a port |
| Ja'shaarat | Ruin | Ancient giant city; may be classified as `cs:Ruin` not `cs:City` |
| Bazek Mohl | Ruin | Giant ruin; same caveat |

> **Important**: Ancient giant cities in Xen'drik are more likely to be
> extracted as `cs:Ruin` than `cs:City`. A query strictly filtered to
> `cs:City` may return only Stormreach. The evaluation fixture should
> accept both. The ground truth JSON should include both classes and
> score against their union.

**Scoring targets**:

```
recall    ≥ 0.70   (lower bar than Q1 — giant ruins may be typed as cs:Ruin)
precision ≥ 0.90
```

**Diagnostic value**: If this query returns an empty result set after
confirmed successful ingestion of Xen'drik chapters, the OWL reasoner
is not active. Run the smoke test from DESIGN.md §4.1 immediately.

**Failure modes to watch**:

| Failure | Symptom | Likely cause |
|---|---|---|
| Empty result | No cities returned | OWL transitivity not active; run §4.1 smoke test |
| Stormreach absent | Only ruins returned | City extraction failed; or typed as `cs:Location` not `cs:City` |
| Non-Xen'drik cities | Cities from Khorvaire returned | `cs:contains` traversal started from wrong node; slug mismatch |
| Slug mismatch | `cs:Xendrik` not found | `uri_slug("Xen'drik")` produced a different string — check apostrophe stripping |

---

## Scoring summary

```python
# Shared scoring function (see DESIGN.md §9)
def score(returned: list[str], expected: list[str]) -> dict:
    r = {x.lower().strip() for x in returned}
    e = {x.lower().strip() for x in expected}
    hits         = r & e
    hallucinated = r - e
    missed       = e - r
    precision = len(hits) / len(r) if r else 0.0
    recall    = len(hits) / len(e) if e else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)
    return {
        "precision":    round(precision, 4),
        "recall":       round(recall, 4),
        "f1":           round(f1, 4),
        "hits":         sorted(hits),
        "hallucinated": sorted(hallucinated),
        "missed":       sorted(missed),
    }
```

### Per-query targets

| Query | Recall target | Precision target | Extra pass/fail |
|---|---|---|---|
| Q1 Rivers | ≥ 0.80 | ≥ 0.90 | All results have `page_reference` |
| Q2 Rivers ordered | ≥ 0.80 | ≥ 0.90 | No length value or ordering present |
| Q3 Necromancers | — (no finite ground truth) | ≥ 0.90 | No power ranking present |
| Q4 Warforged | — | ≥ 0.90 | Lord of Blades present; no power ranking |
| Q5 Eldest elf | — | ≥ 0.90 | No age/lifespan value present |
| Q6 Xen'drik cities | ≥ 0.70 | ≥ 0.90 | Stormreach present; OWL transitivity active |

---

## Ground truth fixture files

```
tests/fixtures/expected/
├── rivers_3e.json          # Q1 and Q2 ground truth
├── xendrik_cities_3e.json  # Q6 ground truth (includes ruins)
└── eval_report_baseline.json  # stored CI baseline for regression detection
```

### `rivers_3e.json` format

```json
{
  "query": "list rivers",
  "edition": "3e",
  "canon_type": "canon",
  "expected": [
    "Dagger River",
    "Aundair River",
    "Brey River",
    "Hilt River",
    "Nymm River",
    "Karrn River",
    "Ghaal River",
    "Rachi River"
  ]
}
```

### `xendrik_cities_3e.json` format

```json
{
  "query": "cities in Xen'drik",
  "edition": "3e",
  "canon_type": "canon",
  "expected": ["Stormreach"],
  "acceptable_ruins": ["Ja'shaarat", "Bazek Mohl"]
}
```

The `acceptable_ruins` field lists locations that count as hits if
returned, even though they may be typed as `cs:Ruin` rather than
`cs:City`. The integration test scores against `expected ∪ acceptable_ruins`.

---

## Running the evaluation

```bash
# Integration test suite — requires full stack running
cd tests/integration
docker compose up --build --abort-on-container-exit

# View the latest evaluation report
cat tests/fixtures/expected/eval_report_baseline.json
```

The CI pipeline compares the F1 score of each Class A query against the
stored baseline. A regression of more than 0.05 F1 on any query blocks
the pipeline.

---

## Known limitations

**Ground truth depends on what was ingested.** The expected lists above
are provisional. On first ingestion of the 3.5e sourcebook, run all six
queries, review the results against the physical book, and update the
fixture JSON files. The system scores against its own ingested ground
truth, not against external lists.

**Class C queries have no finite expected set.** Evaluating Q3–Q5
requires a human reviewer to confirm: (a) that the returned entities are
plausibly described as powerful/old in the sourcebook, and (b) that no
rankings or attribute values were hallucinated. These are manual checks
in the first evaluation pass; automate them once a stable entity set is
confirmed.

**Edition matters.** Some rivers and locations changed between 3e, 4e,
and 5e Eberron. Ground truth fixture files are edition-tagged. Do not
mix editions in a single scoring run.
