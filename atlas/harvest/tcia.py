"""TCIA harvester: the NBIA v1 API joined to DataCite DOI metadata.

The Cancer Imaging Archive splits what one catalog record needs across two
unrelated APIs, and neither is sufficient alone:

- **NBIA v1** (`services.cancerimagingarchive.net`, no auth) knows which
  collections are *publicly downloadable* and, per collection, the imaging
  modalities, body parts and patient list -- but no title, description,
  license, authors or DOI. Its descriptive endpoints
  (`getCollectionDescriptions`, `get*ValuesAndCounts`) answer 500/401 even
  with a guest token, so they are not called at all (see
  `docs/sources/tcia.md`).
- **DataCite** holds TCIA's DOIs under prefix ``10.7937`` -- titles,
  abstracts, `rightsList`, creators, related papers, version -- but its
  landing pages cover more collections than NBIA lists publicly, because
  limited-access/gated collections also get a DOI.

So a harvest is: list collections from NBIA, pull per-collection detail,
list every ``10.7937`` DOI from DataCite, and join the two. The join is
the interesting part -- there is no shared identifier, so
:func:`match_collections` matches on three independently-verified signals
(alternative title, parenthesised title suffix, url slug). Any DataCite
DOI whose landing page is a ``/collection/`` page and that no NBIA
collection claimed is a *gated* collection: it gets a record of its own
with ``nbia: null`` (see :func:`gated_records`).

One envelope per record, payload::

    {"nbia": {"collection", "modalities", "body_parts", "patients"} | null,
     "datacite": <DataCite `attributes` dict> | null}

A collection whose NBIA detail requests fail keeps whatever parts did come
back plus an ``errors`` list, and the run continues -- one flaky endpoint
must not cost a whole harvest.
"""

from __future__ import annotations

import datetime
import re
import time

from atlas import http, io
from atlas.harvest import datacite
from atlas.harvest.base import Harvester, HarvestResult

NBIA_BASE = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
DOI_PREFIX = "10.7937"

# `attributes` fields DataCite recomputes on its own schedule (usage
# counters and the metadata timestamp). They stay in the stored payload --
# they are real API output -- but `RawStore.write` ignores them when
# deciding whether a record changed, so a monthly refresh doesn't rewrite
# all 240 records just because someone viewed one.
VOLATILE = (
    "datacite.updated",
    "datacite.viewCount",
    "datacite.downloadCount",
    "datacite.citationCount",
    "datacite.viewsOverTime",
    "datacite.downloadsOverTime",
    "datacite.citationsOverTime",
)

# Per-collection detail endpoints: endpoint name -> (payload key, the field
# to read out of each returned object). `getPatient` keeps whole objects.
_DETAIL_ENDPOINTS: tuple[tuple[str, str, str | None], ...] = (
    ("getModalityValues", "modalities", "Modality"),
    ("getBodyPartValues", "body_parts", "BodyPartExamined"),
    ("getPatient", "patients", None),
)

# A trailing "(Short-Name)" in a DataCite main title.
_PAREN_SUFFIX_RE = re.compile(r"\(([^()]*)\)\s*$")


# ---------------------------------------------------------------------------
# DataCite record accessors
# ---------------------------------------------------------------------------


def _attributes(record: dict) -> dict:
    return record.get("attributes") or {}


def _doi(record: dict) -> str:
    return _attributes(record).get("doi") or ""


def main_title(attributes: dict) -> str | None:
    """The DataCite title with no `titleType` -- the real one. Alternative
    titles (short names, acronyms) all carry a `titleType`."""
    for title in attributes.get("titles") or []:
        if not title.get("titleType") and title.get("title"):
            return title["title"]
    return None


def _alt_titles(attributes: dict) -> list[str]:
    return [
        title["title"]
        for title in attributes.get("titles") or []
        if title.get("titleType") and title.get("title")
    ]


def url_parts(attributes: dict) -> tuple[str, str]:
    """``(kind, slug)`` of a DataCite landing-page url, e.g.
    ``https://www.cancerimagingarchive.net/collection/4d-lung/`` ->
    ``("collection", "4d-lung")``. ``("", "")`` when there is no url."""
    url = (attributes.get("url") or "").rstrip("/")
    if not url:
        return "", ""
    segments = url.split("/")
    kind = segments[-2] if len(segments) >= 2 else ""
    return kind, segments[-1]


def _publication_year(attributes: dict) -> int:
    try:
        return int(attributes.get("publicationYear") or 0)
    except (TypeError, ValueError):
        return 0


def _preference(record: dict) -> tuple[int, int, str]:
    """Sort key picking the best of several DataCite records for the same
    collection: a ``/collection/`` landing page first, then the most
    recent `publicationYear`, then the lowest DOI. That last term is not
    in the source's own semantics -- it's there so two otherwise-equal
    candidates always resolve the same way and a refresh doesn't flip a
    record's DOI back and forth between runs."""
    attributes = _attributes(record)
    kind, _ = url_parts(attributes)
    return (
        0 if kind == "collection" else 1,
        -_publication_year(attributes),
        _doi(record),
    )


# ---------------------------------------------------------------------------
# NBIA <-> DataCite matching
# ---------------------------------------------------------------------------


def _candidates(collections: list[str], records: list[dict]) -> dict[str, list[dict]]:
    """Every DataCite record that plausibly describes each collection.

    Three signals, any one of which is enough (all three verified against
    the live APIs on 2026-08-22, jointly covering 151 of 156 public
    collections):

    a. an alternative title equal to the collection name,
    b. a parenthesised suffix in the main title equal to the name,
    c. the landing-page url slug equal to ``io.slugify(name)``.

    (a) and (b) are exact, case-sensitive comparisons on purpose -- TCIA's
    short names are the collection names verbatim, and loosening this
    would start matching sibling collections that differ only in case or
    punctuation (e.g. `Vestibular-Schwannoma-MC-RC` vs `-MC-RC 2`).
    """
    by_alt_title: dict[str, list[dict]] = {}
    by_paren: dict[str, list[dict]] = {}
    by_slug: dict[str, list[dict]] = {}

    for record in records:
        attributes = _attributes(record)
        for alt in _alt_titles(attributes):
            by_alt_title.setdefault(alt, []).append(record)
        match = _PAREN_SUFFIX_RE.search(main_title(attributes) or "")
        if match:
            by_paren.setdefault(match.group(1).strip(), []).append(record)
        _, slug = url_parts(attributes)
        if slug:
            by_slug.setdefault(slug, []).append(record)

    candidates: dict[str, list[dict]] = {}
    for name in collections:
        found: dict[str, dict] = {}
        for record in (
            *by_alt_title.get(name, []),
            *by_paren.get(name, []),
            *by_slug.get(io.slugify(name), []),
        ):
            found.setdefault(_doi(record), record)
        if found:
            candidates[name] = list(found.values())
    return candidates


def match_collections(collections: list[str], records: list[dict]) -> dict[str, dict]:
    """``{collection name: DataCite record}`` for every collection that
    matched at least one record, best candidate only (:func:`_preference`).
    Collections with no candidate are simply absent."""
    return {
        name: min(found, key=_preference)
        for name, found in _candidates(collections, records).items()
    }


def gated_records(collections: list[str], records: list[dict]) -> dict[str, dict]:
    """``{url slug: DataCite record}`` for the ``/collection/`` DOIs that
    describe a collection NBIA does not list publicly -- TCIA's gated and
    limited-access collections.

    A record that *was* a candidate for some collection is excluded even
    when it lost the tie-break: it describes that collection, so treating
    it as a separate one would duplicate the collection under a second id.
    Two gated records that somehow share a url slug collapse to the
    :func:`_preference`-preferred one, since the slug is the record's
    native id and two records can't claim the same one.
    """
    claimed = {
        _doi(record)
        for found in _candidates(collections, records).values()
        for record in found
    }

    gated: dict[str, dict] = {}
    for record in records:
        kind, slug = url_parts(_attributes(record))
        if kind != "collection" or not slug or _doi(record) in claimed:
            continue
        incumbent = gated.get(slug)
        if incumbent is None or _preference(record) < _preference(incumbent):
            gated[slug] = record
    return gated


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------


class TciaHarvester(Harvester):
    """Harvest every public TCIA collection, plus the gated ones DataCite
    knows about."""

    name = "tcia"
    harvest_method = "api:nbia+datacite"

    # -- probe ------------------------------------------------------------

    def probe(self) -> dict:
        body = http.get_json(f"{NBIA_BASE}/getCollectionValues")
        if not isinstance(body, list) or not body:
            raise AssertionError(
                f"getCollectionValues returned {type(body).__name__} "
                f"{str(body)[:60]!r}, expected a non-empty list"
            )
        first = body[0]
        name = first.get("Collection") if isinstance(first, dict) else None
        if not isinstance(name, str) or not name:
            raise AssertionError(
                f"getCollectionValues entries lost their 'Collection' string: {first!r}"
            )
        return body

    # -- harvest ----------------------------------------------------------

    def harvest(self, *, fast: bool = False, limit: int | None = None) -> HarvestResult:
        started = time.monotonic()
        # Local calendar date, per the pipeline's `harvested_at` convention.
        harvested_at = datetime.datetime.now().astimezone().date().isoformat()
        endpoints = [
            f"{NBIA_BASE}/getCollectionValues",
            *(f"{NBIA_BASE}/{endpoint}" for endpoint, _, _ in _DETAIL_ENDPOINTS),
            f"{datacite.API}?prefix={DOI_PREFIX}",
        ]
        listed: set[str] = set()

        try:
            all_names = [entry["Collection"] for entry in self.probe()]
            selected = all_names if limit is None else all_names[:limit]
            cached_ids = self.store.existing()

            details = {
                name: self._nbia_detail(name, fast=fast, cached_ids=cached_ids)
                for name in selected
            }

            records = datacite.list_by_prefix(DOI_PREFIX)
            # Gating is decided against the *full* listing, never the
            # `limit`-truncated one -- otherwise a short dev run would
            # invent "not in NBIA" records for collections it merely
            # skipped.
            matched = match_collections(all_names, records)
            gated = gated_records(all_names, records)

            for name in selected:
                record = matched.get(name)
                attributes = _attributes(record) if record else {}
                payload = {
                    "nbia": details[name],
                    "datacite": attributes or None,
                }
                self._write(name, payload, endpoints=endpoints)
                listed.add(name)

            # `limit` caps records, not collections: a truncated run
            # should stay small rather than pull in ~90 gated records.
            budget = None if limit is None else max(0, limit - len(listed))
            for slug in sorted(gated)[:budget]:
                payload = {"nbia": None, "datacite": _attributes(gated[slug])}
                self._write(slug, payload, endpoints=endpoints[-1:])
                listed.add(slug)

            manifest = self.store.finalize(
                listed_ids=listed,
                harvested_at=harvested_at,
                endpoints=endpoints,
                status="ok",
            )
        except Exception as exc:  # noqa: BLE001 -- harvest() never raises
            error = f"{type(exc).__name__}: {exc}"
            manifest = self.store.finalize(
                listed_ids=listed,
                harvested_at=harvested_at,
                endpoints=endpoints,
                status="failed",
                error=error,
            )
            return HarvestResult(
                source=self.name,
                status="failed",
                seconds=round(time.monotonic() - started, 3),
                error=error,
            )

        counts = manifest["counts"]
        return HarvestResult(
            source=self.name,
            status=manifest["status"],
            listed=counts["listed"],
            written=counts["written"],
            unchanged=counts["unchanged"],
            removed=counts["removed"],
            seconds=round(time.monotonic() - started, 3),
            error=manifest["error"],
        )

    # -- internals --------------------------------------------------------

    def _write(self, native_id: str, payload: dict, *, endpoints: list[str]) -> None:
        self.store.write(
            native_id,
            payload,
            harvest_method=self.harvest_method,
            endpoints=endpoints,
            volatile=VOLATILE,
        )

    def _nbia_detail(self, name: str, *, fast: bool, cached_ids: dict) -> dict:
        """The `nbia` half of one collection's payload.

        With `fast` set and the collection already in the manifest, the
        previous run's detail is reused verbatim instead of spending three
        requests on it -- NBIA's per-collection endpoints are the slow part
        of a harvest and a collection's modality/body-part/patient lists
        change only when TCIA republishes it. A record the manifest claims
        but whose file has gone missing falls through to a live fetch.
        """
        if fast and name in cached_ids:
            try:
                cached = self.store.load(name)["payload"].get("nbia")
            except FileNotFoundError:
                cached = None
            if cached:
                return cached

        detail: dict = {"collection": name}
        errors: list[str] = []
        for endpoint, key, field in _DETAIL_ENDPOINTS:
            try:
                detail[key] = self._nbia_list(endpoint, name, field)
            except Exception as exc:  # noqa: BLE001 -- one endpoint, not the run
                errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
        if errors:
            detail["errors"] = errors
        return detail

    @staticmethod
    def _nbia_list(endpoint: str, collection: str, field: str | None) -> list:
        """One per-collection NBIA list. `field` picks a single key out of
        each returned object (`getModalityValues` -> the modality codes);
        `None` keeps the objects whole (`getPatient`). Objects missing
        `field` are dropped -- `getBodyPartValues` returns a bare `{}` for
        series with no BodyPartExamined, which carries no information."""
        body = http.get_json(
            f"{NBIA_BASE}/{endpoint}", params={"Collection": collection}
        )
        entries = body if isinstance(body, list) else None
        if entries is None:
            raise RuntimeError(
                f"expected a list for collection {collection!r}, "
                f"got {type(body).__name__}"
            )
        if field is None:
            return entries
        return [entry[field] for entry in entries if entry.get(field)]
