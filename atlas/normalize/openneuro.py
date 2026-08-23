"""OpenNeuro normalizer: one GraphQL dataset node -> one canonical `Record`.

Maps only fields OpenNeuro's `datasets` GraphQL query reports explicitly
(see `docs/sources/openneuro.md` for the exact query and verified quirks);
no keyword-based domain/modality/condition inference beyond the small,
explicit rules the task brief calls for here (`_map_domains`,
`_map_conditions`) -- broader keyword classification happens later, in the
enrich stage, from `enrichment_text`.

Two real-data quirks drive several of the helpers below (see the module's
tests, each backed by a live-captured fixture in
`tests/fixtures/openneuro/records/`):

- Unfinished/template BIDS submissions leave OpenNeuro's stock
  boilerplate in place: `Authors` becomes exactly
  `["TODO:", "First1 Last1", "First2 Last2", "..."]`, and `readme` becomes
  the stock "TODO: Provide description..." text. Both are detected and
  treated as "not really there" rather than surfaced as real content.
- `associatedPaperDOI` / `ReferencesAndLinks` are free text a depositor
  typed by hand: multiple DOIs joined by commas, `doi:`/`https://doi.org/`/
  `https://dx.doi.org/` prefixes, trailing sentence punctuation stuck to
  the DOI, non-DOI links (OSF, arXiv abstract pages, NeurIPS posters), and
  -- concretely observed in `ds008159` -- the dataset's *own* DOI (an
  older version of it) cited in `ReferencesAndLinks` as a "please also
  cite this dataset" note. `_extract_dois` + the self-DOI check in
  `normalize` handle all of this without fabricating or double-counting
  a paper.
"""

from __future__ import annotations

import re

from atlas import io, vocab
from atlas.normalize import common
from atlas.schema import Author, Condition, Paper, Record, Years

SOURCE = "openneuro"

BASE_URL = "https://openneuro.org/datasets"

# ---------------------------------------------------------------------------
# Modalities: summary.modalities + summary.secondaryModalities -> vocab.MODALITIES
# ---------------------------------------------------------------------------

# Compared case-insensitively: older OpenNeuro snapshots report these
# uppercase ("MRI", "EEG") while current ones are lowercase.
_MODALITY_MAP: dict[str, str] = {
    "mri": "MRI",
    "eeg": "EEG",
    "meg": "MEG",
    "ieeg": "iEEG",
    "pet": "PET",
    "nirs": "fNIRS",
    "mrs": "MRS",
    "beh": "behavioral",
    "motion": "wearable",
    "micr": "pathology",
    "ct": "CT",
}

# Verified secondaryModalities value set (2026-08-22): mri_structural,
# mri_functional, mri_diffusion, mri_perfusion. Anything else falls back
# to the same "unknown -> keywords" handling as an unmapped primary
# modality, rather than being silently dropped.
_SECONDARY_MODALITY_MAP: dict[str, str] = {
    "mri_functional": "fMRI",
    "mri_diffusion": "dMRI",
    "mri_structural": "MRI",
    "mri_perfusion": "MRI",
}


def _map_modalities(
    raw_modalities: list | None, raw_secondary: list | None
) -> tuple[list[str], list[str]]:
    """`(modalities, unmapped)` -- `modalities` is the vocab-ordered union
    of every raw value that maps via `_MODALITY_MAP`/`_SECONDARY_MODALITY_MAP`;
    `unmapped` is every raw string that didn't (the brief's "unknown ->
    keywords" rule -- folded into `keywords` by the caller, not dropped).
    """
    matched: set[str] = set()
    unmapped: list[str] = []
    for value in raw_modalities or []:
        mapped = _MODALITY_MAP.get((value or "").strip().lower())
        if mapped:
            matched.add(mapped)
        elif value:
            unmapped.append(value)
    for value in raw_secondary or []:
        mapped = _SECONDARY_MODALITY_MAP.get((value or "").strip().lower())
        if mapped:
            matched.add(mapped)
        elif value:
            unmapped.append(value)
    ordered = [m for m in vocab.MODALITIES if m in matched]
    return ordered, unmapped


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


# ---------------------------------------------------------------------------
# Species: metadata.species -> vocab.SPECIES
# ---------------------------------------------------------------------------


def _map_species(species_raw: str | None, affirmed_defaced: bool) -> str:
    """human / animal / unknown per the brief: an explicit "human" maps
    to human; any other non-empty free-text species (rat, mouse, macaque,
    "rhesus monkey", "Other", ...) maps to animal -- OpenNeuro's species
    field is only ever used for living study subjects, never phantom/
    simulated data, so "not human" reliably means "animal" here; empty
    falls back to human when the dataset affirms it was defaced (a
    defacing step that is meaningless for non-human/no-subject data,
    so affirming it is itself weak evidence of a human subject) else
    unknown.
    """
    species = (species_raw or "").strip()
    if not species:
        return "human" if affirmed_defaced else "unknown"
    if species.lower() == "human":
        return "human"
    return "animal"


# ---------------------------------------------------------------------------
# Domains: studyDomain/dxStatus keywords -> neurology/psychiatry, else neuroscience
# ---------------------------------------------------------------------------

_NEUROLOGY_KEYWORD = "neurolog"  # matches neurology/neurological/neurologist
_PSYCHIATRY_KEYWORD = "psychiat"  # matches psychiatry/psychiatric


def _map_domains(study_domain: str | None, dx_status: str | None) -> list[str]:
    text = f"{study_domain or ''} {dx_status or ''}".lower()
    domains: list[str] = []
    if _NEUROLOGY_KEYWORD in text:
        domains.append("neurology")
    if _PSYCHIATRY_KEYWORD in text:
        domains.append("psychiatry")
    return domains or ["neuroscience"]


# ---------------------------------------------------------------------------
# Conditions: dxStatus -> vocab.CONDITION_ALIASES (case-insensitive substring)
# ---------------------------------------------------------------------------


def _map_conditions(dx_status: str | None) -> list[Condition]:
    text = (dx_status or "").strip().lower()
    if not text:
        return []
    labels: list[str] = []
    for alias, canonical in vocab.CONDITION_ALIASES.items():
        if alias in text and canonical not in labels:
            labels.append(canonical)
    return [Condition(label=label) for label in labels]


# ---------------------------------------------------------------------------
# Population: "<species>; <dxStatus>; ages a-b", parts present only
# ---------------------------------------------------------------------------

_EN_DASH = "–"


def _map_population(
    species_raw: str | None, dx_status: str | None, ages: list | None
) -> str | None:
    """Free-text population summary built only from what the source
    actually reported: the *raw* species/dxStatus strings (not the
    coarser `species` enum's empty->human/unknown fallback, which is a
    schema-required classification, not something the source said in
    free text) and an age range computed from `ages`' non-null entries.
    Any part with nothing to report is omitted rather than guessed.
    """
    parts: list[str] = []
    species_text = (species_raw or "").strip()
    if species_text:
        parts.append(species_text)
    dx_text = (dx_status or "").strip()
    if dx_text:
        parts.append(dx_text)
    known_ages = [a for a in (ages or []) if a is not None]
    if known_ages:
        lo, hi = min(known_ages), max(known_ages)
        parts.append(f"ages {lo}" if lo == hi else f"ages {lo}{_EN_DASH}{hi}")
    return "; ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Summary: first 40 words of a real readme, else a templated one-liner
# ---------------------------------------------------------------------------


def _is_placeholder_text(text: str | None) -> bool:
    """True for empty text or OpenNeuro's stock "TODO" boilerplate that
    an unfinished BIDS submission leaves in `readme` -- e.g. observed
    verbatim: "TODO: Provide description for the dataset -- basic
    details about the study, possibly pointing to pre-registration ..."
    """
    if not text or not text.strip():
        return True
    return text.strip().lower().startswith("todo")


def _build_summary(name: str, readme: str | None, modalities: list[str], n: int) -> str:
    if readme and not _is_placeholder_text(readme):
        text = io.first_words(readme, 40)
    else:
        modality_str = ", ".join(modalities)
        body = (
            f"{modality_str} dataset on OpenNeuro ({n} participants)."
            if modality_str
            else f"dataset on OpenNeuro ({n} participants)."
        )
        text = f"{name}: {body}"
    # Defensive final cap: guarantees schema's <=40-word summary limit
    # regardless of which branch produced `text` (a long dataset `name`
    # could otherwise push the templated branch over 40 words).
    return io.first_words(text, 40)


# ---------------------------------------------------------------------------
# Authors: drop OpenNeuro's stock placeholder entries
# ---------------------------------------------------------------------------

# Matches "TODO" / "TODO:", "First1 Last1"-style placeholders (with or
# without trailing digits), and a bare "..." -- the exact 4-entry stock
# list an unfinished dataset_description.json leaves behind is
# `["TODO:", "First1 Last1", "First2 Last2", "..."]` (observed verbatim
# in `ds008704` and 11 other sampled datasets).
_PLACEHOLDER_AUTHOR_RE = re.compile(r"(?i)^(?:todo:?|first\d*\s+last\d*|\.{3,})$")


def _is_placeholder_author(name: str) -> bool:
    return bool(_PLACEHOLDER_AUTHOR_RE.match(name.strip()))


def _map_authors(raw_authors: list | None) -> list[Author]:
    return [
        Author(name=a.strip())
        for a in (raw_authors or [])
        if isinstance(a, str) and a.strip() and not _is_placeholder_author(a)
    ]


# ---------------------------------------------------------------------------
# Papers: associatedPaperDOI + a DOI regex over ReferencesAndLinks
# ---------------------------------------------------------------------------

# `common.clean_doi` requires a full-string DOI match; free text here can
# have several DOIs run together (comma-separated) or a DOI immediately
# followed by prose punctuation, so this finds every DOI-shaped substring
# first and only then hands each candidate to `clean_doi`.
_DOI_FIND_RE = re.compile(r"10\.\d{4,9}/\S+")
# A DOI regex match is greedy on non-whitespace, so a comma/semicolon/
# closing-bracket/sentence period stuck directly to the end of a DOI (a
# list separator or the end of a sentence, not part of the DOI) comes
# along for the ride; strip a trailing run of those before validating.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:)\]}'\"]+$")


def _extract_dois(text: str) -> list[str]:
    """Every distinct, version-collapsed DOI found in free text `text`,
    in first-seen order."""
    if not text:
        return []
    found: list[str] = []
    for raw_match in _DOI_FIND_RE.findall(text):
        candidate = _TRAILING_PUNCT_RE.sub("", raw_match)
        cleaned = common.clean_doi(candidate)
        if not cleaned:
            continue
        collapsed = common.collapse_version_doi(cleaned)
        if collapsed not in found:
            found.append(collapsed)
    return found


def _references_text(references_raw) -> str:
    if not isinstance(references_raw, list):
        return ""
    return " ".join(r for r in references_raw if isinstance(r, str))


def _map_papers(
    associated_paper_doi: str | None, references_raw, own_dataset_doi: str | None
) -> list[Paper]:
    candidates = _extract_dois(associated_paper_doi or "") + _extract_dois(
        _references_text(references_raw)
    )
    paper_dois: list[str] = []
    for doi in candidates:
        if doi == own_dataset_doi or doi in paper_dois:
            continue
        paper_dois.append(doi)
    return [Paper(doi=doi, relation="describes") for doi in paper_dois]


# ---------------------------------------------------------------------------
# normalize / enrichment_text
# ---------------------------------------------------------------------------


def normalize(
    envelope: dict, *, harvested_at: str, first_seen: str
) -> Record | common.Excluded:
    payload = envelope["payload"]
    native_id = envelope["native_id"]

    snapshot = payload.get("latestSnapshot")
    if not snapshot:
        # Unpublished/draft: still listed by the GraphQL query (it has an
        # id and metadata) but has no published version to describe --
        # not observed in a live sample of ~1,000 datasets, but explicitly
        # anticipated by the task brief.
        return common.Excluded(native_id=native_id, reason="no_snapshot")

    metadata = payload.get("metadata") or {}
    description = snapshot.get("description") or {}
    summary = snapshot.get("summary") or {}

    # `Name` wraps mid-sentence in a handful of live records, so it needs
    # collapsing, not just stripping, before it can be a one-line title.
    name = common.clean_title(description.get("Name")) or native_id

    cleaned_dataset_doi = common.clean_doi(description.get("DatasetDOI"))
    dataset_doi = (
        common.collapse_version_doi(cleaned_dataset_doi)
        if cleaned_dataset_doi
        else None
    )

    modalities, unmapped_modalities = _map_modalities(
        summary.get("modalities"), summary.get("secondaryModalities")
    )

    subjects = summary.get("subjects") or []
    sample_size = len(subjects)

    tasks = [t for t in (summary.get("tasks") or []) if isinstance(t, str)]
    keywords = _dedup_preserve_order(tasks + unmapped_modalities)

    raw_license = description.get("License")
    license_ = common.license_to_spdx(raw_license) or (raw_license or None)

    published_raw = payload.get("publishDate") or ""
    published = published_raw[:10] or None

    return Record(
        id=f"{SOURCE}:{native_id}",
        source=SOURCE,
        source_native_id=native_id,
        name=name,
        summary=_build_summary(name, snapshot.get("readme"), modalities, sample_size),
        url=f"{BASE_URL}/{native_id}",
        dataset_doi=dataset_doi,
        version=snapshot.get("tag"),
        published=published,
        domains=_map_domains(metadata.get("studyDomain"), metadata.get("dxStatus")),
        modalities=modalities,
        conditions=_map_conditions(metadata.get("dxStatus")),
        keywords=keywords,
        population=_map_population(
            metadata.get("species"), metadata.get("dxStatus"), metadata.get("ages")
        ),
        species=_map_species(
            metadata.get("species"), bool(metadata.get("affirmedDefaced"))
        ),
        sample_size=sample_size,
        sample_unit="participants",
        size_bytes=summary.get("size"),
        years=Years(),
        access="open",
        access_tiers=["open"],
        license=license_,
        authors=_map_authors(description.get("Authors")),
        papers=_map_papers(
            metadata.get("associatedPaperDOI"),
            description.get("ReferencesAndLinks"),
            dataset_doi,
        ),
        record_status="active",
        provenance=common.make_provenance(
            via="api:openneuro-graphql",
            harvested_at=harvested_at,
            first_seen=first_seen,
            raw_hash=io.content_hash(payload),
        ),
    )


def enrichment_text(envelope: dict) -> str:
    """Name + studyDomain + dxStatus + readme + tasks, HTML-stripped and
    capped at 1,500 chars -- fed to the (later-phase) LLM enrich stage for
    keyword-based domain/modality/condition classification beyond the
    explicit rules `normalize` applies above."""
    payload = envelope["payload"]
    metadata = payload.get("metadata") or {}
    snapshot = payload.get("latestSnapshot") or {}
    description = snapshot.get("description") or {}
    summary = snapshot.get("summary") or {}

    name = (description.get("Name") or "").strip() or str(payload.get("id") or "")
    tasks = ", ".join(t for t in (summary.get("tasks") or []) if isinstance(t, str))
    parts = [
        name,
        metadata.get("studyDomain") or "",
        metadata.get("dxStatus") or "",
        snapshot.get("readme") or "",
        tasks,
    ]
    text = io.strip_html(" ".join(p for p in parts if p))
    return text[:1500]
