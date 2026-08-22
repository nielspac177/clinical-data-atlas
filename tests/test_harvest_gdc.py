"""Tests for `atlas.harvest.gdc` (NCI GDC projects harvester).

Every test monkeypatches `atlas.http.get_json` (imported into
`atlas.harvest.gdc` as the `http` module object, so patching either name
patches the same underlying attribute) -- nothing here ever touches the
network; see `tests/conftest.py`'s socket guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.harvest import gdc
from atlas.harvest.base import RawStore

FIXTURES = Path(__file__).parent / "fixtures" / "gdc"


def _load_listing() -> dict:
    return json.loads((FIXTURES / "listing.json").read_text())


# ---------------------------------------------------------------------------
# harvest() against the real fixture -- envelopes + manifest
# ---------------------------------------------------------------------------


def test_harvest_writes_one_envelope_per_hit_and_a_manifest(tmp_path, monkeypatch):
    listing = _load_listing()  # 5 real trimmed hits, pagination.total=93
    calls = []

    def fake_get_json(url, *, params=None, **kwargs):
        calls.append(params)
        return listing

    monkeypatch.setattr(gdc.http, "get_json", fake_get_json)

    store = RawStore("gdc", root=tmp_path)
    harvester = gdc.GDCHarvester(store=store)
    result = harvester.harvest(limit=5)

    # limit==len(hits) is reached while still on the first page, so the
    # (real, if unmocked) 93-project total never forces a second request.
    assert len(calls) == 1
    assert result.source == "gdc"
    assert result.status == "ok"
    assert result.listed == 5
    assert result.written == 5
    assert result.unchanged == 0
    assert result.removed == 0
    assert result.error is None

    envelopes = store.load_all()
    assert set(envelopes) == {
        "TCGA-LUAD",
        "TCGA-GBM",
        "TARGET-AML",
        "MATCH-S1",
        "CGCI-BLGSP",
    }
    for native_id, envelope in envelopes.items():
        assert envelope["source"] == "gdc"
        assert envelope["harvest_method"] == "api:gdc-projects"
        assert envelope["payload"]["project_id"] == native_id
        assert envelope["endpoints"]  # non-empty

    manifest = json.loads((tmp_path / "gdc" / "manifest.json").read_text())
    assert manifest["status"] == "ok"
    assert manifest["counts"] == {
        "listed": 5,
        "written": 5,
        "unchanged": 0,
        "removed": 0,
    }
    assert manifest["endpoints"]


def test_harvest_second_call_reports_unchanged_for_identical_payloads(
    tmp_path, monkeypatch
):
    listing = _load_listing()
    monkeypatch.setattr(gdc.http, "get_json", lambda *a, **k: listing)

    store = RawStore("gdc", root=tmp_path)
    gdc.GDCHarvester(store=store).harvest(limit=5)

    store2 = RawStore("gdc", root=tmp_path)
    result2 = gdc.GDCHarvester(store=store2).harvest(limit=5)

    assert result2.written == 0
    assert result2.unchanged == 5


# ---------------------------------------------------------------------------
# Paging logic: a synthetic two-page fake, independent of the real fixture
# ---------------------------------------------------------------------------


def test_harvest_pages_via_from_until_pagination_total_reached(tmp_path, monkeypatch):
    pages = {
        0: {
            "data": {
                "hits": [{"project_id": "A"}, {"project_id": "B"}],
                "pagination": {"total": 3},
            }
        },
        2: {
            "data": {
                "hits": [{"project_id": "C"}],
                "pagination": {"total": 3},
            }
        },
    }
    seen_from = []

    def fake_get_json(url, *, params=None, **kwargs):
        seen_from.append(params["from"])
        return pages[params["from"]]

    monkeypatch.setattr(gdc.http, "get_json", fake_get_json)

    store = RawStore("gdc", root=tmp_path)
    result = gdc.GDCHarvester(store=store).harvest()

    assert seen_from == [0, 2]
    assert result.status == "ok"
    assert result.listed == 3
    assert set(store.load_all()) == {"A", "B", "C"}


def test_harvest_stops_when_a_page_returns_no_hits(tmp_path, monkeypatch):
    """Defensive: an empty `hits` page (e.g. a `from` past the real end)
    must not spin forever even if `pagination.total` is never reached."""
    body = {"data": {"hits": [], "pagination": {"total": 999}}}
    monkeypatch.setattr(gdc.http, "get_json", lambda *a, **k: body)

    store = RawStore("gdc", root=tmp_path)
    result = gdc.GDCHarvester(store=store).harvest()

    assert result.status == "ok"
    assert result.listed == 0


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------


def test_harvest_limit_caps_records_mid_page(tmp_path, monkeypatch):
    listing = _load_listing()  # 5 hits in one page
    monkeypatch.setattr(gdc.http, "get_json", lambda *a, **k: listing)

    store = RawStore("gdc", root=tmp_path)
    result = gdc.GDCHarvester(store=store).harvest(limit=2)

    assert result.listed == 2
    assert result.written == 2
    assert set(store.load_all()) == {"TCGA-LUAD", "TCGA-GBM"}  # fixture order


# ---------------------------------------------------------------------------
# Failure path: never raise out of harvest()
# ---------------------------------------------------------------------------


def test_harvest_returns_failed_result_when_request_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(gdc.http, "get_json", lambda *a, **k: None)

    store = RawStore("gdc", root=tmp_path)
    result = gdc.GDCHarvester(store=store).harvest()

    assert result.status == "failed"
    assert result.error is not None

    manifest = json.loads((tmp_path / "gdc" / "manifest.json").read_text())
    assert manifest["status"] == "failed"


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


def test_probe_returns_body_on_valid_shape(monkeypatch):
    body = {"data": {"hits": [{"project_id": "X"}], "pagination": {"total": 93}}}
    monkeypatch.setattr(gdc.http, "get_json", lambda *a, **k: body)

    harvester = gdc.GDCHarvester(store=object())
    assert harvester.probe() == body


def test_probe_requests_size_one(monkeypatch):
    seen = {}

    def fake_get_json(url, *, params=None, **kwargs):
        seen.update(params)
        return {"data": {"hits": [{"project_id": "X"}], "pagination": {"total": 1}}}

    monkeypatch.setattr(gdc.http, "get_json", fake_get_json)
    gdc.GDCHarvester(store=object()).probe()

    assert seen["size"] == 1
    assert seen["from"] == 0


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"data": {}},
        {"data": {"hits": []}},
        {
            "data": {
                "hits": [{"name": "no project_id here"}],
                "pagination": {"total": 1},
            }
        },
        {"data": {"hits": [{"project_id": "X"}]}},
        {"data": {"hits": [{"project_id": "X"}], "pagination": {}}},
    ],
)
def test_probe_raises_assertion_error_on_malformed_response(monkeypatch, body):
    monkeypatch.setattr(gdc.http, "get_json", lambda *a, **k: body)
    harvester = gdc.GDCHarvester(store=object())
    with pytest.raises(AssertionError):
        harvester.probe()
