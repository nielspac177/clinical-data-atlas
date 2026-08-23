"""Merge policy: folding a `dedupe.cluster()` group into one canonical
`Record`, and folding rule-based/LLM enrichment onto a single record.

Two independent policies live here, both additive-only (never drop a
source fact, never fabricate a value):

- `merge_cluster`: several records that are the *same dataset* (per
  `dedupe.cluster`) collapse into one -- one primary survives, chosen by
  source trust; every other field is a union or a most-restrictive/
  fill-when-empty pick; the records folded away come back as `Excluded`
  so the merge is auditable rather than a silent drop.
- `apply_enrichment`: rule-based and (optional) LLM classification output
  fold onto *one* record, additively -- domains/modalities/conditions
  grow by union, and anything the source itself already reported
  (name, url, access, license, sample_size, authors, papers, a known
  species, dataset_doi, version, published) is never touched.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from atlas import vocab
from atlas.normalize.common import Excluded
from atlas.schema import Condition, Paper, Record

# ---------------------------------------------------------------------------
# merge_cluster: which record wins, and how the rest fold into it
# ---------------------------------------------------------------------------

# Primary priority, most to least trusted: a direct repository API
# (openneuro/physionet/gdc/tcia) outranks a hand-curated entry, which in
# turn outranks the two data-descriptor journals and the not-yet-built
# Phase 1 sources. Deliberately its own tuple, not `vocab.SOURCES` --
# that tuple's order is for docs rendering, not merge trust.
_SOURCE_PRIORITY: tuple[str, ...] = (
    "openneuro",
    "physionet",
    "gdc",
    "tcia",
    "curated",
    "dhs",
    "worldbank",
    "scientific_data",
    "data_in_brief",
)


def _select_primary(cluster: list[Record]) -> Record:
    """The cluster member with the highest-trust source; ties (same
    source) broken by the lexicographically smaller id, so the choice is
    stable regardless of `cluster`'s input order."""
    return min(cluster, key=lambda r: (_SOURCE_PRIORITY.index(r.source), r.id))


def _ordered_union(
    primary_items: list[Any], secondary_lists: Any, *, key: Any
) -> list[Any]:
    """`primary_items`, then every item from `secondary_lists` (an
    iterable of lists, taken in order) not already present -- "present"
    meaning `key(item)` matches one already kept. Preserves each list's
    own internal order; only ever appends, never reorders or drops a
    primary item."""
    result = list(primary_items)
    seen = {key(item) for item in result}
    for items in secondary_lists:
        for item in items:
            item_key = key(item)
            if item_key not in seen:
                seen.add(item_key)
                result.append(item)
    return result


def _append_secondary_doi_papers(
    papers: list[Paper], secondaries: list[Record]
) -> list[Paper]:
    """Every secondary's own `dataset_doi` (e.g. a Scientific Data
    companion article's DOI) becomes a `Paper(relation="describes")` on
    the merged record -- unless a paper with that doi is already present,
    whether from the primary, from another secondary's `papers` list, or
    from an earlier secondary in this same loop."""
    result = list(papers)
    seen_dois = {paper.doi for paper in result}
    for secondary in secondaries:
        doi = secondary.dataset_doi
        if doi and doi not in seen_dois:
            result.append(Paper(doi=doi, relation="describes"))
            seen_dois.add(doi)
    return result


def _first_truthy(primary_value: Any, secondaries: list[Record], getter: Any) -> Any:
    """`primary_value` unless it's null/empty, in which case the first
    secondary (in id order) with a truthy value for `getter`; otherwise
    `primary_value` unchanged. Never overwrites a primary value that's
    already set."""
    if primary_value:
        return primary_value
    for secondary in secondaries:
        value = getter(secondary)
        if value:
            return value
    return primary_value


def merge_cluster(cluster: list[Record]) -> tuple[Record, list[Excluded]]:
    """Collapse one `dedupe.cluster()` group into `(merged_primary,
    excluded_secondaries)`.

    Primary selection: highest-trust source per `_SOURCE_PRIORITY`, ties
    broken by the smaller id. A singleton cluster returns that record
    completely unchanged, with no exclusions.

    List fields (`domains`, `modalities`, `conditions` by lowercased
    label, `keywords`, `countries`, `institutions` by name, `authors` by
    name, `papers` by doi, `related` by id) union preserving the
    primary's own order, then each secondary's (in id order). Every
    secondary's `dataset_doi` additionally becomes a `describes` `Paper`
    on the result if no paper with that doi is already present.

    Scalar conflicts: `access` resolves to the more restrictive tier
    (`vocab.ACCESS_ORDER`); `access_tiers` is the union of every member's
    tiers, in `ACCESS_ORDER`; `record_status` is `"needs_review"` if any
    member is, else the primary's; `sample_size`, `sample_unit`,
    `license`, `summary`, `name`, `url`, `published`, and `version` stay
    the primary's, filled from a secondary only when the primary's own
    value is null/empty.

    `provenance` is the primary's, with a `merged_from` note (a
    comma-joined, sorted list of the folded-away ids) added to
    `provenance.enrichment.fields`. Every non-primary member comes back
    as `Excluded(native_id=<its atlas id>, reason="merged_into:<primary
    id>")` -- folded away, never silently dropped.
    """
    if not cluster:
        raise ValueError("merge_cluster requires a non-empty cluster")

    primary = _select_primary(cluster)
    secondaries = sorted((r for r in cluster if r.id != primary.id), key=lambda r: r.id)

    if not secondaries:
        return primary, []

    domains = _ordered_union(
        primary.domains, (s.domains for s in secondaries), key=lambda d: d
    )
    modalities = _ordered_union(
        primary.modalities, (s.modalities for s in secondaries), key=lambda m: m
    )
    conditions = _ordered_union(
        primary.conditions,
        (s.conditions for s in secondaries),
        key=lambda c: c.label.lower(),
    )
    keywords = _ordered_union(
        primary.keywords, (s.keywords for s in secondaries), key=lambda k: k
    )
    countries = _ordered_union(
        primary.countries, (s.countries for s in secondaries), key=lambda c: c
    )
    institutions = _ordered_union(
        primary.institutions,
        (s.institutions for s in secondaries),
        key=lambda i: i.name,
    )
    authors = _ordered_union(
        primary.authors, (s.authors for s in secondaries), key=lambda a: a.name
    )
    related = _ordered_union(
        primary.related, (s.related for s in secondaries), key=lambda r: r.id
    )
    papers = _ordered_union(
        primary.papers, (s.papers for s in secondaries), key=lambda p: p.doi
    )
    papers = _append_secondary_doi_papers(papers, secondaries)

    access = max((r.access for r in cluster), key=vocab.ACCESS_ORDER.index)
    access_tiers = sorted(
        {tier for r in cluster for tier in r.access_tiers},
        key=vocab.ACCESS_ORDER.index,
    )
    record_status = (
        "needs_review"
        if any(r.record_status == "needs_review" for r in cluster)
        else primary.record_status
    )

    sample_size = _first_truthy(
        primary.sample_size, secondaries, lambda r: r.sample_size
    )
    sample_unit = _first_truthy(
        primary.sample_unit, secondaries, lambda r: r.sample_unit
    )
    license_ = _first_truthy(primary.license, secondaries, lambda r: r.license)
    summary = _first_truthy(primary.summary, secondaries, lambda r: r.summary)
    name = _first_truthy(primary.name, secondaries, lambda r: r.name)
    url = _first_truthy(primary.url, secondaries, lambda r: r.url)
    published = _first_truthy(primary.published, secondaries, lambda r: r.published)
    version = _first_truthy(primary.version, secondaries, lambda r: r.version)

    merged_from = ",".join(s.id for s in secondaries)  # already id-sorted
    enrichment = primary.provenance.enrichment.model_copy(
        update={
            "fields": {
                **primary.provenance.enrichment.fields,
                "merged_from": merged_from,
            }
        }
    )
    provenance = primary.provenance.model_copy(update={"enrichment": enrichment})

    merged = primary.model_copy(
        update={
            "name": name,
            "url": url,
            "domains": domains,
            "modalities": modalities,
            "conditions": conditions,
            "keywords": keywords,
            "countries": countries,
            "institutions": institutions,
            "authors": authors,
            "papers": papers,
            "related": related,
            "access": access,
            "access_tiers": access_tiers,
            "sample_size": sample_size,
            "sample_unit": sample_unit,
            "license": license_,
            "summary": summary,
            "published": published,
            "version": version,
            "record_status": record_status,
            "provenance": provenance,
        }
    )

    excluded = [
        Excluded(native_id=s.id, reason=f"merged_into:{primary.id}")
        for s in secondaries
    ]
    return merged, excluded


# ---------------------------------------------------------------------------
# apply_enrichment: fold rules + (optional) LLM output onto one record
# ---------------------------------------------------------------------------


def _condition_label(item: Any) -> str | None:
    """A condition label out of an LLM `conditions[]` item, which the
    already-guarded classify output allows to be either a bare label
    string or a `{"label": ...}` dict."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("label")
    return None


def _merge_conditions(
    existing: list[Condition], rule_labels: list[str], llm_labels: list[str]
) -> tuple[list[Condition], str | None]:
    """`existing ∪ rules ∪ llm`, deduped by lowercased label: `existing`'s
    own `Condition` objects are kept as-is (so a resolved `mesh_id` is
    never dropped in favor of a bare new one), and any `rule_labels`/
    `llm_labels` not already present become plain `Condition(label=...)`.

    Also returns which of rules/llm (if either) contributed a genuinely
    new label -- for the `provenance.enrichment.fields` bookkeeping, llm
    wins the attribution when both did.
    """
    seen = {c.label.lower() for c in existing}
    merged = list(existing)

    for label in rule_labels:
        key = label.lower()
        if key not in seen:
            seen.add(key)
            merged.append(Condition(label=label))
    rules_added = len(merged) > len(existing)

    before_llm = len(merged)
    for label in llm_labels:
        key = label.lower()
        if key not in seen:
            seen.add(key)
            merged.append(Condition(label=label))
    llm_added = len(merged) > before_llm

    attribution = "llm" if llm_added else "rules" if rules_added else None
    return merged, attribution


def _attribution(
    old: set[str], rules_contribution: set[str], llm_contribution: set[str]
) -> str | None:
    """Which of rules/llm added a value not already in `old` -- `"llm"`
    wins when both contributed something new, else `"rules"` when only
    rules did, else `None`."""
    if llm_contribution - old:
        return "llm"
    if rules_contribution - old:
        return "rules"
    return None


def apply_enrichment(record: Record, llm_out: dict | None, rule_hits: Any) -> Record:
    """Fold rule-based and (optional) LLM classification onto `record`.

    Source facts are never overwritten: `name`, `url`, `access`,
    `license`, `sample_size`, `authors`, `papers`, `dataset_doi`,
    `version`, and `published` are untouched by this function entirely;
    `species` is untouched unless it's currently `"unknown"`. Only
    `domains`, `modalities`, `conditions`, `species` (fill-if-unknown),
    `summary`, `population`, `countries`, and `provenance.enrichment` are
    ever touched, and only additively (union, or fill-when-empty).

    A record whose `provenance.enrichment.method` is already `"curated"`
    is returned completely untouched -- a human curated it, and no
    automated pass should second-guess that.

    `rule_hits` is duck-typed (see `atlas.enrich.rules.RuleHits`,
    deliberately not imported here) with `domains`/`modalities`/
    `conditions` (lists of canonical labels), `species` (`str | None`),
    and `evidence` (not used by this function). `llm_out` is `None`, or
    the already-guarded per-record classify output: `{"domains": [...],
    "modalities": [...], "conditions": [...], "summary": str | None,
    "population": str | None, "countries": [...]}`, where each
    `conditions` item is a label string or a `{"label": ...}` dict.

    `provenance.enrichment.method` becomes `"rules+llm"` whenever
    `llm_out` is provided, `"rules"` when only rules ran and changed
    something, and `.fields[<field>]` records `"rules"`/`"llm"` per
    field actually changed (llm wins the attribution when both did).
    `.at` is set to today only when something changed; otherwise the
    record -- provenance included -- comes back exactly as given.
    """
    if record.provenance.enrichment.method == "curated":
        return record

    llm = llm_out or {}
    llm_domains = llm.get("domains") or []
    llm_modalities = llm.get("modalities") or []
    llm_condition_labels = [
        label
        for item in (llm.get("conditions") or [])
        if (label := _condition_label(item))
    ]
    llm_summary = llm.get("summary")
    llm_population = llm.get("population")
    llm_countries = llm.get("countries") or []

    fields_changed: dict[str, str] = {}

    domains_old = set(record.domains)
    domains_combined = domains_old | set(rule_hits.domains) | set(llm_domains)
    domains = [d for d in vocab.DOMAINS if d in domains_combined]
    attribution = _attribution(domains_old, set(rule_hits.domains), set(llm_domains))
    if attribution:
        fields_changed["domains"] = attribution

    modalities_old = set(record.modalities)
    modalities_combined = (
        modalities_old | set(rule_hits.modalities) | set(llm_modalities)
    )
    modalities = [m for m in vocab.MODALITIES if m in modalities_combined]
    attribution = _attribution(
        modalities_old, set(rule_hits.modalities), set(llm_modalities)
    )
    if attribution:
        fields_changed["modalities"] = attribution

    conditions, attribution = _merge_conditions(
        record.conditions, list(rule_hits.conditions), llm_condition_labels
    )
    if attribution:
        fields_changed["conditions"] = attribution

    species = record.species
    if record.species == "unknown" and rule_hits.species:
        species = rule_hits.species
        fields_changed["species"] = "rules"

    summary = record.summary
    if llm_summary:
        summary = llm_summary
        fields_changed["summary"] = "llm"

    population = record.population
    if not population and llm_population:
        population = llm_population
        fields_changed["population"] = "llm"

    countries_old = set(record.countries)
    countries_combined = countries_old | set(llm_countries)
    countries = sorted(countries_combined)
    if countries_combined - countries_old:
        fields_changed["countries"] = "llm"

    applied = llm_out is not None or bool(fields_changed)
    if not applied:
        return record

    method = "rules+llm" if llm_out is not None else "rules"
    enrichment = record.provenance.enrichment.model_copy(
        update={
            "method": method,
            "fields": {**record.provenance.enrichment.fields, **fields_changed},
            "at": date.today(),  # noqa: DTZ011
        }
    )
    provenance = record.provenance.model_copy(update={"enrichment": enrichment})

    return record.model_copy(
        update={
            "domains": domains,
            "modalities": modalities,
            "conditions": conditions,
            "species": species,
            "summary": summary,
            "population": population,
            "countries": countries,
            "provenance": provenance,
        }
    )
