# Ontology

Namespace: `http://campaignsetting.io/ontology#` (SPARQL prefix: `cs:`)

All entities extracted from sourcebooks live in this namespace. The graph is a standard RDF/SPARQL dataset hosted on Apache Jena Fuseki with TDB2 persistence. Everything on this page is generated from `config/ontology_schema.yaml`; the MCP tool `describe_schema` returns the same information to client models.

!!! warning "Changing the ontology"
    Any change to `config/ontology_schema.yaml` changes what the graph holds. It requires a **minor version bump** in `server/__init__.py` (CI enforces this) and a **re-ingest**. Backups are stored per major.minor, and serve mode only loads a backup of its own major.minor, so graphs built with different ontologies never mix. `chart/files/ontology_schema.yaml` must stay an exact copy (CI checks).

---

## Entity types

Valid values for `entity_type` in `list_names`, `search_entities`, `search_by_property` and `search_text`. A type includes its subtypes: `list_names(entity_type="Location")` lists rivers, cities, mines, and so on.

### Characters and organisations

| Type | Description |
|---|---|
| `NPC` | Named individuals with a proper name and established identity |
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
| `Feat` | Named character-build options |
| `Creature` | Named creature species or individual monsters |
| `CreatureType` | Creature categories; subtypes `Aberration`, `Beast`, `Celestial`, `Construct`, `Dragon`, `Elemental`, `Fey`, `Fiend`, `Giant`, `Humanoid`, `Monstrosity`, `Ooze`, `Plant`, `Undead` |

### Culture

| Type | Description |
|---|---|
| `Language` | Named languages; subtypes `Dialect`, `Creole`, `Pidgin`, `CodeLanguage` (cants, ciphers, military codes) |
| `Tradition` | Cultural customs of a people: fashion, etiquette, law, dance, music, art, ritual, cuisine, festivals. `category` holds which |
| `Dish` | Named foods and culinary preparations (food only) |
| `Drink` | Named drinks; subtypes `Wine`, `Beer`, `Spirit`, `Cocktail` |

### Resources

| Type | Description |
|---|---|
| `Material` | Named raw materials; subtypes `Metal`, `Gem`, `Textile`, `CraftingMaterial` |

### History

| Type | Description |
|---|---|
| `Event` | Named historical events; subtypes `War`, `Battle`, `Treaty`, `Disaster`, `Founding`, `Era` (named ages such as the Age of Demons) |

### Locations

| Type | Description |
|---|---|
| `Location` | Base class; catch-all for named places |
| `Continent`, `Nation`, `Region` | Broad political and geographic areas |
| `City`, `Ward`, `Neighborhood` | Settlements and their districts |
| `Tavern` | Taverns, inns, pubs and breweries |
| `Restaurant` | Places whose business is serving meals |
| `Shop` | Places that sell products; `shopType` says which kind (blacksmith, florist, magic shop, tailor, spice shop, vendor stall, ...) |
| `Mine` | Named mines, quarries and digs |
| `Winery` | Named vineyards, wineries and distilleries (breweries are Taverns) |
| `River` | Flowing watercourses only |
| `Lake` | Inland bodies of still water |
| `Sea` | Seas, oceans, bays, gulfs, sounds and straits |
| `Mountain`, `MountainRange`, `Forest`, `Jungle`, `Desert`, `Plain`, `Island` | Terrain |
| `Dungeon`, `Ruin`, `Plane`, `Moon` | Structures and planes |

### Items

| Type | Description |
|---|---|
| `Item` | Base class for named items |
| `Attire` | Named clothing and worn accessories, magical or not |
| `Book` | Named books that exist inside the world (author and language linked) |
| `MagicItem` | Base class for magic items |
| `WondrousItem`, `MagicArmor`, `MagicWeapon`, `Potion`, `Ring`, `Rod`, `Scroll`, `Staff`, `Wand` | Magic item kinds |

---

## Time

Two simple fields, chosen so that a small extraction model gets them right:

- **`status`**: `current` or `past`, on locations and languages. Destroyed places, former kingdoms such as Galifar or Cyre, and dead languages are `past`.
- **Years**: `fromYear` / `untilYear` on locations and `startYear` / `endYear` on events, as integers in **YK** (Year of the Kingdom). Years before YK are negative. They are filled **only when the text states the year outright**: the model copies the number and never calculates one. A missing year means *unknown*, not *current*; `status` says whether something still holds.

Capitals and rulers carry their own current/former split: `capitalStatus` (`current` / `former`) on a city, and `ruledBy` / `formerlyRuledBy` on a nation or region.

---

## Relationships

Valid values for `relationship` in `get_relationships`. Each one reads in one direction; inverses have their own names (`memberOf` ↔ `members`).

| Relationship | Reads | Meaning |
|---|---|---|
| `allies`, `enemies`, `sibling`, `spouse` | NPC → NPC | Personal relationships |
| `memberOf` / `members` | NPC ↔ Faction | Membership in factions, houses and families |
| `leaderOf` / `leader` | NPC ↔ Faction | Leadership |
| `factionAllies`, `factionEnemies` | Faction → Faction | Alliances and enmities between factions |
| `headquarters` | Faction → Location | Headquarters |
| `memberFamilyOf` / `families` | Family ↔ DragonmarkedHouse | House structure |
| `operatesIn` | Faction → Location | Where a faction operates |
| `race`, `characterClass`, `nationality` | NPC → Race / Class / Nation | Character facts |
| `locatedIn` / `residents` | NPC ↔ Location | Where an NPC is usually found |
| `equipment` | NPC → Item | Items carried |
| `worships` / `worshippers` | NPC, Faction ↔ Deity | Worship |
| `primaryDeity`, `deityOf` | Religion ↔ Deity | Pantheon structure |
| `contains` / `containedIn` | Location ↔ Location | Direct spatial containment (use `get_location_hierarchy` for all levels) |
| `controlledBy` / `controls` | Location ↔ Faction | Control |
| `capital`, `formerCapitals` | Nation → City | Capital cities (derived, see below) |
| `capitalOf` | City → Nation | The nation a city is or was capital of |
| `ruler`, `formerRulers` / `rules` | Nation, Region ↔ NPC | Rulers now and in the past |
| `produces` / `producedAt` | Location ↔ Material, Drink | What mines, wineries, breweries, regions and nations produce |
| `menu` / `servedIn` | Location ↔ Dish, Drink | What a city, tavern or restaurant serves (also one-line menu boxes) |
| `localDishes` / `origin` | Location ↔ Dish | A place's own dishes, and where a dish comes from (also when implied by its name) |
| `manifestZoneOf` / `manifestZones` | Location ↔ Plane | Manifest zones |
| `participants` / `participatedIn` | Event ↔ NPC, Faction, Nation | Who took part |
| `eventLocation` / `events` | Event ↔ Location | Where it happened |
| `practicedBy` / `traditions` | Tradition ↔ Nation, Race, Faction | Who practices a tradition |
| `spokenIn` / `languages` | Language ↔ Location | Where a language is spoken |
| `spokenByRace`, `spokenByFaction` | Language → Race / Faction | Who speaks it |
| `descendsFrom` / `descendants` | Language ↔ Language | Language descent (creoles have several parents) |
| `dialectOf` / `dialects` | Dialect ↔ Language | Dialects |
| `nativeRegions` | Race → Location | Native regions |
| `author` / `wrote`, `writtenIn` | Book ↔ NPC, Book → Language | In-game books |
| `foundIn` / `creatures` | Creature ↔ Location | Habitats |

**Capitals** are derived at query time. A city is a capital of a nation when it names the nation in `capitalOf`, or, when it names no nation, when it carries a `capitalStatus` and the nation is its nearest `Nation` ancestor in the containment hierarchy. `capital` returns current capitals and `formerCapitals` former ones.

Symmetric relationships (`enemies`, `sibling`, `spouse`, faction allies and enemies) are written in both directions at ingestion time.

---

## Filterable properties

Valid keys for `filters` in `list_names` and `property_name` in `search_by_property`. Literal properties match their value. Entity-valued properties (e.g. `nationality`, `practicedBy`) match the linked entity's **name**, so `{"nationality": "Breland"}` works.

| Group | Properties |
|---|---|
| Shared | `description`, `alias`, `status`, `edition`, `canonType` |
| NPC | `nationality`, `alignment`, `ancestry`, `physicalDescription`, `level`, `wantsAndNeeds`, `worships`, `memberOf`, `leaderOf`, `locatedIn`, `hasRace`, `hasClass` |
| Faction | `factionType`, `dragonmark`, `factionLocatedIn`, `operatesIn` |
| Location | `climate`, `shopType`, `ambiance`, `controlledBy`, `capitalStatus`, `capitalOf`, `ruledBy`, `produces`, `manifestZoneOf`, `fromYear`, `untilYear` |
| Race, feat, food | `nativeRegion`, `prerequisites`, `cuisineType`, `originatesIn`, `servedIn`, `ingredient` |
| Items and books | `rarity`, `requiresAttunement`, `charges`, `author`, `writtenIn` |
| Language | `languageFamily`, `script`, `format`, `spokenIn`, `spokenByRace`, `descendsFrom`, `dialectOf` |
| Event, tradition | `startYear`, `endYear`, `occurredAt`, `participant`, `category`, `practicedBy` |
| Creature | `challengeRating`, `size`, `habitat`, `foundIn` |

Enumerated values: `status` = `current` / `past`; `capitalStatus` = `current` / `former`; Shop `shopType` = `general store`, `blacksmith`, `armorer`, `weaponsmith`, `florist`, `magic shop`, `alchemist`, `apothecary`, `tailor`, `cobbler`, `jeweler`, `bookshop`, `spice shop`, `bakery`, `butcher`, `grocer`, `vendor stall`, `pawn shop`, `curio shop`, `other`; Tradition `category` = `fashion`, `etiquette`, `law`, `dance`, `music`, `art`, `ritual`, `cuisine`, `festival`, `other`.

Scripts and language families are text fields, not entities, on purpose: many Eberron names are a language, a script and a family at once (Corvaguri, Siberine, Rhiavhaan), and entity identity is name-based.

---

## Source provenance

Every entity is linked to one or more `cs:SourceBook` nodes via `cs:mentionedIn`, and to `cs:Mention` nodes that pair a book with a page (`cs:inBook`, `cs:atPage`). Each source book carries:

- `rdfs:label`: the book title
- `cs:edition`: `3e`, `4e`, `5e`, or `any`
- `cs:canonType`: `canon`, `kanon`, or `community`

`get_sources` returns books and pages per entity. `list_names`, `search_by_property` and `get_sources` accept `edition` and `canon_type` filters.
