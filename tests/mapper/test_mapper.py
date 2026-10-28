"""Unit tests for graph_worker.mapper and the extractor prompt builder.

Runs against the real config/ontology_schema.yaml, so a schema edit that breaks
a property map or the prompt shows up here without an LLM.
"""

import graph_worker.extractor as ex
import graph_worker.mapper as mp

CS = mp.CS
META = {
    "document_id": "t",
    "title": "Test",
    "edition": "5e",
    "canon_type": "canon",
}


class FakeRedis(dict):
    """The three Redis calls the mapper makes, backed by a dict."""

    def get(self, key):
        return dict.get(self, key)

    def set(self, key, value):
        self[key] = value

    def sadd(self, *args):
        pass


def _triples(extracted: dict) -> set[str]:
    lines = mp.entities_to_triples(FakeRedis(), extracted, META, "7")
    return {line.strip() for line in lines}


def _t(s: str, p: str, o: str) -> str:
    return f"<{CS}{s}> <{CS}{p}> {o} ."


def _u(slug: str) -> str:
    return f"<{CS}{slug}>"


def test_prompt_mentions_every_new_array():
    for key in ("drinks", "materials", "traditions", "events"):
        assert f'"{key}"' in ex._EXTRACTOR_SYSTEM
    assert "A lake is NEVER a River" in ex._EXTRACTOR_SYSTEM


def test_prompt_leaves_room_for_chunks():
    # The chunker sizes chunks as CONTEXT_WINDOW minus this overhead; keep at
    # least a quarter of the 16k production window for the chunk itself.
    assert ex.prompt_overhead_tokens() < ex.CONTEXT_WINDOW * 3 // 4


def test_lake_subtype_case_insensitive():
    t = _triples({"locations": [{"name": "Lake Galifar", "type": "lake"}]})
    assert f"{_u('Lake_Galifar')} <{mp.RDF}type> <{CS}Lake> ." in t
    assert f"{_u('Lake_Galifar')} <{mp.RDF}type> <{CS}River> ." not in t


def test_capital_ruler_status_years():
    t = _triples(
        {
            "locations": [
                {
                    "name": "Wroat",
                    "type": "City",
                    "is_capital": "Current",
                    "capital_of": "Breland",
                    "status": "current",
                    "from_year": "-1,000 YK",
                },
                {
                    "name": "Breland",
                    "type": "Nation",
                    "ruler": "Boranel",
                    "former_rulers": ["Wroann"],
                    "is_capital": "no",
                    "until_year": "16,000 years ago",
                },
            ]
        }
    )
    assert _t("Wroat", "capitalStatus", '"current"') in t
    assert _t("Wroat", "capitalOf", _u("Breland")) in t
    assert _t("Wroat", "status", '"current"') in t
    assert _t("Wroat", "fromYear", f'"-1000"^^<{mp.XSD}integer>') in t
    assert _t("Breland", "ruledBy", _u("Boranel")) in t
    assert _t("Breland", "formerlyRuledBy", _u("Wroann")) in t
    assert not any("Breland> <" + CS + "capitalStatus" in x for x in t)
    assert not any("untilYear" in x for x in t)


def test_production_links_point_from_location():
    t = _triples(
        {
            "locations": [
                {"name": "Cannith Mine", "type": "Mine", "produces": ["Byeshk"]}
            ],
            "materials": [
                {
                    "name": "Byeshk",
                    "material_type": "Metal",
                    "found_in": ["Mror Holds"],
                }
            ],
            "drinks": [
                {
                    "name": "Dragons Breath",
                    "drink_type": "Cocktail",
                    "origin_location": "Sharn",
                    "ingredients": ["brandy"],
                }
            ],
        }
    )
    assert _t("Cannith_Mine", "produces", _u("Byeshk")) in t
    assert _t("Mror_Holds", "produces", _u("Byeshk")) in t
    assert _t("Sharn", "produces", _u("Dragons_Breath")) in t
    assert f"{_u('Dragons_Breath')} <{mp.RDF}type> <{CS}Cocktail> ." in t
    assert f"{_u('Byeshk')} <{mp.RDF}type> <{CS}Metal> ." in t
    assert _t("Dragons_Breath", "ingredient", '"brandy"') in t


def test_faction_members_and_description():
    t = _triples(
        {
            "factions": [
                {
                    "name": "House Cannith",
                    "faction_class": "DragonmarkedHouse",
                    "members": ["Merrix dCannith"],
                    "description": "Artificers.",
                }
            ]
        }
    )
    assert _t("Merrix_dCannith", "memberOf", _u("House_Cannith")) in t
    assert _t("House_Cannith", "description", '"Artificers."') in t


def test_language_tradition_event_book():
    t = _triples(
        {
            "languages": [
                {
                    "name": "Shaaratalk",
                    "language_kind": "Creole",
                    "status": "current",
                    "descends_from": ["Wroati", "Darguul"],
                    "scripts": ["Rhiavhaan"],
                    "formats": ["spoken"],
                }
            ],
            "traditions": [
                {
                    "name": "Cyran fashion",
                    "category": "Fashion",
                    "practiced_by": ["Cyre"],
                }
            ],
            "events": [
                {
                    "name": "The Last War",
                    "event_type": "War",
                    "start_year": 894,
                    "end_year": "996 YK",
                    "participants": ["Breland"],
                }
            ],
            "items": [
                {
                    "name": "The Ender Disquisition",
                    "item_type": "Book",
                    "author": "Ender",
                },
                {"name": "Stunning Attire", "item_type": "Attire"},
            ],
        }
    )
    assert f"{_u('Shaaratalk')} <{mp.RDF}type> <{CS}Creole> ." in t
    assert _t("Shaaratalk", "descendsFrom", _u("Darguul")) in t
    assert _t("Shaaratalk", "script", '"Rhiavhaan"') in t
    assert _t("Cyran_fashion", "category", '"fashion"') in t
    assert _t("Cyran_fashion", "practicedBy", _u("Cyre")) in t
    assert _t("The_Last_War", "startYear", f'"894"^^<{mp.XSD}integer>') in t
    assert _t("The_Last_War", "endYear", f'"996"^^<{mp.XSD}integer>') in t
    assert _t("The_Last_War", "participant", _u("Breland")) in t
    assert f"{_u('The_Ender_Disquisition')} <{mp.RDF}type> <{CS}Book> ." in t
    assert _t("The_Ender_Disquisition", "author", _u("Ender")) in t
    attire_types = {
        x for x in t if x.startswith(_u("Stunning_Attire") + f" <{mp.RDF}type>")
    }
    assert attire_types == {
        f"{_u('Stunning_Attire')} <{mp.RDF}type> <{CS}Attire> .",
        f"{_u('Stunning_Attire')} <{mp.RDF}type> <{CS}Item> .",
    }


def test_menu_dish_links_origin_and_served_in():
    t = _triples(
        {
            "dishes": [
                {
                    "name": "Brelish Shawarma with Goblin sauce",
                    "origin_location": "Breland",
                    "served_in": ["Stormreach"],
                    "ingredients": ["goblin sauce"],
                }
            ],
            "drinks": [
                {
                    "name": "Fireheart ale",
                    "drink_type": "Beer",
                    "origin_location": "Fireheart Brewery",
                    "served_in": ["Stormreach"],
                }
            ],
        }
    )
    dish = "Brelish_Shawarma_with_Goblin_sauce"
    assert _t(dish, "originatesIn", _u("Breland")) in t
    assert _t(dish, "servedIn", _u("Stormreach")) in t
    assert _t(dish, "ingredient", '"goblin sauce"') in t
    assert _t("Fireheart_ale", "servedIn", _u("Stormreach")) in t
    assert _t("Fireheart_Brewery", "produces", _u("Fireheart_ale")) in t


def test_shop_type_restaurant_and_brewery_tavern():
    t = _triples(
        {
            "locations": [
                {
                    "name": "Petal and Thorn",
                    "type": "Shop",
                    "shop_type": "Florist",
                },
                {
                    "name": "Odd Bits",
                    "type": "Shop",
                    "shop_type": "a bit of everything",
                },
                {"name": "The Golden Plate", "type": "Restaurant"},
                {
                    "name": "Fireheart Brewery",
                    "type": "Tavern",
                    "produces": ["Fireheart ale"],
                },
            ]
        }
    )
    assert _t("Petal_and_Thorn", "shopType", '"florist"') in t
    assert not any("Odd_Bits> <" + CS + "shopType" in x for x in t)
    assert f"{_u('The_Golden_Plate')} <{mp.RDF}type> <{CS}Restaurant> ." in t
    assert f"{_u('Fireheart_Brewery')} <{mp.RDF}type> <{CS}Tavern> ." in t
    assert _t("Fireheart_Brewery", "produces", _u("Fireheart_ale")) in t
