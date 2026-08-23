"""Tests for the TCIA harvester (NBIA v1 + DataCite) and the DataCite client.

Everything here is offline: `atlas.http.get_json` is monkeypatched to serve
`tests/fixtures/tcia/*.json` by URL prefix, so no test touches the network
(see `tests/conftest.py`'s socket guard). Live checks live in
`tests/test_live_tcia.py` behind the `live` marker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas import io
from atlas.harvest import datacite, tcia
from atlas.harvest.base import RawStore

FIXTURES = Path(__file__).parent / "fixtures" / "tcia"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeHTTP:
    """Serves the TCIA fixtures by URL, recording every call.

    `fail` maps an ``(endpoint, collection)`` pair to the exception to raise
    (or `None` to return `None`, i.e. `atlas.http.get_json`'s own
    "every attempt failed" signal), so a test can break exactly one
    per-collection request.
    """

    def __init__(self, *, fail: dict | None = None, datacite_pages: list | None = None):
        self.fail = fail or {}
        self.datacite_pages = datacite_pages
        self.calls: list[str] = []

    def __call__(self, url, *, params=None, **_kwargs):
        self.calls.append(
            url if params is None else f"{url}?{io.canonical_json(params)}"
        )

        if url.startswith(datacite.API):
            if self.datacite_pages is not None:
                return self.datacite_pages.pop(0)
            return fixture("datacite_page.json")

        if not url.startswith(tcia.NBIA_BASE):
            raise AssertionError(f"unexpected URL: {url}")

        endpoint = url.rsplit("/", 1)[-1]
        collection = (params or {}).get("Collection")
        if (endpoint, collection) in self.fail:
            exc = self.fail[(endpoint, collection)]
            if exc is None:
                return None
            raise exc

        if endpoint == "getCollectionValues":
            return fixture("collections.json")
        slug = io.slugify(collection)
        names = {
            "getModalityValues": f"modalities_{slug}.json",
            "getBodyPartValues": f"bodyparts_{slug}.json",
            "getPatient": f"patients_{slug}.json",
        }
        return fixture(names[endpoint])


@pytest.fixture
def http_fixture(monkeypatch):
    """Install a `FakeHTTP` on `atlas.http.get_json` and hand it back."""

    def install(**kwargs):
        fake = FakeHTTP(**kwargs)
        monkeypatch.setattr("atlas.http.get_json", fake)
        return fake

    return install


COLLECTIONS = [
    "4D-Lung",
    "A091105",
    "ACNS0332",
    "ACRIN-6698",
    "ACRIN-Contralateral-Breast-MR",
]
# DataCite `/collection/` DOIs in the fixture page that no fixture
# collection matches -- gated/limited-access collections.
GATED = ["c-nmc-2019", "tcga-gbm", "vestibular-schwannoma-mc-rc2"]


# ---------------------------------------------------------------------------
# datacite.list_by_prefix
# ---------------------------------------------------------------------------


def _page(dois: list[str], *, total: int, next_url: str | None = None) -> dict:
    links = {"self": "https://api.datacite.org/dois?prefix=10.7937"}
    if next_url:
        links["next"] = next_url
    return {
        "data": [{"id": doi, "attributes": {"doi": doi}} for doi in dois],
        "meta": {"total": total},
        "links": links,
    }


def test_list_by_prefix_requests_a_cursor_paged_url(monkeypatch):
    seen: list[str] = []

    def fake(url, **_kwargs):
        seen.append(url)
        return _page(["10.7937/a"], total=1)

    monkeypatch.setattr("atlas.http.get_json", fake)
    datacite.list_by_prefix("10.7937", page_size=25)

    assert seen == [
        (
            "https://api.datacite.org/dois"
            "?prefix=10.7937&page%5Bsize%5D=25&page%5Bcursor%5D=1"
        )
    ]


def test_list_by_prefix_follows_links_next_until_exhausted(monkeypatch):
    pages = [
        _page(["10.7937/a", "10.7937/b"], total=3, next_url="https://api/next-1"),
        _page(["10.7937/c"], total=3),
    ]
    seen: list[str] = []

    def fake(url, **_kwargs):
        seen.append(url)
        return pages.pop(0)

    monkeypatch.setattr("atlas.http.get_json", fake)
    records = datacite.list_by_prefix("10.7937")

    assert [r["attributes"]["doi"] for r in records] == [
        "10.7937/a",
        "10.7937/b",
        "10.7937/c",
    ]
    assert seen[1] == "https://api/next-1"


def test_list_by_prefix_rejects_duplicate_dois(monkeypatch):
    pages = [
        _page(["10.7937/a"], total=2, next_url="https://api/next-1"),
        _page(["10.7937/a"], total=2),
    ]
    monkeypatch.setattr("atlas.http.get_json", lambda url, **_k: pages.pop(0))

    with pytest.raises(AssertionError, match="duplicate DOI"):
        datacite.list_by_prefix("10.7937")


def test_list_by_prefix_rejects_a_count_that_disagrees_with_meta_total(monkeypatch):
    monkeypatch.setattr(
        "atlas.http.get_json", lambda url, **_k: _page(["10.7937/a"], total=9)
    )

    with pytest.raises(AssertionError, match=r"9.*1|1.*9"):
        datacite.list_by_prefix("10.7937")


def test_list_by_prefix_raises_when_a_page_request_fails(monkeypatch):
    monkeypatch.setattr("atlas.http.get_json", lambda url, **_k: None)

    with pytest.raises(RuntimeError, match="DataCite"):
        datacite.list_by_prefix("10.7937")


def test_list_by_prefix_over_the_real_fixture_page(monkeypatch):
    monkeypatch.setattr(
        "atlas.http.get_json", lambda url, **_k: fixture("datacite_page.json")
    )
    records = datacite.list_by_prefix("10.7937")

    assert len(records) == 8
    assert all("attributes" in r for r in records)


# ---------------------------------------------------------------------------
# NBIA <-> DataCite matching
# ---------------------------------------------------------------------------


def _dc(
    doi: str,
    *,
    title: str = "Some collection",
    alt: str | None = None,
    url: str = "https://www.cancerimagingarchive.net/collection/some-collection/",
    year: int = 2020,
) -> dict:
    titles = [{"title": title, "titleType": None}]
    if alt is not None:
        titles.append({"title": alt, "titleType": "AlternativeTitle"})
    return {
        "attributes": {
            "doi": doi,
            "titles": titles,
            "url": url,
            "publicationYear": year,
        }
    }


def test_match_by_alternative_title():
    record = _dc("10.7937/alt", title="Data from Something", alt="My-Collection")
    matches = tcia.match_collections(["My-Collection"], [record])
    assert matches["My-Collection"]["attributes"]["doi"] == "10.7937/alt"


def test_match_by_parenthesised_suffix_in_the_main_title():
    record = _dc("10.7937/paren", title="A long descriptive title (My-Collection)")
    matches = tcia.match_collections(["My-Collection"], [record])
    assert matches["My-Collection"]["attributes"]["doi"] == "10.7937/paren"


def test_match_by_url_slug():
    record = _dc(
        "10.7937/slug",
        title="Unrelated title",
        url="https://www.cancerimagingarchive.net/collection/my-collection/",
    )
    matches = tcia.match_collections(["My Collection"], [record])
    assert matches["My Collection"]["attributes"]["doi"] == "10.7937/slug"


def test_no_match_leaves_the_collection_out():
    record = _dc("10.7937/other", title="Nothing to do with it")
    assert tcia.match_collections(["My-Collection"], [record]) == {}


def test_tie_break_prefers_a_collection_url_over_another_url_kind():
    analysis = _dc(
        "10.7937/analysis",
        alt="My-Collection",
        url="https://www.cancerimagingarchive.net/analysis-result/my-collection/",
        year=2024,
    )
    collection = _dc(
        "10.7937/collection",
        alt="My-Collection",
        url="https://www.cancerimagingarchive.net/collection/my-collection/",
        year=2015,
    )
    matches = tcia.match_collections(["My-Collection"], [analysis, collection])
    assert matches["My-Collection"]["attributes"]["doi"] == "10.7937/collection"


def test_tie_break_prefers_the_latest_publication_year():
    old = _dc("10.7937/old", alt="My-Collection", year=2014)
    new = _dc("10.7937/new", alt="My-Collection", year=2021)
    matches = tcia.match_collections(["My-Collection"], [old, new])
    assert matches["My-Collection"]["attributes"]["doi"] == "10.7937/new"


def test_tie_break_falls_back_to_the_lowest_doi_for_determinism():
    first = _dc("10.7937/aaa", alt="My-Collection", year=2015)
    second = _dc("10.7937/bbb", alt="My-Collection", year=2015)
    assert (
        tcia.match_collections(["My-Collection"], [second, first])["My-Collection"][
            "attributes"
        ]["doi"]
        == "10.7937/aaa"
    )


def test_match_over_the_real_fixtures():
    records = fixture("datacite_page.json")["data"]
    matches = tcia.match_collections(COLLECTIONS, records)

    assert {name: rec["attributes"]["doi"] for name, rec in matches.items()} == {
        "4D-Lung": "10.7937/k9/tcia.2016.eln8ygle",
        "A091105": "10.7937/0wf5-sj50",
        "ACNS0332": "10.7937/tcia.582b-xz89",
        "ACRIN-6698": "10.7937/tcia.kk02-6d95",
        "ACRIN-Contralateral-Breast-MR": "10.7937/q1ee-j082",
    }


def test_gated_records_are_collection_urls_that_no_collection_claimed():
    records = fixture("datacite_page.json")["data"]
    gated = tcia.gated_records(COLLECTIONS, records)
    assert sorted(gated) == GATED


def test_gating_must_be_decided_against_the_full_collection_listing():
    """Why `harvest` passes every collection name, not the `limit`-
    truncated selection: a truncated listing turns real collections into
    phantom "not in NBIA" gated records."""
    records = fixture("datacite_page.json")["data"]
    assert "acrin-6698" in tcia.gated_records(COLLECTIONS[:2], records)
    assert "acrin-6698" not in tcia.gated_records(COLLECTIONS, records)


def test_gated_records_exclude_non_collection_urls():
    analysis = _dc(
        "10.7937/analysis",
        url="https://www.cancerimagingarchive.net/analysis-result/lonely/",
    )
    assert tcia.gated_records([], [analysis]) == {}


def test_gated_records_exclude_a_doi_that_lost_a_match_tie_break():
    """A runner-up for a collection is *that collection's* DOI, not a
    separate gated collection -- it must not become a second record."""
    winner = _dc("10.7937/aaa", alt="My-Collection", year=2021)
    loser = _dc("10.7937/bbb", alt="My-Collection", year=2015)
    assert tcia.gated_records(["My-Collection"], [winner, loser]) == {}


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


def test_probe_returns_the_collection_list(http_fixture, tmp_path):
    fake = http_fixture()
    harvester = tcia.TciaHarvester(store=RawStore("tcia", root=tmp_path))

    body = harvester.probe()

    assert [entry["Collection"] for entry in body] == COLLECTIONS
    assert len(fake.calls) == 1


def test_probe_rejects_an_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr("atlas.http.get_json", lambda url, **_k: [])
    harvester = tcia.TciaHarvester(store=RawStore("tcia", root=tmp_path))

    with pytest.raises(AssertionError, match="getCollectionValues"):
        harvester.probe()


def test_probe_rejects_entries_without_a_collection_name(monkeypatch, tmp_path):
    monkeypatch.setattr("atlas.http.get_json", lambda url, **_k: [{"Nope": 1}])
    harvester = tcia.TciaHarvester(store=RawStore("tcia", root=tmp_path))

    with pytest.raises(AssertionError, match="Collection"):
        harvester.probe()


# ---------------------------------------------------------------------------
# harvest()
# ---------------------------------------------------------------------------


def test_harvest_writes_one_envelope_per_collection_plus_the_gated_ones(
    http_fixture, tmp_path
):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    result = tcia.TciaHarvester(store=store).harvest()

    assert result.status == "ok"
    assert result.listed == len(COLLECTIONS) + len(GATED)
    assert result.written == len(COLLECTIONS) + len(GATED)
    assert sorted(store.load_all()) == sorted(COLLECTIONS + GATED)


def test_harvest_envelope_shape_for_a_matched_collection(http_fixture, tmp_path):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest()

    envelope = store.load("4D-Lung")
    assert envelope["source"] == "tcia"
    assert envelope["native_id"] == "4D-Lung"
    assert envelope["harvest_method"] == "api:nbia+datacite"
    assert envelope["payload"]["nbia"] == {
        "collection": "4D-Lung",
        "modalities": ["CT", "RTSTRUCT"],
        "body_parts": ["LUNG"],
        "patients": envelope["payload"]["nbia"]["patients"],
    }
    assert len(envelope["payload"]["nbia"]["patients"]) == 5
    assert envelope["payload"]["datacite"]["doi"] == "10.7937/k9/tcia.2016.eln8ygle"
    # the DataCite `attributes` dict only -- never `relationships`
    assert "relationships" not in envelope["payload"]["datacite"]


def test_harvest_drops_body_part_entries_without_a_name(http_fixture, tmp_path):
    """`getBodyPartValues` returns bare `{}` entries for series with no
    BodyPartExamined; they carry no information and are dropped."""
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest()

    body_parts = store.load("ACNS0332")["payload"]["nbia"]["body_parts"]
    assert body_parts == ["BRAIN", "HEADNECK", "SPINE"]


def test_harvest_envelope_shape_for_a_gated_datacite_only_record(
    http_fixture, tmp_path
):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest()

    envelope = store.load("tcga-gbm")
    assert envelope["native_id"] == "tcga-gbm"
    assert envelope["payload"]["nbia"] is None
    assert envelope["payload"]["datacite"]["doi"] == "10.7937/k9/tcia.2016.rnyfuye9"


def test_harvest_manifest_records_every_native_id(http_fixture, tmp_path):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest()

    manifest = json.loads((tmp_path / "tcia" / "manifest.json").read_text())
    assert manifest["source"] == "tcia"
    assert manifest["status"] == "ok"
    assert sorted(manifest["records"]) == sorted(COLLECTIONS + GATED)
    assert manifest["endpoints"]


def test_harvest_ignores_datacite_volatile_fields_when_detecting_change(
    http_fixture, tmp_path
):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest()

    page = fixture("datacite_page.json")
    for record in page["data"]:
        record["attributes"]["viewCount"] = 999_999
        record["attributes"]["updated"] = "2099-01-01T00:00:00Z"
    http_fixture(datacite_pages=[page])

    result = tcia.TciaHarvester(store=store).harvest()
    assert result.written == 0
    assert result.unchanged == len(COLLECTIONS) + len(GATED)


def test_harvest_limit_caps_the_number_of_records(http_fixture, tmp_path):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    result = tcia.TciaHarvester(store=store).harvest(limit=2)

    assert result.listed == 2
    assert sorted(store.load_all()) == ["4D-Lung", "A091105"]


def test_harvest_limit_spends_what_is_left_of_it_on_gated_records(
    http_fixture, tmp_path
):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest(limit=6)

    assert sorted(store.load_all()) == sorted([*COLLECTIONS, GATED[0]])


def test_harvest_survives_a_failed_per_collection_request(http_fixture, tmp_path):
    http_fixture(fail={("getPatient", "A091105"): None})
    store = RawStore("tcia", root=tmp_path)
    result = tcia.TciaHarvester(store=store).harvest()

    assert result.status == "ok"
    assert result.listed == len(COLLECTIONS) + len(GATED)

    broken = store.load("A091105")["payload"]["nbia"]
    assert "patients" not in broken
    assert broken["modalities"] == ["RTSTRUCT"]
    assert any("getPatient" in err for err in broken["errors"])

    # every other collection is untouched
    assert "errors" not in store.load("4D-Lung")["payload"]["nbia"]


def test_harvest_survives_an_exception_from_a_per_collection_request(
    http_fixture, tmp_path
):
    http_fixture(fail={("getModalityValues", "ACNS0332"): TimeoutError("slow")})
    store = RawStore("tcia", root=tmp_path)
    result = tcia.TciaHarvester(store=store).harvest()

    assert result.status == "ok"
    broken = store.load("ACNS0332")["payload"]["nbia"]
    assert "modalities" not in broken
    assert broken["body_parts"] and broken["patients"]
    assert any("TimeoutError" in err or "slow" in err for err in broken["errors"])


def test_harvest_fails_cleanly_when_the_collection_listing_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("atlas.http.get_json", lambda url, **_k: None)
    store = RawStore("tcia", root=tmp_path)

    result = tcia.TciaHarvester(store=store).harvest()

    assert result.status == "failed"
    assert result.error
    manifest = json.loads((tmp_path / "tcia" / "manifest.json").read_text())
    assert manifest["status"] == "failed"


def test_harvest_fast_reuses_cached_nbia_detail(http_fixture, tmp_path):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest()

    fake = http_fixture()
    result = tcia.TciaHarvester(store=RawStore("tcia", root=tmp_path)).harvest(
        fast=True
    )

    assert result.status == "ok"
    nbia_calls = [c for c in fake.calls if tcia.NBIA_BASE in c]
    # only the collection listing -- no per-collection detail requests
    assert len(nbia_calls) == 1
    assert store.load("4D-Lung")["payload"]["nbia"]["modalities"] == ["CT", "RTSTRUCT"]


def test_harvest_fast_still_fetches_a_collection_that_is_not_cached(
    http_fixture, tmp_path
):
    fake = http_fixture()
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest(fast=True)

    assert sum(1 for c in fake.calls if "getPatient" in c) == len(COLLECTIONS)


def test_harvester_is_discovered_by_the_registry():
    from atlas.harvest import get_registry

    assert get_registry()["tcia"] is tcia.TciaHarvester


# ---------------------------------------------------------------------------
# datacite paging guards (review fix round 1)
# ---------------------------------------------------------------------------


def test_list_by_prefix_stops_on_an_empty_page_that_still_links_next(monkeypatch):
    """DataCite's cursor can walk past the end and keep handing out a
    `links.next`; without the empty-page break that spins to the cap."""
    pages = [
        _page(["10.7937/a"], total=1, next_url="https://api/next-1"),
        _page([], total=1, next_url="https://api/next-2"),
    ]
    monkeypatch.setattr("atlas.http.get_json", lambda url, **_k: pages.pop(0))

    records = datacite.list_by_prefix("10.7937")

    assert [r["attributes"]["doi"] for r in records] == ["10.7937/a"]
    assert pages == []  # the second page was fetched, a third never was


def test_list_by_prefix_refuses_to_page_past_the_cap(monkeypatch):
    counter = iter(range(10_000))

    def endless(url, **_kwargs):
        n = next(counter)
        return _page([f"10.7937/{n}"], total=10_000, next_url=f"https://api/p{n}")

    monkeypatch.setattr("atlas.http.get_json", endless)

    with pytest.raises(AssertionError, match=f"after {datacite.MAX_PAGES} pages"):
        datacite.list_by_prefix("10.7937")


def test_list_by_prefix_does_not_mistake_two_doi_less_records_for_duplicates(
    monkeypatch,
):
    page = {
        "data": [{"id": "10.7937/a", "attributes": {}}, {"attributes": {}}, {}],
        "meta": {"total": 3},
        "links": {},
    }
    monkeypatch.setattr("atlas.http.get_json", lambda url, **_k: page)

    assert len(datacite.list_by_prefix("10.7937")) == 3


# ---------------------------------------------------------------------------
# url parsing, join sharing, ambiguous claims (review fix round 1)
# ---------------------------------------------------------------------------


def test_url_parts_ignores_query_and_fragment():
    attributes = {
        "url": "https://www.cancerimagingarchive.net/collection/4d-lung/?utm=x#top"
    }
    assert tcia.url_parts(attributes) == ("collection", "4d-lung")


def test_url_parts_is_empty_without_a_url():
    assert tcia.url_parts({}) == ("", "")


def test_match_collections_rejects_a_doi_claimed_by_two_collections():
    shared = _dc(
        "10.7937/shared",
        title="Something (Collection-B)",
        alt="Collection-A",
        url="https://www.cancerimagingarchive.net/collection/shared/",
    )
    with pytest.raises(AssertionError, match="matched both collection"):
        tcia.match_collections(["Collection-A", "Collection-B"], [shared])


def test_join_returns_the_same_pair_as_the_two_halves():
    records = fixture("datacite_page.json")["data"]
    matched, gated = tcia.join(COLLECTIONS, records)

    assert matched == tcia.match_collections(COLLECTIONS, records)
    assert gated == tcia.gated_records(COLLECTIONS, records)


# ---------------------------------------------------------------------------
# harvest ordering, self-healing fast, partial-failure counts (fix round 1)
# ---------------------------------------------------------------------------


def test_harvest_fetches_datacite_before_the_nbia_detail_loop(http_fixture, tmp_path):
    fake = http_fixture()
    tcia.TciaHarvester(store=RawStore("tcia", root=tmp_path)).harvest()

    datacite_at = next(
        i for i, c in enumerate(fake.calls) if c.startswith(datacite.API)
    )
    first_detail_at = next(
        i for i, c in enumerate(fake.calls) if "getModalityValues" in c
    )
    assert datacite_at < first_detail_at


def test_harvest_fails_before_spending_the_nbia_detail_requests(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake(url, *, params=None, **_kwargs):
        calls.append(url)
        if url.startswith(datacite.API):
            return None  # DataCite down
        return fixture("collections.json")

    monkeypatch.setattr("atlas.http.get_json", fake)
    result = tcia.TciaHarvester(store=RawStore("tcia", root=tmp_path)).harvest()

    assert result.status == "failed"
    assert not [c for c in calls if "getPatient" in c or "getModalityValues" in c]


def test_harvest_fast_refetches_a_collection_whose_cached_detail_had_errors(
    http_fixture, tmp_path
):
    http_fixture(fail={("getPatient", "A091105"): None})
    store = RawStore("tcia", root=tmp_path)
    tcia.TciaHarvester(store=store).harvest()
    assert "errors" in store.load("A091105")["payload"]["nbia"]

    fake = http_fixture()
    tcia.TciaHarvester(store=RawStore("tcia", root=tmp_path)).harvest(fast=True)

    # only the collection that was broken is re-fetched; the healthy four
    # are still served from cache
    assert sum(1 for c in fake.calls if "getPatient" in c) == 1
    healed = store.load("A091105")["payload"]["nbia"]
    assert "errors" not in healed
    assert len(healed["patients"]) == 5


def test_failed_harvest_reports_what_it_managed_to_write(http_fixture, tmp_path):
    http_fixture()
    store = RawStore("tcia", root=tmp_path)
    real_write = store.write
    written = 0

    def exploding_write(*args, **kwargs):
        nonlocal written
        if written == 3:
            raise OSError("disk full")
        written += 1
        return real_write(*args, **kwargs)

    store.write = exploding_write
    result = tcia.TciaHarvester(store=store).harvest()

    assert result.status == "failed"
    assert "disk full" in result.error
    assert result.written == 3
    assert result.listed == 3
