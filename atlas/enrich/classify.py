"""LLM classification: batch the asking, guard every answer.

:func:`enrich_records` is the stage entry point and :func:`apply_llm_item`
is where the project's "never fabricate a field value" rule is actually
enforced. Nothing a model returns reaches a `Record` unchecked:

- **Vocabulary.** Domains and modalities outside `atlas.vocab` are
  dropped, not coerced.
- **Evidence.** Every modality and condition must come with an `evidence`
  string of at least :data:`MIN_EVIDENCE_CHARS` characters that contains
  a space and occurs (case-insensitively, whitespace-collapsed) in the
  record's own enrichment text. The length and space requirements are
  what make this a real guard rather than a formality -- a one-character
  "evidence" like ``"a"`` occurs in almost any text, so without them a
  model could evidence anything with anything.
- **Summary.** Refused (and the record's existing summary kept) when it
  runs over 40 words, when it contains a number that is not a number in
  the input, or when fewer than :data:`SUMMARY_GROUNDING_RATIO` of its
  content words appear in the record's own name and text. The last one is
  a soft, whole-summary check: prose can legitimately rephrase, but a
  summary that shares almost no vocabulary with its source is either
  about a different dataset or was written from the model's own
  knowledge.
- **Numbers.** Compared as *normalized tokens*, never as substrings: "20"
  is not evidenced by "2018", and "1,000" and "1000" are the same number.
- **Scope.** `in_scope=false` is advisory: it is *ignored* for repository
  sources (a curated repository listing a dataset is better evidence than
  a model's opinion) and only flags journal records `needs_review` for a
  human, never removes anything. Phase 1 decides actual exclusions.
- **Never overwrite.** Domains, modalities, conditions, and countries are
  unioned with what the source already reported; `population` is only
  filled in when the record has none. A source fact always wins.
- **Validate.** The merged record is re-validated against `Record` before
  it replaces the original, so a guard bug produces a counted failure
  rather than an invalid row in the catalog.

Every guard that fires is counted (`EnrichStats.guard_drops`) rather than
logged and forgotten, so a prompt regression shows up as a number in the
refresh report.

Batching is an efficiency detail, not a semantic one: records are asked
about `batch_size` at a time, but each record's answer is cached under
its *own* key (`atlas.enrich.llm.cache_key`), so re-running with a
different batch size or source selection reuses every cached answer. An
answer served from cache is stamped with the model that *produced* it,
not the backend running today.

A batch's answers are only ever applied to the records that were in that
batch: an item naming some other record -- the shape a prompt injection
in one dataset's text would take -- is discarded, never merged.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from atlas import vocab
from atlas.enrich import llm, prompts
from atlas.schema import Condition, Provenance, Record, word_count

# Sources whose listings are curated data repositories -- i.e. everything
# that isn't a journal. Derived rather than hardcoded so a source added to
# `vocab.SOURCES` later defaults to repository semantics (its `in_scope`
# answers ignored) instead of silently gaining journal semantics.
REPOSITORY_SOURCES: frozenset[str] = frozenset(vocab.SOURCES) - frozenset(
    vocab.JOURNAL_SOURCES
)

_ISO2_RE = re.compile(r"^[A-Z]{2}$")
# A number, with its own internal separators: "45", "1,000", "3.5".
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_NUMBER_NOISE_RE = re.compile(r"[,.\s%]")

# The schema's own cap, re-checked here so a too-long summary costs one
# field rather than failing validation for the whole record.
SUMMARY_MAX_WORDS = 40
# Evidence must be long enough, and have enough internal structure, to
# identify a passage rather than match by accident.
MIN_EVIDENCE_CHARS = 10
# How much of a summary's own vocabulary must come from the input.
SUMMARY_GROUNDING_RATIO = 0.6
# Words shorter than this carry no signal (articles, prepositions).
MIN_CONTENT_WORD_CHARS = 4


def _today() -> date:
    """Today in UTC -- the date stamped on `provenance.enrichment.at`.
    UTC rather than local time so two machines refreshing the same
    minute agree on the date."""
    return datetime.now(tz=UTC).date()


@dataclass
class EnrichStats:
    """What one :func:`enrich_records` pass did, for the refresh report."""

    backend: str
    model: str | None
    calls: int = 0
    cache_hits: int = 0
    guard_drops: int = 0
    failures: int = 0
    records_enriched: int = 0
    # Model id -> how many records this pass took from cache under it.
    # `backend`/`model` above describe the backend that *ran*; cached
    # answers may have come from an entirely different one, and the
    # report must not imply otherwise.
    cached_models: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    """`text` with every whitespace run reduced to one space -- so a
    model quoting across a line break still matches its source."""
    return " ".join(text.split())


def _evidenced(evidence: object, haystack: str) -> bool:
    """True when `evidence` is a usable quotation from `haystack`.

    `haystack` is already lowercased and whitespace-collapsed. The
    length and space requirements are the guard: see the module
    docstring.
    """
    if not isinstance(evidence, str):
        return False
    quote = _collapse(evidence).lower()
    if len(quote) < MIN_EVIDENCE_CHARS or " " not in quote:
        return False
    return quote in haystack


def _numbers(text: str) -> set[str]:
    """The normalized numeric tokens in `text`.

    Normalized (thousands separators, decimal points, and percent signs
    removed) and compared as whole tokens, so "1,000" and "1000" are the
    same number while "20" is *not* found inside "2018" -- the substring
    check this replaces let a fabricated "20 patients" pass on the
    strength of an unrelated year.
    """
    tokens = set()
    for run in _NUMBER_RE.findall(text):
        token = _NUMBER_NOISE_RE.sub("", run)
        if token:
            tokens.add(token)
    return tokens


def _content_words(text: str) -> list[str]:
    """The lowercase, punctuation-stripped words of `text` long enough to
    carry meaning."""
    words = []
    for raw in text.lower().split():
        word = "".join(character for character in raw if character.isalnum())
        if len(word) >= MIN_CONTENT_WORD_CHARS:
            words.append(word)
    return words


def _vocab_ordered(values: set[str], order: tuple[str, ...]) -> list[str]:
    """`values` in vocabulary order -- the project's deterministic-output
    rule for every enumerated list."""
    return [value for value in order if value in values]


def _canonical_condition_label(label: str) -> str:
    """`label` mapped through `vocab.CONDITION_ALIASES` when it is a
    known alias, else its own stripped text -- so an LLM's "Parkinson's
    disease" dedupes against the rules stage's "parkinson disease"."""
    stripped = label.strip()
    return vocab.CONDITION_ALIASES.get(stripped.lower(), stripped)


def _as_list(value: object) -> tuple[list, int]:
    """`(items, drops)` for a field that should hold a list. A non-list
    costs exactly one drop -- iterating a stray string would otherwise
    charge one per character."""
    if value is None:
        return [], 0
    if isinstance(value, list):
        return value, 0
    return [], 1


def apply_llm_item(
    record: Record,
    item: dict,
    text: str,
    *,
    model: str | None = None,
    prompt_version: str | None = None,
) -> tuple[Record, int]:
    """Merge one guarded LLM `item` into `record` against its `text`.

    Returns the updated record and the number of guard drops. When no
    proposed value survives its guard, `record` itself is returned
    unchanged (identity, not a copy) and its provenance is left alone --
    an answer that contributed nothing must not be recorded as
    enrichment.

    `model` and `prompt_version` describe the run that *produced* `item`:
    for a fresh answer they are the current backend's, and for one served
    from cache they come from the cache entry, so a record never claims
    to have been classified by a model that never saw it. Both are
    optional so this function stays callable (and testable) with just a
    record, an answer, and the text that answer must be evidenced
    against.
    """
    haystack = _collapse(text).lower()
    numbers = _numbers(text)
    grounding_words = set(_content_words(f"{record.name} {text}"))
    drops = 0
    updates: dict = {}
    changed: list[str] = []

    # -- domains: vocabulary only -----------------------------------------
    raw_domains, malformed = _as_list(item.get("domains"))
    drops += malformed
    proposed_domains: set[str] = set()
    for value in raw_domains:
        if isinstance(value, str) and value in vocab.DOMAINS:
            proposed_domains.add(value)
        else:
            drops += 1
    if proposed_domains - set(record.domains):
        updates["domains"] = _vocab_ordered(
            proposed_domains | set(record.domains), vocab.DOMAINS
        )
        changed.append("domains")

    # -- modalities: vocabulary *and* evidence ----------------------------
    raw_modalities, malformed = _as_list(item.get("modalities"))
    drops += malformed
    proposed_modalities: set[str] = set()
    for entry in raw_modalities:
        value = entry.get("value") if isinstance(entry, dict) else None
        evidence = entry.get("evidence") if isinstance(entry, dict) else None
        if value in vocab.MODALITIES and _evidenced(evidence, haystack):
            proposed_modalities.add(value)
        else:
            drops += 1
    if proposed_modalities - set(record.modalities):
        updates["modalities"] = _vocab_ordered(
            proposed_modalities | set(record.modalities), vocab.MODALITIES
        )
        changed.append("modalities")

    # -- conditions: evidence, deduped by canonical lowercased label ------
    raw_conditions, malformed = _as_list(item.get("conditions"))
    drops += malformed
    known_labels = {
        _canonical_condition_label(condition.label).lower()
        for condition in record.conditions
    }
    new_conditions: list[Condition] = []
    for entry in raw_conditions:
        label = entry.get("label") if isinstance(entry, dict) else None
        evidence = entry.get("evidence") if isinstance(entry, dict) else None
        if not (isinstance(label, str) and label.strip()):
            drops += 1
            continue
        if not _evidenced(evidence, haystack):
            drops += 1
            continue
        canonical = _canonical_condition_label(label)
        if canonical.lower() in known_labels:
            continue
        known_labels.add(canonical.lower())
        # `mesh_id` stays None: resolving it is the MeSH stage's job.
        new_conditions.append(Condition(label=canonical))
    if new_conditions:
        updates["conditions"] = [*record.conditions, *new_conditions]
        changed.append("conditions")

    # -- summary: length, then numbers, then vocabulary grounding ---------
    summary = item.get("summary")
    if isinstance(summary, str) and summary.strip():
        summary = summary.strip()
        rejected = (
            word_count(summary) > SUMMARY_MAX_WORDS
            or not _numbers(summary) <= numbers
            or not _is_grounded(summary, grounding_words)
        )
        if rejected:
            drops += 1
        elif summary != record.summary:
            updates["summary"] = summary
            changed.append("summary")

    # -- population: only when the record has none ------------------------
    population = item.get("population")
    if record.population is None and isinstance(population, str) and population.strip():
        population = population.strip()
        if _numbers(population) <= numbers:
            updates["population"] = population
            changed.append("population")
        else:
            drops += 1

    # -- countries: ISO-3166-1 alpha-2 ------------------------------------
    raw_countries, malformed = _as_list(item.get("countries"))
    drops += malformed
    new_countries: list[str] = []
    for value in raw_countries:
        code = value.strip().upper() if isinstance(value, str) else ""
        if not _ISO2_RE.match(code):
            drops += 1
            continue
        if code not in record.countries and code not in new_countries:
            new_countries.append(code)
    if new_countries:
        updates["countries"] = [*record.countries, *sorted(new_countries)]
        changed.append("countries")

    # -- scope: advisory, and only for journal sources --------------------
    if item.get("in_scope") is False:
        if record.source in REPOSITORY_SOURCES:
            drops += 1
        elif record.record_status == "active":
            updates["record_status"] = "needs_review"
            changed.append("record_status")

    if not changed:
        return record, drops

    updates["provenance"] = _updated_provenance(record, changed, model, prompt_version)
    return record.model_copy(update=updates), drops


def _is_grounded(summary: str, grounding_words: set[str]) -> bool:
    """True when enough of `summary`'s content words come from the
    record's own name and text (see the module docstring). A summary with
    no content words at all is not evidence of fabrication, so it
    passes."""
    words = _content_words(summary)
    if not words:
        return True
    hits = sum(1 for word in words if word in grounding_words)
    return hits >= SUMMARY_GROUNDING_RATIO * len(words)


def _updated_provenance(
    record: Record,
    changed: list[str],
    model: str | None,
    prompt_version: str | None,
) -> Provenance:
    """`record.provenance` with its enrichment block re-stamped for the
    fields the LLM actually changed.

    `method` records that an LLM ran *in addition to* whatever came
    before: `rules` becomes `rules+llm`, `curated` is left alone (a human
    curator's label is not downgraded by a machine pass -- and
    :func:`enrich_records` skips such records outright), anything else
    becomes `llm`. Per-field origins are merged, not replaced, so fields
    the rules stage set keep saying `rules`.
    """
    enrichment = record.provenance.enrichment
    if enrichment.method == "curated":
        method = "curated"
    elif enrichment.method in ("rules", "rules+llm"):
        method = "rules+llm"
    else:
        method = "llm"

    return record.provenance.model_copy(
        update={
            "enrichment": enrichment.model_copy(
                update={
                    "method": method,
                    "model": model,
                    "prompt_version": prompt_version or prompts.PROMPT_VERSION,
                    "at": _today(),
                    "fields": {
                        **enrichment.fields,
                        **{name: "llm" for name in changed},
                    },
                }
            )
        }
    )


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def enrich_records(
    records: list[Record],
    texts: dict[str, str],
    backend: llm.Backend,
    *,
    max_calls: int | None = None,
    batch_size: int = 8,
    cache_dir: Path = llm.CACHE_DIR,
) -> EnrichStats:
    """Classify `records` with `backend` and merge the guarded answers.

    `texts` maps record id -> the enrichment text to classify from (the
    caller builds it from each source's `enrichment_text(envelope)`); a
    record with no text is skipped entirely, since with no text no
    evidence could ever be verified. Records already enriched by hand
    (`provenance.enrichment.method == "curated"`) are skipped too -- a
    machine pass does not get to edit a curator's work.

    Records are replaced **in place** in `records` -- `Record` is
    immutable-friendly, so each merge produces a new object and this
    function swaps it into the list rather than returning a second list
    the caller could forget to use.

    `max_calls` caps model calls (not records): once the cap is reached
    the remaining records are left rules-only. Cache hits are free and
    are served regardless of the cap.
    """
    stats = EnrichStats(backend=backend.name, model=backend.model)
    pending: list[tuple[int, str, dict]] = []

    for index, record in enumerate(records):
        text = texts.get(record.id)
        if not text or record.provenance.enrichment.method == "curated":
            continue
        item_input = prompts.record_input(record, text)
        key = llm.cache_key(item_input, prompts.OUTPUT_SCHEMA)

        entry = llm.cache_lookup(key, cache_dir=cache_dir)
        if entry is not None:
            stats.cache_hits += 1
            cached_model = entry.get("model")
            label = cached_model if isinstance(cached_model, str) else "unknown"
            stats.cached_models[label] = stats.cached_models.get(label, 0) + 1
            _merge(
                records,
                index,
                entry.get("output"),
                text,
                stats,
                model=cached_model if isinstance(cached_model, str) else None,
                prompt_version=entry.get("prompt_version"),
            )
            continue
        pending.append((index, key, item_input))

    for start in range(0, len(pending), batch_size):
        if max_calls is not None and stats.calls >= max_calls:
            break
        batch = pending[start : start + batch_size]
        nonce = secrets.token_hex(6)
        prompt = prompts.build_batch_prompt(
            [item_input for _, _, item_input in batch], nonce=nonce
        )

        stats.calls += 1
        output = llm.complete(backend, prompt, prompts.OUTPUT_SCHEMA)
        if output is None:
            stats.failures += 1
            continue

        items = output.get("items") if isinstance(output, dict) else None
        if not isinstance(items, list):
            llm.record_error(
                f"malformed response: no 'items' list (got {type(output).__name__})"
            )
            stats.failures += 1
            continue

        # Keyed by id and looked up per *pending record of this batch*:
        # an item for anything else -- including a record elsewhere in
        # `records`, which is what a prompt injection would aim for -- is
        # never applied.
        by_id = {
            item["id"]: item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for index, key, item_input in batch:
            item = by_id.get(item_input["id"])
            if item is None:
                continue
            llm.cache_put(key, backend, item, cache_dir=cache_dir)
            _merge(
                records,
                index,
                item,
                item_input["text"],
                stats,
                model=backend.model,
                prompt_version=prompts.PROMPT_VERSION,
            )

    return stats


def _merge(
    records: list[Record],
    index: int,
    item: object,
    text: str,
    stats: EnrichStats,
    *,
    model: str | None,
    prompt_version: str | None,
) -> None:
    """Apply one answer to `records[index]`, counting drops and whether
    anything actually changed.

    The merged record is re-validated before it replaces the original:
    the guards are meant to make that impossible to fail, which is
    exactly why a failure has to be caught and counted here rather than
    surfacing later as an invalid catalog row.
    """
    if not isinstance(item, dict):
        stats.failures += 1
        return

    original = records[index]
    updated, drops = apply_llm_item(
        original, item, text, model=model, prompt_version=prompt_version
    )
    stats.guard_drops += drops
    if updated is original:
        return

    try:
        Record.model_validate(updated.model_dump())
    except ValidationError as exc:
        llm.record_error(f"{original.id}: merged record failed validation: {exc}")
        stats.failures += 1
        return

    records[index] = updated
    stats.records_enriched += 1
