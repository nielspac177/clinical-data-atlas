"""Tests for `atlas.harvest.openneuro.OpenNeuroHarvester` against
monkeypatched `atlas.http.post_json` -- no real network calls (see
`tests/conftest.py`'s socket guard).

`tests/fixtures/openneuro/listing.json` is a real, live-captured GraphQL
page (trimmed to 5 edges) that happens to include two `null` edges -- a
real quirk this harvester must survive: OpenNeuro's GraphQL resolver
occasionally errors out server-side for a handful of dataset nodes per
page (observed "Not Found" resolving `metadata`/`latestSnapshot`), and the
whole edge for that dataset comes back `null` with no id recoverable at
all. See `docs/sources/openneuro.md` for the exact live evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.harvest import openneuro
from atlas.harvest.base import HarvestResult, RawStore

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "openneuro"


def load_listing_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "listing.json").read_text(encoding="utf-8"))


def make_node(native_id: str) -> dict:
    """A minimal-but-realistically-shaped GraphQL dataset node -- the
    harvester only needs `id` out of it; everything else is passed
    through to the raw store verbatim, untouched."""
    return {
        "id": native_id,
        "created": "2026-01-01T00:00:00.000Z",
        "publishDate": "2026-01-02T00:00:00.000Z",
        "metadata": {
            "species": "Human",
            "studyDomain": "",
            "studyDesign": "",
            "studyLongitudinal": "",
            "dataProcessed": None,
            "ages": [],
            "modalities": ["mri"],
            "associatedPaperDOI": "",
            "grantFunderName": "",
            "dxStatus": "",
            "affirmedDefaced": True,
        },
        "latestSnapshot": {
            "tag": "1.0.0",
            "created": "2026-01-02T00:00:00.000Z",
            "description": {
                "Name": f"Dataset {native_id}",
                "Authors": ["Jane Doe"],
                "License": "CC0",
                "DatasetDOI": f"doi:10.18112/openneuro.{native_id}.v1.0.0",
                "Funding": None,
                "ReferencesAndLinks": None,
                "HowToAcknowledge": None,
            },
            "readme": "A short readme.",
            "summary": {
                "subjects": ["01"],
                "sessions": [],
                "modalities": ["mri"],
                "secondaryModalities": [],
                "tasks": [],
                "totalFiles": 1,
                "size": 100,
                "dataProcessed": False,
            },
        },
    }


def page_response(
    node_ids: list[str], *, has_next: bool, end_cursor: str | None
) -> dict:
    return {
        "data": {
            "datasets": {
                "pageInfo": {
                    "hasNextPage": has_next,
                    "endCursor": end_cursor,
                    "count": len(node_ids),
                },
                "edges": [{"node": make_node(nid)} for nid in node_ids],
            }
        },
        "extensions": {},
    }


class RecordingPostJson:
    """A fake `atlas.http.post_json` that serves a fixed sequence of
    responses and records every `(url, payload)` call it received."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, payload, **kwargs):
        self.calls.append((url, payload))
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise AssertionError(f"unexpected extra call #{index + 1}: {payload!r}")
        return self.responses[index]


# ---------------------------------------------------------------------------
# Harvester identity
# ---------------------------------------------------------------------------


def test_harvester_identity():
    assert openneuro.OpenNeuroHarvester.name == "openneuro"
    assert openneuro.OpenNeuroHarvester.harvest_method == "api:openneuro-graphql"


def test_harvester_is_registered_for_auto_discovery():
    from atlas.harvest import REGISTRY, get_registry

    REGISTRY.clear()
    registry = get_registry()
    assert registry["openneuro"] is openneuro.OpenNeuroHarvester


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


def test_probe_makes_exactly_one_request_with_first_1(tmp_path, monkeypatch):
    fake = RecordingPostJson(
        [page_response(["ds000001"], has_next=True, end_cursor="c1")]
    )
    monkeypatch.setattr(openneuro.http, "post_json", fake)

    harvester = openneuro.OpenNeuroHarvester(store=RawStore("openneuro", root=tmp_path))
    result = harvester.probe()

    assert len(fake.calls) == 1
    _url, payload = fake.calls[0]
    assert payload["variables"] == {"first": 1, "after": None}
    assert result["data"]["datasets"]["edges"][0]["node"]["id"] == "ds000001"


@pytest.mark.parametrize(
    "bad_response",
    [
        None,
        {},
        {"data": {}},
        {"data": {"datasets": {}}},
        {"data": {"datasets": {"edges": []}}},
        {"data": {"datasets": {"edges": [None]}}},
        {"data": {"datasets": {"edges": [{"node": {}}]}}},
    ],
)
def test_probe_raises_assertion_error_on_malformed_response(
    bad_response, tmp_path, monkeypatch
):
    monkeypatch.setattr(openneuro.http, "post_json", lambda *a, **k: bad_response)
    harvester = openneuro.OpenNeuroHarvester(store=RawStore("openneuro", root=tmp_path))
    with pytest.raises(AssertionError):
        harvester.probe()


# ---------------------------------------------------------------------------
# harvest() -- pagination, writes, limit, failure
# ---------------------------------------------------------------------------


def test_harvest_walks_multiple_pages_and_writes_every_record(tmp_path, monkeypatch):
    fake = RecordingPostJson(
        [
            page_response(
                ["ds000001", "ds000002", "ds000003"], has_next=True, end_cursor="c1"
            ),
            page_response(["ds000004", "ds000005"], has_next=False, end_cursor="c2"),
        ]
    )
    monkeypatch.setattr(openneuro.http, "post_json", fake)

    store = RawStore("openneuro", root=tmp_path)
    harvester = openneuro.OpenNeuroHarvester(store=store)
    result = harvester.harvest()

    assert isinstance(result, HarvestResult)
    assert result.status == "ok"
    assert result.listed == 5
    assert result.written == 5
    assert result.unchanged == 0
    assert result.removed == 0
    assert result.error is None

    assert len(fake.calls) == 2
    assert fake.calls[0][1]["variables"] == {"first": 100, "after": None}
    assert fake.calls[1][1]["variables"] == {"first": 100, "after": "c1"}

    envelope = store.load("ds000001")
    assert envelope["source"] == "openneuro"
    assert envelope["native_id"] == "ds000001"
    assert envelope["harvest_method"] == "api:openneuro-graphql"
    assert envelope["payload"]["id"] == "ds000001"  # passed through verbatim
    assert envelope["payload"]["latestSnapshot"]["tag"] == "1.0.0"

    manifest = json.loads((tmp_path / "openneuro" / "manifest.json").read_text())
    assert manifest["status"] == "ok"
    assert set(manifest["records"]) == {
        "ds000001",
        "ds000002",
        "ds000003",
        "ds000004",
        "ds000005",
    }


def test_harvest_respects_limit_and_stops_early(tmp_path, monkeypatch):
    fake = RecordingPostJson(
        [
            page_response(
                ["ds000001", "ds000002", "ds000003"], has_next=True, end_cursor="c1"
            )
        ]
    )
    monkeypatch.setattr(openneuro.http, "post_json", fake)

    store = RawStore("openneuro", root=tmp_path)
    harvester = openneuro.OpenNeuroHarvester(store=store)
    result = harvester.harvest(limit=2)

    assert result.status == "ok"
    assert result.listed == 2
    assert result.written == 2
    # must not have fetched a second page once the limit was reached.
    assert len(fake.calls) == 1


def test_harvest_skips_null_edges_from_a_real_captured_page(tmp_path, monkeypatch):
    listing = load_listing_fixture()
    # The live page this was captured from had more pages after it; force
    # it terminal here so the harvester doesn't try to fetch a second one.
    listing["data"]["datasets"]["pageInfo"]["hasNextPage"] = False

    monkeypatch.setattr(openneuro.http, "post_json", lambda *a, **k: listing)

    store = RawStore("openneuro", root=tmp_path)
    harvester = openneuro.OpenNeuroHarvester(store=store)
    result = harvester.harvest()

    assert result.status == "ok"
    # 5 edges in the fixture, 2 of which are `null` (a real server-side
    # resolver failure) -- only the 3 valid ones are listed/written.
    assert result.listed == 3
    assert result.written == 3
    assert set(store.existing()) == {"ds006676", "ds006673", "ds006670"}


def test_harvest_returns_failed_result_without_raising_when_request_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(openneuro.http, "post_json", lambda *a, **k: None)

    store = RawStore("openneuro", root=tmp_path)
    harvester = openneuro.OpenNeuroHarvester(store=store)
    result = harvester.harvest()  # must not raise

    assert result.status == "failed"
    assert result.error is not None
    assert result.listed == 0

    manifest = json.loads((tmp_path / "openneuro" / "manifest.json").read_text())
    assert manifest["status"] == "failed"


def test_harvest_uses_page_size_100_when_not_limited(tmp_path, monkeypatch):
    fake = RecordingPostJson([page_response([], has_next=False, end_cursor=None)])
    monkeypatch.setattr(openneuro.http, "post_json", fake)

    store = RawStore("openneuro", root=tmp_path)
    harvester = openneuro.OpenNeuroHarvester(store=store)
    harvester.harvest()

    assert fake.calls[0][1]["variables"]["first"] == 100


def test_harvest_endpoint_is_the_verified_graphql_url(tmp_path, monkeypatch):
    fake = RecordingPostJson([page_response([], has_next=False, end_cursor=None)])
    monkeypatch.setattr(openneuro.http, "post_json", fake)

    store = RawStore("openneuro", root=tmp_path)
    harvester = openneuro.OpenNeuroHarvester(store=store)
    harvester.harvest()

    url, _payload = fake.calls[0]
    assert url == "https://openneuro.org/crn/graphql"
