"""The classification prompt: system text, batch rendering, and schema.

Three things live here and nowhere else, because all three are inputs to
the per-record cache key in `atlas.enrich.llm.cache_key` and changing any
of them must invalidate cached answers:

- :data:`PROMPT_VERSION` -- bump it whenever :data:`SYSTEM_PROMPT` or
  :func:`build_batch_prompt`'s rendering changes in a way that could
  change the model's answer.
- :data:`OUTPUT_SCHEMA` -- the JSON Schema the Messages API constrains
  the response to (structured outputs). It is hashed into the cache key,
  so a schema change re-asks on its own, without a version bump.
- :func:`record_input` -- the canonical, per-record payload. It is *both*
  what `build_batch_prompt` renders and what the cache key hashes, which
  is what makes the cache per record rather than per batch: regrouping
  the same records into different batches cannot change any of their
  keys.

Nothing here calls a model or touches the filesystem -- rendering a
prompt is a pure function of a record and its enrichment text, so it is
trivially testable offline.

The schema is written for structured outputs, which require
``additionalProperties: false`` and a `required` list naming *every*
property at every object level. `countries` is deliberately a plain
string array rather than a patterned one: the pattern is enforced on our
side (`atlas.enrich.classify`'s guards) so a model that answers "usa"
loses that one value instead of failing the whole batch.
"""

from __future__ import annotations

from atlas import io, vocab
from atlas.schema import Record

# Bump when the prompt text or rendering changes materially; the cache
# key hashes this, so every record is re-asked on the next run.
PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
You are a clinical-data cataloguing assistant for a public catalog of \
clinical and neuroscience datasets.

Rules, in order of importance:
1. Classify ONLY from the text provided for each dataset. Never use \
outside knowledge about a dataset, and never invent facts.
2. Use only the allowed vocabulary values given in the request. If \
nothing fits, return an empty list rather than inventing a value.
3. Every modality and condition you return must carry `evidence`: a \
short, VERBATIM substring copied from that dataset's own text. If you \
cannot copy such a substring, omit the value.
4. `summary` must be at most 40 words of plain, factual language -- no \
marketing, no adjectives the text does not support, and no numbers that \
do not appear in the text.
5. `in_scope` is false only when the item is not a usable clinical, \
biomedical, or neuroscience dataset (for example a purely methodological \
paper with no data).
6. Return one object per dataset, using the exact `id` you were given.\
"""

# ---------------------------------------------------------------------------
# Output schema (structured outputs)
# ---------------------------------------------------------------------------

_EVIDENCE_DESCRIPTION = (
    "A short verbatim substring of this dataset's text that supports the value."
)


def _object(properties: dict, description: str | None = None) -> dict:
    """A strict JSON-Schema object: no extra properties, every property
    required (both are mandatory for structured outputs)."""
    node: dict = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    if description:
        node["description"] = description
    return node


_ITEM_SCHEMA = _object(
    {
        "id": {
            "type": "string",
            "description": "The dataset id, copied verbatim from the request.",
        },
        "in_scope": {
            "type": "boolean",
            "description": "Whether this is a usable clinical/neuroscience dataset.",
        },
        "domains": {
            "type": "array",
            "items": {"type": "string", "enum": list(vocab.DOMAINS)},
            "description": "Clinical domains this dataset serves.",
        },
        "modalities": {
            "type": "array",
            "items": _object(
                {
                    "value": {"type": "string", "enum": list(vocab.MODALITIES)},
                    "evidence": {
                        "type": "string",
                        "description": _EVIDENCE_DESCRIPTION,
                    },
                }
            ),
            "description": "Data modalities captured, each with its evidence.",
        },
        "conditions": {
            "type": "array",
            "items": _object(
                {
                    "label": {
                        "type": "string",
                        "description": "Condition name as written in the text.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": _EVIDENCE_DESCRIPTION,
                    },
                }
            ),
            "description": "Conditions studied, each with its evidence.",
        },
        "summary": {
            "type": "string",
            "description": "At most 40 words, plain language, no invented numbers.",
        },
        "population": {
            "type": ["string", "null"],
            "description": "Free-text study population, or null if not stated.",
        },
        "countries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ISO-3166-1 alpha-2 country codes, uppercase (e.g. NL).",
        },
    }
)

OUTPUT_SCHEMA: dict = _object(
    {
        "items": {
            "type": "array",
            "items": _ITEM_SCHEMA,
            "description": "One object per dataset in the request, same ids.",
        }
    }
)


# ---------------------------------------------------------------------------
# Per-record input (prompt item == cache-key payload)
# ---------------------------------------------------------------------------


def record_input(record: Record, text: str) -> dict:
    """The canonical per-record payload: one item of the batch prompt and
    the thing :func:`atlas.enrich.llm.cache_key` hashes.

    `facts` carries the source-reported fields worth showing the model:
    they save it re-deriving what the source already states, and being
    part of the cache key means a corrected source fact re-asks on the
    next run. Keep this a plain, JSON-serializable dict of *source* facts
    only -- never anything derived from a previous LLM answer, or the
    cache would invalidate itself on every run.
    """
    return {
        "id": record.id,
        "source": record.source,
        "name": record.name,
        "text": text,
        "facts": {
            "access": record.access,
            "license": record.license,
            "sample_size": record.sample_size,
            "sample_unit": record.sample_unit,
            "species": record.species,
            "domains": list(record.domains),
            "modalities": list(record.modalities),
            "conditions": [condition.label for condition in record.conditions],
            "countries": list(record.countries),
            "population": record.population,
            "keywords": list(record.keywords),
        },
    }


# ---------------------------------------------------------------------------
# Batch prompt
# ---------------------------------------------------------------------------


def build_batch_prompt(items: list[dict]) -> str:
    """Render `items` (each a :func:`record_input`) as one prompt.

    The allowed vocabularies are inlined once, before the datasets,
    rather than repeated per item -- the list is the same for every
    record and duplicating it would only cost tokens. Facts the source
    never reported are omitted for the same reason; the cache key still
    hashes the *full* :func:`record_input`, which keeps the key stricter
    than the prompt (the safe direction -- the reverse would serve a
    stale answer). Rendering is a pure function of `items`, so the same
    batch always produces byte-identical text (good for prompt caching,
    and what makes the prompt reproducible from a committed record).
    """
    lines = [
        "Classify each dataset below from its own text.",
        "",
        f"Allowed domains: {', '.join(vocab.DOMAINS)}",
        f"Allowed modalities: {', '.join(vocab.MODALITIES)}",
        "",
        (
            "`facts` is what the source already reports; treat it as true "
            "and use it for context. Return one object per dataset, reusing "
            "the exact `id` shown."
        ),
        "",
    ]
    for position, item in enumerate(items, start=1):
        facts = {
            key: value
            for key, value in item["facts"].items()
            if value not in (None, [], "")
        }
        lines.extend(
            [
                f"--- DATASET {position} ---",
                f"id: {item['id']}",
                f"source: {item['source']}",
                f"name: {item['name']}",
                f"facts: {io.canonical_json(facts).rstrip()}",
                "text:",
                item["text"],
                "",
            ]
        )
    return "\n".join(lines)
