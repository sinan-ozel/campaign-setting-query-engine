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
import sys
from typing import Any

import litellm
import yaml

logger = logging.getLogger("graph_worker.extractor")


class LLMConnectionError(RuntimeError):
    """LLM endpoint refused the connection — check api_base in llm.yaml."""


LLM_CONFIG_PATH = os.environ.get("LLM_CONFIG_PATH", "/config/llm.yaml")
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "4096"))
_ONTOLOGY_SCHEMA_PATH = os.environ.get(
    "ONTOLOGY_SCHEMA_PATH", "/config/ontology_schema.yaml"
)

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

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            **extra,
        )
    except litellm.InternalServerError as exc:
        if "connection" in str(exc).lower():
            api_base = cfg.get("api_base", "(not set)")
            logger.error(
                "extractor: LLM endpoint unreachable at %r "
                "(model=%s) — is the server running? Error: %s",
                api_base, model, exc,
            )
            raise LLMConnectionError(
                f"LLM unreachable at {api_base!r} (model={model!r})"
            ) from exc
        raise
    return response.choices[0].message.content or ""


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


def _build_classifier_system(schema: dict) -> str:
    """Build the classifier prompt listing every extractable entity type."""
    type_names = sorted(
        name
        for name, type_def in schema["entity_types"].items()
        if type_def.get("llm_key")
    )
    types_line = ", ".join(type_names)
    return (
        "You classify sections of a fantasy RPG sourcebook.\n"
        "Return exactly ONE label:\n"
        f"  ENTITIES — the text names or describes any of: {types_line}\n"
        "  SKIP     — pure narrative prose, flavour text, credits, or page-layout\n"
        "             material with no named or typed entities of the above kinds"
    )


def _format_schema_entry(llm_key: str, schema_obj: dict) -> str:
    """Format one entity type's schema as a JSON array example for the prompt."""
    lines = json.dumps(schema_obj, indent=2).splitlines()
    # Re-indent the inner lines (between the outer { }) by two extra spaces
    inner = "\n".join("  " + line for line in lines[1:-1])
    return f'  "{llm_key}": [{{\n{inner}\n  }}]'


def _build_extractor_system(schema: dict) -> str:
    """Build _EXTRACTOR_SYSTEM from ontology_schema.yaml."""
    parts = [
        "Extract ALL named entities from this RPG sourcebook excerpt.",
        "Return ONLY valid JSON. No preamble, no explanation.",
        "",
        "{",
    ]
    type_entries = []
    for type_def in schema["entity_types"].values():
        llm_key = type_def.get("llm_key")
        llm_schema = type_def.get("llm_schema")
        if llm_key and llm_schema:
            type_entries.append(_format_schema_entry(llm_key, llm_schema))
    parts.append(",\n".join(type_entries))
    parts.append("}")

    notes = [
        type_def["notes"].strip()
        for type_def in schema["entity_types"].values()
        if type_def.get("notes")
    ]
    if notes:
        parts.append(
            "\nClassifier notes — use these rules when deciding which array "
            "to put an entity in:\n"
        )
        parts.append("\n\n".join(notes))

    parts.append("\nReturn empty arrays for types not present in the text.")
    return "\n".join(parts)


_ONTOLOGY = _load_ontology_schema()
_CLASSIFIER_SYSTEM: str = _build_classifier_system(_ONTOLOGY)
_EXTRACTOR_SYSTEM: str = _build_extractor_system(_ONTOLOGY)

_EMPTY_EXTRACTION: dict[str, list] = {
    type_def["llm_key"]: []
    for type_def in _ONTOLOGY["entity_types"].values()
    if type_def.get("llm_key")
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
