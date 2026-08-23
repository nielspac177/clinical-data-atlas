"""Normalize TCIA envelopes (NBIA v1 + DataCite) into canonical `Record`s.

The two halves of the payload answer different questions and neither is
guaranteed present, so almost every mapping here is "DataCite if it said
so, else NBIA, else nothing":

- **DataCite** supplies the human-facing metadata -- title, abstract,
  license, DOI, version, authors, affiliations, related papers.
- **NBIA** supplies the machine-facing facts -- modality codes, body
  parts, and the patient list that `sample_size` and `species` come from.
- **Which halves are present** is itself a signal. NBIA lists exactly the
  collections it will serve without credentials, so an NBIA-listed
  collection is `open`; a DataCite `/collection/` DOI that NBIA never
  listed is gated, and its rights text says whether that gate is an
  application or a registration.

Nothing here infers a domain, modality or condition from prose -- that is
the enrich stage's job, fed by :func:`enrichment_text`. The two narrow
exceptions are spelled out where they happen (`_DOMAIN_OVERRIDES` and
`_TITLE_CONDITIONS`), both driven by the title alone and both an explicit
lookup rather than a guess.
"""

from __future__ import annotations

import re

from atlas import io, vocab
from atlas.harvest.tcia import main_title, strip_volatile
from atlas.normalize import common
from atlas.schema import Author, Condition, Institution, Paper, Record, Years

SOURCE = "tcia"
HARVEST_METHOD = "api:nbia+datacite"

# TCIA has no per-collection landing page outside DataCite, so a record
# with no DataCite match links to the collections index instead.
COLLECTIONS_URL = "https://www.cancerimagingarchive.net/collections/"

# The 22 DICOM modality codes NBIA reports. The imaging ones become
# canonical `modalities`; the rest describe *derived* objects (a
# segmentation, a registration, a structured report) rather than a way of
# imaging a patient, so they are kept as free-text keywords for the enrich
# stage instead of being forced into the modality vocabulary.
_MODALITY_MAP: dict[str, str] = {
    "MR": "MRI",
    "CT": "CT",
    "PT": "PET",
    "NM": "SPECT",
    "US": "ultrasound",
    "MG": "mammography",
    "CR": "xray",
    "DX": "xray",
    "RF": "xray",
    "XA": "xray",
    "RTSTRUCT": "radiotherapy",
    "RTDOSE": "radiotherapy",
    "RTPLAN": "radiotherapy",
    "SM": "pathology",
}
_KEYWORD_MODALITIES = frozenset({"SEG", "REG", "FUSION", "KO", "PR", "SR", "RWV", "OT"})

# DataCite `rightsIdentifier` -> SPDX. `common.license_to_spdx` already
# covers most of these via `vocab.LICENSE_MAP`; the table is repeated in
# full so the mapping this source relies on is visible in one place and
# `cc0-1.0` (absent from `vocab.LICENSE_MAP`, which only carries the bare
# "CC0" spelling) is covered too.
_RIGHTS_SPDX: dict[str, str] = {
    "cc-by-4.0": "CC-BY-4.0",
    "cc-by-3.0": "CC-BY-3.0",
    "cc-by-nc-4.0": "CC-BY-NC-4.0",
    "cc-by-nc-3.0": "CC-BY-NC-3.0",
    "cc-by-nc-nd-3.0": "CC-BY-NC-ND-3.0",
    "cc0-1.0": "CC0-1.0",
}

# Rights wording that means the gate is a data-use *application*, not a
# click-through registration: NIH Controlled Access, the NCTN Data Archive
# DUA, TCIA's Limited Access licenses, and its Data Usage Policy.
_APPLICATION_RIGHTS_RE = re.compile(
    r"Limited Access|Controlled|NCTN|Data Usage Policy", re.IGNORECASE
)

# DataCite `relationType` -> `Paper.relation`. Anything else DataCite
# reports (IsDerivedFrom, IsPartOf, ...) is a real link but not one of the
# three we can characterize, so it lands in `other`.
_PAPER_RELATIONS: dict[str, str] = {
    "iscitedby": "is_cited_by",
    "isdescribedby": "describes",
    "issupplementto": "describes",
}
_MAX_PAPERS = 20

# TCIA is a cancer-imaging archive, so `oncology` is the default rather
# than an inference. These three title patterns are the collections where
# that default is provably wrong or incomplete.
# Word-anchored on purpose: an unanchored `pedi` also fires on
# "impedance" and "expedited", and an unanchored `covid` on "Covidien".
_DOMAIN_OVERRIDES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcovid\b", re.IGNORECASE), "covid"),
    (re.compile(r"\bhealthy\b|\bnormative\b", re.IGNORECASE), "not_oncology"),
    (re.compile(r"\bpedi\w*", re.IGNORECASE), "pediatrics"),
)

# Cancer phrases specific enough to name a condition straight from a
# title, mapped through `vocab.CONDITION_ALIASES`. Deliberately tiny:
# every entry is an unambiguous multi-word disease name, never an
# abbreviation, and only the *title* is scanned -- a description mentioning
# "breast cancer response" says nothing about what the dataset studies.
# Everything else is left to the enrich stage.
_TITLE_CONDITIONS: tuple[str, ...] = (
    "glioblastoma multiforme",
    "lung adenocarcinoma",
    "breast cancer",
    "lung cancer",
)


# ---------------------------------------------------------------------------
# Payload accessors
# ---------------------------------------------------------------------------


def _halves(envelope: dict) -> tuple[dict, dict]:
    """``(nbia, datacite)``, each ``{}`` when the payload has no such half."""
    payload = envelope.get("payload") or {}
    return payload.get("nbia") or {}, payload.get("datacite") or {}


def _description(datacite: dict) -> str:
    for entry in datacite.get("descriptions") or []:
        text = io.strip_html(entry.get("description") or "")
        if text:
            return text
    return ""


def _record_name(envelope: dict, nbia: dict, datacite: dict) -> str:
    return (
        main_title(datacite)
        or nbia.get("collection")
        or envelope.get("native_id")
        or ""
    )


# ---------------------------------------------------------------------------
# Field mappings
# ---------------------------------------------------------------------------


def _modalities_and_keywords(nbia: dict) -> tuple[list[str], list[str]]:
    """Canonical modalities (vocabulary order) and the keywords that fall
    out of the codes and body parts that aren't modalities."""
    codes = nbia.get("modalities") or []
    modalities = {_MODALITY_MAP[code] for code in codes if code in _MODALITY_MAP}

    keywords: list[str] = []
    for code in codes:
        if code in _KEYWORD_MODALITIES and code.lower() not in keywords:
            keywords.append(code.lower())
    for body_part in nbia.get("body_parts") or []:
        if body_part.lower() not in keywords:
            keywords.append(body_part.lower())

    ordered = [m for m in vocab.MODALITIES if m in modalities]
    return ordered, keywords


def _sample_size(nbia: dict) -> int | None:
    """Non-phantom patients, or `None` when NBIA reported no patient list
    at all (a gated record, or a `getPatient` request that failed)."""
    patients = nbia.get("patients")
    if patients is None:
        return None
    return sum(1 for patient in patients if not _is_phantom(patient))


def _is_phantom(patient: dict) -> bool:
    return (patient.get("Phantom") or "").strip().upper() == "YES"


def _species(nbia: dict) -> str:
    """`SPECIES` value implied by the patient list.

    Phantom scans are checked first: an imaging phantom carries a
    `SpeciesDescription` too, so reading species before Phantom would
    label a phantom-only collection "human".
    """
    patients = nbia.get("patients")
    if not patients:
        return "unknown"
    if all(_is_phantom(patient) for patient in patients):
        return "phantom"

    described = {
        (patient.get("SpeciesDescription") or "").strip().lower()
        for patient in patients
        if not _is_phantom(patient)
    }
    described.discard("")
    if not described:
        return "unknown"

    human = "homo sapiens" in described
    others = described - {"homo sapiens"}
    if human and not others:
        return "human"
    if others and not human:
        return "animal"
    return "mixed"


def _rights(datacite: dict) -> list[dict]:
    return datacite.get("rightsList") or []


def _license(datacite: dict) -> str | None:
    """SPDX id when DataCite gave a `rightsIdentifier` we recognize, else
    the first entry's verbatim rights text, else `None`."""
    for entry in _rights(datacite):
        identifier = (entry.get("rightsIdentifier") or "").strip().lower()
        spdx = _RIGHTS_SPDX.get(identifier) or common.license_to_spdx(identifier)
        if spdx:
            return spdx
    for entry in _rights(datacite):
        text = (entry.get("rights") or "").strip()
        if text:
            return text
    return None


def _access(nbia: dict, datacite: dict) -> str:
    """`open` for anything NBIA lists publicly; otherwise the gate the
    DataCite rights text describes."""
    if nbia:
        return "open"
    rights_text = " ".join(entry.get("rights") or "" for entry in _rights(datacite))
    return (
        "application" if _APPLICATION_RIGHTS_RE.search(rights_text) else "registration"
    )


def _authors(datacite: dict) -> list[Author]:
    authors = []
    for creator in datacite.get("creators") or []:
        name = (creator.get("name") or "").strip()
        if not name:
            continue
        authors.append(Author(name=name, orcid=_orcid(creator)))
    return authors


def _orcid(creator: dict) -> str | None:
    """The creator's ORCID exactly as DataCite gave it -- bare id or
    `https://orcid.org/...` url; normalizing the two forms is the enrich
    stage's business, not a mapping decision."""
    for identifier in creator.get("nameIdentifiers") or []:
        scheme = (identifier.get("nameIdentifierScheme") or "").strip().upper()
        value = (identifier.get("nameIdentifier") or "").strip()
        if scheme == "ORCID" and value:
            return value
    return None


def _institutions(datacite: dict) -> list[Institution]:
    """Creator affiliations, deduped by name, first mention wins.

    DataCite's schema allows an affiliation to be either a plain string or
    an object with a `name`; TCIA only uses strings today, but both shapes
    are accepted so a schema-version bump upstream doesn't silently drop
    every institution.
    """
    names: list[str] = []
    for creator in datacite.get("creators") or []:
        for affiliation in creator.get("affiliation") or []:
            if isinstance(affiliation, dict):
                name = (affiliation.get("name") or "").strip()
            else:
                name = str(affiliation).strip()
            if name and name not in names:
                names.append(name)
    return [Institution(name=name) for name in names]


def _papers(datacite: dict) -> list[Paper]:
    """Related DOIs, deduped, capped at `_MAX_PAPERS`.

    The cap is not cosmetic: TCIA's most-cited collections carry hundreds
    of `IsCitedBy` DOIs, which would dwarf the rest of the record.
    """
    papers: list[Paper] = []
    seen: set[str] = set()
    for related in datacite.get("relatedIdentifiers") or []:
        if (related.get("relatedIdentifierType") or "").strip().upper() != "DOI":
            continue
        doi = common.clean_doi(related.get("relatedIdentifier"))
        if not doi or doi in seen:
            continue
        seen.add(doi)
        relation = (related.get("relationType") or "").strip().lower()
        papers.append(Paper(doi=doi, relation=_PAPER_RELATIONS.get(relation, "other")))
        if len(papers) == _MAX_PAPERS:
            break
    return papers


def _domains(name: str) -> list[str]:
    """`["oncology"]`, adjusted by the title patterns in
    `_DOMAIN_OVERRIDES`, in vocabulary order."""
    domains = {"oncology"}
    for pattern, effect in _DOMAIN_OVERRIDES:
        if not pattern.search(name):
            continue
        if effect == "covid":
            domains = {"infectious_disease", "pulmonology"}
        elif effect == "not_oncology":
            domains.discard("oncology")
        else:
            domains.add(effect)
    return [domain for domain in vocab.DOMAINS if domain in domains]


def _conditions(datacite: dict) -> list[Condition]:
    title = (main_title(datacite) or "").lower()
    labels: list[str] = []
    for phrase in _TITLE_CONDITIONS:
        if phrase in title:
            label = vocab.CONDITION_ALIASES[phrase]
            if label not in labels:
                labels.append(label)
    return [Condition(label=label) for label in labels]


def _dataset_doi(datacite: dict) -> str | None:
    """The collection's own DOI, version-collapsed per `Record.dataset_doi`.

    `collapse_version_doi` is a no-op on every TCIA DOI seen so far (they
    carry no `.vX` suffix); it is applied anyway so the field means the
    same thing across sources.
    """
    doi = common.clean_doi(datacite.get("doi"))
    return common.collapse_version_doi(doi) if doi else None


def _summary(name: str, datacite: dict, modalities: list[str], size: int | None) -> str:
    """The DataCite abstract's first 40 words, or -- for the collections
    DataCite has never heard of -- a template built only from facts NBIA
    reported."""
    description = _description(datacite)
    if description:
        return io.first_words(description, 40)

    facts = []
    if modalities:
        facts.append(", ".join(modalities))
    if size is not None:
        facts.append(f"{size} subjects")
    detail = f" ({'; '.join(facts)})" if facts else ""
    return io.first_words(
        f"{name}: imaging collection on The Cancer Imaging Archive{detail}.", 40
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize(envelope: dict, *, harvested_at: str, first_seen: str) -> Record:
    """One TCIA envelope -> one canonical `Record`.

    Never returns `Excluded`: every collection TCIA publishes belongs in
    the catalog, including the animal and phantom ones (`species` says
    which). A record is flagged `needs_review` rather than dropped when
    the join left it thin -- no DataCite match (so no title, license or
    DOI), NBIA never listed it (so the access tier is inferred from
    licence wording), or a per-collection NBIA request failed during the
    harvest.
    """
    nbia, datacite = _halves(envelope)
    native_id = envelope["native_id"]

    name = _record_name(envelope, nbia, datacite)
    modalities, keywords = _modalities_and_keywords(nbia)
    sample_size = _sample_size(nbia)

    complete = bool(nbia) and bool(datacite) and not nbia.get("errors")
    access = _access(nbia, datacite)

    return Record(
        id=f"{SOURCE}:{io.slugify(native_id)}",
        source=SOURCE,
        source_native_id=native_id,
        name=name,
        summary=_summary(name, datacite, modalities, sample_size),
        url=datacite.get("url") or COLLECTIONS_URL,
        dataset_doi=_dataset_doi(datacite),
        version=str(datacite["version"]) if datacite.get("version") else None,
        # DataCite reports only a `publicationYear`, which is when the DOI
        # was minted, not when the data was collected -- neither `published`
        # nor `years` can be filled without inventing a date.
        published=None,
        years=Years(),
        domains=_domains(name),
        modalities=modalities,
        conditions=_conditions(datacite),
        keywords=keywords,
        species=_species(nbia),
        sample_size=sample_size,
        sample_unit="participants" if sample_size is not None else None,
        countries=[],
        access=access,
        access_tiers=[access],
        license=_license(datacite),
        institutions=_institutions(datacite),
        authors=_authors(datacite),
        papers=_papers(datacite),
        record_status="active" if complete else "needs_review",
        provenance=common.make_provenance(
            via=HARVEST_METHOD,
            harvested_at=harvested_at,
            first_seen=first_seen,
            raw_hash=io.content_hash(strip_volatile(envelope.get("payload") or {})),
        ),
    )


def enrichment_text(envelope: dict) -> str:
    """Free text for the enrich stage: everything in the envelope that
    describes what the collection *is*, HTML-stripped, capped at 1,500
    characters."""
    nbia, datacite = _halves(envelope)
    parts = [
        _record_name(envelope, nbia, datacite),
        main_title(datacite) or "",
        _description(datacite),
        " ".join(nbia.get("body_parts") or []),
        " ".join(nbia.get("modalities") or []),
    ]

    # `name` *is* the DataCite title whenever there is one, so emitting
    # both would spend budget repeating it and bias any downstream
    # term-frequency scoring.
    seen: set[str] = set()
    unique = [part for part in parts if part and not (part in seen or seen.add(part))]
    return "\n".join(unique)[:1500]
