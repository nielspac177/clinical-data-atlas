"""Tests for atlas.enrich.mesh and atlas.enrich.ror: label/name -> id
resolution against MeSH and ROR, with an on-disk, negative-caching cache.

Every test here runs fully offline. `atlas.enrich.mesh.http.get_json` /
`atlas.enrich.ror.http.get_json` are monkeypatched with fakes that never
touch a socket (on top of the `tests/conftest.py` socket guard), except
for the two tests that deliberately leave `get_json` untouched to prove
the *real* `atlas.http.OfflineError` path is caught -- those rely on
`ATLAS_OFFLINE=1` being the pytest default (see `tests/conftest.py`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from atlas import config, http, io
from atlas.enrich import mesh, ror

FIXTURES = Path(__file__).parent / "fixtures" / "enrich"


def _boom(*_args, **_kwargs):
    raise AssertionError("get_json should not have been called")


# ---------------------------------------------------------------------------
# mesh.resolve: cache hit / offline short-circuits (no network)
# ---------------------------------------------------------------------------


def test_mesh_resolve_cached_label_returns_without_network(tmp_path, monkeypatch):
    cache_path = tmp_path / "mesh.json"
    io.write_atomic(cache_path, io.pretty_json({"epilepsy": "D004827"}))
    monkeypatch.setattr(mesh.http, "get_json", _boom)

    # Mixed case on the way in -- the cache key is lowercased.
    assert mesh.resolve("Epilepsy", cache_path=cache_path) == "D004827"


def test_mesh_resolve_uncached_explicit_offline_returns_none(tmp_path, monkeypatch):
    cache_path = tmp_path / "mesh.json"
    monkeypatch.setattr(mesh.http, "get_json", _boom)

    assert (
        mesh.resolve("brand new condition", cache_path=cache_path, offline=True) is None
    )
    assert not cache_path.exists()  # nothing learned -> nothing written


def test_mesh_resolve_uncached_global_offline_returns_none_without_crash(tmp_path):
    """No monkeypatching of get_json at all: relies on the real
    atlas.http.get_json raising OfflineError under the pytest-default
    ATLAS_OFFLINE=1, which resolve() must catch rather than propagate."""
    assert config.OFFLINE is True
    cache_path = tmp_path / "mesh.json"

    assert mesh.resolve("another brand new condition", cache_path=cache_path) is None
    assert not cache_path.exists()


# ---------------------------------------------------------------------------
# mesh.resolve: exact -> alias -> startswith lookup order
# ---------------------------------------------------------------------------


def test_mesh_resolve_exact_hit_on_label(tmp_path, monkeypatch):
    calls = []

    def fake(url, *, params=None, **_kwargs):
        calls.append(params)
        assert url == mesh.MESH_LOOKUP_URL
        if params["label"] == "epilepsy" and params["match"] == "exact":
            return [
                {"resource": "http://id.nlm.nih.gov/mesh/D004827", "label": "Epilepsy"}
            ]
        raise AssertionError(f"unexpected lookup {params}")

    monkeypatch.setattr(mesh.http, "get_json", fake)
    assert mesh.resolve("epilepsy", cache_path=tmp_path / "mesh.json") == "D004827"
    assert len(calls) == 1


def test_mesh_resolve_alias_path_exact_hit(tmp_path, monkeypatch):
    """'lung adenocarcinoma' has no direct MeSH label, but
    CONDITION_ALIASES maps it to 'adenocarcinoma of lung', which does."""
    calls = []

    def fake(url, *, params=None, **_kwargs):
        calls.append(dict(params))
        if params == {"label": "lung adenocarcinoma", "match": "exact", "limit": 10}:
            return []
        if params == {"label": "adenocarcinoma of lung", "match": "exact", "limit": 10}:
            return [
                {
                    "resource": "http://id.nlm.nih.gov/mesh/D002289",
                    "label": "Adenocarcinoma of Lung",
                }
            ]
        raise AssertionError(f"unexpected lookup {params}")

    monkeypatch.setattr(mesh.http, "get_json", fake)
    result = mesh.resolve("lung adenocarcinoma", cache_path=tmp_path / "mesh.json")
    assert result == "D002289"
    # exact(label) then exact(alias) -- no startswith call needed once the
    # alias exact-matches.
    assert len(calls) == 2


def test_mesh_resolve_startswith_single_hit_is_accepted(tmp_path, monkeypatch):
    calls = []

    def fake(url, *, params=None, **_kwargs):
        calls.append(dict(params))
        if params["match"] == "exact":
            return []
        assert params["match"] == "startswith"
        return [
            {"resource": "http://id.nlm.nih.gov/mesh/D000001", "label": "Epilepsies"}
        ]

    monkeypatch.setattr(mesh.http, "get_json", fake)
    result = mesh.resolve("epileptic", cache_path=tmp_path / "mesh.json")
    assert result == "D000001"
    assert [c["match"] for c in calls] == ["exact", "startswith"]


def test_mesh_resolve_startswith_multiple_hits_is_rejected(tmp_path, monkeypatch):
    def fake(url, *, params=None, **_kwargs):
        if params["match"] == "exact":
            return []
        return [
            {"resource": "http://id.nlm.nih.gov/mesh/D000001", "label": "Epilepsies"},
            {
                "resource": "http://id.nlm.nih.gov/mesh/D000002",
                "label": "Epilepsy, Other",
            },
        ]

    monkeypatch.setattr(mesh.http, "get_json", fake)
    assert mesh.resolve("epileptic", cache_path=tmp_path / "mesh.json") is None


# ---------------------------------------------------------------------------
# mesh.resolve: negative caching
# ---------------------------------------------------------------------------


def test_mesh_resolve_negative_result_cached_as_null_then_served_from_cache(
    tmp_path, monkeypatch
):
    cache_path = tmp_path / "mesh.json"
    calls = []

    def fake(url, *, params=None, **_kwargs):
        calls.append(dict(params))
        return []

    monkeypatch.setattr(mesh.http, "get_json", fake)
    label = "totally unknown condition xyz"

    assert mesh.resolve(label, cache_path=cache_path) is None
    assert len(calls) == 2  # exact, then startswith, both empty

    on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert on_disk == {label.lower(): None}

    calls.clear()
    assert mesh.resolve(label, cache_path=cache_path) is None
    assert calls == []  # served from cache, no network


# ---------------------------------------------------------------------------
# mesh: cache file format, and rewritten only when it actually changes
# ---------------------------------------------------------------------------


def test_mesh_cache_file_is_pretty_json_with_sorted_keys(tmp_path, monkeypatch):
    def fake(url, *, params=None, **_kwargs):
        if params["match"] != "exact":
            return []
        if params["label"] == "b condition":
            return [{"resource": "http://id.nlm.nih.gov/mesh/D000002", "label": "B"}]
        if params["label"] == "a condition":
            return [{"resource": "http://id.nlm.nih.gov/mesh/D000001", "label": "A"}]
        return []

    monkeypatch.setattr(mesh.http, "get_json", fake)
    cache_path = tmp_path / "mesh.json"

    # Resolved out of alphabetical order -- the file must still come out
    # sorted (io.pretty_json's sort_keys=True).
    mesh.resolve_many(["b condition", "a condition"], cache_path=cache_path)

    expected = io.pretty_json({"a condition": "D000001", "b condition": "D000002"})
    assert cache_path.read_text(encoding="utf-8") == expected


def test_mesh_cache_not_rewritten_when_nothing_changes(tmp_path, monkeypatch):
    cache_path = tmp_path / "mesh.json"
    io.write_atomic(cache_path, io.pretty_json({"epilepsy": "D004827"}))
    before = cache_path.read_bytes()

    monkeypatch.setattr(mesh.http, "get_json", _boom)
    assert mesh.resolve_many(["epilepsy", "Epilepsy"], cache_path=cache_path) == {
        "epilepsy": "D004827",
        "Epilepsy": "D004827",
    }
    assert cache_path.read_bytes() == before


def test_mesh_resolve_many_writes_cache_at_most_once_per_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(mesh.http, "get_json", lambda *a, **kw: [])
    write_calls = []
    real_write_atomic = mesh.io.write_atomic

    def counting_write_atomic(path, text):
        write_calls.append(path)
        real_write_atomic(path, text)

    monkeypatch.setattr(mesh.io, "write_atomic", counting_write_atomic)

    cache_path = tmp_path / "mesh.json"
    mesh.resolve_many(["cond one", "cond two", "cond three"], cache_path=cache_path)
    assert len(write_calls) == 1


# ---------------------------------------------------------------------------
# mesh.resolve_many
# ---------------------------------------------------------------------------


def test_mesh_resolve_many_returns_one_entry_per_label(tmp_path, monkeypatch):
    def fake(url, *, params=None, **_kwargs):
        if params["match"] == "exact" and params["label"] == "epilepsy":
            return [
                {"resource": "http://id.nlm.nih.gov/mesh/D004827", "label": "Epilepsy"}
            ]
        return []

    monkeypatch.setattr(mesh.http, "get_json", fake)
    result = mesh.resolve_many(
        ["epilepsy", "nonexistent xyz"], cache_path=tmp_path / "mesh.json"
    )
    assert result == {"epilepsy": "D004827", "nonexistent xyz": None}


# ---------------------------------------------------------------------------
# mesh: loading a real fixture cache file
# ---------------------------------------------------------------------------


def test_mesh_resolve_loads_fixture_cache_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "mesh.json"
    shutil.copy(FIXTURES / "mesh.json", cache_path)
    monkeypatch.setattr(mesh.http, "get_json", _boom)

    assert mesh.resolve("epilepsy", cache_path=cache_path) == "D004827"
    assert mesh.resolve("some unresolved condition", cache_path=cache_path) is None


# ---------------------------------------------------------------------------
# ror.resolve: cache hit / offline short-circuits (no network)
# ---------------------------------------------------------------------------


def test_ror_resolve_cached_name_returns_without_network(tmp_path, monkeypatch):
    cache_path = tmp_path / "ror.json"
    cached_value = {
        "ror_id": "https://ror.org/02mhbdp94",
        "name": "Example University",
        "country": "US",
    }
    io.write_atomic(cache_path, io.pretty_json({"example university": cached_value}))
    monkeypatch.setattr(ror.http, "get_json", _boom)

    assert ror.resolve("Example University", cache_path=cache_path) == cached_value


def test_ror_resolve_uncached_explicit_offline_returns_none(tmp_path, monkeypatch):
    cache_path = tmp_path / "ror.json"
    monkeypatch.setattr(ror.http, "get_json", _boom)

    assert (
        ror.resolve("Some New University", cache_path=cache_path, offline=True) is None
    )
    assert not cache_path.exists()


def test_ror_resolve_uncached_global_offline_returns_none_without_crash(tmp_path):
    assert config.OFFLINE is True
    cache_path = tmp_path / "ror.json"

    assert ror.resolve("Some Other New University", cache_path=cache_path) is None
    assert not cache_path.exists()


# ---------------------------------------------------------------------------
# ror.resolve: chosen + score threshold, country extraction
# ---------------------------------------------------------------------------


def _ror_response(*, chosen, score, country_code="US"):
    return {
        "items": [
            {
                "organization": {
                    "id": "https://ror.org/02mhbdp94",
                    "name": "Example University",
                    "locations": [{"geonames_details": {"country_code": country_code}}],
                },
                "score": score,
                "chosen": chosen,
            }
        ],
    }


def test_ror_resolve_accepts_chosen_and_high_score(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ror.http, "get_json", lambda *a, **kw: _ror_response(chosen=True, score=1.0)
    )
    result = ror.resolve("Example University", cache_path=tmp_path / "ror.json")
    assert result == {
        "ror_id": "https://ror.org/02mhbdp94",
        "name": "Example University",
        "country": "US",
    }


def test_ror_resolve_accepts_score_exactly_at_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ror.http, "get_json", lambda *a, **kw: _ror_response(chosen=True, score=0.9)
    )
    result = ror.resolve("Example University", cache_path=tmp_path / "ror.json")
    assert result is not None
    assert result["ror_id"] == "https://ror.org/02mhbdp94"


def test_ror_resolve_rejects_score_below_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ror.http, "get_json", lambda *a, **kw: _ror_response(chosen=True, score=0.89)
    )
    assert ror.resolve("Example University", cache_path=tmp_path / "ror.json") is None


def test_ror_resolve_rejects_not_chosen(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ror.http, "get_json", lambda *a, **kw: _ror_response(chosen=False, score=1.0)
    )
    assert ror.resolve("Example University", cache_path=tmp_path / "ror.json") is None


def test_ror_resolve_empty_items_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ror.http, "get_json", lambda *a, **kw: {"items": []})
    assert ror.resolve("Nobody's University", cache_path=tmp_path / "ror.json") is None


def test_ror_resolve_missing_country_is_none(tmp_path, monkeypatch):
    response = _ror_response(chosen=True, score=1.0)
    response["items"][0]["organization"]["locations"] = []
    monkeypatch.setattr(ror.http, "get_json", lambda *a, **kw: response)

    result = ror.resolve("Example University", cache_path=tmp_path / "ror.json")
    assert result["country"] is None


# ---------------------------------------------------------------------------
# ror.resolve: negative caching
# ---------------------------------------------------------------------------


def test_ror_resolve_negative_result_cached_as_null_then_served_from_cache(
    tmp_path, monkeypatch
):
    cache_path = tmp_path / "ror.json"
    calls = []

    def fake(url, *, params=None, **_kwargs):
        calls.append(params)
        return {"items": []}

    monkeypatch.setattr(ror.http, "get_json", fake)
    name = "Totally Unknown Institution"

    assert ror.resolve(name, cache_path=cache_path) is None
    assert len(calls) == 1

    on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert on_disk == {name.strip().lower(): None}

    calls.clear()
    assert ror.resolve(name, cache_path=cache_path) is None
    assert calls == []


# ---------------------------------------------------------------------------
# ror: cache file format, and rewritten only when it actually changes
# ---------------------------------------------------------------------------


def test_ror_cache_file_is_pretty_json_with_sorted_keys(tmp_path, monkeypatch):
    def fake(url, *, params=None, **_kwargs):
        name = params["affiliation"]
        if name == "B University":
            return _ror_response(chosen=True, score=1.0, country_code="CA")
        if name == "A University":
            return _ror_response(chosen=True, score=1.0, country_code="US")
        return {"items": []}

    monkeypatch.setattr(ror.http, "get_json", fake)
    cache_path = tmp_path / "ror.json"

    ror.resolve_many(["B University", "A University"], cache_path=cache_path)

    on_disk_text = cache_path.read_text(encoding="utf-8")
    cache = json.loads(on_disk_text)
    assert list(cache.keys()) == sorted(cache.keys())
    assert on_disk_text == io.pretty_json(cache)


def test_ror_cache_not_rewritten_when_nothing_changes(tmp_path, monkeypatch):
    cache_path = tmp_path / "ror.json"
    value = {
        "ror_id": "https://ror.org/02mhbdp94",
        "name": "Example University",
        "country": "US",
    }
    io.write_atomic(cache_path, io.pretty_json({"example university": value}))
    before = cache_path.read_bytes()

    monkeypatch.setattr(ror.http, "get_json", _boom)
    result = ror.resolve_many(
        ["Example University", "EXAMPLE UNIVERSITY"], cache_path=cache_path
    )
    assert result == {"Example University": value, "EXAMPLE UNIVERSITY": value}
    assert cache_path.read_bytes() == before


def test_ror_resolve_many_writes_cache_at_most_once_per_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(ror.http, "get_json", lambda *a, **kw: {"items": []})
    write_calls = []
    real_write_atomic = ror.io.write_atomic

    def counting_write_atomic(path, text):
        write_calls.append(path)
        real_write_atomic(path, text)

    monkeypatch.setattr(ror.io, "write_atomic", counting_write_atomic)

    cache_path = tmp_path / "ror.json"
    ror.resolve_many(["Org One", "Org Two", "Org Three"], cache_path=cache_path)
    assert len(write_calls) == 1


# ---------------------------------------------------------------------------
# ror.resolve_many
# ---------------------------------------------------------------------------


def test_ror_resolve_many_returns_one_entry_per_name(tmp_path, monkeypatch):
    def fake(url, *, params=None, **_kwargs):
        if params["affiliation"] == "Example University":
            return _ror_response(chosen=True, score=1.0)
        return {"items": []}

    monkeypatch.setattr(ror.http, "get_json", fake)
    result = ror.resolve_many(
        ["Example University", "Nonexistent Org"], cache_path=tmp_path / "ror.json"
    )
    assert result["Nonexistent Org"] is None
    assert result["Example University"]["ror_id"] == "https://ror.org/02mhbdp94"


# ---------------------------------------------------------------------------
# ror: loading a real fixture cache file
# ---------------------------------------------------------------------------


def test_ror_resolve_loads_fixture_cache_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "ror.json"
    shutil.copy(FIXTURES / "ror.json", cache_path)
    monkeypatch.setattr(ror.http, "get_json", _boom)

    result = ror.resolve("example university", cache_path=cache_path)
    assert result == {
        "ror_id": "https://ror.org/02mhbdp94",
        "name": "Example University",
        "country": "US",
    }
    assert ror.resolve("some unresolved institution", cache_path=cache_path) is None


# ---------------------------------------------------------------------------
# Both modules never raise, even on a genuine http.OfflineError mid-lookup
# ---------------------------------------------------------------------------


def test_mesh_resolve_never_raises_when_get_json_raises_offline_error(
    tmp_path, monkeypatch
):
    def fake(*_a, **_kw):
        raise http.OfflineError("nope")

    monkeypatch.setattr(mesh.http, "get_json", fake)
    cache_path = tmp_path / "mesh.json"
    assert mesh.resolve("whatever condition", cache_path=cache_path) is None
    assert not cache_path.exists()


def test_ror_resolve_never_raises_when_get_json_raises_offline_error(
    tmp_path, monkeypatch
):
    def fake(*_a, **_kw):
        raise http.OfflineError("nope")

    monkeypatch.setattr(ror.http, "get_json", fake)
    cache_path = tmp_path / "ror.json"
    assert ror.resolve("Whatever University", cache_path=cache_path) is None
    assert not cache_path.exists()


# ---------------------------------------------------------------------------
# Both modules: a get_json failure (returns None) is not cached as negative
# ---------------------------------------------------------------------------


def test_mesh_resolve_network_failure_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(mesh.http, "get_json", lambda *a, **kw: None)
    cache_path = tmp_path / "mesh.json"
    assert mesh.resolve("flaky condition", cache_path=cache_path) is None
    assert not cache_path.exists()


def test_ror_resolve_network_failure_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(ror.http, "get_json", lambda *a, **kw: None)
    cache_path = tmp_path / "ror.json"
    assert ror.resolve("Flaky University", cache_path=cache_path) is None
    assert not cache_path.exists()


# ---------------------------------------------------------------------------
# Sanity: our fixtures parse and pytest can just load them directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mesh.json", "ror.json"])
def test_fixture_caches_have_exactly_two_entries(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert len(data) == 2
