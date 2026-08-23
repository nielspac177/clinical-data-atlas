"""Tests for Task 2.7: `atlas.refresh`, the one-command orchestrator.

Everything here runs fully offline against `tmp_path`: `atlas.config`'s
five path constants are monkeypatched into a temporary tree, the source
registry and normalizer registry are replaced with fakes, and the raw store
is seeded through the *real* `RawStore` so the harvest/normalize boundary
is exercised rather than mocked away. No test touches the network (the
suite-wide socket guard would raise if one tried), the real `data/` tree,
or a real harvester.

What the fakes let each test steer, via one payload dict per raw record:
`exclude` (the normalizer returns an `Excluded`), `boom` (it raises, to
prove per-record isolation), plus any `Record` field to build interesting
catalogs (duplicate DOIs to merge, colliding titles to flag, conditions
and institutions for the MeSH/ROR stages).
"""

from __future__ import annotations

import json

import pytest

from atlas import config, harvest, io, normalize, refresh, schema
from atlas.enrich import llm, prompts
from atlas.harvest.base import Harvester, HarvestResult, RawStore
from atlas.normalize.common import Excluded
from atlas.schema import Record

# ---------------------------------------------------------------------------
# Fixtures: a whole atlas tree inside tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture
def tree(monkeypatch, tmp_path):
    """Point every `atlas.config` path at `tmp_path` and hand back the
    resolved `refresh.Paths` for assertions."""
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "RAW", tmp_path / "data" / "raw")
    monkeypatch.setattr(config, "CATALOG", tmp_path / "data" / "catalog")
    monkeypatch.setattr(config, "GRAPH", tmp_path / "data" / "graph")
    monkeypatch.setattr(config, "CHANGELOG", tmp_path / "data" / "changelog")
    return refresh.paths()


def _seed_raw(
    tree, source: str, payloads: dict[str, dict], *, harvested_at: str = "2026-08-20"
) -> None:
    """Write `payloads` into `source`'s raw store through the real
    `RawStore`, then finalize it so a manifest exists."""
    store = RawStore(source, root=tree.raw)
    endpoints = [f"https://example.org/api/{source}"]
    for native_id, payload in payloads.items():
        store.write(native_id, payload, harvest_method="api", endpoints=endpoints)
    store.finalize(
        listed_ids=set(payloads),
        harvested_at=harvested_at,
        endpoints=endpoints,
        status="ok",
    )


def _record_payload(**overrides) -> dict:
    """A raw payload whose keys are (mostly) `Record` fields, so a test can
    write the record it wants to see in the catalog."""
    payload = {"name": "Example dataset", "text": "A dataset of things."}
    payload.update(overrides)
    return payload


def _make_normalizer(source: str):
    """`(normalize, enrichment_text)` for `source`, driven by the payload."""

    def normalize_fn(envelope, *, harvested_at, first_seen):
        payload = dict(envelope["payload"])
        native_id = envelope["native_id"]
        if payload.get("boom"):
            raise RuntimeError(payload["boom"])
        if payload.get("exclude"):
            return Excluded(native_id=native_id, reason=payload["exclude"])
        payload.pop("text", None)
        payload.pop("text_boom", None)
        data = {
            "id": f"{source}:{native_id}",
            "source": source,
            "source_native_id": native_id,
            "name": payload.pop("name", native_id),
            "summary": payload.pop("summary", "A short summary of the dataset."),
            "url": payload.pop("url", f"https://example.org/{source}/{native_id}"),
            "species": "human",
            "sample_size": None,
            "sample_unit": None,
            "years": {},
            "access": "open",
            "record_status": "active",
            "provenance": {
                "harvested_via": "api",
                "harvested_at": first_seen or harvested_at,
                "last_verified": harvested_at,
                "raw_hash": None,
                "enrichment": {"method": "rules", "fields": {}},
            },
        }
        data.update(payload)
        return Record.model_validate(data)

    def enrichment_text_fn(envelope):
        payload = envelope["payload"]
        if payload.get("text_boom"):
            raise RuntimeError(payload["text_boom"])
        return payload.get("text", "")

    return normalize_fn, enrichment_text_fn


def _install(monkeypatch, sources: list[str], *, failing: dict | None = None) -> None:
    """Register fake normalizers for `sources` and a harvester registry
    that maps each to a `Harvester` whose `harvest()` behaves as `failing`
    says (a `HarvestResult`, or an exception to raise)."""
    failing = failing or {}
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {source: _make_normalizer(source) for source in sources},
    )

    def _harvester(name):
        behaviour = failing.get(name)

        def probe(self):
            return {}

        def do_harvest(self, *, fast=False, limit=None):
            if isinstance(behaviour, Exception):
                raise behaviour
            if isinstance(behaviour, HarvestResult):
                return behaviour
            return HarvestResult(source=name, status="ok", listed=1, unchanged=1)

        return type(
            "_FakeHarvester",
            (Harvester,),
            {
                "name": name,
                "harvest_method": "api",
                "probe": probe,
                "harvest": do_harvest,
            },
        )

    monkeypatch.setattr(
        harvest,
        "get_registry",
        lambda: {source: _harvester(source) for source in sources},
    )


def _catalog(tree) -> list[dict]:
    return io.read_jsonl(tree.catalog_file)


def _ids(tree) -> list[str]:
    return [record["id"] for record in _catalog(tree)]


# ---------------------------------------------------------------------------
# The happy path, end to end
# ---------------------------------------------------------------------------


def test_offline_run_writes_catalog_excluded_graph_and_changelog(
    tree, monkeypatch, capsys
):
    """One offline run over three seeded sources produces every artifact
    the site and the monthly PR consume."""
    _install(monkeypatch, ["openneuro", "physionet", "gdc"])
    _seed_raw(
        tree,
        "openneuro",
        {
            "ds002": _record_payload(name="Second study"),
            "ds001": _record_payload(name="First study", text="An EEG study."),
        },
    )
    _seed_raw(
        tree,
        "physionet",
        {
            "waveforms": _record_payload(name="Waveform archive"),
            "dropme": _record_payload(exclude="not clinical data"),
        },
    )
    _seed_raw(tree, "gdc", {"TCGA-GBM": _record_payload(name="Glioblastoma cohort")})

    exit_code = refresh.run(None, offline=True, skip_enrich=True)

    assert exit_code == 0
    assert _ids(tree) == [
        "gdc:TCGA-GBM",
        "openneuro:ds001",
        "openneuro:ds002",
        "physionet:waveforms",
    ]
    assert io.read_jsonl(tree.excluded_file) == [
        {"id": "physionet:dropme", "reason": "not clinical data"}
    ]

    graph, index, stats = refresh.read_graph_outputs(tree.graph)
    assert stats["record_count"] == 4
    assert [row["id"] for row in index] == _ids(tree)
    assert refresh.graph_contract_errors(graph, index, stats, _catalog(tree)) == []

    latest = (tree.changelog / "latest.md").read_text(encoding="utf-8")
    assert "## Sources" in latest
    assert "## Added (4)" in latest
    assert "## Validation warnings:" in latest
    dated = sorted(tree.changelog.glob("2*.md"))
    assert len(dated) == 1
    assert dated[0].read_text(encoding="utf-8") == latest

    out = capsys.readouterr().out
    assert "catalog: 4 records, 1 excluded" in out
    assert "stages:" in out and "total" in out


def test_rules_enrichment_runs_over_normalized_records(tree, monkeypatch):
    """The rules stage classifies from `enrichment_text`, and its result is
    stamped in the record's enrichment provenance."""
    _install(monkeypatch, ["openneuro"])
    _seed_raw(
        tree,
        "openneuro",
        {"ds001": _record_payload(text="Resting-state fMRI in epilepsy patients.")},
    )

    assert refresh.run(None, offline=True, skip_enrich=True) == 0

    record = _catalog(tree)[0]
    assert "fMRI" in record["modalities"]
    assert record["provenance"]["enrichment"]["method"] == "rules"
    assert record["provenance"]["enrichment"]["fields"]["modalities"] == "rules"


def test_second_run_is_byte_identical_and_diffs_to_nothing(tree, monkeypatch, capsys):
    """Idempotency: re-running over an unchanged raw store must reproduce
    the catalog byte for byte and report an empty diff."""
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})

    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    first = tree.catalog_file.read_bytes()
    first_graph = (tree.graph / "graph.json").read_bytes()
    capsys.readouterr()

    assert refresh.run(None, offline=True, skip_enrich=True) == 0

    assert tree.catalog_file.read_bytes() == first
    assert (tree.graph / "graph.json").read_bytes() == first_graph
    out = capsys.readouterr().out
    assert "diff: +0 new, ~0 changed, -0 removed, 1 unchanged" in out
    assert "## Added (0)" in (tree.changelog / "latest.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-source isolation
# ---------------------------------------------------------------------------


def test_failing_source_keeps_previous_records_and_run_continues(
    tree, monkeypatch, capsys
):
    """A source whose harvest raises loses freshness, not data: its
    previous catalog records survive and the run still exits 0."""
    _install(monkeypatch, ["openneuro", "physionet"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload(name="Kept study")})
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})
    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    capsys.readouterr()

    _install(
        monkeypatch,
        ["openneuro", "physionet"],
        failing={"openneuro": RuntimeError("API is down")},
    )
    exit_code = refresh.run(None, skip_enrich=True)

    assert exit_code == 0
    assert _ids(tree) == ["openneuro:ds001", "physionet:waveforms"]
    captured = capsys.readouterr()
    assert "1 source(s) failed: openneuro" in captured.err
    assert "failed" in captured.out
    latest = (tree.changelog / "latest.md").read_text(encoding="utf-8")
    assert "API is down" in latest
    assert "## Changed (0)" in latest


def test_source_reporting_failure_is_treated_as_a_failure(tree, monkeypatch):
    """A harvester that *returns* `status="failed"` (rather than raising)
    fails its source just the same."""
    _install(
        monkeypatch,
        ["openneuro", "physionet"],
        failing={
            "openneuro": HarvestResult(
                source="openneuro", status="failed", error="listing shrank"
            )
        },
    )
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})

    assert refresh.run(None, skip_enrich=True) == 0
    assert _ids(tree) == ["physionet:waveforms"]


def test_failing_source_with_strict_returns_2(tree, monkeypatch):
    _install(
        monkeypatch,
        ["openneuro", "physionet"],
        failing={"openneuro": RuntimeError("API is down")},
    )
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})

    assert refresh.run(None, skip_enrich=True, strict=True) == 2
    # Exit 2 still means the run completed: the good source was written.
    assert _ids(tree) == ["physionet:waveforms"]


def test_every_source_failing_returns_3_and_writes_nothing(tree, monkeypatch, capsys):
    _install(
        monkeypatch,
        ["openneuro", "physionet"],
        failing={
            "openneuro": RuntimeError("down"),
            "physionet": RuntimeError("also down"),
        },
    )

    assert refresh.run(None, skip_enrich=True) == 3
    assert not tree.catalog_file.exists()
    assert not tree.graph.exists()
    assert not tree.changelog.exists()
    assert "every source failed; nothing written" in capsys.readouterr().err


def test_source_without_raw_data_is_skipped_not_failed(tree, monkeypatch):
    _install(monkeypatch, ["openneuro", "physionet"])
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})

    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    assert _ids(tree) == ["physionet:waveforms"]
    latest = (tree.changelog / "latest.md").read_text(encoding="utf-8")
    assert "no raw data" in latest


def test_source_without_a_normalizer_fails_only_itself(tree, monkeypatch):
    _install(monkeypatch, ["physionet"])
    monkeypatch.setattr(
        harvest,
        "get_registry",
        dict,
    )
    # `openneuro` has raw data and no normalizer at all.
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})

    assert refresh.run(["physionet"], offline=True, skip_enrich=True) == 0
    assert _ids(tree) == ["physionet:waveforms"]


def test_record_level_exception_is_isolated_and_counted(tree, monkeypatch, capsys):
    """A normalizer raising on one envelope costs that envelope only, and
    the failure is reported rather than swallowed."""
    _install(monkeypatch, ["openneuro"])
    _seed_raw(
        tree,
        "openneuro",
        {
            "ds001": _record_payload(),
            "ds002": _record_payload(boom="malformed payload"),
            "ds003": _record_payload(),
        },
    )

    assert refresh.run(None, offline=True, skip_enrich=True) == 0

    assert _ids(tree) == ["openneuro:ds001", "openneuro:ds003"]
    out = capsys.readouterr().out
    assert "openneuro:ds002: RuntimeError: malformed payload" in out
    latest = (tree.changelog / "latest.md").read_text(encoding="utf-8")
    assert "1 record error(s)" in latest


def test_enrichment_text_failure_does_not_lose_the_record(tree, monkeypatch, capsys):
    """A record whose enrichment text can't be built is still catalogued --
    it just goes un-enriched."""
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload(text_boom="no text")})

    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    assert _ids(tree) == ["openneuro:ds001"]
    assert "enrichment_text failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------


def test_unknown_source_returns_2_without_running(tree, monkeypatch, capsys):
    _install(monkeypatch, ["openneuro"])

    assert refresh.run(["nope"], offline=True) == 2
    assert not tree.catalog_file.exists()
    assert "unknown source(s) nope" in capsys.readouterr().err


def test_no_sources_registered_returns_2(tree, monkeypatch, capsys):
    monkeypatch.setattr(harvest, "get_registry", dict)
    monkeypatch.setattr(normalize, "get_normalizers", dict)

    assert refresh.run(None, offline=True) == 2
    assert "no sources registered" in capsys.readouterr().err


def test_refreshing_one_source_keeps_the_others_records(tree, monkeypatch):
    """`make refresh SOURCE=openneuro` must not delete the rest of the
    catalog: unselected sources keep every record they had."""
    _install(monkeypatch, ["openneuro", "physionet"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})
    assert refresh.run(None, offline=True, skip_enrich=True) == 0

    _seed_raw(
        tree,
        "openneuro",
        {"ds001": _record_payload(), "ds002": _record_payload(name="New study")},
    )
    assert refresh.run(["openneuro"], offline=True, skip_enrich=True) == 0

    assert _ids(tree) == [
        "openneuro:ds001",
        "openneuro:ds002",
        "physionet:waveforms",
    ]


# ---------------------------------------------------------------------------
# Validation gate and --dry-run
# ---------------------------------------------------------------------------


def test_validation_error_exits_1_and_writes_nothing(tree, monkeypatch, capsys):
    """Nothing is written when validation fails.

    The error is injected because the pipeline's own stages make an
    invalid record unreachable: `remap_related` drops dangling links and
    `dedupe.cluster` folds duplicate ids together (see the test below), so
    the two error classes `validate_records` reports cannot survive to this
    point. That is the property worth protecting -- but the gate itself
    still has to be exercised.
    """
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    monkeypatch.setattr(
        schema, "validate_records", lambda rows: (["openneuro:ds001: boom"], [])
    )

    assert refresh.run(None, offline=True, skip_enrich=True) == 1
    assert not tree.catalog_file.exists()
    assert not tree.graph.exists()
    assert not tree.changelog.exists()
    err = capsys.readouterr().err
    assert "error: openneuro:ds001: boom" in err
    assert "1 validation error(s); nothing written" in err


def test_duplicate_ids_are_folded_before_validation(tree, monkeypatch):
    """Two raw records normalizing to the same id collapse into one rather
    than reaching the catalog as a duplicate."""
    _install(monkeypatch, ["openneuro"])
    _seed_raw(
        tree,
        "openneuro",
        {"ds001": _record_payload(), "ds001-copy": _record_payload(id="dummy")},
    )
    # Force both envelopes onto one id by giving the second the first's.
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {"openneuro": _same_id_normalizer()},
    )

    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    assert _ids(tree) == ["openneuro:ds001"]


def _same_id_normalizer():
    normalize_fn, text_fn = _make_normalizer("openneuro")

    def collapsing(envelope, *, harvested_at, first_seen):
        envelope = {**envelope, "native_id": "ds001"}
        return normalize_fn(envelope, harvested_at=harvested_at, first_seen=first_seen)

    return collapsing, text_fn


def test_dry_run_writes_nothing_but_reports_what_would_change(
    tree, monkeypatch, capsys
):
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})

    assert refresh.run(None, offline=True, skip_enrich=True, dry_run=True) == 0

    assert not tree.catalog_file.exists()
    assert not tree.excluded_file.exists()
    assert not tree.graph.exists()
    assert not tree.changelog.exists()
    out = capsys.readouterr().out
    assert "would write catalog: 1 records" in out
    assert "diff: +1 new" in out


def test_dry_run_does_not_write_report_or_summary(tree, monkeypatch, tmp_path):
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    report = tmp_path / "report.md"
    summary = tmp_path / "summary.json"

    assert (
        refresh.run(
            None,
            offline=True,
            skip_enrich=True,
            dry_run=True,
            report=report,
            summary_json=summary,
        )
        == 0
    )
    assert not report.exists()
    assert not summary.exists()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_report_and_summary_json_are_written(tree, monkeypatch, tmp_path):
    _install(monkeypatch, ["openneuro", "physionet"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    report = tmp_path / "report.md"
    summary_path = tmp_path / "summary.json"

    exit_code = refresh.run(
        None,
        offline=True,
        skip_enrich=True,
        report=report,
        summary_json=summary_path,
    )

    assert exit_code == 0
    assert report.read_text(encoding="utf-8") == (
        tree.changelog / "latest.md"
    ).read_text(encoding="utf-8")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert set(summary) == {"title", "date", "counts", "sources", "exit_code"}
    assert summary["title"].startswith("Monthly refresh ")
    assert summary["exit_code"] == 0
    assert summary["counts"]["added"] == 1
    assert summary["counts"]["records"] == 1
    assert [source["source"] for source in summary["sources"]] == [
        "openneuro",
        "physionet",
    ]
    assert summary["sources"][1]["status"] == "skipped"
    assert all("seconds" in source for source in summary["sources"])


def test_summary_json_records_the_strict_exit_code(tree, monkeypatch, tmp_path):
    _install(
        monkeypatch,
        ["openneuro", "physionet"],
        failing={"openneuro": RuntimeError("down")},
    )
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})
    summary_path = tmp_path / "summary.json"

    assert (
        refresh.run(None, skip_enrich=True, strict=True, summary_json=summary_path) == 2
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["exit_code"] == 2
    assert summary["sources"][0] == {
        **summary["sources"][0],
        "source": "openneuro",
        "status": "failed",
        "error": "RuntimeError: down",
        "retained": 0,
    }


# ---------------------------------------------------------------------------
# Dedupe / merge / cross-links inside the pipeline
# ---------------------------------------------------------------------------


def test_duplicate_doi_across_sources_merges_and_records_the_exclusion(
    tree, monkeypatch
):
    _install(monkeypatch, ["openneuro", "physionet"])
    doi = "10.1000/shared"
    _seed_raw(tree, "openneuro", {"ds001": _record_payload(dataset_doi=doi)})
    _seed_raw(tree, "physionet", {"waveforms": _record_payload(dataset_doi=doi)})

    assert refresh.run(None, offline=True, skip_enrich=True) == 0

    assert _ids(tree) == ["openneuro:ds001"]
    assert io.read_jsonl(tree.excluded_file) == [
        {"id": "physionet:waveforms", "reason": "merged_into:openneuro:ds001"}
    ]


def test_cross_source_title_collision_is_flagged_needs_review(tree, monkeypatch):
    _install(monkeypatch, ["openneuro", "physionet"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload(name="Identical Title")})
    _seed_raw(tree, "physionet", {"waveforms": _record_payload(name="Identical title")})

    assert refresh.run(None, offline=True, skip_enrich=True) == 0

    assert {record["record_status"] for record in _catalog(tree)} == {"needs_review"}


def test_same_cohort_records_are_linked_both_ways(tree, monkeypatch):
    _install(monkeypatch, ["gdc", "tcia"])
    _seed_raw(tree, "gdc", {"TCGA-GBM": _record_payload(name="GDC cohort")})
    _seed_raw(tree, "tcia", {"tcga-gbm": _record_payload(name="TCIA collection")})

    assert refresh.run(None, offline=True, skip_enrich=True) == 0

    related = {record["id"]: record["related"] for record in _catalog(tree)}
    assert related["gdc:TCGA-GBM"] == [
        {"id": "tcia:tcga-gbm", "relation": "same_cohort"}
    ]
    assert related["tcia:tcga-gbm"] == [
        {"id": "gdc:TCGA-GBM", "relation": "same_cohort"}
    ]


# ---------------------------------------------------------------------------
# _finalize_record
# ---------------------------------------------------------------------------


def _bare_record(**overrides) -> Record:
    data = {
        "id": "openneuro:ds001",
        "source": "openneuro",
        "source_native_id": "ds001",
        "name": "Example",
        "summary": "A short summary.",
        "url": "https://example.org/ds001",
        "species": "human",
        "sample_size": None,
        "sample_unit": None,
        "years": {},
        "access": "open",
        "record_status": "active",
        "provenance": {
            "harvested_via": "api",
            "harvested_at": "2026-08-01",
            "last_verified": "2026-08-20",
            "enrichment": {"method": "rules", "fields": {}},
        },
    }
    data.update(overrides)
    return Record.model_validate(data)


def test_finalize_record_orders_and_dedupes_vocabulary_lists():
    record = _bare_record(
        domains=["oncology", "neurology", "oncology"],
        modalities=["CT", "MRI", "CT"],
    )
    finalized = refresh._finalize_record(record)
    assert finalized.domains == ["neurology", "oncology"]
    assert finalized.modalities == ["MRI", "CT"]


def test_finalize_record_returns_the_same_object_when_already_ordered():
    record = _bare_record(domains=["neurology"], modalities=["MRI"])
    assert refresh._finalize_record(record) is record


def test_catalog_lists_are_vocab_ordered(tree, monkeypatch):
    _install(monkeypatch, ["openneuro"])
    _seed_raw(
        tree,
        "openneuro",
        {
            "ds001": _record_payload(
                domains=["oncology", "neurology"], modalities=["CT", "MRI"]
            )
        },
    )

    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    record = _catalog(tree)[0]
    assert record["domains"] == ["neurology", "oncology"]
    assert record["modalities"] == ["MRI", "CT"]


# ---------------------------------------------------------------------------
# Enrichment stage
# ---------------------------------------------------------------------------


def test_skip_enrich_reports_the_stage_as_skipped(tree, monkeypatch):
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})

    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    latest = (tree.changelog / "latest.md").read_text(encoding="utf-8")
    assert "- backend: `skipped`" in latest


def test_offline_enrichment_uses_the_null_backend_and_no_calls(tree, monkeypatch):
    """`--offline` must never attempt a model call, and must still serve
    cached answers -- so the backend is `NullBackend` and the call budget
    is zero, not "unlimited but broken"."""
    seen = {}

    def fake_enrich_records(records, texts, backend, **kwargs):
        seen["backend"] = backend
        seen["kwargs"] = kwargs
        return classify_stats(backend)

    def classify_stats(backend):
        from atlas.enrich.classify import EnrichStats

        return EnrichStats(backend=backend.name, model=backend.model)

    monkeypatch.setattr("atlas.enrich.classify.enrich_records", fake_enrich_records)
    stats = refresh.stage_enrich([], {}, offline=True)

    assert isinstance(seen["backend"], llm.NullBackend)
    assert seen["kwargs"]["max_calls"] == 0
    assert seen["kwargs"]["batch_size"] == refresh.LLM_BATCH_SIZE
    assert seen["kwargs"]["cache_dir"] == tree.llm_cache_dir
    assert stats["backend"] == "none"
    assert stats["calls"] == 0


def test_cached_llm_answers_are_applied_offline(tree, monkeypatch):
    """An answer already in the per-record cache is merged even with no
    backend available -- that is what makes an offline refresh reproduce a
    connected one.

    The cached answer proposes a domain the rules stage does *not* find
    (`public_health`), so the assertion below can only pass if the cached
    answer really was applied, not merely re-confirmed by the rules.
    """
    _install(monkeypatch, ["openneuro"])
    text = "An intensive care unit dataset of adult admissions."
    record = _bare_record(summary="Placeholder summary.")
    records = [record]
    texts = {record.id: text}

    # Enrich once with rules only, so the cache key is computed against the
    # exact record state `classify` will see on the LLM pass.
    refresh.stage_enrich(records, texts, skip_enrich=True, offline=True)
    key = llm.cache_key(prompts.record_input(records[0], text), prompts.OUTPUT_SCHEMA)
    io.write_atomic(
        llm.cache_path(key, cache_dir=tree.llm_cache_dir),
        io.pretty_json(
            {
                "key": key,
                "backend": "claude_cli",
                "model": "test-model",
                "prompt_version": prompts.PROMPT_VERSION,
                "output": {"id": records[0].id, "domains": ["public_health"]},
            }
        ),
    )

    stats = refresh.stage_enrich(records, texts, offline=True)

    assert stats["calls"] == 0
    assert stats["cache_hits"] == 1
    assert stats["cached_models"] == {"test-model": 1}
    assert records[0].domains == ["critical_care", "public_health"]
    assert records[0].provenance.enrichment.model == "test-model"


def test_mesh_and_ror_fill_from_their_caches_offline(tree, monkeypatch):
    io.write_atomic(tree.mesh_cache, io.pretty_json({"epilepsy": "D004827"}))
    io.write_atomic(
        tree.ror_cache,
        io.pretty_json(
            {
                "massachusetts general hospital": {
                    "ror_id": "https://ror.org/002pd6e78",
                    "name": "Massachusetts General Hospital",
                    "country": "US",
                }
            }
        ),
    )
    record = _bare_record(
        conditions=[{"label": "Epilepsy"}, {"label": "Nothing known"}],
        institutions=[{"name": "Massachusetts General Hospital"}],
    )
    records = [record]

    stats = refresh.stage_enrich(records, {}, skip_enrich=True, offline=True)

    assert stats["mesh_resolved"] == 1
    assert stats["ror_resolved"] == 1
    updated = records[0]
    assert updated.conditions[0].mesh_id == "D004827"
    assert updated.conditions[1].mesh_id is None
    assert updated.institutions[0].ror_id == "https://ror.org/002pd6e78"
    assert updated.institutions[0].country == "US"


def test_mesh_never_overwrites_an_existing_id(tree):
    io.write_atomic(tree.mesh_cache, io.pretty_json({"epilepsy": "D004827"}))
    records = [_bare_record(conditions=[{"label": "Epilepsy", "mesh_id": "D999999"}])]

    refresh.stage_enrich(records, {}, skip_enrich=True, offline=True)

    assert records[0].conditions[0].mesh_id == "D999999"


def test_unknown_llm_backend_returns_2(tree, monkeypatch, capsys):
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})

    assert refresh.run(None, offline=False, llm="not-a-backend") == 2
    assert "unknown LLM backend" in capsys.readouterr().err
    assert not tree.catalog_file.exists()


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------


def test_load_previous_catalog_reads_the_working_tree_file(tree):
    io.write_jsonl(tree.catalog_file, [{"id": "openneuro:ds001"}])
    assert refresh.load_previous_catalog(tree.catalog_file) == [
        {"id": "openneuro:ds001"}
    ]


def test_load_previous_catalog_is_empty_outside_a_repo(tree):
    """No file and a path git can't answer for is a normal first run."""
    assert refresh.load_previous_catalog(tree.catalog_file) == []


def test_unreadable_previous_catalog_is_reported_not_fatal(tree, monkeypatch, capsys):
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    io.write_atomic(tree.catalog_file, "{not json\n")

    assert refresh.run(None, offline=True, skip_enrich=True) == 0
    assert "previous catalog unreadable" in capsys.readouterr().out
    assert _ids(tree) == ["openneuro:ds001"]


# ---------------------------------------------------------------------------
# The graph contract
# ---------------------------------------------------------------------------


def test_graph_contract_catches_a_dangling_link():
    graph = {
        "nodes": [{"id": "openneuro:ds001"}],
        "links": [{"source": "openneuro:ds001", "target": "ghost", "type": "related"}],
    }
    errors = refresh.graph_contract_errors(
        graph,
        [{"id": "openneuro:ds001"}],
        {"record_count": 1},
        [{"id": "openneuro:ds001"}],
    )
    assert errors == ["graph.json: 1 link endpoint(s) with no node: ghost"]


def test_graph_contract_catches_duplicate_nodes_and_index_gaps():
    graph = {"nodes": [{"id": "a"}, {"id": "a"}], "links": []}
    errors = refresh.graph_contract_errors(
        graph, [], {"record_count": 0}, [{"id": "openneuro:ds001"}]
    )
    assert any("duplicate node id" in message for message in errors)
    assert any("missing" in message for message in errors)
    assert any("record_count" in message for message in errors)


def test_graph_contract_ignores_removed_records():
    graph = {"nodes": [{"id": "openneuro:ds001"}], "links": []}
    catalog = [
        {"id": "openneuro:ds001"},
        {"id": "openneuro:ds002", "record_status": "removed"},
    ]
    assert (
        refresh.graph_contract_errors(
            graph, [{"id": "openneuro:ds001"}], {"record_count": 1}, catalog
        )
        == []
    )


def test_read_graph_outputs_names_the_missing_file(tree):
    with pytest.raises(FileNotFoundError, match="graph.json"):
        refresh.read_graph_outputs(tree.graph)


# ---------------------------------------------------------------------------
# python -m atlas.refresh
# ---------------------------------------------------------------------------


def test_module_entry_point_mirrors_run(tree, monkeypatch, capsys):
    _install(monkeypatch, ["openneuro", "physionet"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})
    _seed_raw(tree, "physionet", {"waveforms": _record_payload()})

    exit_code = refresh.main(
        ["--offline", "--skip-enrich", "--sources", "openneuro , physionet"]
    )

    assert exit_code == 0
    assert _ids(tree) == ["openneuro:ds001", "physionet:waveforms"]
    assert "stages:" in capsys.readouterr().out


def test_module_entry_point_dry_run_writes_nothing(tree, monkeypatch):
    _install(monkeypatch, ["openneuro"])
    _seed_raw(tree, "openneuro", {"ds001": _record_payload()})

    assert refresh.main(["--offline", "--skip-enrich", "--dry-run"]) == 0
    assert not tree.catalog_file.exists()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("a", ["a"]),
        (" a , b ,", ["a", "b"]),
    ],
)
def test_parse_sources(value, expected):
    assert refresh.parse_sources(value) == expected
