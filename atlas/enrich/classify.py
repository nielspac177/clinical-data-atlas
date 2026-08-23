"""LLM classification: batch the asking, guard every answer.

:func:`enrich_records` is the stage entry point and :func:`apply_llm_item`
is where the project's "never fabricate a field value" rule is actually
enforced. Nothing a model returns reaches a `Record` unchecked:

- **Vocabulary.** Domains and modalities outside `atlas.vocab` are
  dropped, not coerced.
- **Evidence.** Every modality and condition must come with an
  `evidence` string that occurs (case-insensitively) in the record's own
  enrichment text; values whose evidence isn't there are dropped. This is
  the single strongest anti-hallucination guard: a model cannot quote
  text that doesn't exist.
- **Summary.** Over 40 words (the schema's own limit) or containing a
  number that never appears in the input, and the record keeps the
  summary it already had.
- **Scope.** `in_scope=false` is advisory: it is *ignored* for repository
  sources (a curated repository listing a dataset is better evidence than
  a model's opinion) and only flags journal records `needs_review` for a
  human, never removes anything. Phase 1 decides actual exclusions.
- **Never overwrite.** Domains, modalities, conditions, and countries are
  unioned with what the source already reported; `population` is only
  filled in when the record has none. A source fact always wins.

Every guard that fires is counted (`EnrichStats.guard_drops`) rather than
logged and forgotten, so a prompt regression shows up as a number in the
refresh report.

Batching is an efficiency detail, not a semantic one: records are asked
about `batch_size` at a time, but each record's answer is cached under
its *own* key (`atlas.enrich.llm.cache_key`), so re-running with a
different batch size or source selection reuses every cached answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from atlas import vocab
from atlas.enrich import llm, prompts
from atlas.schema import Condition, Provenance, Record, word_count

# Sources whose listings are curated data repositories: their say-so that
# a record is a dataset outranks the model's, so `in_scope=false` is
# ignored for them (see the module docstring).
REPOSITORY_SOURCES: frozenset[str] = frozenset(
    {"openneuro", "physionet", "gdc", "tcia", "curated"}
)

_ISO2_RE = re.compile(r"^[A-Z]{2}$")
_DIGIT_RUN_RE = re.compile(r"\d+")

# The schema's own cap, re-checked here so a too-long summary costs one
# field rather than failing validation for the whole record.
SUMMARY_MAX_WORDS = 40


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


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _evidenced(evidence: object, haystack: str) -> bool:
    """True when `evidence` is a non-empty string occurring in
    `haystack` (already lowercased by the caller)."""
    return (
        isinstance(evidence, str)
        and bool(evidence.strip())
        and evidence.strip().lower() in haystack
    )


def _digits_are_grounded(text: str, haystack: str) -> bool:
    """True when every run of digits in `text` also appears in
    `haystack` -- the cheap fabrication check for free-text fields. A
    model that invents "1,200 participants" for a dataset whose text
    never says 1200 fails this."""
    return all(run in haystack for run in _DIGIT_RUN_RE.findall(text))


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


def apply_llm_item(
    record: Record, item: dict, text: str, *, model: str | None = None
) -> tuple[Record, int]:
    """Merge one guarded LLM `item` into `record` against its `text`.

    Returns the updated record and the number of guard drops. When no
    proposed value survives its guard, `record` itself is returned
    unchanged (identity, not a copy) and its provenance is left alone --
    an answer that contributed nothing must not be recorded as
    enrichment.

    `model` is the backend's model id, recorded in
    `provenance.enrichment.model`; it is optional so this function stays
    callable (and testable) with just a record, an answer, and the text
    that answer must be evidenced against.
    """
    haystack = text.lower()
    drops = 0
    updates: dict = {}
    changed: list[str] = []

    # -- domains: vocabulary only -----------------------------------------
    proposed_domains: set[str] = set()
    for value in item.get("domains") or []:
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
    proposed_modalities: set[str] = set()
    for entry in item.get("modalities") or []:
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

    # -- conditions: evidence, deduped by lowercased label ----------------
    known_labels = {condition.label.lower() for condition in record.conditions}
    new_conditions: list[Condition] = []
    for entry in item.get("conditions") or []:
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

    # -- summary: word limit, then no invented numbers --------------------
    summary = item.get("summary")
    if isinstance(summary, str) and summary.strip():
        summary = summary.strip()
        too_long = word_count(summary) > SUMMARY_MAX_WORDS
        invented_numbers = not _digits_are_grounded(summary, haystack)
        if too_long or invented_numbers:
            drops += 1
        elif summary != record.summary:
            updates["summary"] = summary
            changed.append("summary")

    # -- population: only when the record has none ------------------------
    population = item.get("population")
    if record.population is None and isinstance(population, str) and population.strip():
        population = population.strip()
        if _digits_are_grounded(population, haystack):
            updates["population"] = population
            changed.append("population")
        else:
            drops += 1

    # -- countries: ISO-3166-1 alpha-2, uppercase -------------------------
    new_countries: list[str] = []
    for value in item.get("countries") or []:
        if not (isinstance(value, str) and _ISO2_RE.match(value)):
            drops += 1
            continue
        if value not in record.countries and value not in new_countries:
            new_countries.append(value)
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

    updates["provenance"] = _updated_provenance(record, changed, model)
    return record.model_copy(update=updates), drops


def _updated_provenance(
    record: Record, changed: list[str], model: str | None
) -> Provenance:
    """`record.provenance` with its enrichment block re-stamped for the
    fields the LLM actually changed.

    `method` records that an LLM ran *in addition to* whatever came
    before: `rules` becomes `rules+llm`, `curated` is left alone (a human
    curator's label is not downgraded by a machine pass), anything else
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
                    "prompt_version": prompts.PROMPT_VERSION,
                    "at": _today(),
                    "fields": {
                        **enrichment.fields,
                        **{field: "llm" for field in changed},
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
    evidence could ever be verified.

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
        if not text:
            continue
        item_input = prompts.record_input(record, text)
        key = llm.cache_key(item_input, prompts.OUTPUT_SCHEMA)

        cached = llm.cache_get(key, cache_dir=cache_dir)
        if cached is not None:
            stats.cache_hits += 1
            _merge(records, index, cached, text, stats, backend.model)
            continue
        pending.append((index, key, item_input))

    for start in range(0, len(pending), batch_size):
        if max_calls is not None and stats.calls >= max_calls:
            break
        batch = pending[start : start + batch_size]
        prompt = prompts.build_batch_prompt([item_input for _, _, item_input in batch])

        stats.calls += 1
        try:
            output = backend.complete_json(prompt, prompts.OUTPUT_SCHEMA)
        except llm.LLMError as exc:
            llm.record_error(str(exc))
            stats.failures += 1
            continue

        items = output.get("items") if isinstance(output, dict) else None
        if not isinstance(items, list):
            llm.record_error(
                f"malformed response: no 'items' list (got {type(output).__name__})"
            )
            stats.failures += 1
            continue

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
            _merge(records, index, item, item_input["text"], stats, backend.model)

    return stats


def _merge(
    records: list[Record],
    index: int,
    item: object,
    text: str,
    stats: EnrichStats,
    model: str | None,
) -> None:
    """Apply one answer to `records[index]`, counting drops and whether
    anything actually changed."""
    if not isinstance(item, dict):
        stats.failures += 1
        return
    updated, drops = apply_llm_item(records[index], item, text, model=model)
    stats.guard_drops += drops
    if updated is not records[index]:
        records[index] = updated
        stats.records_enriched += 1
