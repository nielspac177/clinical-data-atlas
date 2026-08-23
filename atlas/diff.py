"""Catalog diffing and the monthly-refresh changelog.

`diff_catalog` compares two full catalog snapshots -- lists of plain
record dicts, shaped like `atlas.schema.Record.model_dump()` output, but
this module never imports the schema and only ever looks at `id`, so any
dict-with-an-id works -- and returns a `Diff`: which ids are new, which
disappeared, and which common ids differ, down to the exact field path.

`render_changelog` turns a `Diff` plus a run's `HarvestResult`s into the
markdown body of the PR a monthly refresh opens; `write_changelog` writes
that text to `data/changelog/<date>.md` and `data/changelog/latest.md`.
`summary_title` renders the same counts as a one-line PR title.

Determinism matters here as much as correctness: the same two snapshots
must always diff to the same `Diff`, and the same `Diff` must always
render to the same markdown, byte for byte, no matter how many times or
in what process. That's why every collection here is walked in sorted
order and why the only "time" that ever appears in the output is the
caller-supplied `date` string -- nothing in this module reads the clock.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas import config, io
from atlas.harvest.base import HarvestResult

# Dotted fields that change on every run without describing a real change
# to the record -- diffing them would make every refresh's changelog
# "changed" section noisy with nothing a human needs to review.
DEFAULT_IGNORE: tuple[str, ...] = (
    "provenance.last_verified",
    "provenance.enrichment.at",
)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@dataclass
class Diff:
    """The result of comparing two catalog snapshots by `id`.

    `added`/`removed` carry the full record dict for each id that's only
    on one side, so a renderer can show its name/url without a second
    lookup. `changed` maps every id present (post-`ignore`) on both sides
    but with at least one field-level difference to that difference, as
    `(json_pointer_path, old_value, new_value)` tuples -- paths like
    `"/access"`, `"/modalities/1"`, `"/provenance/enrichment/method"`.

    `names` maps each `changed` id to a display name (see `diff_catalog`)
    -- `changed`'s tuples alone don't carry the record's name when `name`
    itself is not one of the fields that changed, but the changelog's
    "Changed" section header line needs it and `added`/`removed`, unlike
    `changed`, already have it via their full dicts.

    `unchanged` is the count of common ids with no diff after `ignore`
    stripping. It's a plain count, not a list of records, because nothing
    in this module ever needs to show *which* records were unchanged --
    only `counts` does, alongside `added`/`removed`/`changed`'s own
    `len()`.
    """

    added: list[dict]
    removed: list[dict]
    changed: dict[str, list[tuple[str, Any, Any]]]
    unchanged: int = 0
    names: dict[str, str] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        """`{"added", "removed", "changed", "unchanged"}` -> count."""
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
            "unchanged": self.unchanged,
        }


# ---------------------------------------------------------------------------
# diff_catalog
# ---------------------------------------------------------------------------


def _pop_dotted(obj: dict, dotted_path: str) -> None:
    """Remove `dotted_path` (e.g. "provenance.last_verified") from `obj`
    in place. Missing paths are ignored; only dict nesting is traversed --
    a list anywhere along the path stops the walk silently rather than
    raising (mirrors `atlas.harvest.base._pop_dotted`; duplicated here
    since this task doesn't touch that module)."""
    *parents, leaf = dotted_path.split(".")
    node = obj
    for part in parents:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(leaf, None)


def _strip_ignored(record: dict, ignore: tuple[str, ...]) -> dict:
    """A deep copy of `record` with every `ignore` dotted path removed,
    ready to diff. Returns `record` itself, uncopied, when `ignore` is
    empty -- and never mutates `record` either way, so `diff_catalog`
    never touches its caller's data."""
    if not ignore:
        return record
    stripped = copy.deepcopy(record)
    for dotted_path in ignore:
        _pop_dotted(stripped, dotted_path)
    return stripped


# Sentinel for "this key doesn't exist on this side" -- distinct from a
# real `None` value, so a key that's merely absent from one side (e.g.
# comparing dicts from two schema versions) is still reported as a
# change rather than silently compared as `None == None`.
_MISSING = object()


def _diff_value(
    path: str, old_v: Any, new_v: Any, diffs: list[tuple[str, Any, Any]]
) -> None:
    """Append every leaf-level difference between `old_v` and `new_v` to
    `diffs`, as `(json_pointer_path, old, new)`.

    Both dicts: recurse over the sorted union of keys, one path segment
    per key. Both lists of equal length: recurse element-wise, path
    segment = index. Anything else (scalars, lists of different length,
    or a type mismatch between the two sides) is compared and reported
    as one whole-value change -- kept simple and deterministic rather
    than attempting a element-level diff of a resized list.
    """
    if isinstance(old_v, dict) and isinstance(new_v, dict):
        for key in sorted(old_v.keys() | new_v.keys()):
            _diff_value(
                f"{path}/{key}",
                old_v.get(key, _MISSING),
                new_v.get(key, _MISSING),
                diffs,
            )
        return

    if isinstance(old_v, list) and isinstance(new_v, list) and len(old_v) == len(new_v):
        for index, (old_item, new_item) in enumerate(zip(old_v, new_v)):
            _diff_value(f"{path}/{index}", old_item, new_item, diffs)
        return

    if old_v != new_v:
        resolved_old = None if old_v is _MISSING else old_v
        resolved_new = None if new_v is _MISSING else new_v
        diffs.append((path, resolved_old, resolved_new))


def _display_name(record: dict, fallback: str) -> str:
    """`record["name"]` if present and truthy, else `fallback` (the
    record's own id -- always at least *something* to show)."""
    return record.get("name") or fallback


def diff_catalog(
    old: list[dict],
    new: list[dict],
    *,
    ignore: tuple[str, ...] = DEFAULT_IGNORE,
) -> Diff:
    """Compare two catalog snapshots by `id` and return a `Diff`.

    Both lists are indexed by `id`; ids only in `new` are `added`, ids
    only in `old` are `removed`, and every id common to both is compared
    field by field (see `_diff_value`) after each side has had every
    `ignore` dotted path removed from a deep copy (`old`/`new` themselves
    are never mutated). A common id with at least one surviving
    difference lands in `changed`; otherwise it's counted in
    `unchanged`.

    Deterministic by construction: `added`/`removed` are sorted by id,
    `changed`'s keys are visited in sorted-id order, and `_diff_value`
    visits dict keys in sorted order too -- so the same two snapshots
    always produce the same `Diff`, field for field, path for path.
    """
    old_by_id = {record["id"]: record for record in old}
    new_by_id = {record["id"]: record for record in new}

    added_ids = sorted(new_by_id.keys() - old_by_id.keys())
    removed_ids = sorted(old_by_id.keys() - new_by_id.keys())
    common_ids = sorted(old_by_id.keys() & new_by_id.keys())

    added = [new_by_id[record_id] for record_id in added_ids]
    removed = [old_by_id[record_id] for record_id in removed_ids]

    changed: dict[str, list[tuple[str, Any, Any]]] = {}
    names: dict[str, str] = {}
    unchanged = 0
    for record_id in common_ids:
        old_record = old_by_id[record_id]
        new_record = new_by_id[record_id]
        diffs: list[tuple[str, Any, Any]] = []
        _diff_value(
            "",
            _strip_ignored(old_record, ignore),
            _strip_ignored(new_record, ignore),
            diffs,
        )
        if diffs:
            changed[record_id] = diffs
            names[record_id] = _display_name(
                new_record, _display_name(old_record, record_id)
            )
        else:
            unchanged += 1

    return Diff(
        added=added, removed=removed, changed=changed, unchanged=unchanged, names=names
    )


# ---------------------------------------------------------------------------
# render_changelog
# ---------------------------------------------------------------------------

# Markdown-list caps: a refresh touching thousands of records must not
# turn the changelog into an unreadable wall of bullets.
_MAX_LISTED = 200
_MAX_PATHS_PER_RECORD = 10
_TRUNCATE_AT = 120


# Non-whitespace control characters (deliberately excludes tab/LF/VT/FF/CR --
# 0x09-0x0d -- which are whitespace and handled by the collapse below instead
# of being stripped outright).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _plain(text: str) -> str:
    """Make free text safe to interpolate anywhere in the rendered
    markdown. Every value this is applied to (a name, url, error message,
    or diff value) can originate from a harvested source and is otherwise
    untrusted -- without this, one nasty field could split a table row,
    forge a heading, or break out of a backtick span.

    Non-whitespace control characters are dropped outright; every run of
    whitespace -- carriage returns, newlines, tabs, or plain spaces --
    collapses to a single space and the ends are trimmed, so the result
    is always one line no matter how many newlines (or blank lines) were
    in the input. That single-line guarantee is what keeps a multi-line
    `HarvestResult.error` on one Sources table row and stops a value with
    a blank line followed by a leading `#` from forging a heading.
    Finally, backticks become `'` (so a value can't break out of the
    surrounding `` `...` `` span it's rendered in) and `|` is escaped (so
    it can't be mistaken for a table column separator -- harmless outside
    a table too).

    Not applied to ids or json-pointer paths: both are built from
    schema-controlled strings (the id regex, fixed field names/list
    indices), never from free text.
    """
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _WHITESPACE_RUN_RE.sub(" ", text).strip()
    text = text.replace("`", "'")
    return text.replace("|", "\\|")


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_plain(cell) for cell in row) + " |")
    return "\n".join(lines)


def _display(value: Any) -> str:
    """Turn one diff leaf value into its raw string form: `None` as
    `"null"` (these are JSON records), dicts/lists as compact JSON,
    everything else via `str()`. Markdown-safety (control characters,
    newlines, backticks, `|`) is `_plain`'s job, applied separately by
    the caller."""
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _truncate(text: str, limit: int = _TRUNCATE_AT) -> str:
    """`text`, cut to `limit` characters with a trailing "…" if it was
    longer. Truncation happens here, in the renderer, so `Diff` itself
    always keeps the full values."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _summary_line(counts: dict[str, int]) -> str:
    return (
        f"**+{counts['added']} new, ~{counts['changed']} changed, "
        f"-{counts['removed']} removed, {counts['unchanged']} unchanged**"
    )


def _sources_section(results: list[HarvestResult]) -> str:
    rows = [
        (
            result.source,
            result.status,
            str(result.listed),
            str(result.written),
            str(result.unchanged),
            str(result.removed),
            f"{result.seconds:.2f}",
            result.error or "",
        )
        for result in sorted(results, key=lambda result: result.source)
    ]
    table = _table(
        (
            "source",
            "status",
            "listed",
            "written",
            "unchanged",
            "removed",
            "seconds",
            "error",
        ),
        rows,
    )
    return f"## Sources\n\n{table}"


def _failure_line(result: HarvestResult) -> str:
    line = f"- `{result.source}`: {result.status}"
    if result.error:
        line += f" — {_plain(result.error)}"
    return line


def _failures_section(results: list[HarvestResult]) -> str:
    failed = sorted(
        (result for result in results if result.status != "ok"),
        key=lambda result: result.source,
    )
    if not failed:
        return "## Failures\n\nnone"
    return "## Failures\n\n" + "\n".join(_failure_line(result) for result in failed)


def _record_line(record: dict) -> str:
    name = _plain(record.get("name", ""))
    url = _plain(record.get("url", ""))
    return f"- `{record['id']}` — {name} — {url}"


def _record_list_section(heading: str, records: list[dict]) -> str:
    n = len(records)
    if n == 0:
        return f"## {heading} (0)\n\nnone"
    lines = [_record_line(record) for record in records[:_MAX_LISTED]]
    if n > _MAX_LISTED:
        lines.append(f"- … and {n - _MAX_LISTED} more")
    return f"## {heading} ({n})\n\n" + "\n".join(lines)


def _changed_record_block(
    record_id: str, diffs: list[tuple[str, Any, Any]], name: str
) -> str:
    lines = [f"- `{record_id}` — {_plain(name)}"]
    for path, old_v, new_v in diffs[:_MAX_PATHS_PER_RECORD]:
        old_text = _truncate(_plain(_display(old_v)))
        new_text = _truncate(_plain(_display(new_v)))
        lines.append(f"  - `{path}`: `{old_text}` → `{new_text}`")
    remaining = len(diffs) - _MAX_PATHS_PER_RECORD
    if remaining > 0:
        lines.append(f"  - … and {remaining} more")
    return "\n".join(lines)


def _changed_section(d: Diff) -> str:
    n = len(d.changed)
    if n == 0:
        return "## Changed (0)\n\nnone"
    shown_ids = list(d.changed)[:_MAX_LISTED]
    blocks = [
        _changed_record_block(
            record_id, d.changed[record_id], d.names.get(record_id, record_id)
        )
        for record_id in shown_ids
    ]
    if n > _MAX_LISTED:
        blocks.append(f"- … and {n - _MAX_LISTED} more")
    return f"## Changed ({n})\n\n" + "\n".join(blocks)


def _enrichment_section(stats: dict | None) -> str:
    if stats is None:
        return "## Enrichment\n\nnot run"
    lines = [
        f"- backend: `{_plain(str(stats.get('backend', 'unknown')))}`",
        f"- calls: {_plain(str(stats.get('calls', 0)))}",
        f"- cache_hits: {_plain(str(stats.get('cache_hits', 0)))}",
        f"- guard_drops: {_plain(str(stats.get('guard_drops', 0)))}",
        f"- failures: {_plain(str(stats.get('failures', 0)))}",
    ]
    return "## Enrichment\n\n" + "\n".join(lines)


def render_changelog(
    d: Diff,
    *,
    date: str,
    source_results: list[HarvestResult],
    enrich_stats: dict | None = None,
    warnings: int = 0,
    title: str | None = None,
) -> str:
    """Render `d` (plus this run's other outcomes) as the markdown body
    of a monthly refresh's changelog entry / PR description.

    Section order: title, summary line, `## Sources` (one row per
    `source_results` entry), `## Failures` (non-"ok" sources, or "none"),
    `## Added`/`## Removed` (id/name/url, capped), `## Changed` (id/name
    then each field-level diff, both capped), `## Enrichment` (from
    `enrich_stats`, or "not run"), `## Validation warnings`.

    Deterministic for the same inputs: no wall-clock read anywhere here,
    `date` is the only "time" in the output, and every list this
    function itself iterates (sources, failures) is sorted first --
    `d`'s own collections are already sorted by `diff_catalog`.
    """
    heading = title if title is not None else f"Refresh {date}"
    blocks = [
        f"# {heading}",
        _summary_line(d.counts),
        _sources_section(source_results),
        _failures_section(source_results),
        _record_list_section("Added", d.added),
        _record_list_section("Removed", d.removed),
        _changed_section(d),
        _enrichment_section(enrich_stats),
        f"## Validation warnings: {warnings}",
    ]
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# write_changelog
# ---------------------------------------------------------------------------


def write_changelog(
    markdown: str, *, date: str, out_dir: Path = config.CHANGELOG
) -> tuple[Path, Path]:
    """Write `markdown` to `<out_dir>/<date>.md` and `<out_dir>/latest.md`
    (byte-identical, both written atomically), and return their paths as
    `(dated_path, latest_path)`."""
    out_dir = Path(out_dir)
    dated_path = out_dir / f"{date}.md"
    latest_path = out_dir / "latest.md"
    io.write_atomic(dated_path, markdown)
    io.write_atomic(latest_path, markdown)
    return dated_path, latest_path


# ---------------------------------------------------------------------------
# summary_title
# ---------------------------------------------------------------------------


def summary_title(d: Diff, *, date: str) -> str:
    """One-line PR-title-style summary, e.g. `"Monthly refresh
    2026-09-01: +14 new, ~37 changed, -2 removed"` -- the same counts as
    `d.counts` / `render_changelog`'s summary line, minus `unchanged`
    (not newsworthy in a title)."""
    counts = d.counts
    return (
        f"Monthly refresh {date}: +{counts['added']} new, "
        f"~{counts['changed']} changed, -{counts['removed']} removed"
    )
