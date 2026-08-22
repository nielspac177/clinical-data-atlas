"""Tests for atlas.harvest: HarvestResult, RawStore, Harvester, and the
auto-discovery registry in atlas/harvest/__init__.py.

All filesystem tests pass root=tmp_path to RawStore -- nothing here ever
touches the real data/raw/ tree.
"""

from __future__ import annotations

import json
import types
from types import SimpleNamespace

import pytest

from atlas import harvest as harvest_pkg
from atlas import io
from atlas.harvest import base
from atlas.harvest.base import Harvester, HarvestResult, RawStore

# ---------------------------------------------------------------------------
# HarvestResult
# ---------------------------------------------------------------------------


def test_harvest_result_defaults():
    result = HarvestResult(source="demo", status="ok")
    assert result.listed == 0
    assert result.written == 0
    assert result.unchanged == 0
    assert result.removed == 0
    assert result.seconds == 0.0
    assert result.error is None


def test_harvest_result_accepts_all_fields():
    result = HarvestResult(
        source="demo",
        status="failed",
        listed=10,
        written=3,
        unchanged=7,
        removed=1,
        seconds=1.5,
        error="boom",
    )
    assert (result.listed, result.written, result.unchanged, result.removed) == (
        10,
        3,
        7,
        1,
    )
    assert result.seconds == 1.5
    assert result.error == "boom"


# ---------------------------------------------------------------------------
# RawStore.write -- new / unchanged / changed
# ---------------------------------------------------------------------------


def test_write_new_record_returns_true_and_writes_file(tmp_path):
    store = RawStore("demo", root=tmp_path)
    changed = store.write(
        "d1", {"title": "Demo"}, harvest_method="api", endpoints=["https://api/d1"]
    )
    assert changed is True
    assert (tmp_path / "demo" / "records" / "d1.json").exists()


def test_write_same_payload_twice_returns_false_second_time_and_file_bytes_unchanged(
    tmp_path,
):
    store = RawStore("demo", root=tmp_path)
    payload = {"title": "Demo Dataset", "n": 42}

    first = store.write(
        "d1", payload, harvest_method="api", endpoints=["https://api/d1"]
    )
    path = tmp_path / "demo" / "records" / "d1.json"
    bytes_after_first = path.read_bytes()

    second = store.write(
        "d1", payload, harvest_method="api", endpoints=["https://api/d1"]
    )
    bytes_after_second = path.read_bytes()

    assert first is True
    assert second is False
    assert bytes_after_second == bytes_after_first


def test_write_changed_payload_returns_true_and_rewrites_file(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write("d1", {"title": "Demo", "n": 1}, harvest_method="api", endpoints=[])
    changed = store.write(
        "d1", {"title": "Demo", "n": 2}, harvest_method="api", endpoints=[]
    )
    path = tmp_path / "demo" / "records" / "d1.json"
    assert changed is True
    assert json.loads(path.read_text(encoding="utf-8"))["payload"] == {
        "title": "Demo",
        "n": 2,
    }


def test_write_volatile_field_change_only_returns_false_and_file_untouched(tmp_path):
    store = RawStore("demo", root=tmp_path)
    payload_v1 = {
        "id": "d1",
        "attributes": {"title": "Demo", "updated": "2026-01-01T00:00:00Z"},
    }
    store.write(
        "d1",
        payload_v1,
        harvest_method="api",
        endpoints=[],
        volatile=("attributes.updated",),
    )
    path = tmp_path / "demo" / "records" / "d1.json"
    bytes_before = path.read_bytes()

    payload_v2 = {
        "id": "d1",
        "attributes": {"title": "Demo", "updated": "2026-06-15T00:00:00Z"},
    }
    changed = store.write(
        "d1",
        payload_v2,
        harvest_method="api",
        endpoints=[],
        volatile=("attributes.updated",),
    )

    assert changed is False
    assert path.read_bytes() == bytes_before
    # the file was never rewritten -- it still carries the *original* value
    assert "2026-01-01" in bytes_before.decode("utf-8")


def test_write_volatile_missing_path_is_ignored(tmp_path):
    store = RawStore("demo", root=tmp_path)
    changed = store.write(
        "d1",
        {"id": "d1"},
        harvest_method="api",
        endpoints=[],
        volatile=("attributes.updated",),
    )
    assert changed is True


def test_write_volatile_does_not_traverse_into_lists(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write(
        "d1",
        {"id": "d1", "relationships": [{"updated": "x"}]},
        harvest_method="api",
        endpoints=[],
        volatile=("relationships",),
    )
    # whole-key strip of a list-valued field works (dict-level pop)...
    changed = store.write(
        "d1",
        {"id": "d1", "relationships": [{"updated": "y"}]},
        harvest_method="api",
        endpoints=[],
        volatile=("relationships",),
    )
    assert changed is False

    # ...but indexing *through* a list is not supported: it's a no-op, not a crash.
    store2 = RawStore("other", root=tmp_path)
    changed2 = store2.write(
        "d1",
        {"a": [{"b": 1}]},
        harvest_method="api",
        endpoints=[],
        volatile=("a.0.b",),
    )
    assert changed2 is True


# ---------------------------------------------------------------------------
# RawStore.write -- envelope shape
# ---------------------------------------------------------------------------


def test_write_creates_pretty_json_envelope_with_expected_shape(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write(
        "d1",
        {"title": "Demo"},
        harvest_method="api",
        endpoints=["https://api/x", "https://api/y"],
    )
    path = tmp_path / "demo" / "records" / "d1.json"
    expected = io.pretty_json(
        {
            "source": "demo",
            "native_id": "d1",
            "harvest_method": "api",
            "endpoints": ["https://api/x", "https://api/y"],
            "payload": {"title": "Demo"},
        }
    )
    assert path.read_text(encoding="utf-8") == expected


def _all_keys(obj):
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item)
    return keys


def test_envelope_has_no_timestamp_keys(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write(
        "d1",
        {"title": "Demo", "nested": {"a": 1}, "items": [{"b": 2}]},
        harvest_method="api",
        endpoints=["https://api/x"],
    )
    envelope = store.load("d1")
    assert _all_keys(envelope).isdisjoint({"harvested_at", "fetched_at", "timestamp"})


# ---------------------------------------------------------------------------
# RawStore.write -- filename slugging + collisions
# ---------------------------------------------------------------------------


def test_write_native_id_needing_slug_uses_slug_filename_but_keeps_verbatim_in_envelope(
    tmp_path,
):
    store = RawStore("demo", root=tmp_path)
    store.write("abc/123", {"title": "Demo"}, harvest_method="api", endpoints=[])
    slug_path = tmp_path / "demo" / "records" / "abc-123.json"
    assert slug_path.exists()
    envelope = json.loads(slug_path.read_text(encoding="utf-8"))
    assert envelope["native_id"] == "abc/123"


def test_write_native_id_already_safe_is_used_verbatim_as_filename(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write(
        "file_name.v2-final", {"title": "Demo"}, harvest_method="api", endpoints=[]
    )
    assert (tmp_path / "demo" / "records" / "file_name.v2-final.json").exists()


def test_write_slug_collision_raises_value_error(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write("A/B", {"title": "one"}, harvest_method="api", endpoints=[])
    with pytest.raises(ValueError, match="a-b.json"):
        store.write("a-b", {"title": "two"}, harvest_method="api", endpoints=[])


def test_write_same_native_id_twice_is_not_a_collision(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write("A/B", {"title": "one"}, harvest_method="api", endpoints=[])
    # same native id again -- must not raise, even though it re-resolves
    # to the same slug filename.
    store.write("A/B", {"title": "one"}, harvest_method="api", endpoints=[])


# ---------------------------------------------------------------------------
# RawStore.existing / load / load_all
# ---------------------------------------------------------------------------


def test_existing_is_empty_when_no_manifest(tmp_path):
    store = RawStore("demo", root=tmp_path)
    assert store.existing() == {}


def test_existing_reflects_a_manifest_already_on_disk(tmp_path):
    manifest = {
        "source": "demo",
        "harvested_at": "2026-01-01",
        "endpoints": [],
        "status": "ok",
        "error": None,
        "counts": {"listed": 1, "written": 1, "unchanged": 0, "removed": 0},
        "records": {"x": {"hash": "sha256:" + "0" * 64, "first_seen": "2026-01-01"}},
    }
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    store = RawStore("demo", root=tmp_path)
    assert store.existing() == manifest["records"]


def test_load_returns_verbatim_envelope(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write(
        "d1", {"title": "Demo"}, harvest_method="api", endpoints=["https://api/x"]
    )
    assert store.load("d1") == {
        "source": "demo",
        "native_id": "d1",
        "harvest_method": "api",
        "endpoints": ["https://api/x"],
        "payload": {"title": "Demo"},
    }


def test_load_all_uses_manifest_records(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write("a", {"v": 1}, harvest_method="api", endpoints=[])
    store.write("b", {"v": 2}, harvest_method="api", endpoints=[])
    store.finalize(
        listed_ids={"a", "b"}, harvested_at="2026-01-01", endpoints=[], status="ok"
    )

    fresh_store = RawStore("demo", root=tmp_path)
    envelopes = fresh_store.load_all()
    assert set(envelopes) == {"a", "b"}
    assert envelopes["a"]["payload"] == {"v": 1}


def test_load_all_falls_back_to_glob_without_manifest(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write("a", {"v": 1}, harvest_method="api", endpoints=[])
    store.write("b", {"v": 2}, harvest_method="api", endpoints=[])

    envelopes = store.load_all()
    assert set(envelopes) == {"a", "b"}


# ---------------------------------------------------------------------------
# RawStore.finalize -- manifest shape, first_seen, removal, shrink guard, failure
# ---------------------------------------------------------------------------


def test_finalize_writes_manifest_with_expected_shape(tmp_path):
    store = RawStore("demo", root=tmp_path)
    store.write("a", {"x": 1}, harvest_method="api", endpoints=["https://api/a"])
    manifest = store.finalize(
        listed_ids={"a"},
        harvested_at="2026-01-01",
        endpoints=["https://api/list"],
        status="ok",
    )

    assert manifest == {
        "source": "demo",
        "harvested_at": "2026-01-01",
        "endpoints": ["https://api/list"],
        "status": "ok",
        "error": None,
        "counts": {"listed": 1, "written": 1, "unchanged": 0, "removed": 0},
        "records": {
            "a": {"hash": io.content_hash({"x": 1}), "first_seen": "2026-01-01"}
        },
    }
    manifest_path = tmp_path / "demo" / "manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert manifest_path.read_text(encoding="utf-8") == io.pretty_json(manifest)


def test_finalize_first_seen_preserved_across_runs(tmp_path):
    store1 = RawStore("demo", root=tmp_path)
    store1.write("a", {"x": 1}, harvest_method="api", endpoints=[])
    store1.finalize(
        listed_ids={"a"}, harvested_at="2026-01-01", endpoints=[], status="ok"
    )

    store2 = RawStore("demo", root=tmp_path)
    changed = store2.write("a", {"x": 1}, harvest_method="api", endpoints=[])
    manifest = store2.finalize(
        listed_ids={"a"}, harvested_at="2026-02-01", endpoints=[], status="ok"
    )

    assert changed is False
    assert manifest["records"]["a"]["first_seen"] == "2026-01-01"


def test_finalize_removes_stale_record_and_counts_removed(tmp_path):
    ids = ["a", "b", "c", "d", "e"]
    store1 = RawStore("demo", root=tmp_path)
    for nid in ids:
        store1.write(nid, {"id": nid}, harvest_method="api", endpoints=[])
    store1.finalize(
        listed_ids=set(ids), harvested_at="2026-01-01", endpoints=[], status="ok"
    )

    e_path = tmp_path / "demo" / "records" / "e.json"
    assert e_path.exists()

    store2 = RawStore("demo", root=tmp_path)
    for nid in ["a", "b", "c", "d"]:
        store2.write(nid, {"id": nid}, harvest_method="api", endpoints=[])
    manifest = store2.finalize(
        listed_ids={"a", "b", "c", "d"},
        harvested_at="2026-01-02",
        endpoints=[],
        status="ok",
    )

    assert manifest["status"] == "ok"
    assert manifest["counts"] == {
        "listed": 4,
        "written": 0,
        "unchanged": 4,
        "removed": 1,
    }
    assert "e" not in manifest["records"]
    assert not e_path.exists()


def test_finalize_shrink_guard_triggers_failed_and_keeps_files(tmp_path):
    ids = ["a", "b", "c", "d", "e"]
    store1 = RawStore("demo", root=tmp_path)
    for nid in ids:
        store1.write(nid, {"id": nid}, harvest_method="api", endpoints=[])
    store1.finalize(
        listed_ids=set(ids), harvested_at="2026-01-01", endpoints=[], status="ok"
    )

    store2 = RawStore("demo", root=tmp_path)
    store2.write("a", {"id": "a"}, harvest_method="api", endpoints=[])
    manifest = store2.finalize(
        listed_ids={"a"}, harvested_at="2026-01-02", endpoints=[], status="ok"
    )

    assert manifest["status"] == "failed"
    assert (
        manifest["error"]
        == "listing shrank from 5 to 1 (>20%); keeping previous records"
    )
    assert manifest["counts"]["removed"] == 0
    assert set(manifest["records"]) == set(ids)
    for nid in ids:
        assert (tmp_path / "demo" / "records" / f"{nid}.json").exists()


def test_finalize_failed_status_keeps_previous_records_and_deletes_nothing(tmp_path):
    store1 = RawStore("demo", root=tmp_path)
    store1.write("a", {"id": "a"}, harvest_method="api", endpoints=[])
    store1.write("b", {"id": "b"}, harvest_method="api", endpoints=[])
    store1.finalize(
        listed_ids={"a", "b"}, harvested_at="2026-01-01", endpoints=[], status="ok"
    )

    store2 = RawStore("demo", root=tmp_path)
    manifest = store2.finalize(
        listed_ids=set(),
        harvested_at="2026-01-02",
        endpoints=[],
        status="failed",
        error="boom: connection reset",
    )

    assert manifest["status"] == "failed"
    assert manifest["error"] == "boom: connection reset"
    assert manifest["counts"]["removed"] == 0
    assert set(manifest["records"]) == {"a", "b"}
    assert (tmp_path / "demo" / "records" / "a.json").exists()
    assert (tmp_path / "demo" / "records" / "b.json").exists()


def test_finalize_failed_status_merges_records_written_before_the_exception(tmp_path):
    store1 = RawStore("demo", root=tmp_path)
    store1.write("a", {"id": "a", "v": 1}, harvest_method="api", endpoints=[])
    store1.finalize(
        listed_ids={"a"}, harvested_at="2026-01-01", endpoints=[], status="ok"
    )

    store2 = RawStore("demo", root=tmp_path)
    store2.write(
        "a", {"id": "a", "v": 2}, harvest_method="api", endpoints=[]
    )  # changed
    store2.write("z", {"id": "z"}, harvest_method="api", endpoints=[])  # brand new
    manifest = store2.finalize(
        listed_ids={"a", "z"},
        harvested_at="2026-01-02",
        endpoints=[],
        status="failed",
        error="boom",
    )

    assert set(manifest["records"]) == {"a", "z"}
    assert manifest["records"]["a"]["hash"] == io.content_hash({"id": "a", "v": 2})
    assert manifest["records"]["a"]["first_seen"] == "2026-01-01"
    assert manifest["records"]["z"]["first_seen"] == "2026-01-02"


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------


class _DemoHarvester(Harvester):
    name = "demo"
    harvest_method = "api"

    def probe(self) -> dict:
        return {}

    def harvest(self, *, fast: bool = False, limit: int | None = None) -> HarvestResult:
        return HarvestResult(source=self.name, status="ok")


def test_harvester_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Harvester()


def test_harvester_uses_injected_store_instead_of_default(tmp_path):
    store = RawStore("demo", root=tmp_path)
    harvester = _DemoHarvester(store=store)
    assert harvester.store is store


def test_harvester_default_constructs_rawstore_named_after_class(monkeypatch):
    calls = []

    class FakeStore:
        def __init__(self, source):
            calls.append(source)
            self.source = source

    monkeypatch.setattr(base, "RawStore", FakeStore)

    harvester = _DemoHarvester()

    assert calls == ["demo"]
    assert isinstance(harvester.store, FakeStore)


# ---------------------------------------------------------------------------
# atlas.harvest registry: auto-discovery
# ---------------------------------------------------------------------------


def test_get_registry_is_empty_with_no_source_modules():
    assert harvest_pkg.get_registry() == {}


def test_harvester_subclass_in_test_module_is_not_auto_registered(monkeypatch):
    monkeypatch.setattr(harvest_pkg, "REGISTRY", {})

    class _NotRegistered(Harvester):
        name = "not_registered_from_test"
        harvest_method = "api"

        def probe(self) -> dict:
            return {}

        def harvest(
            self, *, fast: bool = False, limit: int | None = None
        ) -> HarvestResult:
            return HarvestResult(source=self.name, status="ok")

    registry = harvest_pkg.get_registry()

    assert "not_registered_from_test" not in registry
    assert _NotRegistered not in registry.values()


def test_discover_registers_harvester_subclass_from_a_fake_module(monkeypatch):
    monkeypatch.setattr(harvest_pkg, "REGISTRY", {})

    class _FakeSourceHarvester(Harvester):
        name = "fake_source"
        harvest_method = "api"

        def probe(self) -> dict:
            return {}

        def harvest(
            self, *, fast: bool = False, limit: int | None = None
        ) -> HarvestResult:
            return HarvestResult(source=self.name, status="ok")

    fake_module = types.ModuleType("atlas.harvest.fake_source")
    fake_module.FakeSourceHarvester = _FakeSourceHarvester
    fake_module.NOT_A_CLASS = 42
    fake_module.Harvester = Harvester  # a re-exported base class must not be registered

    fake_listing = [
        SimpleNamespace(name="fake_source"),
        SimpleNamespace(name="base"),
        SimpleNamespace(name="_private"),
    ]

    def fake_import_module(name):
        assert name == "atlas.harvest.fake_source", f"must not import {name!r}"
        return fake_module

    monkeypatch.setattr(harvest_pkg.pkgutil, "iter_modules", lambda path: fake_listing)
    monkeypatch.setattr(harvest_pkg.importlib, "import_module", fake_import_module)

    registry = harvest_pkg.get_registry()

    assert registry == {"fake_source": _FakeSourceHarvester}


def test_get_registry_caches_result_across_calls_once_non_empty(monkeypatch):
    monkeypatch.setattr(harvest_pkg, "REGISTRY", {})

    class _CachedHarvester(Harvester):
        name = "cached_source"
        harvest_method = "api"

        def probe(self) -> dict:
            return {}

        def harvest(
            self, *, fast: bool = False, limit: int | None = None
        ) -> HarvestResult:
            return HarvestResult(source=self.name, status="ok")

    fake_module = types.ModuleType("atlas.harvest.cached_source")
    fake_module.CachedHarvester = _CachedHarvester

    call_count = 0

    def fake_import_module(name):
        nonlocal call_count
        call_count += 1
        return fake_module

    monkeypatch.setattr(
        harvest_pkg.pkgutil,
        "iter_modules",
        lambda path: [SimpleNamespace(name="cached_source")],
    )
    monkeypatch.setattr(harvest_pkg.importlib, "import_module", fake_import_module)

    first = harvest_pkg.get_registry()
    second = harvest_pkg.get_registry()

    assert first == second == {"cached_source": _CachedHarvester}
    assert call_count == 1  # discovery only ran once; second call served from REGISTRY
