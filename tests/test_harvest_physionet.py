"""Tests for `atlas.harvest.physionet.PhysioNetHarvester`.

Fully offline: every test monkeypatches `atlas.http.get_json` instead of
touching the network (see `tests/conftest.py`'s socket guard). The real
fixture (`tests/fixtures/physionet/listing.json`, captured live -- see
`tests/test_live_physionet.py` and `docs/sources/physionet.md`) covers
the "realistic end-to-end" cases; small hand-built listings cover
specific branches (version-filtering, resource-type-agnostic keep,
`limit`, malformed responses) that the 5-entry fixture alone can't
exercise deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas import http
from atlas.harvest.base import RawStore
from atlas.harvest.physionet import LISTING_URL, PhysioNetHarvester

FIXTURES = Path(__file__).parent / "fixtures" / "physionet"


def _load_listing() -> list[dict]:
    return json.loads((FIXTURES / "listing.json").read_text(encoding="utf-8"))


def _entry(**overrides) -> dict:
    """A minimal, structurally-valid listing entry for branch tests that
    don't need a full real record."""
    base = {
        "slug": "demo",
        "version": "1.0.0",
        "is_latest_version": True,
        "resource_type": "Database",
        "access_policy": "Open",
        "title": "Demo",
        "short_description": "A demo dataset.",
        "abstract": "<p>A demo dataset.</p>",
        "topics": ["demo"],
        "source_url": "https://physionet.org/content/demo/1.0.0/",
        "core_doi": None,
        "version_doi": "10.13026/demo",
        "publish_date": "2020-01-01",
        "license": {"name": "CC0"},
        "dua": None,
        "main_storage_size": 100,
    }
    base.update(overrides)
    return base


def _harvester(tmp_path: Path) -> PhysioNetHarvester:
    return PhysioNetHarvester(store=RawStore("physionet", root=tmp_path))


def _counting_get_json(return_value):
    calls = []

    def fake(url, **kwargs):
        calls.append(url)
        return return_value

    return fake, calls


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


def test_probe_returns_the_listing_body(tmp_path, monkeypatch):
    listing = _load_listing()
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)
    result = _harvester(tmp_path).probe()
    assert result == listing


def test_probe_makes_exactly_one_request(tmp_path, monkeypatch):
    fake, calls = _counting_get_json(_load_listing())
    monkeypatch.setattr(http, "get_json", fake)
    _harvester(tmp_path).probe()
    assert calls == [LISTING_URL]


def test_probe_raises_when_request_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: None)
    with pytest.raises(AssertionError):
        _harvester(tmp_path).probe()


def test_probe_raises_on_non_list_body(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: {"not": "a list"})
    with pytest.raises(AssertionError):
        _harvester(tmp_path).probe()


def test_probe_raises_on_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: [])
    with pytest.raises(AssertionError):
        _harvester(tmp_path).probe()


def test_probe_raises_when_first_entry_missing_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: [{"access_policy": "Open"}])
    with pytest.raises(AssertionError):
        _harvester(tmp_path).probe()


def test_probe_raises_when_first_entry_missing_access_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: [{"slug": "x"}])
    with pytest.raises(AssertionError):
        _harvester(tmp_path).probe()


# ---------------------------------------------------------------------------
# harvest() -- happy path against the real captured fixture
# ---------------------------------------------------------------------------


def test_harvest_writes_one_envelope_per_fixture_entry(tmp_path, monkeypatch):
    listing = _load_listing()
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)

    result = _harvester(tmp_path).harvest()

    assert result.status == "ok"
    assert result.listed == len(listing) == 5
    assert result.written == 5
    assert result.unchanged == 0
    assert result.removed == 0
    assert result.error is None

    store = RawStore("physionet", root=tmp_path)
    written = store.load_all()
    assert set(written) == {e["slug"] for e in listing}


def test_harvest_envelope_payload_is_the_entry_verbatim(tmp_path, monkeypatch):
    listing = _load_listing()
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)
    _harvester(tmp_path).harvest()

    store = RawStore("physionet", root=tmp_path)
    entry = next(e for e in listing if e["slug"] == "slpdb")
    envelope = store.load("slpdb")
    assert envelope["payload"] == entry
    assert envelope["source"] == "physionet"
    assert envelope["harvest_method"] == "api:physionet-published"
    assert envelope["endpoints"] == [LISTING_URL]


def test_harvest_keeps_the_software_entry_in_raw(tmp_path, monkeypatch):
    """The raw cache keeps every resource type -- narrowing to
    dataset-shaped resources is the normalizer's job, not the
    harvester's."""
    listing = _load_listing()
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)
    _harvester(tmp_path).harvest()

    store = RawStore("physionet", root=tmp_path)
    envelope = store.load("wfdb-swig-matlab")
    assert envelope["payload"]["resource_type"] == "Software"


def test_harvest_makes_exactly_one_request(tmp_path, monkeypatch):
    fake, calls = _counting_get_json(_load_listing())
    monkeypatch.setattr(http, "get_json", fake)
    _harvester(tmp_path).harvest()
    assert calls == [LISTING_URL]


# ---------------------------------------------------------------------------
# harvest() -- version filtering, resource-type-agnostic keep, limit
# ---------------------------------------------------------------------------


def test_harvest_skips_entries_where_is_latest_version_is_false(tmp_path, monkeypatch):
    listing = [
        _entry(slug="proj", version="1.0.0", is_latest_version=False),
        _entry(slug="proj", version="2.0.0", is_latest_version=True),
    ]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)

    result = _harvester(tmp_path).harvest()

    assert result.listed == 1
    store = RawStore("physionet", root=tmp_path)
    envelope = store.load("proj")
    assert envelope["payload"]["version"] == "2.0.0"


def test_harvest_keeps_all_resource_types_when_latest(tmp_path, monkeypatch):
    listing = [
        _entry(slug="a-db", resource_type="Database"),
        _entry(slug="a-challenge", resource_type="Challenge"),
        _entry(slug="a-software", resource_type="Software"),
        _entry(slug="a-model", resource_type="Model"),
    ]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)

    result = _harvester(tmp_path).harvest()

    assert result.listed == 4
    store = RawStore("physionet", root=tmp_path)
    assert set(store.load_all()) == {"a-db", "a-challenge", "a-software", "a-model"}


def test_harvest_limit_caps_written_records(tmp_path, monkeypatch):
    listing = [_entry(slug=f"proj-{i}") for i in range(4)]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)

    result = _harvester(tmp_path).harvest(limit=2)

    assert result.listed == 2
    assert result.written == 2
    store = RawStore("physionet", root=tmp_path)
    assert len(store.load_all()) == 2


def test_harvest_limit_none_processes_everything(tmp_path, monkeypatch):
    listing = [_entry(slug=f"proj-{i}") for i in range(4)]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)

    result = _harvester(tmp_path).harvest(limit=None)

    assert result.listed == 4


def test_harvest_skips_malformed_entries_without_crashing(tmp_path, monkeypatch):
    listing = [
        "not a dict",
        {"is_latest_version": True},  # no slug
        _entry(slug="ok"),
    ]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)

    result = _harvester(tmp_path).harvest()

    assert result.status == "ok"
    assert result.listed == 1
    store = RawStore("physionet", root=tmp_path)
    assert set(store.load_all()) == {"ok"}


# ---------------------------------------------------------------------------
# harvest() -- second run: unchanged / removed bookkeeping via RawStore
# ---------------------------------------------------------------------------


def test_harvest_second_run_unchanged_when_listing_is_identical(tmp_path, monkeypatch):
    listing = _load_listing()
    monkeypatch.setattr(http, "get_json", lambda url, **kw: listing)

    store = RawStore("physionet", root=tmp_path)
    PhysioNetHarvester(store=store).harvest()
    second = PhysioNetHarvester(store=RawStore("physionet", root=tmp_path)).harvest()

    assert second.written == 0
    assert second.unchanged == 5


def test_harvest_second_run_removes_slug_no_longer_latest(tmp_path, monkeypatch):
    full_listing = [_entry(slug="a"), _entry(slug="b")]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: full_listing)
    PhysioNetHarvester(store=RawStore("physionet", root=tmp_path)).harvest()

    shrunk_listing = [_entry(slug="a")]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: shrunk_listing)
    result = PhysioNetHarvester(store=RawStore("physionet", root=tmp_path)).harvest()

    # a 1-of-2 shrink trips RawStore's >20% shrink guard -- this proves
    # the harvester's `listed_ids` faithfully reflects what it actually
    # saw (RawStore.finalize's own guard behavior is exercised in
    # tests/test_rawstore.py, not re-tested here).
    assert result.status == "failed"
    assert "shrank" in (result.error or "")


# ---------------------------------------------------------------------------
# harvest() -- failure paths never raise out of harvest()
# ---------------------------------------------------------------------------


def test_harvest_marks_failed_when_request_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: None)

    result = _harvester(tmp_path).harvest()

    assert result.status == "failed"
    assert result.error is not None
    assert LISTING_URL in result.error


def test_harvest_marks_failed_when_response_is_not_a_list(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: {"unexpected": "shape"})

    result = _harvester(tmp_path).harvest()

    assert result.status == "failed"
    assert result.error is not None


def test_harvest_failure_writes_a_failed_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: None)
    store = RawStore("physionet", root=tmp_path)

    _harvester_result = PhysioNetHarvester(store=store).harvest()

    manifest = json.loads((tmp_path / "physionet" / "manifest.json").read_text())
    assert manifest["status"] == "failed"


def test_harvest_never_raises_on_request_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: None)
    # Must not raise -- this call itself is the assertion.
    _harvester(tmp_path).harvest()


# ---------------------------------------------------------------------------
# Registry auto-discovery
# ---------------------------------------------------------------------------


def test_physionet_harvester_is_discovered_by_the_real_registry():
    from atlas import harvest as harvest_pkg

    registry = harvest_pkg.get_registry()
    assert registry.get("physionet") is PhysioNetHarvester


def test_harvester_class_attributes():
    assert PhysioNetHarvester.name == "physionet"
    assert PhysioNetHarvester.harvest_method == "api:physionet-published"
