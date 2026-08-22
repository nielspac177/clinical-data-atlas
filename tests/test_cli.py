"""Tests for the atlas CLI subcommands added in Task 0.5: `harvest`,
`normalize`, and the stub commands (`enrich`, `graph`, `diff`,
`validate`, `refresh`, `check-urls`, `dod`).

`schema` and the no-args help path are already covered in
tests/test_schema.py. Nothing here touches the network or the real
`data/` tree: harvesters are fakes injected via monkeypatching
`atlas.harvest.get_registry`, and the one `normalize` round-trip test
monkeypatches `atlas.cli.RawStore` and `atlas.config.CATALOG` so it only
ever touches `tmp_path`.
"""

from __future__ import annotations

import json

import pytest

from atlas import cli, config, harvest, normalize, schema
from atlas.harvest.base import Harvester, HarvestResult
from atlas.normalize import common as normalize_common

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


def test_harvest_unmatched_source_flag_is_treated_as_no_sources(monkeypatch, capsys):
    fake = _make_fake_harvester(name="fake")
    monkeypatch.setattr(harvest, "get_registry", lambda: {"fake": fake})
    assert cli.main(["harvest", "--source", "does-not-exist"]) == 0
    assert capsys.readouterr().out.strip() == "no sources registered"


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


def test_normalize_unmatched_source_flag_is_treated_as_no_normalizers(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        normalize,
        "get_normalizers",
        lambda: {"curated": (lambda **kw: None, lambda e: "")},
    )
    assert cli.main(["normalize", "--source", "does-not-exist"]) == 0
    assert capsys.readouterr().out.strip() == "no normalizers registered"


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


def _make_fake_rawstore_cls(envelopes: dict, manifest: dict, tmp_path):
    """A stand-in for `atlas.harvest.base.RawStore`, injected via
    monkeypatching `atlas.cli.RawStore` -- serves canned envelopes and a
    canned manifest without ever touching `data/raw/`."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    class _FakeRawStore:
        def __init__(self, source: str) -> None:
            self.source = source
            self.manifest_path = manifest_path

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
    monkeypatch.setattr(
        cli, "RawStore", _make_fake_rawstore_cls(envelopes, manifest, tmp_path)
    )
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


# ---------------------------------------------------------------------------
# stub commands: enrich, graph, diff, validate, refresh, check-urls, dod
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,cmd",
    [
        (["enrich"], "enrich"),
        (["graph"], "graph"),
        (["diff"], "diff"),
        (["validate"], "validate"),
        (["validate", "--strict"], "validate"),
        (["refresh"], "refresh"),
        (["refresh", "--sources", "openneuro,physionet", "--dry-run"], "refresh"),
        (["check-urls", "--sample", "10", "--seed", "0"], "check-urls"),
        (["dod", "--phase", "0", "--url", "https://example.org"], "dod"),
    ],
)
def test_stub_commands_print_not_implemented_to_stderr_and_return_2(argv, cmd, capsys):
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"{cmd}: not implemented yet"


def test_enrich_llm_flag_accepts_documented_choices():
    assert cli.main(["enrich", "--llm", "claude_cli", "--max-llm-calls", "5"]) == 2


def test_enrich_llm_flag_rejects_invalid_choice():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["enrich", "--llm", "bogus"])
    assert exc_info.value.code == 2


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
