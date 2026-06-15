"""LLM-based section classifier and combined entity extractor.

The LLM provider, model, and connection details are read from a YAML file
(default /config/llm.yaml). Values matching ${VAR_NAME} are substituted from
environment variables at load time, so secrets and endpoint URLs never need to
be hardcoded.

Classifier: one call per chunk, returns ENTITIES or SKIP.
Extractor:  one call per ENTITIES chunk, returns all entity types as JSON.
"""

import json
import logging
import os
import re
from typing import Any

import litellm
import yaml

logger = logging.getLogger("graph_worker.extractor")

LLM_CONFIG_PATH = os.environ.get("LLM_CONFIG_PATH", "/config/llm.yaml")
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "4096"))

_llm_config: dict | None = None

# Silence litellm's verbose success logging.
litellm.success_callback = []
litellm.failure_callback = []


def _load_llm_config() -> dict:
    """Load config/llm.yaml, substituting ${VAR} from the environment."""
    try:
        with open(LLM_CONFIG_PATH) as f:
            raw = f.read()
    except FileNotFoundError:
        logger.warning(
            "extractor: %s not found — falling back to env vars "
            "LLAMA_CPP_HOST / LLM_MODEL.",
            LLM_CONFIG_PATH,
        )
        return {
            "api_base": os.environ.get("LLAMA_CPP_HOST", "http://localhost:8080/v1"),
            "model": os.environ.get("LLM_MODEL", "openai/gemma4:e2b"),
            "api_key": "dummy",
            "timeout": 150,
        }

    def _sub(m: re.Match) -> str:
        var = m.group(1)
        return os.environ.get(var, m.group(0))

    substituted = re.sub(r"\$\{([^}]+)\}", _sub, raw)
    config = yaml.safe_load(substituted) or {}
    logger.info(
        "extractor: LLM config loaded (model=%s, api_base=%s)",
        config.get("model"),
        config.get("api_base"),
    )
    return config


def _get_llm_config() -> dict:
    global _llm_config
    if _llm_config is None:
        _llm_config = _load_llm_config()
    return _llm_config


def _complete(messages: list[dict], max_tokens: int) -> str:
    """Call the LLM synchronously; all provider params come from llm.yaml.

    max_tokens is always passed explicitly per call so classifier (5 tokens)
    and extractor (1024 tokens) use different budgets regardless of any
    max_tokens value in the YAML config.
    """
    cfg = _get_llm_config()
    model = cfg["model"]
    # Pass everything except "model" and "max_tokens" from the YAML through
    # to litellm as keyword arguments.
    extra = {k: v for k, v in cfg.items() if k not in ("model", "max_tokens")}
    extra["max_tokens"] = max_tokens

    response = litellm.completion(
        model=model,
        messages=messages,
        **extra,
    )
    return response.choices[0].message.content or ""


_CLASSIFIER_SYSTEM = """\
You classify sections of a fantasy RPG sourcebook.
Return exactly ONE label:
  ENTITIES — the text describes named places, characters, factions,
             religions, races, or character classes worth extracting
  SKIP     — narrative history, atmospheric prose, rules text,
             tables, or appendix material with no extractable named entities"""

_EXTRACTOR_SYSTEM = """\
Extract ALL named entities from this RPG sourcebook excerpt.
Return ONLY valid JSON. No preamble, no explanation.

{
  "npcs": [{
    "name": "canonical name as written",
    "aliases": ["titles, other names"],
    "race": "race name or null",
    "character_class": "class name or null",
    "alignment": "alignment string or null",
    "nationality": "nation name or null",
    "location": "primary location or null",
    "factions": ["faction names"],
    "worships": "deity name or null",
    "potential_motives": [
      {"summary": "one sentence", "source_quote": "verbatim text or null"}
    ],
    "description": "brief description",
    "relationships": [
      {"target": "entity name", "type": "ally|enemy|rival|mentor|subordinate|other"}
    ],
    "page_reference": "42 or 42-43"
  }],
  "locations": [{
    "name": "canonical name",
    "aliases": ["other names"],
    "type": "City|River|Region|Nation|Dungeon|Sea|Mountain|Forest|Ruin|Plane|Other",
    "parent_location": "containing region or null",
    "controlling_faction": "faction name or null",
    "description": "brief description",
    "notable_npcs": ["NPC names"],
    "connected_locations": ["location names"],
    "page_reference": "page number(s)"
  }],
  "factions": [{
    "name": "canonical name",
    "aliases": ["other names"],
    "type": "Military|Criminal|Religious|Political|Mercantile|Arcane|Druidic|Other",
    "headquarters": "location name or null",
    "leader": "NPC name or null",
    "members": ["notable NPC names"],
    "potential_motives": [
      {"summary": "one sentence", "source_quote": "text or null"}
    ],
    "allies": ["faction names"],
    "enemies": ["faction names"],
    "operates_in": ["location names"],
    "worships": "deity name or null",
    "page_reference": "page number(s)"
  }],
  "religions": [{
    "name": "canonical religion name",
    "aliases": ["other names"],
    "primary_deity": "deity name or null",
    "deities": ["all associated deity names"],
    "worshipping_factions": ["faction names"],
    "dominant_in": ["nation names"],
    "description": "brief description",
    "page_reference": "page number(s)"
  }],
  "deities": [{
    "name": "canonical deity name",
    "aliases": ["titles, epithets"],
    "religion": "parent religion or null",
    "alignment": "alignment or null",
    "domains": ["divine domain names"],
    "description": "brief description",
    "page_reference": "page number(s)"
  }],
  "races": [{
    "name": "canonical race name",
    "aliases": ["other names or subtypes"],
    "description": "brief description",
    "typical_classes": ["class names"],
    "native_regions": ["location names"],
    "notable_npcs": ["named individuals"],
    "page_reference": "page number(s)"
  }],
  "classes": [{
    "name": "canonical class name",
    "aliases": ["variants"],
    "description": "brief description",
    "associated_skills": ["skill names"],
    "page_reference": "page number(s)"
  }],
  "skills": [{
    "name": "canonical skill name",
    "aliases": ["other names"],
    "description": "brief description",
    "page_reference": "page number(s)"
  }]
}

Return empty arrays for types not present in the text."""

_EMPTY_EXTRACTION: dict[str, list] = {
    "npcs": [], "locations": [], "factions": [],
    "religions": [], "deities": [], "races": [],
    "classes": [], "skills": [],
}


def classify_chunk(chunk_text: str, max_tokens: int = 5) -> str:
    """Return 'ENTITIES' or 'SKIP' for this chunk."""
    raw = _complete(
        messages=[
            {"role": "system", "content": _CLASSIFIER_SYSTEM},
            {"role": "user", "content": chunk_text},
        ],
        max_tokens=max_tokens,
    )
    return "ENTITIES" if "ENTITIES" in raw.strip().upper() else "SKIP"


def _strip_code_fence(text: str) -> str:
    """Remove a markdown code fence from LLM output.

    Handles any language tag (```json, ```JSON, ```), missing closing fence
    (truncated responses), and leading/trailing whitespace.
    """
    lines = text.strip().splitlines()
    if not lines:
        return ""
    if lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def extract_entities(
    chunk_text: str,
    known_entities: list[str],
    max_tokens: int | None = None,
) -> dict[str, list[Any]]:
    """Extract all entity types from the chunk in a single LLM call."""
    if max_tokens is None:
        max_tokens = CONTEXT_WINDOW // 4

    known_hint = ""
    if known_entities:
        names = "\n".join(f"- {n}" for n in known_entities[:20])
        known_hint = (
            "\n\nKnown entities already in graph "
            "(use these exact names when referring to them):\n" + names
        )

    raw = _complete(
        messages=[
            {"role": "system", "content": _EXTRACTOR_SYSTEM + known_hint},
            {"role": "user", "content": chunk_text},
        ],
        max_tokens=max_tokens,
    )

    stripped = _strip_code_fence(raw)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        logger.warning(
            "extractor: JSON parse failed (%s) — truncated? Raw snippet: %.500s",
            exc, raw,
        )
        return dict(_EMPTY_EXTRACTION)

    result = dict(_EMPTY_EXTRACTION)
    result.update({k: v for k, v in data.items() if isinstance(v, list)})
    return result
