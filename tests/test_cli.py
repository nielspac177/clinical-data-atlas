"""Tests for the atlas CLI subcommands: `harvest` and `normalize` (Task
0.5), and `enrich`, `graph`, `diff`, `validate`, `refresh`, `check-urls`
and `dod` (Task 2.7).

`schema` and the no-args help path are already covered in
tests/test_schema.py; `atlas.refresh`'s own behaviour is covered in
tests/test_refresh.py -- what matters here is the *wiring*: which files
each subcommand reads and writes, what it prints, and its exit code.

Nothing here touches the network or the real `data/` tree. Harvesters are
fakes injected via monkeypatching `atlas.harvest.get_registry`, and the
module-wide `_isolate_data_tree` fixture points every `atlas.config` path
at `tmp_path` so a subcommand that writes can only ever write there.
"""

from __future__ import annotations

import json

import pytest

from atlas import cli, config, harvest, http, io, normalize, refresh, schema
from atlas.harvest.base import Harvester, HarvestResult, RawStore
from atlas.normalize import common as normalize_common


@pytest.fixture(autouse=True)
def _isolate_data_tree(monkeypatch, tmp_path):
    """No test in this module may touch the repo's real `data/` tree.

    Every subcommand here writes somewhere under `atlas.config`'s path
    constants, so pointing all of them at `tmp_path` for the whole module
    means a test that forgets to isolate itself still cannot clobber the
    committed catalog, graph or changelog -- `RawStore` included, since it
    resolves `config.RAW` per construction rather than at import time, and
    `ROOT` too, so `.cache/` and `_site/` land in `tmp_path` as well.
    Tests that need to *see* the tree use `tree` below, which resolves the
    same paths.

    This is not hypothetical: an earlier version of this module ran
    `cli.main(["refresh"])` for real and rewrote all four `data/raw/*/
    manifest.json` files with the offline harvest failures it produced.
    """
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "RAW", tmp_path / "data" / "raw")
    monkeypatch.setattr(config, "CATALOG", tmp_path / "data" / "catalog")
    monkeypatch.setattr(config, "GRAPH", tmp_path / "data" / "graph")
    monkeypatch.setattr(config, "CHANGELOG", tmp_path / "data" / "changelog")


@pytest.fixture
def tree():
    """The isolated tree's resolved paths."""
    return refresh.paths()


# ---------------------------------------------------------------------------
# harvest: fake Harvester machinery
# ---------------------------------------------------------------------------


def _make_fake_harvester(
    *,
    name: str,
    probe_error: Exception | None = None,
    harvest_result: HarvestResult | None = None,
    harvest_error: Exception | None = None,
) -> type[Harvester]:
    """A fresh `Harvester` subclass, isolated per call (its own `calls`
    list), that never touches RawStore, the filesystem, or the network.

    Built via `type(...)` rather than a `class` statement so `probe`/
    `harvest` land in the class namespace at creation time -- `ABCMeta`
    freezes `__abstractmethods__` right then, so assigning them onto the
    class *afterward* (e.g. `Cls.probe = probe`) would leave the class
    still abstract and uninstantiable.
    """
    calls: list[tuple[bool, int | None]] = []

    def probe(self) -> dict:
        if probe_error is not None:
            raise probe_error
        return {}

    def do_harvest(
        self, *, fast: bool = False, limit: int | None = None
    ) -> HarvestResult:
        calls.append((fast, limit))
        if harvest_error is not None:
            raise harvest_error
        return harvest_result or HarvestResult(source=name, status="ok")

    return type(
        "_FakeHarvester",
        (Harvester,),
        {
            "name": name,
            "harvest_method": "api",
            "calls": calls,
            "probe": probe,
            "harvest": do_harvest,
        },
    )


# ---------------------------------------------------------------------------
# harvest: no sources registered
# ---------------------------------------------------------------------------


def test_harvest_no_sources_registered_prints_message_and_returns_0(
    monkeypatch, capsys
):
    monkeypatch.setattr(harvest, "get_registry", dict)
    assert cli.main(["harvest"]) == 0
    assert capsys.readouterr().out.strip() == "no sources registered"


def test_harvest_source_flag_on_empty_registry_still_prints_no_sources(
    monkeypatch, capsys
):
    monkeypatch.setattr(harvest, "get_registry", dict)
    assert cli.main(["harvest", "--source", "anything"]) == 0
    assert capsys.readouterr().out.strip() == "no sources registered"


def test_harvest_unknown_source_prints_available_and_returns_2(monkeypatch, capsys):
    a = _make_fake_harvester(name="a")
    b = _make_fake_harvester(name="b")
    monkeypatch.setattr(harvest, "get_registry", lambda: {"b": b, "a": a})

    assert cli.main(["harvest", "--source", "does-not-exist"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "unknown source 'does-not-exist' (available: a, b)"


def test_harvest_probe_unknown_source_prints_available_and_returns_2(
    monkeypatch, capsys
):
    a = _make_fake_harvester(name="a")
    monkeypatch.setattr(harvest, "get_registry", lambda: {"a": a})

    assert cli.main(["harvest", "--probe", "--source", "does-not-exist"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "unknown source 'does-not-exist' (available: a)"


# ---------------------------------------------------------------------------
# harvest --probe
# ---------------------------------------------------------------------------


def test_harvest_probe_all_ok_prints_sorted_lines_and_returns_0(monkeypatch, capsys):
    a = _make_fake_harvester(name="a")
    b = _make_fake_harvester(name="b")
    monkeypatch.setattr(harvest, "get_registry", lambda: {"b": b, "a": a})

    assert cli.main(["harvest", "--probe"]) == 0

    assert capsys.readouterr().out.strip().splitlines() == ["a: ok", "b: ok"]


def test_harvest_probe_reports_failure_and_returns_1(monkeypatch, capsys):
    ok = _make_fake_harvester(name="ok_source")
    bad = _make_fake_harvester(
        name="bad_source", probe_error=AssertionError("missing field 'id'")
    )
    monkeypatch.setattr(
        harvest, "get_registry", lambda: {"ok_source": ok, "bad_source": bad}
    )

    assert cli.main(["harvest", "--probe"]) == 1

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == ["bad_source: FAILED missing field 'id'", "ok_source: ok"]


def test_harvest_probe_source_filter_narrows_to_one(monkeypatch, capsys):
    a = _make_fake_harvester(name="a")
    b = _make_fake_harvester(name="b")
    monkeypatch.setattr(harvest, "get_registry", lambda: {"a": a, "b": b})

    assert cli.main(["harvest", "--probe", "--source", "a"]) == 0

    assert capsys.readouterr().out.strip() == "a: ok"


# ---------------------------------------------------------------------------
# harvest (plain)
# ---------------------------------------------------------------------------


def test_harvest_plain_run_forwards_fast_and_limit_and_prints_summary(
    monkeypatch, capsys
):
    result = HarvestResult(
        source="fake",
        status="ok",
        listed=10,
        written=3,
        unchanged=7,
        removed=0,
        seconds=1.23,
    )
    fake = _make_fake_harvester(name="fake", harvest_result=result)
    monkeypatch.setattr(harvest, "get_registry", lambda: {"fake": fake})

    exit_code = cli.main(["harvest", "--fast", "--limit", "5"])

    assert exit_code == 0
    assert fake.calls == [(True, 5)]
    assert (
        capsys.readouterr().out.strip()
        == "fake: ok listed=10 written=3 unchanged=7 removed=0 seconds=1.23"
    )


def test_harvest_exception_in_one_source_does_not_abort_others(monkeypatch, capsys):
    ok = _make_fake_harvester(
        name="ok_source",
        harvest_result=HarvestResult(
            source="ok_source", status="ok", listed=1, written=1
        ),
    )
    bad = _make_fake_harvester(
        name="bad_source", harvest_error=RuntimeError("connection reset")
    )
    monkeypatch.setattr(
        harvest, "get_registry", lambda: {"ok_source": ok, "bad_source": bad}
    )

    exit_code = cli.main(["harvest"])

    assert exit_code == 0  # not every source failed
    out = capsys.readouterr().out
    assert (
        "bad_source: failed listed=0 written=0 unchanged=0 removed=0 "
        "seconds=0.00 error=connection reset" in out
    )
    assert "ok_source: ok listed=1 written=1 unchanged=0 removed=0 seconds=0.00" in out


def test_harvest_all_sources_failed_returns_3(monkeypatch, capsys):
    bad1 = _make_fake_harvester(name="bad1", harvest_error=RuntimeError("boom1"))
    bad2 = _make_fake_harvester(
        name="bad2",
        harvest_result=HarvestResult(source="bad2", status="failed", error="boom2"),
    )
    monkeypatch.setattr(harvest, "get_registry", lambda: {"bad1": bad1, "bad2": bad2})

    assert cli.main(["harvest"]) == 3


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_no_normalizers_registered_prints_message_and_returns_0(
    monkeypatch, capsys
):
    monkeypatch.setattr(normalize, "get_normalizers", dict)
    assert cli.main(["normalize"]) == 0
    assert capsys.readouterr().out.strip() == "no normalizers registered"


def test_normalize_source_flag_on_empty_registry_still_prints_no_normalizers(
    monkeypatch, capsys
):
    monkeypatch.setattr(normalize, "get_normalizers", dict)
    assert cli.main(["normalize", "--source", "anything"]) == 0
    assert capsys.readouterr().out.strip() == "no normalizers registered"


def test_normalize_unknown_source_prints_available_and_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {
            "curated": (lambda **kw: None, lambda e: ""),
            "openneuro": (lambda **kw: None, lambda e: ""),
        },
    )
    assert cli.main(["normalize", "--source", "does-not-exist"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err.strip()
        == "unknown source 'does-not-exist' (available: curated, openneuro)"
    )


def _fake_envelope(native_id: str, title: str) -> dict:
    return {
        "source": "curated",
        "native_id": native_id,
        "harvest_method": "api",
        "endpoints": [f"https://example.org/api/{native_id}"],
        "payload": {"title": title},
    }


def _fake_normalize(envelope, *, harvested_at, first_seen):
    native_id = envelope["native_id"]
    if native_id == "bad-1":
        return normalize_common.Excluded(native_id=native_id, reason="wrong species")
    return schema.Record(
        id=f"curated:{native_id}",
        source="curated",
        source_native_id=native_id,
        name=envelope["payload"]["title"],
        summary="A short summary.",
        url=f"https://example.org/{native_id}",
        species="human",
        sample_size=None,
        sample_unit=None,
        years={},
        access="open",
        record_status="active",
        provenance=normalize_common.make_provenance(
            via="api", harvested_at=harvested_at, first_seen=first_seen, raw_hash=None
        ),
    )


def _fake_enrichment_text(envelope: dict) -> str:
    return envelope["payload"]["title"]


def _make_fake_rawstore_cls(envelopes: dict, manifest: dict | None):
    """A stand-in for `atlas.harvest.base.RawStore`, injected via
    monkeypatching `atlas.cli.RawStore` -- serves a canned manifest
    (`None` simulates a source that was never harvested) and canned
    envelopes without ever touching `data/raw/`."""

    class _FakeRawStore:
        def __init__(self, source: str) -> None:
            self.source = source

        def manifest(self) -> dict | None:
            return manifest

        def load_all(self) -> dict:
            return dict(envelopes)

    return _FakeRawStore


def test_normalize_writes_sorted_records_and_excluded_jsonl(
    monkeypatch, tmp_path, capsys
):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setattr(config, "CATALOG", catalog_dir)

    # Deliberately inserted out of id order, to prove the CLI sorts the
    # output rather than preserving dict/manifest iteration order.
    envelopes = {
        "good-2": _fake_envelope("good-2", "Second Dataset"),
        "bad-1": _fake_envelope("bad-1", "Excluded Dataset"),
        "good-1": _fake_envelope("good-1", "First Dataset"),
    }
    manifest = {
        "source": "curated",
        "harvested_at": "2026-08-20",
        "endpoints": ["https://example.org/api"],
        "status": "ok",
        "error": None,
        "counts": {"listed": 3, "written": 3, "unchanged": 0, "removed": 0},
        "records": {
            "good-2": {"hash": "sha256:aaa", "first_seen": "2026-01-01"},
            "bad-1": {"hash": "sha256:bbb", "first_seen": "2026-02-01"},
            "good-1": {"hash": "sha256:ccc", "first_seen": "2026-03-01"},
        },
    }
    monkeypatch.setattr(cli, "RawStore", _make_fake_rawstore_cls(envelopes, manifest))
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {"curated": (_fake_normalize, _fake_enrichment_text)},
    )

    exit_code = cli.main(["normalize", "--source", "curated"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "curated: 2 records, 1 excluded"

    records_path = catalog_dir / "normalized" / "curated.jsonl"
    excluded_path = catalog_dir / "normalized" / "curated.excluded.jsonl"

    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
    ]
    excluded = [
        json.loads(line)
        for line in excluded_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [r["id"] for r in records] == ["curated:good-1", "curated:good-2"]
    good_2 = next(r for r in records if r["id"] == "curated:good-2")
    assert good_2["provenance"]["harvested_at"] == "2026-01-01"  # first_seen
    assert good_2["provenance"]["last_verified"] == "2026-08-20"  # this run
    assert excluded == [{"native_id": "bad-1", "reason": "wrong species"}]


def test_normalize_missing_manifest_is_skipped_not_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "RawStore", _make_fake_rawstore_cls(envelopes={}, manifest=None)
    )
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {"curated": (_fake_normalize, _fake_enrichment_text)},
    )

    exit_code = cli.main(["normalize", "--source", "curated"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "curated: skipped (no raw data)"


def test_normalize_exception_in_one_source_does_not_abort_others(
    monkeypatch, tmp_path, capsys
):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setattr(config, "CATALOG", catalog_dir)

    ok_manifest = {
        "harvested_at": "2026-08-20",
        "records": {"good-1": {"hash": "sha256:aaa", "first_seen": "2026-01-01"}},
    }
    ok_envelopes = {"good-1": _fake_envelope("good-1", "Good Dataset")}

    class _OkStore:
        def __init__(self, source: str) -> None:
            self.source = source

        def manifest(self) -> dict:
            return ok_manifest

        def load_all(self) -> dict:
            return dict(ok_envelopes)

    class _BadStore:
        def __init__(self, source: str) -> None:
            self.source = source

        def manifest(self) -> dict:
            return {"harvested_at": "2026-08-20", "records": {}}

        def load_all(self) -> dict:
            raise RuntimeError("disk on fire")

    def fake_rawstore(source: str):
        return _OkStore(source) if source == "ok_source" else _BadStore(source)

    monkeypatch.setattr(cli, "RawStore", fake_rawstore)
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {
            "ok_source": (_fake_normalize, _fake_enrichment_text),
            "bad_source": (_fake_normalize, _fake_enrichment_text),
        },
    )

    exit_code = cli.main(["normalize"])

    assert exit_code == 0  # not every source failed
    out = capsys.readouterr().out
    assert "bad_source: FAILED RuntimeError: disk on fire" in out
    assert "ok_source: 1 records, 0 excluded" in out

    # the good source's output was still written despite the other's failure
    assert (catalog_dir / "normalized" / "ok_source.jsonl").exists()
    assert not (catalog_dir / "normalized" / "bad_source.jsonl").exists()


def test_normalize_all_sources_failed_returns_3(monkeypatch, capsys):
    class _AlwaysBadStore:
        def __init__(self, source: str) -> None:
            self.source = source

        def manifest(self) -> dict:
            raise RuntimeError("boom")

    monkeypatch.setattr(cli, "RawStore", _AlwaysBadStore)
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {
            "a": (_fake_normalize, _fake_enrichment_text),
            "b": (_fake_normalize, _fake_enrichment_text),
        },
    )

    assert cli.main(["normalize"]) == 3


# ---------------------------------------------------------------------------
# atlas.harvest.base.RawStore.manifest() -- accessor added for _cmd_normalize
# ---------------------------------------------------------------------------


def test_rawstore_manifest_returns_none_before_first_harvest_then_the_manifest(
    tmp_path,
):
    store = RawStore("demo", root=tmp_path)
    assert store.manifest() is None

    store.write("d1", {"title": "Demo"}, harvest_method="api", endpoints=[])
    store.finalize(
        listed_ids={"d1"}, harvested_at="2026-01-01", endpoints=[], status="ok"
    )

    manifest = RawStore("demo", root=tmp_path).manifest()

    assert manifest is not None
    assert manifest["harvested_at"] == "2026-01-01"
    assert manifest["records"]["d1"]["first_seen"] == "2026-01-01"


# ---------------------------------------------------------------------------
# Task 2.7 subcommands: shared helpers
# ---------------------------------------------------------------------------


def _record(**overrides) -> dict:
    """One valid catalog row, as `Record.model_dump(mode="json")` shapes
    it, with any field overridable."""
    record = {
        "id": "openneuro:ds001",
        "source": "openneuro",
        "source_native_id": "ds001",
        "name": "Example dataset",
        "summary": "A short summary of the dataset.",
        "url": "https://example.org/ds001",
        "domains": ["neurology"],
        "modalities": ["MRI"],
        "conditions": [],
        "keywords": [],
        "countries": [],
        "years": {"start": None, "end": None},
        "institutions": [],
        "authors": [],
        "papers": [],
        "related": [],
        "species": "human",
        "sample_size": None,
        "sample_unit": None,
        "access": "open",
        "access_tiers": [],
        "record_status": "active",
        "provenance": {
            "harvested_via": "api",
            "harvested_at": "2026-08-01",
            "last_verified": "2026-08-20",
            "raw_hash": None,
            "enrichment": {
                "method": "rules",
                "model": None,
                "prompt_version": None,
                "at": None,
                "fields": {},
            },
        },
    }
    record.update(overrides)
    return record


def _write_catalog(tree, rows: list[dict]) -> None:
    io.write_jsonl(tree.catalog_file, rows)


def _build_graph_outputs(tree, rows: list[dict]) -> None:
    """Build the real graph outputs for `rows`, the way `atlas graph`
    would -- so the contract checks below run against genuine files."""
    records = [schema.Record.model_validate(row) for row in rows]
    graph, index, stats = refresh.stage_graph(records)
    from atlas.graph import build as graph_build

    graph_build.write_outputs(graph, index, stats, out_dir=tree.graph)


def _squeezed(text: str) -> str:
    """`text` with every run of spaces collapsed, so an assertion about a
    padded table row doesn't depend on the width of its widest column."""
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------


def test_enrich_without_any_input_returns_1(capsys):
    assert cli.main(["enrich"]) == 1
    assert "nothing to enrich" in capsys.readouterr().err


def test_enrich_help_names_the_file_it_actually_writes():
    """Both the subcommand help and the docstring used to promise
    `data/catalog/enriched.jsonl` -- a file this command has never
    written, and must not: the enriched records are a cache artefact,
    which is why they go to the gitignored `.cache/` instead."""
    help_text = " ".join(cli.build_parser().format_help().split())

    assert ".cache/enriched.jsonl" in help_text
    assert "data/catalog/enriched.jsonl" not in help_text
    assert "data/catalog/enriched.jsonl" not in (cli._cmd_enrich.__doc__ or "")


def test_enrich_reads_normalized_records_and_writes_enriched_jsonl(
    tree, monkeypatch, capsys
):
    io.write_jsonl(
        tree.catalog / "normalized" / "openneuro.jsonl",
        [
            _record(
                id="openneuro:ds002",
                source_native_id="ds002",
                domains=[],
                modalities=[],
            ),
            _record(
                id="openneuro:ds001",
                source_native_id="ds001",
                domains=[],
                modalities=[],
            ),
        ],
    )
    io.write_jsonl(
        tree.catalog / "normalized" / "openneuro.excluded.jsonl",
        [{"native_id": "ds999", "reason": "not a dataset"}],
    )
    # One raw envelope, so one of the two records has an enrichment text.
    store = RawStore("openneuro", root=tree.raw)
    store.write(
        "ds001",
        {"text": "An EEG study of epilepsy."},
        harvest_method="api",
        endpoints=[],
    )
    store.finalize(
        listed_ids={"ds001"}, harvested_at="2026-08-20", endpoints=[], status="ok"
    )
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {
            "openneuro": (
                lambda envelope, **kw: None,
                lambda envelope: envelope["payload"]["text"],
            )
        },
    )

    assert cli.main(["enrich", "--llm", "none"]) == 0

    rows = io.read_jsonl(tree.cache / "enriched.jsonl")
    assert [row["id"] for row in rows] == ["openneuro:ds001", "openneuro:ds002"]
    assert rows[0]["modalities"] == ["EEG"]  # rules ran on the enrichment text
    assert rows[1]["modalities"] == []  # no text: left alone, never guessed
    out = capsys.readouterr().out
    assert "enriching 2 records" in out
    assert "1 record(s) have no enrichment text" in out
    assert "backend=none" in out


def test_enrich_falls_back_to_the_catalog_when_nothing_is_normalized(tree, capsys):
    _write_catalog(tree, [_record()])

    assert cli.main(["enrich", "--llm", "none"]) == 0
    assert str(tree.catalog_file) in capsys.readouterr().out
    assert (tree.cache / "enriched.jsonl").exists()
    # Never under data/: `.cache/` is gitignored, so a debugging artifact
    # cannot be swept into a data commit.
    assert not (tree.catalog / "enriched.jsonl").exists()


def test_enrich_never_writes_the_catalog(tree):
    """Half a pipeline must not be able to produce `catalog.jsonl` -- that
    file is `refresh`'s output, written only after dedupe and
    validation."""
    _write_catalog(tree, [_record()])
    before = tree.catalog_file.read_bytes()

    assert cli.main(["enrich", "--llm", "none"]) == 0
    assert tree.catalog_file.read_bytes() == before


def test_enrich_llm_flag_accepts_documented_choices():
    # Exit 1 == "nothing to enrich" (the tree is empty), i.e. the flag and
    # its value parsed fine and the command got as far as looking for input.
    assert cli.main(["enrich", "--llm", "claude_cli", "--max-llm-calls", "5"]) == 1


def test_enrich_llm_flag_rejects_invalid_choice():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["enrich", "--llm", "bogus"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def test_graph_without_a_catalog_returns_1(tree, capsys):
    assert cli.main(["graph"]) == 1
    assert "no catalog at" in capsys.readouterr().err


def test_graph_writes_the_three_outputs(tree, capsys):
    _write_catalog(tree, [_record()])

    assert cli.main(["graph"]) == 0

    _graph, index, stats = refresh.read_graph_outputs(tree.graph)
    assert stats["record_count"] == 1
    assert [row["id"] for row in index] == ["openneuro:ds001"]
    assert "1 records" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_without_a_catalog_returns_1(tree, capsys):
    assert cli.main(["diff"]) == 1
    assert "no catalog at" in capsys.readouterr().err


def test_diff_writes_a_changelog_against_head(tree, monkeypatch, capsys):
    _write_catalog(tree, [_record()])
    monkeypatch.setattr(refresh, "head_catalog", lambda *a, **kw: [])
    monkeypatch.setattr(harvest, "get_registry", dict)
    monkeypatch.setattr(
        normalize, "get_normalizers", lambda: {"openneuro": (None, None)}
    )

    assert cli.main(["diff"]) == 0

    latest = (tree.changelog / "latest.md").read_text(encoding="utf-8")
    assert "## Added (1)" in latest
    assert "openneuro" in latest  # the Sources table row, from the raw manifest
    assert "+1 new, ~0 changed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_without_a_catalog_returns_1(tree, capsys):
    assert cli.main(["validate"]) == 1
    assert "no catalog at" in capsys.readouterr().err


def test_validate_passes_on_a_catalog_and_matching_graph(tree, capsys):
    rows = [_record()]
    _write_catalog(tree, rows)
    _build_graph_outputs(tree, rows)

    assert cli.main(["validate", "--strict"]) == 0
    assert "1 records, 0 error(s), 0 warning(s)" in capsys.readouterr().out


def test_validate_reports_schema_errors_and_returns_1(tree, capsys):
    _write_catalog(tree, [_record(species="martian")])

    assert cli.main(["validate"]) == 1
    captured = capsys.readouterr()
    assert "error: openneuro:ds001 (species)" in captured.err
    assert "1 error(s)" in captured.out


def test_validate_counts_warnings_without_failing(tree, capsys):
    rows = [_record(domains=[], modalities=[])]
    _write_catalog(tree, rows)
    _build_graph_outputs(tree, rows)

    assert cli.main(["validate"]) == 0
    assert "0 error(s), 2 warning(s)" in capsys.readouterr().out


def test_validate_catches_a_graph_that_disagrees_with_the_catalog(tree, capsys):
    rows = [_record()]
    _write_catalog(tree, rows)
    _build_graph_outputs(tree, rows)
    # The catalog gains a record the graph has never heard of.
    _write_catalog(
        tree, [*rows, _record(id="openneuro:ds002", source_native_id="ds002")]
    )

    assert cli.main(["validate"]) == 1
    err = capsys.readouterr().err
    assert "search-index.json: 1 catalog id(s) missing: openneuro:ds002" in err
    assert "stats.json: record_count" in err


def test_validate_strict_requires_the_graph_to_exist(tree, capsys):
    _write_catalog(tree, [_record()])

    assert cli.main(["validate"]) == 0
    assert "graph outputs not built" in capsys.readouterr().out

    assert cli.main(["validate", "--strict"]) == 1
    assert "graph outputs not built" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_refresh_forwards_every_flag_to_refresh_run(monkeypatch, tmp_path):
    seen = {}

    def fake_run(sources, **kwargs):
        seen["sources"] = sources
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(refresh, "run", fake_run)

    exit_code = cli.main(
        [
            "refresh",
            "--sources",
            "openneuro, physionet",
            "--skip-enrich",
            "--offline",
            "--fast",
            "--strict",
            "--dry-run",
            "--llm",
            "claude_cli",
            "--max-llm-calls",
            "7",
            "--report",
            str(tmp_path / "report.md"),
            "--summary-json",
            str(tmp_path / "summary.json"),
        ]
    )

    assert exit_code == 0
    assert seen["sources"] == ["openneuro", "physionet"]
    assert seen["skip_enrich"] is True
    assert seen["offline"] is True
    assert seen["fast"] is True
    assert seen["strict"] is True
    assert seen["dry_run"] is True
    assert seen["llm"] == "claude_cli"
    assert seen["max_llm_calls"] == 7
    assert seen["report"] == tmp_path / "report.md"
    assert seen["summary_json"] == tmp_path / "summary.json"


def test_refresh_defaults_are_all_off(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        refresh, "run", lambda sources, **kwargs: seen.update(kwargs) or 0
    )

    assert cli.main(["refresh"]) == 0
    assert seen == {
        "skip_enrich": False,
        "offline": False,
        "fast": False,
        "strict": False,
        "dry_run": False,
        "llm": None,
        "max_llm_calls": None,
        "report": None,
        "summary_json": None,
    }


def test_refresh_propagates_the_exit_code(monkeypatch):
    monkeypatch.setattr(refresh, "run", lambda sources, **kwargs: 3)
    assert cli.main(["refresh"]) == 3


# ---------------------------------------------------------------------------
# check-urls
# ---------------------------------------------------------------------------


def test_check_urls_offline_checks_nothing_and_exits_0(tree, capsys):
    _write_catalog(tree, [_record()])
    assert cli.main(["check-urls", "--sample", "5", "--seed", "0", "--offline"]) == 0
    assert "offline, nothing checked" in capsys.readouterr().out


def test_check_urls_reports_a_status_table_and_the_failures(tree, monkeypatch, capsys):
    monkeypatch.setattr(config, "OFFLINE", False)
    rows = [
        _record(id=f"openneuro:ds00{n}", source_native_id=f"ds00{n}", url=url)
        for n, url in enumerate(
            [
                "https://example.org/a",
                "https://example.org/b",
                "https://example.org/gone",
                "https://example.org/d",
            ]
        )
    ]
    _write_catalog(tree, rows)
    statuses = {"https://example.org/gone": 404}
    checked: list[str] = []

    def fake_head_status(url, timeout=20):
        checked.append(url)
        return statuses.get(url, 200), url

    monkeypatch.setattr(http, "head_status", fake_head_status)

    assert cli.main(["check-urls", "--sample", "4", "--seed", "0"]) == 0

    assert sorted(checked) == sorted(row["url"] for row in rows)
    out = _squeezed(capsys.readouterr().out)
    assert "checked 4 of 4 urls (seed 0)" in out
    assert "200 3" in out
    assert "404 1" in out
    assert "1 failing url(s):" in out
    assert "404 https://example.org/gone" in out


def test_check_urls_sample_is_seeded_and_reproducible(tree, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    _write_catalog(
        tree,
        [
            _record(
                id=f"openneuro:ds{n:03d}",
                source_native_id=f"ds{n:03d}",
                url=f"https://example.org/{n}",
            )
            for n in range(20)
        ],
    )
    seen: list[list[str]] = []

    def fake_head_status(url, timeout=20):
        seen[-1].append(url)
        return 200, url

    monkeypatch.setattr(http, "head_status", fake_head_status)

    for _ in range(2):
        seen.append([])
        assert cli.main(["check-urls", "--sample", "5", "--seed", "42"]) == 0
    seen.append([])
    assert cli.main(["check-urls", "--sample", "5", "--seed", "7"]) == 0

    assert len(seen[0]) == 5
    assert seen[0] == seen[1]
    assert seen[2] != seen[0]


def test_check_urls_unreachable_is_reported_not_fatal(tree, monkeypatch, capsys):
    monkeypatch.setattr(config, "OFFLINE", False)
    _write_catalog(tree, [_record()])
    monkeypatch.setattr(http, "head_status", lambda url, timeout=20: (0, url))

    assert cli.main(["check-urls", "--sample", "1", "--seed", "0"]) == 0
    out = _squeezed(capsys.readouterr().out)
    assert "unreachable 1" in out
    assert "0 https://example.org/ds001" in out


# ---------------------------------------------------------------------------
# dod
# ---------------------------------------------------------------------------


def _pass_every_local_gate(tree, monkeypatch, tmp_path, *, records: int = 2_400):
    """Build the tree a green Phase-0 `dod` run expects: enough records, a
    matching graph, a changelog, an ok manifest, and a built site."""
    rows = [
        _record(
            id=f"openneuro:ds{n:05d}",
            source_native_id=f"ds{n:05d}",
            url=f"https://example.org/ds{n:05d}",
        )
        for n in range(records)
    ]
    _write_catalog(tree, rows)
    _build_graph_outputs(tree, rows)
    io.write_atomic(tree.changelog / "latest.md", "# Refresh\n")
    store = RawStore("openneuro", root=tree.raw)
    store.write("ds00000", {"a": 1}, harvest_method="api", endpoints=[])
    store.finalize(
        listed_ids={"ds00000"}, harvested_at="2026-08-20", endpoints=[], status="ok"
    )
    monkeypatch.setattr(harvest, "get_registry", dict)
    monkeypatch.setattr(
        normalize, "get_normalizers", lambda: {"openneuro": (None, None)}
    )
    io.write_atomic(tmp_path / "_site" / "index.html", "<!doctype html>")
    return rows


def test_dod_unknown_phase_returns_2(capsys):
    assert cli.main(["dod", "--phase", "9", "--url", "https://example.org/"]) == 2
    assert "no definition-of-done defined for phase 9" in capsys.readouterr().err


def test_dod_all_local_gates_pass(tree, monkeypatch, tmp_path, capsys):
    _pass_every_local_gate(tree, monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(http, "head_status", lambda url, timeout=20: (200, url))

    assert cli.main(["dod", "--phase", "0", "--url", "https://example.org/site/"]) == 0

    out = _squeezed(capsys.readouterr().out)
    assert "catalog >= 2400 records pass 2400 records" in out
    assert "validate: 0 errors pass" in out
    assert "graph contract pass" in out
    assert "every source harvested ok pass 1/1 ok" in out
    assert "site built pass" in out
    assert "site url 200 pass 200 https://example.org/site/" in out
    assert "e2e suite green n/a CI" in out
    assert "url sample 2xx/3xx pass 20/20 ok (seed 0)" in out
    assert out.strip().endswith("phase 0: 10 pass, 3 n/a, 0 FAIL")


def test_dod_checks_the_deployed_url_and_its_stats_json(tree, monkeypatch, tmp_path):
    _pass_every_local_gate(tree, monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OFFLINE", False)
    checked: list[str] = []

    def fake_head_status(url, timeout=20):
        checked.append(url)
        return 200, url

    monkeypatch.setattr(http, "head_status", fake_head_status)

    assert (
        cli.main(
            [
                "dod",
                "--phase",
                "0",
                "--url",
                "https://example.org/site/",
                "--sample",
                "3",
            ]
        )
        == 0
    )
    assert checked[:2] == [
        "https://example.org/site/",
        "https://example.org/site/data/stats.json",
    ]
    # …then a seeded sample of the catalog's own urls.
    assert len(checked) == 5
    assert all(url.startswith("https://example.org/ds") for url in checked[2:])


def test_dod_offline_marks_the_url_gates_not_applicable(
    tree, monkeypatch, tmp_path, capsys
):
    _pass_every_local_gate(tree, monkeypatch, tmp_path)

    def explode(url, timeout=20):  # pragma: no cover -- must never be called
        raise AssertionError("dod --offline must not touch the network")

    monkeypatch.setattr(http, "head_status", explode)

    assert (
        cli.main(
            ["dod", "--phase", "0", "--url", "https://example.org/site/", "--offline"]
        )
        == 0
    )
    out = _squeezed(capsys.readouterr().out)
    assert "site url 200 n/a offline" in out
    assert "data/stats.json 200 n/a offline" in out


def test_dod_fails_on_a_short_catalog_and_a_missing_site(
    tree, monkeypatch, tmp_path, capsys
):
    _pass_every_local_gate(tree, monkeypatch, tmp_path, records=3)
    (tmp_path / "_site" / "index.html").unlink()

    assert (
        cli.main(["dod", "--phase", "0", "--url", "https://example.org/", "--offline"])
        == 1
    )
    out = _squeezed(capsys.readouterr().out)
    assert "catalog >= 2400 records FAIL 3 records" in out
    assert "site built FAIL" in out
    assert "2 FAIL" in out


def test_dod_without_any_data_fails_loudly(tree, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(harvest, "get_registry", dict)
    monkeypatch.setattr(normalize, "get_normalizers", dict)

    assert (
        cli.main(["dod", "--phase", "0", "--url", "https://example.org/", "--offline"])
        == 1
    )
    out = _squeezed(capsys.readouterr().out)
    assert "catalog exists FAIL" in out
    assert "graph contract FAIL" in out
    assert "every source harvested ok FAIL 0/0 ok" in out


# ---------------------------------------------------------------------------
# data-tree isolation
# ---------------------------------------------------------------------------


def test_rawstore_follows_a_monkeypatched_config_raw(tmp_path, monkeypatch):
    """`RawStore(source)` must resolve `config.RAW` when it is
    constructed, not when `atlas.harvest.base` was imported -- otherwise
    the isolation fixture above is decorative and a subcommand that
    harvests writes into the repo's real `data/raw/`.
    """
    monkeypatch.setattr(config, "RAW", tmp_path / "elsewhere")
    assert RawStore("openneuro").root == tmp_path / "elsewhere" / "openneuro"


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------


def test_check_urls_requires_sample_and_seed():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["check-urls"])
    assert exc_info.value.code == 2


def test_dod_requires_phase_and_url():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["dod"])
    assert exc_info.value.code == 2


def test_unknown_subcommand_exits_2():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["not-a-real-command"])
    assert exc_info.value.code == 2
