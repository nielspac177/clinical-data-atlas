"""Merge policy: folding a `dedupe.cluster()` group into one canonical
`Record`, and folding rule-based/LLM enrichment onto a single record.

Two independent policies live here, both additive-only (never drop a
source fact, never fabricate a value):

- `merge_cluster`: several records that are the *same dataset* (per
  `dedupe.cluster`) collapse into one -- one primary survives, chosen by
  source trust; every other field is a union or a most-restrictive/
  fill-when-empty pick; the records folded away come back as `Excluded`
  so the merge is auditable rather than a silent drop. `build_alias_map`
  and `remap_related` are the orchestrator-facing follow-up: once every
  cluster in a whole catalog has been merged, they fix up any `related`
  link elsewhere that still names a now-folded-away id.
- `apply_enrichment`: rule-based and (optional) LLM classification output
  fold onto *one* record, additively -- domains/modalities/conditions
  grow by union, and anything the source itself already reported
  (name, url, access, license, sample_size, authors, papers, a known
  species, dataset_doi, version, published) is never touched.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from atlas import vocab
from atlas.enrich.dedupe import doi_key
from atlas.normalize.common import Excluded
from atlas.schema import Condition, Paper, Record, Related, Years

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


def _vocab_ordered_union(
    primary_items: list[str], secondary_lists: Any, vocab_order: tuple[str, ...]
) -> list[str]:
    """The union of `primary_items` and every list in `secondary_lists`,
    rendered in `vocab_order` rather than insertion order -- unlike
    `_ordered_union`, used only for `domains`/`modalities`, whose values
    come from a fixed, ordered vocabulary (`atlas.vocab.DOMAINS`/
    `MODALITIES`) and should always read in that canonical order,
    matching `apply_enrichment`'s same rule and the project-wide
    "vocab-ordered lists" convention."""
    combined = set(primary_items)
    for items in secondary_lists:
        combined.update(items)
    return [v for v in vocab_order if v in combined]


def _doi_paper_key(paper: Paper) -> str:
    """Dedupe key for a `Paper`: `dedupe.doi_key`-normalized (so a
    version-suffixed or differently-cased doi matches its canonical
    form), falling back to the raw string for anything that doesn't
    parse as a DOI at all -- two differently-malformed values must never
    collide just because both normalized to `None`."""
    return doi_key(paper.doi) or paper.doi


def _append_secondary_doi_papers(
    papers: list[Paper], secondaries: list[Record]
) -> list[Paper]:
    """Every secondary's own `dataset_doi` (e.g. a Scientific Data
    companion article's DOI) becomes a `Paper(relation="describes")` on
    the merged record -- unless a paper with that doi (compared via
    `dedupe.doi_key`, so a version suffix or casing difference doesn't
    fool the check) is already present, whether from the primary, from
    another secondary's `papers` list, or from an earlier secondary in
    this same loop. The original secondary-reported doi string is kept
    verbatim, never rewritten to its normalized form."""
    result = list(papers)
    seen = {_doi_paper_key(paper) for paper in result}
    for secondary in secondaries:
        doi = secondary.dataset_doi
        if not doi:
            continue
        key = doi_key(doi) or doi
        if key not in seen:
            result.append(Paper(doi=doi, relation="describes"))
            seen.add(key)
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


def _fill_species(primary_species: str, secondaries: list[Record]) -> str:
    """`primary_species` unless it's `"unknown"`, in which case the
    first secondary (in id order) whose own species isn't `"unknown"`
    either; otherwise `"unknown"` stands (nothing known anywhere in the
    cluster)."""
    if primary_species != "unknown":
        return primary_species
    for secondary in secondaries:
        if secondary.species != "unknown":
            return secondary.species
    return primary_species


def _fill_years(primary_years: Years, secondaries: list[Record]) -> Years:
    """`primary_years`, with `start`/`end` each independently filled
    from the first secondary (in id order) that has a value for that
    specific endpoint when the primary's own is `None` -- so a primary
    with a known `start` but no `end` can pick up just the `end` from
    one secondary, even if a *different* secondary is the one that
    happens to supply it. Returns `primary_years` unchanged (same
    object) when neither endpoint needs filling."""
    start = primary_years.start
    if start is None:
        for secondary in secondaries:
            if secondary.years.start is not None:
                start = secondary.years.start
                break

    end = primary_years.end
    if end is None:
        for secondary in secondaries:
            if secondary.years.end is not None:
                end = secondary.years.end
                break

    if start == primary_years.start and end == primary_years.end:
        return primary_years
    return Years(start=start, end=end)


def merge_cluster(cluster: list[Record]) -> tuple[Record, list[Excluded]]:
    """Collapse one `dedupe.cluster()` group into `(merged_primary,
    excluded_secondaries)`.

    Primary selection: highest-trust source per `_SOURCE_PRIORITY`, ties
    broken by the smaller id. A singleton cluster returns that record
    completely unchanged, with no exclusions.

    `domains`/`modalities` are the union of every member's, rendered in
    `vocab.DOMAINS`/`MODALITIES` order (not insertion order -- these two
    fields draw from a fixed, ordered vocabulary). Every other list field
    (`conditions` by lowercased label, `keywords`, `countries`,
    `institutions` by name, `authors` by name, `papers` by
    `dedupe.doi_key`, `related` by id) unions preserving the primary's
    own order, then each secondary's (in id order). `related` additionally
    drops any entry that points at the primary or at any other member of
    this same cluster -- a link like that would become a meaningless
    self-reference once the cluster collapses to one record; see
    `remap_related` for fixing up links *elsewhere* in the catalog that
    pointed at a now-folded-away secondary. Every secondary's own
    `dataset_doi` additionally becomes a `describes` `Paper` on the
    result if no paper with that doi is already present.

    Scalar conflicts: `access` resolves to the more restrictive tier
    (`vocab.ACCESS_ORDER`); when that resolves to a *secondary's* tier
    rather than the primary's own, that secondary's `access_notes`/
    `access_howto` come along with it (they're prose about that specific
    tier, so the primary's own notes -- about a less restrictive tier --
    would misdescribe the result); `access_tiers` is the union of every
    member's tiers, in `ACCESS_ORDER`. `record_status` is
    `"needs_review"` if any member is, else the primary's. `species`
    fills from the first secondary with a known species when the
    primary's is `"unknown"`; `years` fills `start`/`end` independently,
    each from the first secondary that has one, when the primary lacks
    it. `sample_size`, `sample_unit`, `size_bytes`, `license`, `summary`,
    `population`, `name`, `url`, `published`, and `version` stay the
    primary's, filled from the first secondary (id order) only when the
    primary's own value is null/empty.

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

    cluster_ids = {r.id for r in cluster}

    domains = _vocab_ordered_union(
        primary.domains, (s.domains for s in secondaries), vocab.DOMAINS
    )
    modalities = _vocab_ordered_union(
        primary.modalities, (s.modalities for s in secondaries), vocab.MODALITIES
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
    related = [r for r in related if r.id not in cluster_ids]
    papers = _ordered_union(
        primary.papers, (s.papers for s in secondaries), key=_doi_paper_key
    )
    papers = _append_secondary_doi_papers(papers, secondaries)

    access = max((r.access for r in cluster), key=vocab.ACCESS_ORDER.index)
    if access == primary.access:
        access_notes = primary.access_notes
        access_howto = primary.access_howto
    else:
        escalator = next(s for s in secondaries if s.access == access)
        access_notes = escalator.access_notes
        access_howto = escalator.access_howto
    access_tiers = sorted(
        {tier for r in cluster for tier in r.access_tiers},
        key=vocab.ACCESS_ORDER.index,
    )
    record_status = (
        "needs_review"
        if any(r.record_status == "needs_review" for r in cluster)
        else primary.record_status
    )

    species = _fill_species(primary.species, secondaries)
    years = _fill_years(primary.years, secondaries)
    sample_size = _first_truthy(
        primary.sample_size, secondaries, lambda r: r.sample_size
    )
    sample_unit = _first_truthy(
        primary.sample_unit, secondaries, lambda r: r.sample_unit
    )
    size_bytes = _first_truthy(primary.size_bytes, secondaries, lambda r: r.size_bytes)
    license_ = _first_truthy(primary.license, secondaries, lambda r: r.license)
    summary = _first_truthy(primary.summary, secondaries, lambda r: r.summary)
    population = _first_truthy(primary.population, secondaries, lambda r: r.population)
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
            "population": population,
            "species": species,
            "countries": countries,
            "years": years,
            "institutions": institutions,
            "authors": authors,
            "papers": papers,
            "related": related,
            "access": access,
            "access_tiers": access_tiers,
            "access_notes": access_notes,
            "access_howto": access_howto,
            "sample_size": sample_size,
            "sample_unit": sample_unit,
            "size_bytes": size_bytes,
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
# build_alias_map / remap_related: orchestrator follow-up, run once after
# every cluster in a whole catalog has been merge_cluster()-ed
# ---------------------------------------------------------------------------

_MERGED_INTO_PREFIX = "merged_into:"


def build_alias_map(excluded: list[Excluded]) -> dict[str, str]:
    """`{secondary_id: primary_id}` for every `merge_cluster` exclusion,
    parsed from its `"merged_into:<primary id>"` reason. An `Excluded`
    entry whose reason isn't a `merged_into:` note (e.g. a normalize-time
    exclusion for an unrelated cause) is skipped.

    For the orchestrator: once a whole catalog's clusters have all been
    merged, pass every `Excluded` produced along the way (concatenated
    across clusters) here, then pass the result to `remap_related` to fix
    up any `related` link elsewhere that still names a now-folded-away
    secondary id.
    """
    alias_map: dict[str, str] = {}
    for item in excluded:
        if item.reason.startswith(_MERGED_INTO_PREFIX):
            alias_map[item.native_id] = item.reason[len(_MERGED_INTO_PREFIX) :]
    return alias_map


def remap_related(records: list[Record], alias_map: dict[str, str]) -> list[Record]:
    """Rewrite every `related[].id` in `records` through `alias_map`
    (typically `build_alias_map`'s output). An id with no entry in
    `alias_map` passes through unchanged. After remapping: a link now
    dangling (its id, old or remapped, isn't any record in `records`) is
    dropped; a link now pointing at the record's own id (a self-loop the
    remap just created) is dropped; and duplicate ids (e.g. two
    secondaries of the same cluster both linked from one record, now
    both aliasing to the same primary) are deduped, first occurrence
    kept.

    Returns a new list, same order as `records`; a record whose
    `related` doesn't actually change comes back as the same object, not
    rebuilt.
    """
    known_ids = {r.id for r in records}
    updated: list[Record] = []
    for record in records:
        new_related: list[Related] = []
        seen_ids: set[str] = set()
        for rel in record.related:
            new_id = alias_map.get(rel.id, rel.id)
            if new_id == record.id or new_id not in known_ids or new_id in seen_ids:
                continue
            seen_ids.add(new_id)
            if new_id == rel.id:
                new_related.append(rel)
            else:
                new_related.append(rel.model_copy(update={"id": new_id}))

        if new_related != record.related:
            updated.append(record.model_copy(update={"related": new_related}))
        else:
            updated.append(record)
    return updated


# ---------------------------------------------------------------------------
# apply_enrichment: fold rules + (optional) LLM output onto one record
# ---------------------------------------------------------------------------


class RuleHitsLike(Protocol):
    """The duck-typed shape `apply_enrichment` expects from `rule_hits`
    -- matches `atlas.enrich.rules.RuleHits`, deliberately not imported
    here (see the task brief: that module lands on another branch).
    `evidence` is accepted but not used by this module."""

    domains: list[str]
    modalities: list[str]
    conditions: list[str]
    species: str | None
    evidence: dict[str, Any]


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
    new_delta: set[str], rules_contribution: set[str], llm_contribution: set[str]
) -> str | None:
    """Which of rules/llm is responsible for `new_delta` -- the set of
    values that ended up in the *final* output and weren't there before
    (i.e. already vocab-filtered, so a rule/llm value that didn't survive
    filtering -- e.g. not a real `vocab.DOMAINS` member -- can never earn
    an attribution for a change that isn't actually visible anywhere).
    `"llm"` wins when both rules and llm proposed at least one value in
    the delta, `"rules"` when only rules did, else `None`."""
    if new_delta & llm_contribution:
        return "llm"
    if new_delta & rules_contribution:
        return "rules"
    return None


def apply_enrichment(
    record: Record, llm_out: dict | None, rule_hits: RuleHitsLike | None
) -> Record:
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
    automated pass should second-guess that. `rule_hits=None` is treated
    as an empty `RuleHitsLike` (no domains/modalities/conditions,
    unknown species) rather than an error, so a record with no rule
    engine hits at all can still go through an LLM-only pass.

    `rule_hits` is duck-typed (see `RuleHitsLike`, matching
    `atlas.enrich.rules.RuleHits`, deliberately not imported here).
    `llm_out` is `None`, or the already-guarded per-record classify
    output: `{"domains": [...], "modalities": [...], "conditions": [...],
    "summary": str | None, "population": str | None, "countries": [...]}`,
    where each `conditions` item is a label string or a `{"label": ...}`
    dict.

    Nothing is stamped unless something actually, visibly changed:
    `provenance.enrichment.method`/`.at`/`.fields` are only touched when
    at least one field's value genuinely differs from `record`'s own --
    a `llm_out` that's present but confirms exactly what `record` already
    had (or a `rule_hits` proposing only invalid/already-known values)
    leaves the record byte-for-byte as given. When something did change,
    `.method` becomes `"rules+llm"` if `llm_out` was provided, else
    `"rules"`; `.fields[<field>]` records `"rules"`/`"llm"` per field
    actually changed (llm wins the attribution when both did); `.at` is
    set to today.
    """
    if record.provenance.enrichment.method == "curated":
        return record

    if rule_hits is None:
        rule_domains: list[str] = []
        rule_modalities: list[str] = []
        rule_conditions: list[str] = []
        rule_species: str | None = None
    else:
        rule_domains = list(rule_hits.domains)
        rule_modalities = list(rule_hits.modalities)
        rule_conditions = list(rule_hits.conditions)
        rule_species = rule_hits.species

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
    domains = _vocab_ordered_union(
        record.domains, (rule_domains, llm_domains), vocab.DOMAINS
    )
    domains_delta = set(domains) - domains_old
    attribution = _attribution(domains_delta, set(rule_domains), set(llm_domains))
    if attribution:
        fields_changed["domains"] = attribution

    modalities_old = set(record.modalities)
    modalities = _vocab_ordered_union(
        record.modalities, (rule_modalities, llm_modalities), vocab.MODALITIES
    )
    modalities_delta = set(modalities) - modalities_old
    attribution = _attribution(
        modalities_delta, set(rule_modalities), set(llm_modalities)
    )
    if attribution:
        fields_changed["modalities"] = attribution

    conditions, attribution = _merge_conditions(
        record.conditions, rule_conditions, llm_condition_labels
    )
    if attribution:
        fields_changed["conditions"] = attribution

    species = record.species
    if record.species == "unknown" and rule_species:
        species = rule_species
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

    if not fields_changed:
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
