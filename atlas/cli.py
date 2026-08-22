"""Command-line entry point for the ``atlas`` console script.

``schema``, ``harvest``, and ``normalize`` are fully wired up (Tasks 0.2
and 0.5). ``enrich``, ``graph``, ``diff``, ``validate``, ``refresh``,
``check-urls``, and ``dod`` are argument-parsing-only stubs for now: each
prints ``<cmd>: not implemented yet`` to stderr and returns 2 -- later
tasks (see each stub's docstring) replace their ``func`` with a real
implementation. An unrecognized subcommand, or invalid arguments to a
known one, is handled by argparse itself (exit 2), as usual.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import TypeVar

from atlas import config, harvest, io, normalize, schema
from atlas.harvest.base import HarvestResult, RawStore
from atlas.normalize import common

_T = TypeVar("_T")


def _select(registry: dict[str, _T], source: str | None) -> dict[str, _T]:
    """Filter `registry` down to `--source`'s single entry, or return it
    whole when no `--source` was given.

    A `--source` value that matches nothing resolves to `{}` -- on
    purpose indistinguishable from "nothing registered at all", so
    callers only need one "nothing to do" branch.
    """
    if source is None:
        return registry
    if source in registry:
        return {source: registry[source]}
    return {}


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def _cmd_schema(args: argparse.Namespace) -> int:
    """Print, export, or check ``docs/schema.json`` + ``docs/schema.md``."""
    json_text = schema.export_json_schema_text()
    md_text = schema.render_markdown()

    if args.export:
        schema.SCHEMA_JSON_PATH.write_text(json_text, encoding="utf-8")
        schema.SCHEMA_MD_PATH.write_text(md_text, encoding="utf-8")
        print(f"wrote {schema.SCHEMA_JSON_PATH}")
        print(f"wrote {schema.SCHEMA_MD_PATH}")
        return 0

    if args.check:
        drifted = []
        if (
            not schema.SCHEMA_JSON_PATH.exists()
            or schema.SCHEMA_JSON_PATH.read_text(encoding="utf-8") != json_text
        ):
            drifted.append(str(schema.SCHEMA_JSON_PATH))
        if (
            not schema.SCHEMA_MD_PATH.exists()
            or schema.SCHEMA_MD_PATH.read_text(encoding="utf-8") != md_text
        ):
            drifted.append(str(schema.SCHEMA_MD_PATH))
        if drifted:
            for path in drifted:
                print(f"drift: {path} does not match atlas/schema.py", file=sys.stderr)
            return 1
        print("schema docs are up to date")
        return 0

    print(json_text, end="")
    return 0


# ---------------------------------------------------------------------------
# harvest
# ---------------------------------------------------------------------------


def _print_harvest_summary(result: HarvestResult) -> None:
    line = (
        f"{result.source}: {result.status} listed={result.listed} "
        f"written={result.written} unchanged={result.unchanged} "
        f"removed={result.removed} seconds={result.seconds:.2f}"
    )
    if result.error:
        line += f" error={result.error}"
    print(line)


def _cmd_harvest(args: argparse.Namespace) -> int:
    """Run harvesters from the registry (`atlas.harvest.get_registry`).

    Exit codes: 0 when there's nothing to do (no sources registered at
    all, or `--source` matched none) or when every selected source's run
    completed without every one of them failing; 1 when `--probe` found
    at least one source unreachable; 3 when a plain harvest ran and
    *every* selected source failed. A single source raising never aborts
    the others -- its exception is caught and reported as a `"failed"`
    result like any other failure.
    """
    selected = _select(harvest.get_registry(), args.source)
    if not selected:
        print("no sources registered")
        return 0

    if args.probe:
        any_failed = False
        for name, harvester_cls in sorted(selected.items()):
            try:
                harvester_cls().probe()
            except Exception as exc:  # noqa: BLE001 -- one bad source must not hide the others
                print(f"{name}: FAILED {exc}")
                any_failed = True
            else:
                print(f"{name}: ok")
        return 1 if any_failed else 0

    results: list[HarvestResult] = []
    for name, harvester_cls in sorted(selected.items()):
        try:
            result = harvester_cls().harvest(fast=args.fast, limit=args.limit)
        except Exception as exc:  # noqa: BLE001 -- one bad source must not kill the run
            result = HarvestResult(source=name, status="failed", error=str(exc))
        results.append(result)
        _print_harvest_summary(result)

    if all(result.status == "failed" for result in results):
        return 3
    return 0


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def _cmd_normalize(args: argparse.Namespace) -> int:
    """Normalize each selected source's raw store into the canonical
    schema, writing `data/catalog/normalized/<source>.jsonl` (records,
    canonical-JSONL, sorted by id) and `<source>.excluded.jsonl`
    (records a normalizer deliberately left out).

    Always returns 0: with no normalizers registered (`atlas.normalize.
    get_normalizers`) -- or `--source` matching none -- it prints `no
    normalizers registered` and does nothing else. Unlike `harvest`,
    per-source failures aren't isolated here: that isolation is the
    `refresh` orchestrator's job (Task 2.7), which tries harvest+
    normalize together per source; this is the lower-level, one-source-
    at-a-time debugging command.
    """
    selected = _select(normalize.get_normalizers(), args.source)
    if not selected:
        print("no normalizers registered")
        return 0

    out_dir = config.CATALOG / "normalized"
    for source, (normalize_fn, _enrichment_text_fn) in sorted(selected.items()):
        store = RawStore(source)
        manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        harvested_at = manifest["harvested_at"]
        envelopes = store.load_all()

        records: list[schema.Record] = []
        excluded: list[common.Excluded] = []
        for native_id, envelope in envelopes.items():
            first_seen = manifest["records"][native_id]["first_seen"]
            outcome = normalize_fn(
                envelope, harvested_at=harvested_at, first_seen=first_seen
            )
            if isinstance(outcome, common.Excluded):
                excluded.append(outcome)
            else:
                records.append(outcome)

        records.sort(key=lambda record: record.id)
        excluded.sort(key=lambda item: item.native_id)

        io.write_jsonl(
            out_dir / f"{source}.jsonl",
            [record.model_dump(mode="json") for record in records],
        )
        io.write_jsonl(
            out_dir / f"{source}.excluded.jsonl",
            [dataclasses.asdict(item) for item in excluded],
        )
        print(f"{source}: {len(records)} records, {len(excluded)} excluded")

    return 0


# ---------------------------------------------------------------------------
# Stubs: enrich, graph, diff, validate, refresh, check-urls, dod
# ---------------------------------------------------------------------------


def _not_implemented(cmd: str) -> int:
    """Shared body for every stub subcommand below: print `<cmd>: not
    implemented yet` to stderr and return 2."""
    print(f"{cmd}: not implemented yet", file=sys.stderr)
    return 2


def _cmd_enrich(args: argparse.Namespace) -> int:
    """Stub -- classification/summarization lands in Tasks 2.1-2.2.
    Always returns 2."""
    return _not_implemented("enrich")


def _cmd_graph(args: argparse.Namespace) -> int:
    """Stub -- graph + search index + stats output lands in Task 2.5.
    Always returns 2."""
    return _not_implemented("graph")


def _cmd_diff(args: argparse.Namespace) -> int:
    """Stub -- changelog diffing lands in Task 2.6. Always returns 2."""
    return _not_implemented("diff")


def _cmd_validate(args: argparse.Namespace) -> int:
    """Stub -- catalog/graph validation lands in Task 2.7. Always
    returns 2."""
    return _not_implemented("validate")


def _cmd_refresh(args: argparse.Namespace) -> int:
    """Stub -- the one-command pipeline orchestrator lands in Task 2.7.
    Always returns 2 (its eventual real exit codes are 0 ok / 1
    validation failed / 2 a source failed under `--strict` / 3 every
    source failed)."""
    return _not_implemented("refresh")


def _cmd_check_urls(args: argparse.Namespace) -> int:
    """Stub -- the URL spot-check sampler lands in Task 2.7. Always
    returns 2."""
    return _not_implemented("check-urls")


def _cmd_dod(args: argparse.Namespace) -> int:
    """Stub -- the definition-of-done gate lands in Task 4.4. Always
    returns 2."""
    return _not_implemented("dod")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``atlas`` argparse parser."""
    parser = argparse.ArgumentParser(
        prog="atlas", description="Clinical Data Atlas pipeline CLI"
    )
    subparsers = parser.add_subparsers(dest="command")

    schema_parser = subparsers.add_parser(
        "schema", help="Print, export, or check the canonical schema docs"
    )
    mode = schema_parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--export",
        action="store_true",
        help="Write docs/schema.json and docs/schema.md",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the checked-in docs have drifted from atlas/schema.py",
    )
    schema_parser.set_defaults(func=_cmd_schema)

    harvest_parser = subparsers.add_parser(
        "harvest", help="Fetch raw records from one or all registered sources"
    )
    harvest_parser.add_argument(
        "--source", default=None, help="Only harvest this source (default: all)"
    )
    harvest_parser.add_argument(
        "--probe",
        action="store_true",
        help="Sanity-check each source with one request instead of a full harvest",
    )
    harvest_parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip optional per-record detail requests",
    )
    harvest_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of records fetched per source",
    )
    harvest_parser.set_defaults(func=_cmd_harvest)

    normalize_parser = subparsers.add_parser(
        "normalize", help="Normalize raw records into the canonical schema"
    )
    normalize_parser.add_argument(
        "--source", default=None, help="Only normalize this source (default: all)"
    )
    normalize_parser.set_defaults(func=_cmd_normalize)

    enrich_parser = subparsers.add_parser(
        "enrich",
        help="Classify, summarize, and dedupe records (not implemented yet)",
    )
    enrich_parser.add_argument(
        "--llm",
        choices=("anthropic", "claude_cli", "none"),
        default=None,
        help="LLM backend to use",
    )
    enrich_parser.add_argument(
        "--max-llm-calls",
        type=int,
        default=None,
        help="Cap the number of LLM calls made",
    )
    enrich_parser.set_defaults(func=_cmd_enrich)

    graph_parser = subparsers.add_parser(
        "graph",
        help="Build the graph, search index, and stats outputs (not implemented yet)",
    )
    graph_parser.set_defaults(func=_cmd_graph)

    diff_parser = subparsers.add_parser(
        "diff",
        help="Write a changelog entry for the current catalog state (not implemented yet)",
    )
    diff_parser.set_defaults(func=_cmd_diff)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate the catalog against the canonical schema (not implemented yet)",
    )
    validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on warnings, not just errors",
    )
    validate_parser.set_defaults(func=_cmd_validate)

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Run the full pipeline end to end (not implemented yet)",
    )
    refresh_parser.add_argument(
        "--sources", default=None, help="Comma-separated list of sources (default: all)"
    )
    refresh_parser.add_argument("--skip-enrich", action="store_true")
    refresh_parser.add_argument("--offline", action="store_true")
    refresh_parser.add_argument("--fast", action="store_true")
    refresh_parser.add_argument("--strict", action="store_true")
    refresh_parser.add_argument("--dry-run", action="store_true")
    refresh_parser.add_argument("--llm", default=None, help="LLM backend override")
    refresh_parser.add_argument("--max-llm-calls", type=int, default=None)
    refresh_parser.add_argument(
        "--report", default=None, help="Write a markdown changelog to this path"
    )
    refresh_parser.add_argument(
        "--summary-json",
        default=None,
        help="Write a machine-readable run summary to this path",
    )
    refresh_parser.set_defaults(func=_cmd_refresh)

    check_urls_parser = subparsers.add_parser(
        "check-urls",
        help="Spot-check a random sample of catalog URLs (not implemented yet)",
    )
    check_urls_parser.add_argument(
        "--sample", type=int, required=True, help="Number of URLs to sample"
    )
    check_urls_parser.add_argument(
        "--seed", type=int, required=True, help="Random seed for a reproducible sample"
    )
    check_urls_parser.set_defaults(func=_cmd_check_urls)

    dod_parser = subparsers.add_parser(
        "dod",
        help="Check a phase's definition-of-done against a deployed URL (not implemented yet)",
    )
    dod_parser.add_argument(
        "--phase", type=int, required=True, help="Phase number to check"
    )
    dod_parser.add_argument(
        "--url", required=True, help="Deployed site URL to check against"
    )
    dod_parser.set_defaults(func=_cmd_dod)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` and dispatch to the matching subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
