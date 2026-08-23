"""Normalizer for NCI Genomic Data Commons (GDC) project envelopes.

Every GDC "project" (e.g. ``TCGA-LUAD``, ``TARGET-AML``) becomes at most
one catalog record. A project whose listing says it isn't actually
released yet -- ``released`` false, or ``state`` other than ``"open"``
(e.g. ``"submitted"``, seen live for ``CGCI-BLGSP``) -- is excluded
outright rather than cataloged as a placeholder.

GDC mixes two very different kinds of project ``name``: a plain disease
name (``"Lung Adenocarcinoma"``, TCGA-style) and a clinical-trial/study
title (``"Genomic Characterization CS-MATCH-0007 Arm S1"``,
``"Adjuvant Lung Cancer Enrichment Marker Identification and Sequencing
Trial"``). `conditions` picks between the two: a `name` counts as a
disease phrase -- and becomes the sole `Condition` -- only when it is
*both* free of every study-title keyword in `_STUDY_NAME_RE` *and* at
most `_MAX_DISEASE_PHRASE_WORDS` words long; anything else falls back to
the project's own ``disease_type`` list instead of trying to parse a
disease out of a title. See ``docs/sources/gdc.md`` for the full rule
(ruling R13) and why the word-count cap is needed alongside the keyword
list.
"""

from __future__ import annotations

import re

from atlas import io, vocab
from atlas.normalize.common import Excluded, make_provenance
from atlas.schema import Condition, Record, Years

SOURCE = "gdc"

PORTAL_URL_TEMPLATE = "https://portal.gdc.cancer.gov/projects/{project_id}"

# `summary.data_categories[].data_category` -> canonical modality. Data
# categories with no entry here (e.g. a category GDC adds later) simply
# contribute no modality -- see `_modalities`.
_DATA_CATEGORY_MODALITY: dict[str, str] = {
    "Clinical": "clinical_tabular",
    "Biospecimen": "clinical_tabular",
    "Sequencing Reads": "genomics",
    "Simple Nucleotide Variation": "genomics",
    "Copy Number Variation": "genomics",
    "Structural Variation": "genomics",
    "Somatic Structural Variation": "genomics",
    "Combined Nucleotide Variation": "genomics",
    "DNA Methylation": "genomics",
    "Transcriptome Profiling": "transcriptomics",
    "Proteome Profiling": "proteomics",
}

# `summary.experimental_strategies[].experimental_strategy` values that map
# to a modality. Every other strategy still lands in `keywords` (see
# `_keywords`) -- it just doesn't contribute a modality of its own.
_PATHOLOGY_STRATEGIES = frozenset({"Tissue Slide", "Diagnostic Slide"})

# A project `name` reads as a clinical-trial/study title -- and therefore
# falls back to `disease_type` entries instead of the name itself -- when
# it contains any of these words (word-boundary, case-insensitive) OR runs
# longer than `_MAX_DISEASE_PHRASE_WORDS`. The word list started as just
# {MATCH, Arm, Characterization, Phase, Study} (Task 1.3's original
# mapping) but missed real live titles built the same way without any of
# those five words -- e.g. "Adjuvant Lung Cancer Enrichment Marker
# Identification and Sequencing Trial" (ALCHEMIST-ALCH) -- so a title that
# simply runs long is now also routed to `disease_type`, whether or not it
# happens to contain one of the flagged words (ruling R13; see
# docs/sources/gdc.md).
_STUDY_NAME_RE = re.compile(
    r"\b("
    r"MATCH|Arm|Characterization|Phase|Study|Trial|Enrichment|Identification|"
    r"Sequencing|Consortium|Program|Project|Initiative|Cohort|Screening|Pilot"
    r")\b",
    re.IGNORECASE,
)
_MAX_DISEASE_PHRASE_WORDS = 6

# `program.name` values that additionally mark a project pediatric.
_PEDIATRIC_PROGRAMS = frozenset({"TARGET", "CCDI"})

_ACCESS_NOTE_PREFIX = (
    "Open tier: clinical, biospecimen, masked somatic mutations, expression. "
    "Controlled tier via dbGaP"
)


def _alias(label: str) -> str:
    """`label` (already lowercased), passed through
    `vocab.CONDITION_ALIASES` when it has an entry, else unchanged."""
    return vocab.CONDITION_ALIASES.get(label, label)


def _modalities(summary: dict) -> list[str]:
    """Modalities implied by this project's data categories and
    experimental strategies, deduplicated and ordered per
    `vocab.MODALITIES` (iterating the vocab tuple itself both dedupes and
    orders in one pass)."""
    found: set[str] = set()

    for entry in summary.get("data_categories") or []:
        modality = _DATA_CATEGORY_MODALITY.get(entry.get("data_category", ""))
        if modality:
            found.add(modality)

    for entry in summary.get("experimental_strategies") or []:
        if entry.get("experimental_strategy") in _PATHOLOGY_STRATEGIES:
            found.add("pathology")

    return [m for m in vocab.MODALITIES if m in found]


def _is_disease_phrase(name: str) -> bool:
    """True when `name` reads as a plain disease name rather than a
    clinical-trial/study title: at most `_MAX_DISEASE_PHRASE_WORDS` words
    long AND free of every study-title keyword in `_STUDY_NAME_RE`. Both
    conditions must hold -- a short title can still name a study ("NCI
    MATCH Arm S1" is 4 words), so the keyword list alone isn't enough;
    conversely a long title with none of the flagged keywords (e.g.
    ALCHEMIST-ALCH's) still reads as a study name to a human, so the
    length cap catches what the keyword list alone doesn't."""
    words = name.split()
    return len(words) <= _MAX_DISEASE_PHRASE_WORDS and not _STUDY_NAME_RE.search(name)


def _conditions(name: str, disease_type: list[str]) -> list[Condition]:
    """One `Condition` for a disease-phrase `name`, else one per
    `disease_type` entry -- see `_is_disease_phrase`."""
    if _is_disease_phrase(name):
        return [Condition(label=_alias(name.lower()))]
    return [Condition(label=_alias(label.lower())) for label in disease_type]


def _keywords(primary_site: list[str], program_name: str, summary: dict) -> list[str]:
    """`primary_site` entries, then the program name, then every
    experimental strategy verbatim (including the ones that also
    contributed a `pathology` modality -- they still count as keywords),
    deduplicated (first occurrence wins) with empty strings dropped (a
    missing `experimental_strategy`/blank `program.name` would otherwise
    show up as a bare `""` keyword)."""
    strategies = [
        entry.get("experimental_strategy", "")
        for entry in summary.get("experimental_strategies") or []
    ]
    seen: set[str] = set()
    keywords: list[str] = []
    for candidate in (*primary_site, program_name, *strategies):
        if candidate and candidate not in seen:
            seen.add(candidate)
            keywords.append(candidate)
    return keywords


def _top_data_categories(summary: dict, n: int = 3) -> list[str]:
    """The `n` `data_category` names with the highest `case_count`,
    ties broken by original listing order (Python's sort is stable)."""
    categories = summary.get("data_categories") or []
    ranked = sorted(categories, key=lambda c: c.get("case_count", 0) or 0, reverse=True)
    return [c.get("data_category", "") for c in ranked[:n]]


def _access_notes(payload: dict, program: dict) -> str:
    """`"Open tier: ... Controlled tier via dbGaP <phs>."`, `<phs>` being
    the project's own `dbgap_accession_number` when set, else the
    program's -- and the plain unqualified sentence when neither exists
    (both fields are absent outright on some hits, e.g. live
    `ALCHEMIST-ALCH`, not just `null`, hence `.get`)."""
    phs = payload.get("dbgap_accession_number") or program.get("dbgap_accession_number")
    if phs:
        return f"{_ACCESS_NOTE_PREFIX} {phs}."
    return f"{_ACCESS_NOTE_PREFIX}."


def _summary_text(
    name: str,
    program_name: str,
    case_count: int | None,
    sites: list[str],
    summary: dict,
) -> str:
    sites_text = ", ".join(sites)
    data_text = ", ".join(_top_data_categories(summary))
    case_clause = f" with {case_count} cases" if case_count is not None else ""
    text = (
        f"{name}: {program_name} project in the NCI Genomic Data Commons"
        f"{case_clause}; primary sites: {sites_text}; data: {data_text}."
    )
    return io.first_words(text, 40)


def normalize(
    envelope: dict, *, harvested_at: str, first_seen: str
) -> Record | Excluded:
    payload = envelope["payload"]
    project_id = payload["project_id"]

    if not (payload.get("released") and payload.get("state") == "open"):
        return Excluded(native_id=project_id, reason="not_released")

    name = payload.get("name", "")
    primary_site = payload.get("primary_site") or []
    disease_type = payload.get("disease_type") or []
    program = payload.get("program") or {}
    program_name = program.get("name", "")
    summary = payload.get("summary") or {}
    case_count = summary.get("case_count")

    domains = ["oncology"]
    if program_name in _PEDIATRIC_PROGRAMS:
        domains.append("pediatrics")

    return Record(
        id=f"gdc:{project_id}",
        source=SOURCE,
        source_native_id=project_id,
        name=f"{name} ({project_id})",
        summary=_summary_text(name, program_name, case_count, primary_site, summary),
        url=PORTAL_URL_TEMPLATE.format(project_id=project_id),
        dataset_doi=None,
        version=None,
        published=None,
        domains=domains,
        modalities=_modalities(summary),
        conditions=_conditions(name, disease_type),
        keywords=_keywords(primary_site, program_name, summary),
        population=None,
        species="human",
        sample_size=case_count,
        sample_unit="cases" if case_count is not None else None,
        size_bytes=None,
        countries=[],
        years=Years(),
        access="open",
        access_tiers=["open", "application"],
        access_notes=_access_notes(payload, program),
        access_howto=None,
        license=None,
        institutions=[],
        authors=[],
        papers=[],
        related=[],
        record_status="active",
        provenance=make_provenance(
            via="api:gdc-projects",
            harvested_at=harvested_at,
            first_seen=first_seen,
            raw_hash=io.content_hash(payload),
        ),
    )


def enrichment_text(envelope: dict) -> str:
    """``name + primary sites + disease types + program + data categories
    + experimental strategies``, space-joined and capped at 1,500 chars
    for the enrich stage's keyword/domain/condition classification."""
    payload = envelope["payload"]
    program = payload.get("program") or {}
    summary = payload.get("summary") or {}

    parts = [
        payload.get("name", ""),
        ", ".join(payload.get("primary_site") or []),
        ", ".join(payload.get("disease_type") or []),
        program.get("name", ""),
        ", ".join(
            c.get("data_category", "") for c in summary.get("data_categories") or []
        ),
        ", ".join(
            e.get("experimental_strategy", "")
            for e in summary.get("experimental_strategies") or []
        ),
    ]
    text = " ".join(part for part in parts if part)
    return text[:1500]
