# Ontology

Namespace: `http://campaignsetting.io/ontology#` (SPARQL prefix: `cs:`)

All entities extracted from sourcebooks live in this namespace. The graph is a standard OWL/SPARQL dataset hosted on Apache Jena Fuseki with TDB2 persistence.

---

## Entity types

All of these are valid values for `entity_type` in `list_entities`, `get_entity`, and `search_by_property`.

### Characters

| Type | Description |
|---|---|
| `NPC` | Named individuals with a proper name and established identity |

### Organisations

| Type | Description |
|---|---|
| `Faction` | Named organisations: guilds, crime families, military orders, spy agencies, newspapers, noble houses |
| `DragonmarkedHouse` | Dragonmarked houses (subtype of Faction) |
| `Newspaper` | Press organisations (subtype of Faction) |
| `Family` | Noble or criminal families (subtype of Faction) |

### Beliefs

| Type | Description |
|---|---|
| `Religion` | Named religious systems and organised spiritual movements |
| `Deity` | Named divine beings, gods, and spiritual entities |

### Species and mechanics

| Type | Description |
|---|---|
| `Race` | Named biological species or ancestry types |
| `CharacterClass` | Named character classes, subclasses, archetypes, and prestige paths |
| `Skill` | Named discrete abilities, powers, or proficiencies |
| `Feat` | Named character-build options (feats and passive special abilities) |
| `Creature` | Named creature species or individual monsters |
| `CreatureType` | Creature categories (Aberration, Beast, Dragon, Undead, etc.) |

### Culture

| Type | Description |
|---|---|
| `Language` | Named languages, dialects, scripts, and coded communication systems |
| `Dish` | Named foods, drinks, and culinary preparations |

### Locations — broad

| Type | Description |
|---|---|
| `Location` | Base class; catch-all for named places |
| `Continent` | Continent-scale landmasses |
| `Nation` | Countries and political entities |
| `Region` | Sub-national areas |

### Locations — settlements

| Type | Description |
|---|---|
| `City` | Cities and towns |
| `Ward` | City districts |
| `Neighborhood` | Sub-ward areas |
| `Tavern` | Named taverns and inns |
| `Shop` | Named shops and establishments |

### Locations — terrain

| Type | Description |
|---|---|
| `River` | Named rivers |
| `Sea` | Seas, lakes, and large bodies of water |
| `Mountain` | Individual named mountains |
| `MountainRange` | Named mountain ranges |
| `Forest` | Named forests |
| `Jungle` | Named jungles |
| `Desert` | Named deserts |
| `Plain` | Named plains and flatlands |
| `Island` | Named islands |

### Locations — structures and planes

| Type | Description |
|---|---|
| `Dungeon` | Named dungeons and underground complexes |
| `Ruin` | Named ruins |
| `Plane` | Named planes of existence |
| `Moon` | Named moons |

### Items

| Type | Description |
|---|---|
| `Item` | Base class for named items |
| `MagicItem` | Generic named magic items |
| `WondrousItem` | Wondrous items |
| `Attire` | Named clothing, fashion garments, and worn accessories |
| `MagicArmor` | Named magic armour |
| `MagicWeapon` | Named magic weapons |
| `Potion` | Named potions |
| `Ring` | Named rings |
| `Rod` | Named rods |
| `Scroll` | Named scrolls |
| `Staff` | Named staves |
| `Wand` | Named wands |

---

## Relationships

Valid values for the `relationship` parameter in `get_relationships`.

| Relationship | Direction | Description |
|---|---|---|
| `allies` | NPC / Faction → NPC / Faction | Allied entities |
| `enemies` | NPC / Faction → NPC / Faction | Enemy entities (symmetric) |
| `sibling` | NPC → NPC | Siblings (symmetric) |
| `spouse` | NPC → NPC | Spouses (symmetric) |
| `members` | Entity → NPC | Members of an entity |
| `operatesIn` | Faction → Location | Locations a faction operates in |
| `contains` | Location → Location | Spatial containment |
| `worships` | NPC / Faction → Deity | Worship relationship |
| `hasPotentialMotive` | Entity → PotentialMotive | Extracted motives |
| `controlledBy` | Location → Faction | Controlling faction |
| `locatedIn` | Entity → Location | Primary location |
| `nationality` | NPC → Nation | Nationality |
| `hasEquipment` | NPC → Item | Equipment carried |
| `memberFamilyOf` | Family → DragonmarkedHouse | House membership |
| `foundIn` | Creature → Location | Habitats |
| `grantedSpell` | Deity → Skill | Spells granted by a deity |
| `craftedBy` | Item → Faction/NPC | Creator |
| `attuneRequiredClass` | Item → CharacterClass | Attunement class requirement |
| `itemFoundIn` | Item → Location | Where an item can be found |

Symmetric relationships (`enemies`, `sibling`, `spouse`, and faction allies/enemies) are written in both directions at ingestion time.

Spatial containment (`contains`) is traversed transitively via SPARQL property path `cs:contains+`. A query for everything Xen'drik contains returns all nested locations — regions, cities, dungeons, rivers — without any application-level recursion.

---

## Searchable properties

Valid values for `property_name` in `search_by_property`:

`nationality`, `alignment`, `ancestry`, `physicalDescription`, `level`, `wantsAndNeeds`, `worships`, `memberOf`, `leaderOf`, `locatedIn`, `controlledBy`, `factionType`, `factionClass`, `dragonmark`, `description`, `alias`, `canonicalName`, `pageNumber`, `edition`, `canonType`, `publisher`, `factionLocatedIn`, `operatesIn`, `dominantReligion`, `typicalClass`, `nativeRegion`, `hasRace`, `hasClass`, `hasSkill`, `climate`, `ambiance`, `prerequisites`, `cuisineType`, `originLocation`, `itemCategory`, `rarity`, `requiresAttunement`, `charges`, `rechargeCondition`, `bodySlot`, `grantedSpell`, `craftedBy`, `attuneRequiredClass`, `itemFoundIn`, `writingSystem`, `spokenIn`, `challengeRating`, `size`, `habitat`, `foundIn`

---

## Source provenance

Every entity is linked to one or more `cs:SourceBook` nodes via `cs:mentionedIn`. Each source book carries:

- `cs:edition` — `3e`, `4e`, `5e`, or `any`
- `cs:canonType` — `canon`, `kanon`, or `community`
- `rdfs:label` — the book title
- `cs:pageNumber` — page reference

All MCP tools accept `edition` and `canon_type` filters that push down to the SPARQL query, so you can restrict results to a specific edition or canonicity level.
