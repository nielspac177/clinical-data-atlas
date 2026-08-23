"""Dedupe keys and clustering: the same dataset arrives from several
sources -- a descriptor paper in Scientific Data, the actual repository
entry on OpenNeuro/PhysioNet/GDC/TCIA, sometimes a hand-curated entry too.
Without a shared key across those, the catalog would carry the same
dataset three or four times.

Four keys, two trust levels (ported from the prior `neurodatahub` project's
`scripts/lib/keys.py`, adapted to pull from `Record` fields and to this
project's conventions rather than reimplementing DOI parsing -- the DOI key
here reuses `atlas.normalize.common.clean_doi`/`collapse_version_doi`
instead of its own regex):

- `doi`, `url`, `accession` are *authoritative*: two records sharing any
  one of these are, for certain, the same dataset, and `cluster` merges
  them without hesitation.
- `title` is *indicative only*: a suggestive signal, never enough on its
  own (two unrelated datasets can share a generic title; the same dataset
  can be titled differently by two sources). `cluster` never merges on
  title alone -- `flag_title_collisions` surfaces the suspicious pairs
  instead, for a human (via `record_status = "needs_review"`, set by the
  refresh step, not here) to actually decide.
"""

from __future__ import annotations

import re

from atlas.normalize.common import clean_doi, collapse_version_doi
from atlas.schema import Record, Related

# ---------------------------------------------------------------------------
# doi: version-collapsed, lowercase
# ---------------------------------------------------------------------------


def doi_key(doi: str | None) -> str | None:
    """The dataset-DOI dedupe key: `clean_doi`-normalized (lowercase, a
    `doi:` or doi.org-URL prefix stripped, shape-validated) and then
    version-collapsed -- so an OpenNeuro dataset's `.v1.0.0`-suffixed DOI
    and its bare form, or any other source's per-version vs. concept DOI,
    key identically. `None` when `doi` is falsy or not DOI-shaped.
    """
    cleaned = clean_doi(doi)
    if cleaned is None:
        return None
    return collapse_version_doi(cleaned)


# ---------------------------------------------------------------------------
# url: scheme/www/query/fragment/trailing-slash dropped, host lowercased
# ---------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)


def normalize_url(url: str | None) -> str | None:
    """The URL dedupe key: scheme dropped, a leading `www.` dropped, the
    query string and fragment dropped (portals append tracking parameters
    that would otherwise make an already-known link look new), a trailing
    slash dropped, and the host lowercased.

    Only the host is lowercased, not the whole URL: hostnames are
    case-insensitive but HTTP paths are not, and a source's dataset slug
    in the path should be compared exactly as reported. `None` when `url`
    is falsy or reduces to nothing (e.g. was only a scheme).
    """
    if not url:
        return None
    text = url.strip()
    text = _SCHEME_RE.sub("", text)
    text = text.split("#", 1)[0].split("?", 1)[0]
    text = text.rstrip("/")
    if not text:
        return None

    host, sep, rest = text.partition("/")
    host = _WWW_RE.sub("", host.lower())
    if not host:
        return None
    return host + sep + rest


# ---------------------------------------------------------------------------
# accession: native repository identifier, e.g. "openneuro:ds000001"
# ---------------------------------------------------------------------------

# `ds\d{6}` is OpenNeuro's own accession shape. Guarded on both sides
# (`(?<![A-Za-z])` / `(?!\d)`) so it can't match as a substring of a longer
# word or a longer digit run (e.g. "...seeds000001kg" or "ds0000012").
_OPENNEURO_RE = re.compile(r"(?<![A-Za-z])ds\d{6}(?!\d)", re.IGNORECASE)

# PhysioNet content URLs are `physionet.org/content/<slug>/<version>/...`;
# the slug is the accession, the version is a separate path segment.
_PHYSIONET_RE = re.compile(
    r"physionet\.org/content/([a-z0-9][a-z0-9-]*)", re.IGNORECASE
)

# GDC project ids: one of the program codes GDC actually uses, followed by
# a project-specific, uppercase/digit/hyphen suffix (e.g. "TCGA-BRCA",
# "BEATAML1.0", "CPTAC-3"). Deliberately case-sensitive -- GDC project ids
# are always reported upper-case, and a case-insensitive match here would
# risk pulling a project code out of unrelated prose.
_GDC_RE = re.compile(
    r"(TCGA|TARGET|CPTAC|CGCI|HCMI|MMRF|BEATAML1\.0|CMI|WCDT|OHSU|ORGANOID|TRIO|"
    r"EXCEPTIONAL_RESPONDERS|NCICCR|CTSP|VAREPOP|FM|GENIE|APOLLO|REBC|MATCH|"
    r"ALCHEMIST|CDDP_EAGLE|MP2PRT)[-A-Z0-9]*"
)

# TCIA collection pages are `cancerimagingarchive.net/collection/<slug>`.
_TCIA_RE = re.compile(
    r"cancerimagingarchive\.net/collection/([a-z0-9][a-z0-9-]*)", re.IGNORECASE
)


def accession_key(*texts: str | None) -> str | None:
    """The native-repository accession dedupe key (e.g.
    `"openneuro:ds000001"`) -- the strongest signal available, since it
    survives a portal changing domains or URL schemes entirely. Tries
    each pattern in turn (openneuro, physionet, gdc, tcia) against the
    concatenation of `texts` (falsy entries skipped) and returns the
    first hit; `None` when nothing matches.
    """
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None

    match = _OPENNEURO_RE.search(blob)
    if match:
        return f"openneuro:{match.group(0).lower()}"

    match = _PHYSIONET_RE.search(blob)
    if match:
        return f"physionet:{match.group(1).lower()}"

    match = _GDC_RE.search(blob)
    if match:
        return f"gdc:{match.group(0)}"

    match = _TCIA_RE.search(blob)
    if match:
        return f"tcia:{match.group(1).lower()}"

    return None


# ---------------------------------------------------------------------------
# title: indicative-only fingerprint
# ---------------------------------------------------------------------------

_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Generic filler words that carry no distinguishing signal in a dataset
# title. Deliberately short and English-only -- every Phase 0 source
# reports English titles, so this isn't the multilingual stopword list the
# prior project needed.
_TITLE_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "data",
        "database",
        "dataset",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "with",
    }
)


def title_fingerprint(title: str | None) -> str | None:
    """Indicative-only dedupe key for a dataset title: lowercased,
    punctuation stripped, stopwords dropped, the remaining tokens
    deduplicated and sorted (so word order, repeats, and casing never
    matter) and rejoined with single spaces. `None` when `title` is
    falsy or reduces to nothing but stopwords/punctuation.

    Never used to merge records on its own -- see `cluster` (which uses
    only `doi`/`url`/`accession`) and `flag_title_collisions` (which uses
    this fingerprint to flag, not merge).
    """
    if not title:
        return None
    tokens = {
        token
        for token in _TITLE_TOKEN_RE.findall(title.lower())
        if token not in _TITLE_STOPWORDS
    }
    if not tokens:
        return None
    return " ".join(sorted(tokens))


# ---------------------------------------------------------------------------
# keys(): all four, for one record
# ---------------------------------------------------------------------------


def keys(record: Record) -> dict[str, str | None]:
    """The four dedupe keys for `record`. `doi`/`url`/`accession` are
    authoritative (safe to merge on, see `cluster`); `title` is
    indicative only (see `flag_title_collisions`)."""
    return {
        "doi": doi_key(record.dataset_doi),
        "url": normalize_url(record.url),
        "accession": accession_key(record.url, record.source_native_id, record.name),
        "title": title_fingerprint(record.name),
    }


# ---------------------------------------------------------------------------
# cluster(): union-find over authoritative keys only
# ---------------------------------------------------------------------------

_AUTHORITATIVE_KEYS: tuple[str, ...] = ("doi", "url", "accession")


def cluster(records: list[Record]) -> list[list[Record]]:
    """Group `records` into the same-dataset clusters implied by
    `keys`'s authoritative fields: two records join a cluster the moment
    they share any one `doi`/`url`/`accession` value (title never merges
    records -- see `flag_title_collisions`).

    Deterministic regardless of `records`' input order: a union always
    keeps the lexicographically smaller id as its cluster's root, each
    cluster's members are sorted by id, and the returned list of clusters
    is sorted by its first (i.e. smallest-id) member. Singleton clusters
    -- records that share no authoritative key with anything else -- are
    included, one-element list each.
    """
    parent: dict[str, str] = {record.id: record.id for record in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path halving
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a == root_b:
            return
        if root_b < root_a:
            root_a, root_b = root_b, root_a
        parent[root_b] = root_a

    first_seen: dict[tuple[str, str], str] = {}
    for record in records:
        record_keys = keys(record)
        for key_name in _AUTHORITATIVE_KEYS:
            value = record_keys[key_name]
            if not value:
                continue
            composite = (key_name, value)
            other_id = first_seen.get(composite)
            if other_id is None:
                first_seen[composite] = record.id
            else:
                union(record.id, other_id)

    groups: dict[str, list[Record]] = {}
    for record in records:
        groups.setdefault(find(record.id), []).append(record)

    clusters = list(groups.values())
    for members in clusters:
        members.sort(key=lambda r: r.id)
    clusters.sort(key=lambda members: members[0].id)
    return clusters


# ---------------------------------------------------------------------------
# flag_title_collisions(): indicative-only cross-source flag
# ---------------------------------------------------------------------------


def flag_title_collisions(records: list[Record]) -> set[str]:
    """Ids of records whose title fingerprint (`title_fingerprint`)
    matches another record's from a *different* source. These are never
    merged (title is indicative only, see `cluster`) -- this just
    identifies which records look suspicious enough for a human to
    review; the refresh step is responsible for actually setting
    `record_status = "needs_review"` on them.

    Two records sharing a fingerprint from the *same* source are not
    flagged by this alone -- that's an intra-source concern (e.g. a
    literal duplicate listing), not a cross-source collision.
    """
    by_fingerprint: dict[str, list[Record]] = {}
    for record in records:
        fingerprint = title_fingerprint(record.name)
        if fingerprint:
            by_fingerprint.setdefault(fingerprint, []).append(record)

    flagged: set[str] = set()
    for group in by_fingerprint.values():
        if len({record.source for record in group}) > 1:
            flagged.update(record.id for record in group)
    return flagged


# ---------------------------------------------------------------------------
# link_same_cohort(): GDC <-> TCIA cross-references, never merged
# ---------------------------------------------------------------------------

_GDC_COHORT_RE = re.compile(r"^gdc:(tcga|cptac|target)-(.+)$", re.IGNORECASE)
_TCIA_COHORT_RE = re.compile(r"^tcia:(tcga|cptac|target)-(.+)$", re.IGNORECASE)


def _cohort_key(record: Record) -> tuple[str, str] | None:
    """`(family, cohort)`, both lowercased, for a GDC or TCIA record
    whose id names a TCGA/CPTAC/TARGET cohort -- e.g. both `"gdc:TCGA-GBM"`
    and `"tcia:tcga-gbm"` key as `("tcga", "gbm")`. `None` for anything
    else (a different source, or an id that doesn't match the pattern)."""
    if record.source == "gdc":
        pattern = _GDC_COHORT_RE
    elif record.source == "tcia":
        pattern = _TCIA_COHORT_RE
    else:
        return None

    match = pattern.match(record.id)
    if not match:
        return None
    family, cohort = match.groups()
    return family.lower(), cohort.lower()


def link_same_cohort(records: list[Record]) -> list[Record]:
    """Add a `Related(relation="same_cohort")` link, both ways, between
    every GDC `TCGA-*`/`CPTAC-*`/`TARGET-*` project and every TCIA
    collection naming the same cohort (case-insensitive compare of the
    part after the prefix) -- e.g. `gdc:TCGA-GBM` <-> `tcia:tcga-gbm`.

    These are deliberately *linked*, never merged by `cluster`: a GDC
    project and a TCIA collection for the same patient cohort are
    genuinely different data (genomic vs. imaging), just about the same
    people.

    Returns a new list, same order as `records`; only records that gain
    a link are rebuilt (via `model_copy`), and a link already present
    (by id) is never duplicated.
    """
    gdc_by_cohort: dict[tuple[str, str], list[Record]] = {}
    tcia_by_cohort: dict[tuple[str, str], list[Record]] = {}
    for record in records:
        cohort = _cohort_key(record)
        if cohort is None:
            continue
        bucket = gdc_by_cohort if record.source == "gdc" else tcia_by_cohort
        bucket.setdefault(cohort, []).append(record)

    additions: dict[str, set[str]] = {}
    for cohort, gdc_records in gdc_by_cohort.items():
        for tcia_record in tcia_by_cohort.get(cohort, []):
            for gdc_record in gdc_records:
                additions.setdefault(gdc_record.id, set()).add(tcia_record.id)
                additions.setdefault(tcia_record.id, set()).add(gdc_record.id)

    if not additions:
        return list(records)

    updated: list[Record] = []
    for record in records:
        new_ids = additions.get(record.id)
        if not new_ids:
            updated.append(record)
            continue
        existing_ids = {rel.id for rel in record.related}
        new_related = list(record.related) + [
            Related(id=other_id, relation="same_cohort")
            for other_id in sorted(new_ids)
            if other_id not in existing_ids
        ]
        updated.append(record.model_copy(update={"related": new_related}))
    return updated
