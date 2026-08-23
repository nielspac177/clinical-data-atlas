"""The one-command pipeline: harvest -> normalize -> enrich -> dedupe ->
validate -> write -> graph -> changelog.

`make refresh`, `atlas refresh`, and `python -m atlas.refresh` are all the
same thing: :func:`run`. It is the only place in the project that knows the
*order* of the stages; every stage itself lives in its own module and is
called from here, never the other way round.

Three properties are worth more than any feature this module has:

- **Per-source isolation.** One source raising -- a dead API, a normalizer
  bug, a corrupt manifest -- is caught, reported in the run's own output,
  and costs only that source: its records are carried over unchanged from
  the previous catalog and every other source proceeds. The same isolation
  applies one level down, per *record*: a normalizer that raises on one
  envelope loses that envelope, not its source. Only `--strict` turns a
  source failure into a non-zero exit, and only every source failing
  (exit 3) stops the run from writing at all.
- **Nothing half-written.** Validation runs before the first byte is
  written, and a validation error exits 1 having written nothing. Every
  write is atomic (`atlas.io.write_atomic`), so a reader never sees a
  partial file even if the process dies mid-run.
- **Determinism.** Records are sorted by id, list fields are rendered in
  vocabulary order (see :func:`_finalize_record`), JSON is canonical, and
  the only clock reading in the output is the run's own date. Re-running
  over an unchanged raw store therefore reproduces byte-identical
  outputs and an empty diff -- which is exactly what the changelog claims,
  so it had better be true.

Exit codes: ``0`` ok (source failures reported but tolerated), ``1``
validation errors (nothing written), ``2`` a source failed under
`--strict`, or the run was misconfigured (unknown source or LLM backend),
``3`` every selected source failed (nothing written).

Paths come from :func:`paths`, which reads `atlas.config` *at call time*
rather than at import time, so a test can point the whole pipeline at a
`tmp_path` by monkeypatching `config.CATALOG` & co.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from atlas import config, harvest, io, normalize, schema, vocab
from atlas import diff as diffmod
from atlas.enrich import classify, dedupe, llm, merge, mesh, ror, rules
from atlas.graph import build as graphbuild
from atlas.harvest.base import HarvestResult, RawStore
from atlas.normalize.common import Excluded
from atlas.schema import Record

# How many per-record failure messages to keep per source for the report --
# a source whose normalizer breaks on every envelope must not turn the run's
# output (or the summary JSON) into a log dump.
MAX_REPORTED_RECORD_ERRORS = 10

# How many records to ask the LLM about per call. Matches the batch size
# `atlas.enrich.classify` documents; kept here because the orchestrator is
# what decides the cost/latency trade-off for a whole run.
LLM_BATCH_SIZE = 8


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    """Where this run reads and writes, resolved once per call to
    :func:`paths`."""

    raw: Path
    catalog: Path
    graph: Path
    changelog: Path

    @property
    def catalog_file(self) -> Path:
        return self.catalog / "catalog.jsonl"

    @property
    def excluded_file(self) -> Path:
        return self.catalog / "excluded.jsonl"

    @property
    def llm_cache_dir(self) -> Path:
        return self.raw / "enrich" / "llm"

    @property
    def mesh_cache(self) -> Path:
        return self.raw / "enrich" / "mesh.json"

    @property
    def ror_cache(self) -> Path:
        return self.raw / "enrich" / "ror.json"


def paths() -> Paths:
    """The pipeline's paths, read from `atlas.config` **now**.

    Every module-level default in the codebase (`RawStore(root=config.RAW)`,
    `llm.CACHE_DIR`, `mesh._DEFAULT_CACHE_PATH`, ...) binds its path at
    import time, which is fine for a real run and useless for a test.
    Reading them here, per call, and passing them down explicitly is what
    lets `tests/test_refresh.py` run the entire pipeline inside `tmp_path`.
    """
    return Paths(
        raw=config.RAW,
        catalog=config.CATALOG,
        graph=config.GRAPH,
        changelog=config.CHANGELOG,
    )


def _today() -> str:
    """Today's date in UTC, ISO-8601. UTC rather than local time so two
    machines refreshing in the same minute agree on the date."""
    return datetime.now(tz=UTC).date().isoformat()


# ---------------------------------------------------------------------------
# Per-source outcome
# ---------------------------------------------------------------------------


@dataclass
class SourceOutcome:
    """What one source contributed to this run.

    `status` is `"ok"`, `"skipped"` (never harvested -- no raw manifest
    yet), or `"failed"`. A `failed` or `skipped` source keeps its previous
    catalog records (`retained`), so a broken API costs freshness, never
    data.
    """

    source: str
    status: str = "ok"
    listed: int = 0
    written: int = 0
    unchanged: int = 0
    removed: int = 0
    records: int = 0
    excluded: int = 0
    record_errors: int = 0
    first_errors: list[str] = field(default_factory=list)
    retained: int = 0
    seconds: float = 0.0
    error: str | None = None

    def note_record_error(self, message: str) -> None:
        """Count one per-record failure, keeping the first few messages."""
        self.record_errors += 1
        if len(self.first_errors) < MAX_REPORTED_RECORD_ERRORS:
            self.first_errors.append(message)

    def as_harvest_result(self) -> HarvestResult:
        """This outcome as a `HarvestResult`, which is what
        `atlas.diff.render_changelog` renders its Sources table from.

        `seconds` is the *whole source's* wall time (harvest **and**
        normalize), not just the harvest's -- the changelog's table is the
        only per-source timing a reader gets, so it should account for all
        of the run's time. Per-record failures ride along in `error` even
        when `status` is `"ok"`, so they show up in the table rather than
        only in this process's stdout.
        """
        error = self.error
        if self.record_errors:
            note = f"{self.record_errors} record error(s)"
            error = f"{error}; {note}" if error else note
        return HarvestResult(
            source=self.source,
            status=self.status,
            listed=self.listed,
            written=self.written,
            unchanged=self.unchanged,
            removed=self.removed,
            seconds=self.seconds,
            error=error,
        )

    def as_summary(self) -> dict:
        """This outcome as plain JSON for the run's summary file."""
        return {
            "source": self.source,
            "status": self.status,
            "listed": self.listed,
            "written": self.written,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "records": self.records,
            "excluded": self.excluded,
            "record_errors": self.record_errors,
            "retained": self.retained,
            "seconds": round(self.seconds, 2),
            "error": self.error,
        }


class _SourceFailed(Exception):
    """A source's own failure, raised inside `_run_source` so every way a
    source can fail funnels through one `except`."""


# ---------------------------------------------------------------------------
# Previous catalog snapshot
# ---------------------------------------------------------------------------


def _git_show(path: Path) -> str | None:
    """`git show HEAD:<path>` from the repo root, or `None` if that isn't
    answerable here (path outside the repo, no git, not a repo, or the file
    isn't in HEAD yet). Never raises: an unavailable snapshot is a normal
    first-run state, not an error."""
    try:
        relative = path.resolve().relative_to(config.ROOT.resolve())
    except ValueError:
        return None
    try:
        completed = subprocess.run(
            ["git", "show", f"HEAD:{relative.as_posix()}"],
            cwd=config.ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def head_catalog(path: Path | None = None) -> list[dict]:
    """The catalog as committed at `HEAD`, or `[]` when there isn't one
    (no git, not a repo, path outside it, or the file was never
    committed). This is the "old" side `atlas diff` compares the working
    tree against."""
    path = path if path is not None else paths().catalog_file
    text = _git_show(path)
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_previous_catalog(path: Path | None = None) -> list[dict]:
    """The catalog this run is diffing against: the working-tree file if
    it exists, else the version committed at `HEAD`, else empty.

    The `HEAD` fallback is what makes a fresh clone's first refresh produce
    a *diff* rather than "everything is new": CI checks out the repo
    without `data/catalog/` being regenerated, so the committed catalog is
    the only previous state there is. An unreadable file is reported by the
    caller and treated as empty rather than crashing the run -- the diff is
    then noisy, which is visible, instead of the refresh dying.
    """
    path = path if path is not None else paths().catalog_file
    if path.exists():
        return io.read_jsonl(path)
    return head_catalog(path)


def read_manifests(
    sources: Iterable[str], *, raw_root: Path | None = None
) -> dict[str, dict | None]:
    """`{source: manifest | None}` for `sources`, straight off disk --
    what the last harvest of each source reported, without re-running it.
    `None` means that source has never been harvested."""
    root = raw_root if raw_root is not None else paths().raw
    manifests: dict[str, dict | None] = {}
    for source in sources:
        try:
            manifests[source] = RawStore(source, root=root).manifest()
        except (OSError, ValueError):
            manifests[source] = None
    return manifests


def manifest_results(
    sources: Iterable[str], *, raw_root: Path | None = None
) -> list[HarvestResult]:
    """The raw store's own state as `HarvestResult`s, for a changelog
    written outside a refresh run (`atlas diff`): each source's last
    harvest status and counts, or `skipped` where it has never run."""
    results = []
    for source, manifest in read_manifests(sources, raw_root=raw_root).items():
        if manifest is None:
            results.append(
                HarvestResult(source=source, status="skipped", error="no raw data")
            )
            continue
        counts = manifest.get("counts") or {}
        results.append(
            HarvestResult(
                source=source,
                status=manifest.get("status", "ok"),
                listed=counts.get("listed", 0),
                written=counts.get("written", 0),
                unchanged=counts.get("unchanged", 0),
                removed=counts.get("removed", 0),
                error=manifest.get("error"),
            )
        )
    return results


def _by_source(records: Iterable[dict]) -> dict[str, list[dict]]:
    """Group plain catalog dicts by their `source` field."""
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(str(record.get("source")), []).append(record)
    return grouped


def _exclusions_by_source(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """Group `excluded.jsonl` rows by the source their id names.

    Both kinds of exclusion carry a `<source>:<id>` id -- a normalizer's
    ("this raw record isn't a dataset") and a merge's ("folded into
    ...") -- so the prefix is enough to say whose exclusion it is.
    """
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        source = str(row.get("id", "")).split(":", 1)[0]
        grouped.setdefault(source, []).append(row)
    return grouped


def load_previous_exclusions(path: Path | None = None) -> list[dict]:
    """The previous `excluded.jsonl`, or `[]` when it is absent or
    unreadable -- a missing exclusions file is never worth failing a run
    over, since every exclusion a *selected* source produces is
    recomputed from scratch anyway."""
    path = path if path is not None else paths().excluded_file
    if not path.exists():
        return []
    try:
        return io.read_jsonl(path)
    except (OSError, ValueError):
        return []


def _parse_retained(rows: list[dict], outcome: SourceOutcome) -> list[Record]:
    """Previous-catalog `rows` for a failed/skipped/unselected source,
    parsed back into `Record`s.

    A row that no longer validates (the schema moved on since it was
    written) is dropped and counted as a record error rather than failing
    the run: the alternative is a validation error at the end of a refresh
    that never touched this source.
    """
    records: list[Record] = []
    for row in rows:
        try:
            records.append(Record.model_validate(row))
        except Exception as exc:  # noqa: BLE001 -- one stale row must not kill the run
            outcome.note_record_error(
                f"{row.get('id', '<no id>')}: retained record is no longer "
                f"valid: {type(exc).__name__}: {exc}"
            )
    return records


# ---------------------------------------------------------------------------
# Stage 1: harvest + normalize, one source at a time
# ---------------------------------------------------------------------------


def _run_source(
    source: str,
    *,
    offline: bool,
    fast: bool,
    registry: dict,
    normalizers: dict[str, tuple[Callable, Callable]],
    raw_root: Path,
) -> tuple[SourceOutcome, list[Record], list[dict], dict[str, str]]:
    """Harvest (unless `offline`) and normalize one source.

    Returns `(outcome, records, excluded_rows, texts)`, where `texts` maps
    each produced record's id to the free text the enrich stage classifies
    it from (`enrichment_text(envelope)`), and `excluded_rows` are
    `{"id", "reason"}` dicts for envelopes the normalizer deliberately left
    out.

    Every failure mode -- the harvester raising, the harvester *reporting*
    a failure, no normalizer registered, an unreadable manifest -- ends up
    as `status="failed"` with an `error`, never as an exception escaping to
    the caller.
    """
    outcome = SourceOutcome(source=source)
    started = time.monotonic()
    records: list[Record] = []
    excluded_rows: list[dict] = []
    texts: dict[str, str] = {}

    try:
        if not offline and source in registry:
            result = registry[source]().harvest(fast=fast)
            outcome.listed = result.listed
            outcome.written = result.written
            outcome.unchanged = result.unchanged
            outcome.removed = result.removed
            if result.status == "failed":
                raise _SourceFailed(result.error or "harvest failed")

        if source not in normalizers:
            raise _SourceFailed(f"no normalizer registered for {source!r}")
        normalize_fn, enrichment_text_fn = normalizers[source]

        store = RawStore(source, root=raw_root)
        manifest = store.manifest()
        if manifest is None:
            outcome.status = "skipped"
            outcome.error = "no raw data"
            outcome.seconds = time.monotonic() - started
            return outcome, records, excluded_rows, texts

        harvested_at = manifest["harvested_at"]
        envelopes = store.load_all()
        if offline:
            # Nothing was fetched, so "listed" is simply what the store
            # holds and every record of it counts as unchanged.
            outcome.listed = len(envelopes)
            outcome.unchanged = len(envelopes)

        for native_id, envelope in sorted(envelopes.items()):
            first_seen = manifest["records"].get(native_id, {}).get("first_seen")
            try:
                outcome_or_record = normalize_fn(
                    envelope, harvested_at=harvested_at, first_seen=first_seen
                )
            except Exception as exc:  # noqa: BLE001 -- one bad record, not one bad source
                outcome.note_record_error(
                    f"{source}:{native_id}: {type(exc).__name__}: {exc}"
                )
                continue

            if isinstance(outcome_or_record, Excluded):
                excluded_rows.append(
                    {
                        "id": f"{source}:{outcome_or_record.native_id}",
                        "reason": outcome_or_record.reason,
                    }
                )
                continue

            records.append(outcome_or_record)
            try:
                texts[outcome_or_record.id] = enrichment_text_fn(envelope)
            except Exception as exc:  # noqa: BLE001 -- no text just means rules-only
                outcome.note_record_error(
                    f"{source}:{native_id}: enrichment_text failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    except _SourceFailed as exc:
        outcome.status = "failed"
        outcome.error = str(exc)
        records, excluded_rows, texts = [], [], {}
    except Exception as exc:  # noqa: BLE001 -- one bad source must not kill the run
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
        records, excluded_rows, texts = [], [], {}

    outcome.records = len(records)
    outcome.excluded = len(excluded_rows)
    outcome.seconds = time.monotonic() - started
    return outcome, records, excluded_rows, texts


# ---------------------------------------------------------------------------
# Stage 2: enrichment (rules -> LLM -> MeSH -> ROR)
# ---------------------------------------------------------------------------


def _apply_rules(records: list[Record], texts: dict[str, str]) -> tuple[int, list[str]]:
    """Deterministic, network-free classification over every record that
    has an enrichment text. Replaces records in place; returns
    `(records_changed, errors)`."""
    changed = 0
    errors: list[str] = []
    for index, record in enumerate(records):
        text = texts.get(record.id)
        if not text:
            continue
        try:
            hits = rules.hints(
                text, source=record.source, raw_hints={"keywords": record.keywords}
            )
            updated = merge.apply_enrichment(record, None, hits)
        except Exception as exc:  # noqa: BLE001 -- one record, not the whole stage
            if len(errors) < MAX_REPORTED_RECORD_ERRORS:
                errors.append(f"{record.id}: rules: {type(exc).__name__}: {exc}")
            continue
        if updated is not record:
            records[index] = updated
            changed += 1
    return changed, errors


def _resolve_mesh(records: list[Record], *, offline: bool, cache_path: Path) -> int:
    """Fill `conditions[].mesh_id` for every condition label MeSH resolves.

    Never overwrites an id a record already carries, and never invents one:
    an unresolved label simply keeps `mesh_id=None`. Offline-safe -- the
    resolver serves cache hits and skips the network entirely.
    """
    labels = sorted(
        {c.label for r in records for c in r.conditions if c.mesh_id is None}
    )
    if not labels:
        return 0
    resolved = mesh.resolve_many(labels, cache_path=cache_path, offline=offline)
    filled = 0
    for index, record in enumerate(records):
        conditions = []
        changed = False
        for condition in record.conditions:
            mesh_id = (
                resolved.get(condition.label) if condition.mesh_id is None else None
            )
            if mesh_id:
                conditions.append(condition.model_copy(update={"mesh_id": mesh_id}))
                changed = True
                filled += 1
            else:
                conditions.append(condition)
        if changed:
            records[index] = record.model_copy(update={"conditions": conditions})
    return filled


def _resolve_ror(records: list[Record], *, offline: bool, cache_path: Path) -> int:
    """Fill `institutions[].ror_id` (and `country`, when the record has
    none) for every institution name ROR matches confidently.

    Same rules as :func:`_resolve_mesh`: fill only, never overwrite, never
    invent, offline-safe.
    """
    names = sorted(
        {i.name for r in records for i in r.institutions if i.ror_id is None}
    )
    if not names:
        return 0
    resolved = ror.resolve_many(names, cache_path=cache_path, offline=offline)
    filled = 0
    for index, record in enumerate(records):
        institutions = []
        changed = False
        for institution in record.institutions:
            match = (
                resolved.get(institution.name) if institution.ror_id is None else None
            )
            if not match or not match.get("ror_id"):
                institutions.append(institution)
                continue
            update: dict = {"ror_id": match["ror_id"]}
            if institution.country is None and match.get("country"):
                update["country"] = match["country"]
            institutions.append(institution.model_copy(update=update))
            changed = True
            filled += 1
        if changed:
            records[index] = record.model_copy(update={"institutions": institutions})
    return filled


def stage_enrich(
    records: list[Record],
    texts: dict[str, str],
    *,
    skip_enrich: bool = False,
    offline: bool = False,
    llm_backend: str | None = None,
    max_llm_calls: int | None = None,
) -> dict:
    """Run the enrichment stages over `records`, in place, and return the
    stats the changelog reports.

    Order is deliberate and cheap-first: deterministic rules over every
    record, then (unless `skip_enrich`) the LLM pass, then MeSH and ROR
    resolution of whatever labels the first two stages produced.

    `offline` makes the whole stage network-free without disabling it: the
    LLM backend becomes `NullBackend` **and** the call budget becomes 0, so
    `classify.enrich_records` still serves every cached answer (cache hits
    are free and cap-exempt) while never attempting -- and never counting
    as a failure -- a single model call. MeSH/ROR likewise resolve from
    their on-disk caches only.
    """
    p = paths()
    stats: dict = {
        "backend": "skipped" if skip_enrich else "none",
        "model": None,
        "calls": 0,
        "cache_hits": 0,
        "guard_drops": 0,
        "failures": 0,
        "records_enriched": 0,
        "cached_models": {},
        "errors": [],
    }

    rules_changed, rule_errors = _apply_rules(records, texts)
    stats["rules_applied"] = rules_changed
    stats["errors"].extend(rule_errors)

    if not skip_enrich:
        backend = llm.NullBackend() if offline else llm.select_backend(llm_backend)
        # `llm.LAST_ERRORS` is process-global and survives across runs; only
        # the entries this pass appended belong in this pass's stats.
        error_mark = len(llm.LAST_ERRORS)
        llm_stats = classify.enrich_records(
            records,
            texts,
            backend,
            max_calls=0 if offline else max_llm_calls,
            batch_size=LLM_BATCH_SIZE,
            cache_dir=p.llm_cache_dir,
        )
        stats.update(
            {
                "backend": llm_stats.backend,
                "model": llm_stats.model,
                "calls": llm_stats.calls,
                "cache_hits": llm_stats.cache_hits,
                "guard_drops": llm_stats.guard_drops,
                "failures": llm_stats.failures,
                "records_enriched": llm_stats.records_enriched,
                "cached_models": dict(llm_stats.cached_models),
            }
        )
        stats["errors"].extend(
            llm.LAST_ERRORS[error_mark:][:MAX_REPORTED_RECORD_ERRORS]
        )

    stats["mesh_resolved"] = _resolve_mesh(
        records, offline=offline, cache_path=p.mesh_cache
    )
    stats["ror_resolved"] = _resolve_ror(
        records, offline=offline, cache_path=p.ror_cache
    )
    return stats


# ---------------------------------------------------------------------------
# Stage 3: dedupe / merge / cross-links
# ---------------------------------------------------------------------------


def stage_dedupe(records: list[Record]) -> tuple[list[Record], list[Excluded]]:
    """Collapse same-dataset clusters, flag cross-source title collisions,
    repair `related` links, and add same-cohort links.

    Returns `(records, excluded)` -- `excluded` being every record folded
    into another by `merge.merge_cluster`, so a merge is auditable rather
    than a silent disappearance. The order matters: links can only be
    remapped once every cluster has a primary, and same-cohort links can
    only be added once the ids they point at are final.
    """
    merged: list[Record] = []
    excluded: list[Excluded] = []
    for group in dedupe.cluster(records):
        primary, folded = merge.merge_cluster(group)
        merged.append(primary)
        excluded.extend(folded)

    flagged = dedupe.flag_title_collisions(merged)
    if flagged:
        merged = [
            record.model_copy(update={"record_status": "needs_review"})
            if record.id in flagged and record.record_status == "active"
            else record
            for record in merged
        ]

    merged = merge.remap_related(merged, merge.build_alias_map(excluded))
    return dedupe.link_same_cohort(merged), excluded


def _finalize_record(record: Record) -> Record:
    """`record` with its vocabulary-backed lists deduplicated and rendered
    in vocabulary order.

    Normalizers build `domains`/`modalities` in whatever order the source
    reported them, and a merge appends -- both perfectly correct, both
    insertion-ordered. This is the one pass that guarantees the project's
    deterministic-output rule at the catalog's edge: the same set of values
    always serializes to the same bytes, whichever stage put them there.
    Returns `record` itself, unrebuilt, when nothing needed reordering.
    """
    domains = [d for d in vocab.DOMAINS if d in set(record.domains)]
    modalities = [m for m in vocab.MODALITIES if m in set(record.modalities)]
    if domains == record.domains and modalities == record.modalities:
        return record
    return record.model_copy(update={"domains": domains, "modalities": modalities})


# ---------------------------------------------------------------------------
# Stage 4: graph + diff
# ---------------------------------------------------------------------------


def stage_graph(records: list[Record]) -> tuple[dict, list[dict], dict]:
    """`(graph, search_index, stats)` for `records` -- the site's three
    generated inputs. Thin by design: `atlas.graph.build` owns every rule
    about what a node, a link, or a stat is."""
    return graphbuild.build_graph(records)


def stage_diff(
    old: list[dict],
    new: list[dict],
    *,
    date: str,
    source_results: list[HarvestResult],
    enrich_stats: dict | None = None,
    warnings: int = 0,
) -> tuple[diffmod.Diff, str]:
    """`(diff, changelog_markdown)` for this run: what changed since `old`,
    and the human-readable rendering of it plus the run's own outcomes."""
    catalog_diff = diffmod.diff_catalog(old, new)
    markdown = diffmod.render_changelog(
        catalog_diff,
        date=date,
        source_results=source_results,
        enrich_stats=enrich_stats,
        warnings=warnings,
        title=diffmod.summary_title(catalog_diff, date=date),
    )
    return catalog_diff, markdown


# ---------------------------------------------------------------------------
# The graph contract (also checked standalone by `atlas validate`)
# ---------------------------------------------------------------------------


def graph_contract_errors(
    graph: dict, index: list[dict], stats: dict, catalog: list[dict]
) -> list[str]:
    """Everything wrong with a built graph, relative to `catalog`.

    The contract the site relies on: node ids are unique, every link
    endpoint is a node that exists, `search-index.json` has exactly one row
    per catalogued dataset, and `stats.json`'s `record_count` agrees with
    the catalog. `removed` records are excluded from all three by
    `atlas.graph.build`, so they are excluded here too -- with nothing
    marked removed (the Phase 0 state) that is simply the catalog's own
    line count.
    """
    errors: list[str] = []

    node_ids = [node.get("id") for node in graph.get("nodes", [])]
    duplicates = sorted({n for n in node_ids if node_ids.count(n) > 1})
    if duplicates:
        errors.append(
            f"graph.json: {len(duplicates)} duplicate node id(s): "
            f"{', '.join(str(d) for d in duplicates[:5])}"
        )

    known_nodes = set(node_ids)
    dangling = sorted(
        {
            str(link.get(end))
            for link in graph.get("links", [])
            for end in ("source", "target")
            if link.get(end) not in known_nodes
        }
    )
    if dangling:
        errors.append(
            f"graph.json: {len(dangling)} link endpoint(s) with no node: "
            f"{', '.join(dangling[:5])}"
        )

    expected_ids = {
        str(record["id"])
        for record in catalog
        if record.get("record_status") != "removed"
    }
    index_ids = {str(row.get("id")) for row in index}
    missing = sorted(expected_ids - index_ids)
    if missing:
        errors.append(
            f"search-index.json: {len(missing)} catalog id(s) missing: "
            f"{', '.join(missing[:5])}"
        )
    extra = sorted(index_ids - expected_ids)
    if extra:
        errors.append(
            f"search-index.json: {len(extra)} id(s) not in the catalog: "
            f"{', '.join(extra[:5])}"
        )

    if stats.get("record_count") != len(expected_ids):
        errors.append(
            f"stats.json: record_count {stats.get('record_count')!r} != "
            f"{len(expected_ids)} catalog records"
        )
    return errors


def read_graph_outputs(graph_dir: Path) -> tuple[dict, list[dict], dict]:
    """The three generated graph files, parsed. Raises `FileNotFoundError`
    naming the first missing one -- the caller decides whether that is an
    error or simply "not built yet"."""
    outputs = []
    for name in ("graph.json", "search-index.json", "stats.json"):
        path = graph_dir / name
        if not path.exists():
            raise FileNotFoundError(str(path))
        outputs.append(json.loads(path.read_text(encoding="utf-8")))
    graph, index, stats = outputs
    return graph, index, stats


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_TABLE_HEADERS = (
    "source",
    "status",
    "listed",
    "records",
    "excluded",
    "errors",
    "retained",
    "seconds",
)


def _render_table(outcomes: list[SourceOutcome]) -> str:
    """The per-source table printed to stdout at the end of a run."""
    rows = [
        (
            outcome.source,
            outcome.status,
            str(outcome.listed),
            str(outcome.records),
            str(outcome.excluded),
            str(outcome.record_errors),
            str(outcome.retained),
            f"{outcome.seconds:.2f}",
        )
        for outcome in outcomes
    ]
    widths = [
        max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
        for i, header in enumerate(_TABLE_HEADERS)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(_TABLE_HEADERS, widths, strict=True))]
    lines += [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True))
        for row in rows
    ]
    return "\n".join(line.rstrip() for line in lines)


def _print_report(
    outcomes: list[SourceOutcome],
    *,
    counts: dict,
    warnings: int,
    timings: dict[str, float],
    dry_run: bool,
) -> None:
    print(_render_table(outcomes))
    for outcome in outcomes:
        for message in outcome.first_errors:
            print(f"  {outcome.source}: {message}")
        if outcome.record_errors > len(outcome.first_errors):
            hidden = outcome.record_errors - len(outcome.first_errors)
            print(f"  {outcome.source}: … and {hidden} more record error(s)")
    prefix = "would write " if dry_run else ""
    print(
        f"{prefix}catalog: {counts['records']} records, "
        f"{counts['excluded']} excluded, {warnings} validation warning(s)"
    )
    print(
        f"diff: +{counts['added']} new, ~{counts['changed']} changed, "
        f"-{counts['removed']} removed, {counts['unchanged']} unchanged"
    )
    print(
        "stages: "
        + ", ".join(f"{name} {seconds:.2f}s" for name, seconds in timings.items())
    )


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def _select_sources(
    requested: list[str] | None, available: list[str]
) -> tuple[list[str], str | None]:
    """`(sources, error)` -- `requested` validated against `available`, or
    all of `available` when nothing was requested."""
    if not requested:
        return available, None
    unknown = [name for name in requested if name not in available]
    if unknown:
        return [], (
            f"unknown source(s) {', '.join(sorted(unknown))} "
            f"(available: {', '.join(available)})"
        )
    return [name for name in available if name in set(requested)], None


def run(
    sources: list[str] | None = None,
    *,
    skip_enrich: bool = False,
    offline: bool = False,
    fast: bool = False,
    strict: bool = False,
    dry_run: bool = False,
    llm: str | None = None,
    max_llm_calls: int | None = None,
    report: Path | None = None,
    summary_json: Path | None = None,
) -> int:
    """Run the whole pipeline and return the process exit code.

    `sources` selects a subset (default: every registered source). Sources
    *not* selected keep their previous catalog records untouched, so
    `make refresh SOURCE=openneuro` refreshes one source without deleting
    the other three from the catalog.

    `offline` skips harvesting entirely and normalizes the raw store as it
    stands; it also puts the enrich stage in cache-only mode. `fast` is
    passed to each harvester. `skip_enrich` runs rules, MeSH and ROR but no
    LLM. `dry_run` performs every stage and writes no pipeline output
    (catalog, excluded, graph, changelog, report, summary) -- harvesting,
    if it runs at all, still updates `data/raw/`, and the enrich caches
    still fill, because those are inputs, not outputs.

    Exit codes: 0 ok, 1 validation errors (nothing written), 2 a source
    failed under `strict` or the run was misconfigured, 3 every selected
    source failed (nothing written).
    """
    p = paths()
    date = _today()
    timings: dict[str, float] = {}
    started = time.monotonic()

    registry = harvest.get_registry()
    normalizers = normalize.get_normalizers()
    available = sorted(set(registry) | set(normalizers))
    if not available:
        print("no sources registered", file=sys.stderr)
        return 2
    selected, error = _select_sources(sources, available)
    if error:
        print(error, file=sys.stderr)
        return 2

    try:
        previous = load_previous_catalog(p.catalog_file)
    except (OSError, ValueError) as exc:
        print(f"previous catalog unreadable ({exc}); diffing against nothing")
        previous = []
    previous_by_source = _by_source(previous)
    previous_exclusions = _exclusions_by_source(
        load_previous_exclusions(p.excluded_file)
    )

    # -- per source: harvest + normalize ---------------------------------
    outcomes: list[SourceOutcome] = []
    records: list[Record] = []
    excluded_rows: list[dict] = []
    texts: dict[str, str] = {}
    # Sources whose records came from the snapshot rather than this run --
    # their exclusions have to come from the snapshot too, or the catalog
    # would keep a source's records while silently forgetting what it left
    # out. Merge-time exclusions are *not* carried over: dedupe runs over
    # the whole catalog every time, so it re-derives all of those itself.
    retained_sources: list[str] = []

    for source in selected:
        outcome, source_records, source_excluded, source_texts = _run_source(
            source,
            offline=offline,
            fast=fast,
            registry=registry,
            normalizers=normalizers,
            raw_root=p.raw,
        )
        if outcome.status != "ok":
            retained = _parse_retained(previous_by_source.get(source, []), outcome)
            outcome.retained = len(retained)
            records.extend(retained)
            retained_sources.append(source)
        else:
            records.extend(source_records)
            excluded_rows.extend(source_excluded)
            texts.update(source_texts)
        outcomes.append(outcome)

    # Sources this run did not touch keep every record they had.
    for source in available:
        if source not in set(selected):
            untouched = SourceOutcome(source=source, status="unselected")
            retained = _parse_retained(previous_by_source.get(source, []), untouched)
            records.extend(retained)
            retained_sources.append(source)

    timings["harvest+normalize"] = time.monotonic() - started

    failed = [o for o in outcomes if o.status == "failed"]
    if len(failed) == len(outcomes):
        timings["total"] = time.monotonic() - started
        _print_report(
            outcomes,
            counts={
                "records": 0,
                "excluded": 0,
                "added": 0,
                "changed": 0,
                "removed": 0,
                "unchanged": 0,
            },
            warnings=0,
            timings=timings,
            dry_run=dry_run,
        )
        print("every source failed; nothing written", file=sys.stderr)
        return 3

    # -- enrich ----------------------------------------------------------
    stage_started = time.monotonic()
    try:
        enrich_stats = stage_enrich(
            records,
            texts,
            skip_enrich=skip_enrich,
            offline=offline,
            llm_backend=llm,
            max_llm_calls=max_llm_calls,
        )
    except ValueError as exc:  # an unknown --llm backend name
        print(str(exc), file=sys.stderr)
        return 2
    timings["enrich"] = time.monotonic() - stage_started

    # -- dedupe ----------------------------------------------------------
    stage_started = time.monotonic()
    records, merge_excluded = stage_dedupe(records)
    excluded_rows.extend(
        {"id": item.native_id, "reason": item.reason} for item in merge_excluded
    )
    records = [_finalize_record(record) for record in records]
    records.sort(key=lambda record: record.id)

    known_ids = {record.id for record in records}
    seen_exclusions = {row["id"] for row in excluded_rows}
    for source in retained_sources:
        for row in previous_exclusions.get(source, []):
            if row["id"] in seen_exclusions or row["id"] in known_ids:
                continue
            seen_exclusions.add(row["id"])
            excluded_rows.append(row)
    excluded_rows.sort(key=lambda row: (row["id"], row["reason"]))
    timings["dedupe"] = time.monotonic() - stage_started

    # -- validate (before writing anything) -------------------------------
    stage_started = time.monotonic()
    rows = [record.model_dump(mode="json") for record in records]
    errors, warnings = schema.validate_records(rows)
    timings["validate"] = time.monotonic() - stage_started
    if errors:
        for message in errors[:50]:
            print(f"error: {message}", file=sys.stderr)
        if len(errors) > 50:
            print(f"error: … and {len(errors) - 50} more", file=sys.stderr)
        print(f"{len(errors)} validation error(s); nothing written", file=sys.stderr)
        return 1

    # -- graph + diff -----------------------------------------------------
    stage_started = time.monotonic()
    graph, index, graph_stats = stage_graph(records)
    timings["graph"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    catalog_diff, markdown = stage_diff(
        previous,
        rows,
        date=date,
        source_results=[outcome.as_harvest_result() for outcome in outcomes],
        enrich_stats=enrich_stats,
        warnings=len(warnings),
    )
    timings["diff"] = time.monotonic() - stage_started

    exit_code = 0
    if failed and strict:
        exit_code = 2

    summary = {
        "title": diffmod.summary_title(catalog_diff, date=date),
        "date": date,
        "counts": {
            **catalog_diff.counts,
            "records": len(records),
            "excluded": len(excluded_rows),
            "warnings": len(warnings),
        },
        "sources": [outcome.as_summary() for outcome in outcomes],
        "exit_code": exit_code,
    }

    # -- write ------------------------------------------------------------
    stage_started = time.monotonic()
    if not dry_run:
        io.write_jsonl(p.catalog_file, rows)
        io.write_jsonl(p.excluded_file, excluded_rows)
        graphbuild.write_outputs(graph, index, graph_stats, out_dir=p.graph)
        dated_path, latest_path = diffmod.write_changelog(
            markdown, date=date, out_dir=p.changelog
        )
        if report is not None and Path(report).resolve() not in (
            dated_path.resolve(),
            latest_path.resolve(),
        ):
            io.write_atomic(Path(report), markdown)
        if summary_json is not None:
            io.write_atomic(Path(summary_json), io.pretty_json(summary))
        print(f"catalog: {p.catalog_file}")
        print(f"graph:   {p.graph}")
        print(f"changelog: {dated_path}")
    timings["write"] = time.monotonic() - stage_started
    timings["total"] = time.monotonic() - started

    _print_report(
        outcomes,
        counts={
            **catalog_diff.counts,
            "records": len(records),
            "excluded": len(excluded_rows),
        },
        warnings=len(warnings),
        timings=timings,
        dry_run=dry_run,
    )
    if failed:
        names = ", ".join(outcome.source for outcome in failed)
        print(f"{len(failed)} source(s) failed: {names}", file=sys.stderr)
    return exit_code


# ---------------------------------------------------------------------------
# python -m atlas.refresh
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """`python -m atlas.refresh`'s parser -- the same flags `atlas refresh`
    takes, so the two entry points are genuinely interchangeable."""
    parser = argparse.ArgumentParser(
        prog="python -m atlas.refresh",
        description="Run the Clinical Data Atlas pipeline end to end",
    )
    parser.add_argument(
        "--sources", default=None, help="Comma-separated list of sources (default: all)"
    )
    parser.add_argument(
        "--skip-enrich", action="store_true", help="Skip the LLM classification pass"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Don't harvest; normalize the raw store as it stands (caches only)",
    )
    parser.add_argument(
        "--fast", action="store_true", help="Skip optional per-record detail requests"
    )
    parser.add_argument(
        "--strict", action="store_true", help="Exit 2 if any source failed"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Run every stage, write no output"
    )
    parser.add_argument("--llm", default=None, help="LLM backend override")
    parser.add_argument(
        "--max-llm-calls", type=int, default=None, help="Cap the number of LLM calls"
    )
    parser.add_argument(
        "--report", default=None, help="Also write the changelog markdown here"
    )
    parser.add_argument(
        "--summary-json", default=None, help="Write a machine-readable run summary here"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` and run the pipeline."""
    args = build_parser().parse_args(argv)
    return run(
        parse_sources(args.sources),
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


def parse_sources(value: str | None) -> list[str] | None:
    """`"a, b"` -> `["a", "b"]`; `None`/empty -> `None` (meaning "all").
    Shared with `atlas.cli` so both spellings of the flag behave the
    same."""
    if not value:
        return None
    names = [part.strip() for part in value.split(",") if part.strip()]
    return names or None


if __name__ == "__main__":
    raise SystemExit(main())
