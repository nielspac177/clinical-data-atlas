"""Command-line entry point for the ``atlas`` console script.

Every subcommand is thin: it resolves paths, loads or writes files, and
prints. The pipeline logic lives in `atlas.refresh` (which owns the stage
order) and in the stage modules themselves, so `atlas refresh` and
`python -m atlas.refresh` cannot drift apart, and `enrich`/`graph`/`diff`
run *the same* code the full pipeline runs, one stage at a time, for
debugging.

`harvest`, `normalize`, `enrich`, `graph` and `diff` are stage commands;
`refresh` is all of them in order; `validate`, `check-urls` and `dod` are
checks. An unrecognized subcommand, or invalid arguments to a known one,
is handled by argparse itself (exit 2), as usual.
"""

from __future__ import annotations

import argparse
import dataclasses
import random
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from atlas import config, diff, harvest, http, io, normalize, refresh, schema
from atlas.graph import build as graph_build
from atlas.harvest.base import HarvestResult, RawStore
from atlas.normalize import common

_T = TypeVar("_T")


class _NoSuchSource(Exception):
    """Raised by `_select` when `--source` is given, the registry isn't
    empty, and `--source` doesn't match anything in it -- the caller
    should treat this as a user error (exit 2), distinct from "nothing
    registered at all"."""

    def __init__(self, source: str, available: list[str]) -> None:
        super().__init__(source)
        self.source = source
        self.available = available


def _select(registry: dict[str, _T], source: str | None) -> dict[str, _T]:
    """Filter `registry` down to `--source`'s single entry, or return it
    whole when no `--source` was given.

    Raises `_NoSuchSource` when `--source` is given against a non-empty
    registry that doesn't contain it. A `--source` value given against a
    *truly empty* registry is deliberately left alone (returns `{}`,
    same as no `--source` at all): with nothing registered, every
    possible `--source` value is equally "not found", so the caller's
    own "nothing registered" message is more useful than an "available:"
    list with nothing in it.
    """
    if source is None:
        return registry
    if source in registry:
        return {source: registry[source]}
    if registry:
        raise _NoSuchSource(source, sorted(registry))
    return {}


def _print_unknown_source(exc: _NoSuchSource) -> None:
    available = ", ".join(exc.available)
    print(f"unknown source {exc.source!r} (available: {available})", file=sys.stderr)


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
    all) or when a plain harvest ran without every selected source
    failing; 1 when `--probe` found at least one source unreachable; 2
    when `--source` doesn't match anything in a non-empty registry; 3
    when a plain harvest ran and *every* selected source failed. A
    single source raising never aborts the others -- its exception is
    caught and reported as a `"failed"` result like any other failure.
    """
    try:
        selected = _select(harvest.get_registry(), args.source)
    except _NoSuchSource as exc:
        _print_unknown_source(exc)
        return 2
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


def _normalize_source(source: str, normalize_fn: Callable, out_dir: Path) -> str:
    """Normalize one source's raw store; returns `"ok"` or `"skipped"`
    (this source has never been harvested -- no manifest yet). Any other
    problem (a corrupt manifest, a normalizer bug, a `Record`
    `ValidationError`, ...) propagates, for `_cmd_normalize` to catch
    and report as a per-source failure rather than aborting the run.
    """
    store = RawStore(source)
    manifest = store.manifest()
    if manifest is None:
        print(f"{source}: skipped (no raw data)")
        return "skipped"

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
    return "ok"


def _cmd_normalize(args: argparse.Namespace) -> int:
    """Normalize each selected source's raw store into the canonical
    schema, writing `data/catalog/normalized/<source>.jsonl` (records,
    canonical-JSONL, sorted by id) and `<source>.excluded.jsonl`
    (records a normalizer deliberately left out).

    Exit codes: 0 when there's nothing to do (no normalizers registered
    at all) or when at least one selected source didn't fail; 2 when
    `--source` doesn't match anything in a non-empty registry; 3 when
    every selected source failed. A source that has never been harvested
    (no manifest yet) is reported `skipped`, not a failure. A single
    source's exception -- a normalizer bug, a `Record` `ValidationError`,
    a missing/corrupt manifest, anything -- is caught and reported like
    any other failure; it never aborts the others.
    """
    try:
        selected = _select(normalize.get_normalizers(), args.source)
    except _NoSuchSource as exc:
        _print_unknown_source(exc)
        return 2
    if not selected:
        print("no normalizers registered")
        return 0

    out_dir = config.CATALOG / "normalized"
    statuses: list[str] = []
    for source, (normalize_fn, _enrichment_text_fn) in sorted(selected.items()):
        try:
            status = _normalize_source(source, normalize_fn, out_dir)
        except Exception as exc:  # noqa: BLE001 -- one bad source must not kill the run
            print(f"{source}: FAILED {type(exc).__name__}: {exc}")
            status = "failed"
        statuses.append(status)

    if all(status == "failed" for status in statuses):
        return 3
    return 0


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------


def _load_records(paths: refresh.Paths) -> tuple[list[schema.Record], str]:
    """`(records, where)` -- the records a stage-only subcommand should
    work on: the per-source `data/catalog/normalized/*.jsonl` files when
    the normalize stage has run, else the catalog itself, else nothing.

    Normalized files come first deliberately: they are the freshest
    pre-enrichment state, so `atlas enrich` re-runs classification from
    the same starting point `atlas refresh` would, rather than layering a
    second pass on top of an already-enriched catalog.
    """
    normalized_dir = paths.catalog / "normalized"
    files = [
        path
        for path in sorted(normalized_dir.glob("*.jsonl"))
        if not path.name.endswith(".excluded.jsonl")
    ]
    if files:
        rows = [row for path in files for row in io.read_jsonl(path)]
        return (
            [schema.Record.model_validate(row) for row in rows],
            str(normalized_dir),
        )
    if paths.catalog_file.exists():
        rows = io.read_jsonl(paths.catalog_file)
        return (
            [schema.Record.model_validate(row) for row in rows],
            str(paths.catalog_file),
        )
    return [], ""


def _collect_texts(
    records: list[schema.Record], paths: refresh.Paths
) -> tuple[dict[str, str], int]:
    """`(texts, missing)` -- each record's enrichment text, rebuilt from
    its raw envelope via its source's `enrichment_text`, plus a count of
    the records no text could be built for (their raw record is gone, or
    the source has no normalizer registered). Those records still go
    through the stage; they just have nothing to classify from.
    """
    normalizers = normalize.get_normalizers()
    envelopes_by_source: dict[str, dict] = {}
    texts: dict[str, str] = {}
    missing = 0
    for record in records:
        pair = normalizers.get(record.source)
        if pair is None:
            missing += 1
            continue
        if record.source not in envelopes_by_source:
            try:
                envelopes_by_source[record.source] = RawStore(
                    record.source, root=paths.raw
                ).load_all()
            except (OSError, ValueError, KeyError):
                envelopes_by_source[record.source] = {}
        envelope = envelopes_by_source[record.source].get(record.source_native_id)
        if envelope is None:
            missing += 1
            continue
        try:
            texts[record.id] = pair[1](envelope)
        except Exception:  # noqa: BLE001 -- no text just means rules-only
            missing += 1
    return texts, missing


def _cmd_enrich(args: argparse.Namespace) -> int:
    """Run the enrichment stages -- rules, LLM, MeSH, ROR -- over the
    normalized records (or the catalog, when nothing is normalized) and
    write `.cache/enriched.jsonl`.

    A debugging/cache-warming counterpart to `refresh`, which runs the
    same stage in the middle of the full pipeline: the LLM answers, MeSH
    ids and ROR ids this fills are cached on disk, so a later `refresh`
    reuses them instead of re-asking. Its own output goes to the
    gitignored `.cache/`, not `data/`: it is a debugging artifact, and a
    file under `data/catalog/` would sooner or later be swept into a data
    commit as though it were part of the catalog. It deliberately does
    **not** touch `catalog.jsonl` either -- that file is `refresh`'s
    output, after dedupe and validation, and half a pipeline must not be
    able to produce it.

    Exit codes: 0 on success, 1 when there is nothing to enrich (no
    normalized records and no catalog -- run `harvest`/`normalize` first)
    or the input can't be parsed, 2 for an unknown `--llm` backend.
    """
    paths = refresh.paths()
    try:
        records, where = _load_records(paths)
    except Exception as exc:  # noqa: BLE001 -- a bad input file, not a crash
        print(f"cannot read records: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not records:
        print(
            "nothing to enrich: no normalized records and no catalog "
            "(run `atlas harvest` and `atlas normalize` first)",
            file=sys.stderr,
        )
        return 1

    texts, missing = _collect_texts(records, paths)
    print(f"enriching {len(records)} records from {where}")
    if missing:
        print(f"{missing} record(s) have no enrichment text")

    try:
        stats = refresh.stage_enrich(
            records,
            texts,
            offline=config.OFFLINE,
            llm_backend=args.llm,
            max_llm_calls=args.max_llm_calls,
        )
    except ValueError as exc:  # an unknown --llm backend name
        print(str(exc), file=sys.stderr)
        return 2

    records.sort(key=lambda record: record.id)
    out_path = paths.cache / "enriched.jsonl"
    io.write_jsonl(out_path, [record.model_dump(mode="json") for record in records])
    print(
        f"backend={stats['backend']} calls={stats['calls']} "
        f"cache_hits={stats['cache_hits']} guard_drops={stats['guard_drops']} "
        f"failures={stats['failures']} rules={stats['rules_applied']} "
        f"llm={stats['records_enriched']} mesh={stats['mesh_resolved']} "
        f"ror={stats['ror_resolved']} record_errors={stats['record_errors']}"
    )
    for message in stats["errors"]:
        print(f"  {message}", file=sys.stderr)
    hidden = stats["record_errors"] - len(stats["errors"])
    if hidden > 0:
        print(f"  … and {hidden} more record error(s)", file=sys.stderr)
    print(f"wrote {out_path}")
    return 0


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def _read_catalog(paths: refresh.Paths) -> list[dict] | None:
    """The catalog as plain dicts, or `None` (having said so on stderr)
    when it hasn't been built yet."""
    if not paths.catalog_file.exists():
        print(
            f"no catalog at {paths.catalog_file} (run `atlas refresh` first)",
            file=sys.stderr,
        )
        return None
    return io.read_jsonl(paths.catalog_file)


def _cmd_graph(args: argparse.Namespace) -> int:
    """Rebuild `data/graph/{graph,search-index,stats}.json` from the
    committed catalog. Exit 1 when there is no catalog to build from."""
    paths = refresh.paths()
    rows = _read_catalog(paths)
    if rows is None:
        return 1

    records = [schema.Record.model_validate(row) for row in rows]
    graph, index, stats = refresh.stage_graph(records)
    graph_build.write_outputs(graph, index, stats, out_dir=paths.graph)
    print(
        f"{stats['node_count']} nodes, {stats['link_count']} links, "
        f"{len(index)} index rows, {stats['record_count']} records"
    )
    print(f"wrote {paths.graph}")
    return 0


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def _cmd_diff(args: argparse.Namespace) -> int:
    """Write a changelog entry for the working tree's catalog versus the
    one committed at `HEAD` -- the same rendering `refresh` produces, for
    a catalog that was rebuilt stage by stage. Exit 1 without a catalog."""
    paths = refresh.paths()
    rows = _read_catalog(paths)
    if rows is None:
        return 1

    sources = sorted(set(harvest.get_registry()) | set(normalize.get_normalizers()))
    catalog_diff, markdown = refresh.stage_diff(
        refresh.head_catalog(paths.catalog_file),
        rows,
        date=refresh.today(),
        source_results=refresh.manifest_results(sources, raw_root=paths.raw),
    )
    dated_path, _latest = diff.write_changelog(
        markdown, date=refresh.today(), out_dir=paths.changelog
    )
    counts = catalog_diff.counts
    print(
        f"+{counts['added']} new, ~{counts['changed']} changed, "
        f"-{counts['removed']} removed, {counts['unchanged']} unchanged"
    )
    print(f"wrote {dated_path}")
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate the committed catalog against the schema, and the built
    graph against its contract.

    Always checked: every record parses as a `Record`, ids are unique, and
    every `related` id names a record that exists. When the graph outputs
    exist they are checked too -- unique node ids, no dangling link
    endpoint, one search-index row per catalogued dataset, and a
    `stats.json` `record_count` that agrees with the catalog.

    `--strict` is about *completeness*, not severity: it additionally
    requires the graph outputs to exist at all (CI runs it after a full
    refresh, where a missing `graph.json` means the pipeline didn't
    finish). Errors always exit 1, with or without `--strict`; warnings
    are counted and printed, never fatal -- they are quality signal
    (a record with no modalities), not breakage.
    """
    paths = refresh.paths()
    rows = _read_catalog(paths)
    if rows is None:
        return 1

    errors, warnings = schema.validate_records(rows)

    try:
        graph, index, stats = refresh.read_graph_outputs(paths.graph)
    except (FileNotFoundError, ValueError) as exc:
        message = f"graph outputs not built: {exc}"
        if args.strict:
            errors.append(message)
        else:
            print(message)
    else:
        errors.extend(refresh.graph_contract_errors(graph, index, stats, rows))

    for message in errors[:50]:
        print(f"error: {message}", file=sys.stderr)
    if len(errors) > 50:
        print(f"error: … and {len(errors) - 50} more", file=sys.stderr)

    print(f"{len(rows)} records, {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def _cmd_refresh(args: argparse.Namespace) -> int:
    """Run the whole pipeline (`atlas.refresh.run`). Exit codes: 0 ok, 1
    validation errors, 2 a source failed under `--strict` or the run was
    misconfigured, 3 every source failed."""
    return refresh.run(
        refresh.parse_sources(args.sources),
        skip_enrich=args.skip_enrich,
        offline=args.offline,
        fast=args.fast,
        strict=args.strict,
        dry_run=args.dry_run,
        llm=args.llm,
        max_llm_calls=args.max_llm_calls,
        report=Path(args.report) if args.report else None,
        summary_json=Path(args.summary_json) if args.summary_json else None,
    )


# ---------------------------------------------------------------------------
# check-urls
# ---------------------------------------------------------------------------

# What a link check counts as alive: 2xx and 3xx. Everything else --
# including 0, this project's "the request never completed" code -- is
# reported as a failure for a human to look at.
_URL_OK_RANGE = range(200, 400)


def _catalog_urls(rows: list[dict]) -> list[str]:
    """Every distinct url in `rows`, sorted -- the population both
    `check-urls` and `dod`'s url gate sample from. Sorted first so a given
    seed picks the same urls whatever order the catalog was read in."""
    return sorted({row["url"] for row in rows if row.get("url")})


def _sample_url_statuses(
    urls: list[str], sample_size: int, seed: int
) -> tuple[list[str], Counter, list[tuple[int, str]]]:
    """Check a seeded random sample of `urls`; return `(sample, counts,
    failures)` where `counts` maps status code -> how many, and `failures`
    lists the `(status, url)` pairs outside 2xx/3xx (0 == unreachable).

    Shared by `check-urls` and `dod` so the definition-of-done gate and the
    command a human runs to investigate it sample the *same* urls for the
    same seed."""
    sample = sorted(random.Random(seed).sample(urls, min(sample_size, len(urls))))
    counts: Counter = Counter()
    failures: list[tuple[int, str]] = []
    for url in sample:
        status, _final_url = http.head_status(url)
        counts[status] += 1
        if status not in _URL_OK_RANGE:
            failures.append((status, url))
    return sample, counts, failures


def _cmd_check_urls(args: argparse.Namespace) -> int:
    """HEAD (falling back to GET) a seeded random sample of catalog URLs
    and report the status codes.

    Informational by design: it always exits 0, even with dead links.
    Sources move pages, and a monthly refresh must not fail because one
    dataset was renamed -- the sample is evidence for a human (and for
    `atlas dod`), not a gate. `--seed` makes the sample reproducible, so
    re-running after a fix checks the same URLs.
    """
    paths = refresh.paths()
    if args.offline or config.OFFLINE:
        print("check-urls: offline, nothing checked")
        return 0
    if not paths.catalog_file.exists():
        print(f"no catalog at {paths.catalog_file}", file=sys.stderr)
        return 0

    urls = _catalog_urls(io.read_jsonl(paths.catalog_file))
    if not urls:
        print("no urls in the catalog")
        return 0

    sample, counts, failures = _sample_url_statuses(urls, args.sample, args.seed)
    print(f"checked {len(sample)} of {len(urls)} urls (seed {args.seed})")
    print("status  count")
    for status, count in sorted(counts.items()):
        label = "unreachable" if status == 0 else str(status)
        print(f"{label:<7} {count}")
    if failures:
        print(f"\n{len(failures)} failing url(s):")
        for status, url in failures:
            print(f"  {status} {url}")
    return 0


# ---------------------------------------------------------------------------
# dod
# ---------------------------------------------------------------------------

# graph.json's budget: the site fetches it on first paint, so it is the
# one generated file with a hard size ceiling (plan §M4/T4.4).
MAX_GRAPH_BYTES = 2_500_000
MIN_PHASE0_RECORDS = 2_400
# The url sample the Phase 0 gate takes, and the share of it that has to
# answer 2xx/3xx. Not 100%: sources rename pages between refreshes, and the
# plan's gate is "2xx/3xx or documented", so one stale link out of twenty is
# a residual to document, not a failed phase.
DOD_URL_SAMPLE = 20
DOD_URL_SEED = 0
DOD_URL_PASS_RATIO = 0.95


def _gate(name: str, ok: bool | None, detail: str = "") -> tuple[str, str, str]:
    """One definition-of-done row: `ok=None` means "not checkable here"."""
    status = "n/a" if ok is None else ("pass" if ok else "FAIL")
    return name, status, detail


def _phase0_gates(
    url: str | None, *, offline: bool, sample: int, seed: int
) -> list[tuple[str, str, str]]:
    """Every Phase 0 gate this machine can answer, plus `n/a` rows for the
    ones only CI can (the browser suite and the Pages deploy)."""
    paths = refresh.paths()
    gates: list[tuple[str, str, str]] = []

    rows: list[dict] = []
    if paths.catalog_file.exists():
        rows = io.read_jsonl(paths.catalog_file)
        gates.append(
            _gate(
                f"catalog >= {MIN_PHASE0_RECORDS} records",
                len(rows) >= MIN_PHASE0_RECORDS,
                f"{len(rows)} records",
            )
        )
    else:
        gates.append(_gate("catalog exists", False, str(paths.catalog_file)))

    errors, warnings = schema.validate_records(rows) if rows else ([], [])
    gates.append(
        _gate(
            "validate: 0 errors",
            not errors,
            f"{len(errors)} error(s), {len(warnings)} warning(s)",
        )
    )

    try:
        graph, index, stats = refresh.read_graph_outputs(paths.graph)
    except (FileNotFoundError, ValueError) as exc:
        gates.append(_gate("graph contract", False, str(exc)))
        graph_bytes = None
    else:
        contract = refresh.graph_contract_errors(graph, index, stats, rows)
        gates.append(
            _gate(
                "graph contract",
                not contract,
                contract[0] if contract else f"{len(index)} index rows",
            )
        )
        graph_bytes = (paths.graph / "graph.json").stat().st_size
    gates.append(
        _gate(
            f"graph.json <= {MAX_GRAPH_BYTES // 1000} kB",
            graph_bytes is not None and graph_bytes <= MAX_GRAPH_BYTES,
            "not built" if graph_bytes is None else f"{graph_bytes // 1000} kB",
        )
    )

    latest = paths.changelog / "latest.md"
    gates.append(_gate("changelog latest.md", latest.exists(), str(latest)))

    sources = sorted(set(harvest.get_registry()) | set(normalize.get_normalizers()))
    manifests = refresh.read_manifests(sources, raw_root=paths.raw)
    ok_sources = [
        name
        for name, manifest in manifests.items()
        if manifest and manifest.get("status") == "ok"
    ]
    gates.append(
        _gate(
            "every source harvested ok",
            bool(sources) and len(ok_sources) == len(sources),
            f"{len(ok_sources)}/{len(sources)} ok",
        )
    )

    index_html = config.ROOT / "_site" / "index.html"
    gates.append(_gate("site built", index_html.exists(), str(index_html)))

    if url and not offline:
        for label, target in (
            ("site url 200", url),
            ("data/stats.json 200", url.rstrip("/") + "/data/stats.json"),
        ):
            status, _final = http.head_status(target)
            gates.append(_gate(label, status in _URL_OK_RANGE, f"{status} {target}"))

        catalog_urls = _catalog_urls(rows)
        if catalog_urls:
            checked, _counts, failures = _sample_url_statuses(
                catalog_urls, sample, seed
            )
            alive = len(checked) - len(failures)
            ratio = alive / len(checked)
            gates.append(
                _gate(
                    "url sample 2xx/3xx",
                    ratio >= DOD_URL_PASS_RATIO,
                    f"{alive}/{len(checked)} ok (seed {seed})"
                    + (
                        f", worst: {failures[0][0]} {failures[0][1]}"
                        if failures
                        else ""
                    ),
                )
            )
        else:
            gates.append(_gate("url sample 2xx/3xx", False, "no catalog urls"))
    else:
        reason = "offline" if offline else "no --url"
        gates.append(_gate("site url 200", None, reason))
        gates.append(_gate("data/stats.json 200", None, reason))
        gates.append(
            _gate("url sample 2xx/3xx", None, f"{reason} — run `atlas check-urls`")
        )

    gates.append(_gate("e2e suite green", None, "CI"))
    gates.append(_gate("0 console errors", None, "CI"))
    gates.append(_gate("Pages deploy green", None, "CI"))
    return gates


def _cmd_dod(args: argparse.Namespace) -> int:
    """Print a phase's definition-of-done as a pass/fail table.

    Only the gates answerable from here are actually evaluated; the
    browser suite, the console-error count and the Pages deploy are CI's
    job and print as `n/a`, so the table never claims to have checked
    something it didn't. With `--url` and a network, the deployed site, its
    `data/stats.json`, and a `--sample`-sized seeded sample of catalog urls
    are checked for real. Exit 1 if any evaluated gate fails, 2 for a phase
    with no gates defined yet.
    """
    if args.phase != 0:
        print(f"no definition-of-done defined for phase {args.phase}", file=sys.stderr)
        return 2

    offline = args.offline or config.OFFLINE
    gates = _phase0_gates(args.url, offline=offline, sample=args.sample, seed=args.seed)
    width = max(len(name) for name, _status, _detail in gates)
    for name, status, detail in gates:
        print(f"{name.ljust(width)}  {status:<4}  {detail}".rstrip())

    passed = [name for name, status, _detail in gates if status == "pass"]
    skipped = [name for name, status, _detail in gates if status == "n/a"]
    failed = [name for name, status, _detail in gates if status == "FAIL"]
    print(
        f"phase {args.phase}: {len(passed)} pass, {len(skipped)} n/a, "
        f"{len(failed)} FAIL"
    )
    return 1 if failed else 0


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
        help="Run the enrichment stages and write .cache/enriched.jsonl",
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
        help="Build the graph, search index, and stats outputs from the catalog",
    )
    graph_parser.set_defaults(func=_cmd_graph)

    diff_parser = subparsers.add_parser(
        "diff",
        help="Write a changelog entry for the catalog versus the one at HEAD",
    )
    diff_parser.set_defaults(func=_cmd_diff)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate the catalog against the schema and the graph contract",
    )
    validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="Also require the graph outputs to exist",
    )
    validate_parser.set_defaults(func=_cmd_validate)

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Run the full pipeline end to end",
    )
    refresh_parser.add_argument(
        "--sources", default=None, help="Comma-separated list of sources (default: all)"
    )
    refresh_parser.add_argument(
        "--skip-enrich", action="store_true", help="Skip the LLM classification pass"
    )
    refresh_parser.add_argument(
        "--offline",
        action="store_true",
        help="Don't harvest; normalize the raw store as it stands (caches only)",
    )
    refresh_parser.add_argument(
        "--fast", action="store_true", help="Skip optional per-record detail requests"
    )
    refresh_parser.add_argument(
        "--strict", action="store_true", help="Exit 2 if any source failed"
    )
    refresh_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run every stage; write no catalog/graph/changelog "
            "(harvest and caches still update data/)"
        ),
    )
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
        help="Spot-check a seeded random sample of catalog URLs (always exits 0)",
    )
    check_urls_parser.add_argument(
        "--sample", type=int, required=True, help="Number of URLs to sample"
    )
    check_urls_parser.add_argument(
        "--seed", type=int, required=True, help="Random seed for a reproducible sample"
    )
    check_urls_parser.add_argument(
        "--offline", action="store_true", help="Check nothing and exit 0"
    )
    check_urls_parser.set_defaults(func=_cmd_check_urls)

    dod_parser = subparsers.add_parser(
        "dod",
        help="Check a phase's definition-of-done against this checkout and a URL",
    )
    dod_parser.add_argument(
        "--phase", type=int, required=True, help="Phase number to check"
    )
    dod_parser.add_argument(
        "--url", required=True, help="Deployed site URL to check against"
    )
    dod_parser.add_argument(
        "--offline", action="store_true", help="Skip the gates that need the network"
    )
    dod_parser.add_argument(
        "--sample",
        type=int,
        default=DOD_URL_SAMPLE,
        help="How many catalog urls the url-sample gate checks",
    )
    dod_parser.add_argument(
        "--seed",
        type=int,
        default=DOD_URL_SEED,
        help="Random seed for the url-sample gate",
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
