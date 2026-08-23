"""Tests for atlas.diff: catalog diffing (Diff, diff_catalog) and the
changelog renderer/writer (render_changelog, write_changelog,
summary_title).

Records here are plain dicts shaped like `atlas.schema.Record.model_dump()`
output, but diff.py itself is schema-agnostic (it only ever looks at
`id`), so tests keep them minimal rather than fully schema-valid.
"""

from __future__ import annotations

from atlas import diff
from atlas.diff import Diff
from atlas.harvest.base import HarvestResult


def _record(id: str = "openneuro:ds000001", **overrides: object) -> dict:
    """A minimal catalog-record-shaped dict; override fields as needed."""
    record: dict = {
        "id": id,
        "source": "openneuro",
        "name": "Example dataset",
        "url": f"https://openneuro.org/datasets/{id.split(':', 1)[1]}",
        "access": "open",
        "modalities": ["MRI", "CT"],
        "provenance": {
            "harvested_via": "graphql",
            "harvested_at": "2026-08-01",
            "last_verified": "2026-08-22",
            "enrichment": {"method": "rules", "at": "2026-08-01", "fields": {}},
        },
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# diff_catalog: added / removed / changed / unchanged counts
# ---------------------------------------------------------------------------


def test_added_record_is_reported():
    new_rec = _record("openneuro:ds000002")
    d = diff.diff_catalog([], [new_rec])
    assert d.added == [new_rec]
    assert d.removed == []
    assert d.changed == {}
    assert d.counts == {"added": 1, "removed": 0, "changed": 0, "unchanged": 0}


def test_removed_record_is_reported():
    old_rec = _record("openneuro:ds000001")
    d = diff.diff_catalog([old_rec], [])
    assert d.removed == [old_rec]
    assert d.added == []
    assert d.counts == {"added": 0, "removed": 1, "changed": 0, "unchanged": 0}


def test_identical_record_is_unchanged():
    rec = _record()
    d = diff.diff_catalog([rec], [_record()])
    assert d.changed == {}
    assert d.counts == {"added": 0, "removed": 0, "changed": 0, "unchanged": 1}


def test_changed_field_is_reported():
    old_rec = _record(access="open")
    new_rec = _record(access="registration")
    d = diff.diff_catalog([old_rec], [new_rec])
    assert d.counts == {"added": 0, "removed": 0, "changed": 1, "unchanged": 0}
    assert d.changed[old_rec["id"]] == [("/access", "open", "registration")]


def test_synthetic_old_new_mixed_counts():
    """One added, one removed, one changed, one unchanged, in one call."""
    stay_same = _record("openneuro:same")
    to_remove = _record("openneuro:gone")
    to_change_old = _record("openneuro:changed", access="open")
    to_change_new = _record("openneuro:changed", access="credentialed")
    to_add = _record("openneuro:new")

    old = [stay_same, to_remove, to_change_old]
    new = [_record("openneuro:same"), to_change_new, to_add]

    d = diff.diff_catalog(old, new)

    assert d.counts == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}
    assert d.added == [to_add]
    assert d.removed == [to_remove]
    assert "openneuro:changed" in d.changed


# ---------------------------------------------------------------------------
# diff_catalog: ignore list
# ---------------------------------------------------------------------------


def test_default_ignore_constant_matches_the_documented_default():
    assert diff.DEFAULT_IGNORE == (
        "provenance.last_verified",
        "provenance.enrichment.at",
    )


def test_default_ignore_hides_last_verified_only_difference():
    old_rec = _record()
    new_rec = _record()
    new_rec["provenance"]["last_verified"] = "2026-09-01"
    assert (
        old_rec["provenance"]["last_verified"] != new_rec["provenance"]["last_verified"]
    )

    d = diff.diff_catalog([old_rec], [new_rec])

    assert d.changed == {}
    assert d.counts["unchanged"] == 1


def test_default_ignore_hides_enrichment_at_only_difference():
    old_rec = _record()
    new_rec = _record()
    new_rec["provenance"]["enrichment"]["at"] = "2026-09-01"

    d = diff.diff_catalog([old_rec], [new_rec])

    assert d.changed == {}
    assert d.counts["unchanged"] == 1


def test_ignored_field_change_combined_with_real_change_still_reports_only_real_change():
    old_rec = _record(access="open")
    new_rec = _record(access="registration")
    new_rec["provenance"]["last_verified"] = "2026-09-01"

    d = diff.diff_catalog([old_rec], [new_rec])

    paths = [path for path, _old, _new in d.changed[old_rec["id"]]]
    assert paths == ["/access"]


def test_custom_ignore_list_is_honoured():
    old_rec = _record(access="open")
    new_rec = _record(access="registration")

    d = diff.diff_catalog([old_rec], [new_rec], ignore=("access",))

    assert d.changed == {}
    assert d.counts["unchanged"] == 1


def test_empty_ignore_list_compares_everything():
    old_rec = _record()
    new_rec = _record()
    new_rec["provenance"]["last_verified"] = "2026-09-01"

    d = diff.diff_catalog([old_rec], [new_rec], ignore=())

    assert old_rec["id"] in d.changed


def test_ignore_path_absent_from_record_does_not_crash_and_stays_unchanged():
    # `ignore` naming a path that exists on neither side (or only
    # partway, e.g. a missing parent dict) must be a silent no-op, not
    # a KeyError/AttributeError.
    old_rec = _record()
    new_rec = _record()

    d = diff.diff_catalog(
        [old_rec],
        [new_rec],
        ignore=("nonexistent_field", "provenance.nonexistent", "no.such.nesting"),
    )

    assert d.changed == {}
    assert d.counts["unchanged"] == 1


def test_diff_catalog_does_not_mutate_inputs():
    old_rec = _record()
    new_rec = _record()
    new_rec["provenance"]["last_verified"] = "2026-09-01"
    new_rec["access"] = "registration"

    diff.diff_catalog([old_rec], [new_rec])

    assert old_rec["provenance"]["last_verified"] == "2026-08-22"
    assert new_rec["provenance"]["last_verified"] == "2026-09-01"
    assert new_rec["access"] == "registration"


# ---------------------------------------------------------------------------
# diff_catalog: nested + list-element paths
# ---------------------------------------------------------------------------


def test_nested_dict_path_is_reported():
    old_rec = _record()
    old_rec["provenance"]["enrichment"]["method"] = "rules"
    new_rec = _record()
    new_rec["provenance"]["enrichment"]["method"] = "llm"

    d = diff.diff_catalog([old_rec], [new_rec])

    assert ("/provenance/enrichment/method", "rules", "llm") in d.changed[old_rec["id"]]


def test_list_element_path_for_equal_length_lists():
    old_rec = _record(modalities=["MRI", "CT"])
    new_rec = _record(modalities=["MRI", "PET"])

    d = diff.diff_catalog([old_rec], [new_rec])

    assert d.changed[old_rec["id"]] == [("/modalities/1", "CT", "PET")]


def test_list_of_different_length_is_compared_as_whole_value():
    old_rec = _record(modalities=["MRI"])
    new_rec = _record(modalities=["MRI", "CT"])

    d = diff.diff_catalog([old_rec], [new_rec])

    assert d.changed[old_rec["id"]] == [("/modalities", ["MRI"], ["MRI", "CT"])]


def test_pure_list_reordering_counts_as_changed():
    # Same elements, same length, different order -- element-wise
    # comparison means this is NOT treated as unchanged just because
    # the set of modalities is identical.
    old_rec = _record(modalities=["MRI", "CT"])
    new_rec = _record(modalities=["CT", "MRI"])

    d = diff.diff_catalog([old_rec], [new_rec])

    assert old_rec["id"] in d.changed
    paths = {path for path, _old, _new in d.changed[old_rec["id"]]}
    assert paths == {"/modalities/0", "/modalities/1"}


# ---------------------------------------------------------------------------
# diff_catalog: determinism (sorted ids)
# ---------------------------------------------------------------------------


def test_added_and_removed_are_sorted_by_id_regardless_of_input_order():
    new_recs = [_record("openneuro:c"), _record("openneuro:a"), _record("openneuro:b")]
    d = diff.diff_catalog([], new_recs)
    assert [r["id"] for r in d.added] == ["openneuro:a", "openneuro:b", "openneuro:c"]

    old_recs = [_record("openneuro:z"), _record("openneuro:x"), _record("openneuro:y")]
    d = diff.diff_catalog(old_recs, [])
    assert [r["id"] for r in d.removed] == ["openneuro:x", "openneuro:y", "openneuro:z"]


# ---------------------------------------------------------------------------
# Diff.counts
# ---------------------------------------------------------------------------


def test_diff_counts_reflects_all_four_buckets():
    d = Diff(
        added=[_record("a:1")],
        removed=[_record("a:2"), _record("a:3")],
        changed={"a:4": [("/access", "open", "registration")]},
        unchanged=5,
    )
    assert d.counts == {"added": 1, "removed": 2, "changed": 1, "unchanged": 5}


# ---------------------------------------------------------------------------
# render_changelog: headings + table
# ---------------------------------------------------------------------------


def _ok_result(source: str = "openneuro") -> HarvestResult:
    return HarvestResult(
        source=source,
        status="ok",
        listed=10,
        written=2,
        unchanged=8,
        removed=0,
        seconds=1.234,
    )


def test_render_changelog_has_title_and_section_headings():
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(d, date="2026-09-01", source_results=[_ok_result()])

    assert md.startswith("# Refresh 2026-09-01\n")
    for heading in (
        "## Sources",
        "## Failures",
        "## Added (0)",
        "## Removed (0)",
        "## Changed (0)",
        "## Enrichment",
        "## Validation warnings: 0",
    ):
        assert heading in md


def test_render_changelog_title_override():
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(
        d, date="2026-09-01", source_results=[], title="Monthly refresh 2026-09-01"
    )
    assert md.startswith("# Monthly refresh 2026-09-01\n")
    assert "# Refresh 2026-09-01" not in md


def test_render_changelog_sources_table_header_row():
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(d, date="2026-09-01", source_results=[_ok_result()])

    assert (
        "| source | status | listed | written | unchanged | removed | seconds | error |"
        in md
    )
    assert "| openneuro | ok | 10 | 2 | 8 | 0 | 1.23 |" in md


def test_render_changelog_sources_rows_sorted_by_source():
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(
        d, date="2026-09-01", source_results=[_ok_result("tcia"), _ok_result("gdc")]
    )
    assert md.index("| gdc |") < md.index("| tcia |")


def test_render_changelog_summary_line_has_all_four_counts():
    old = [_record("a:1"), _record("a:2")]
    new = [_record("a:1", access="registration"), _record("a:3")]
    d = diff.diff_catalog(old, new)  # a:1 changed, a:2 removed, a:3 added, 0 unchanged

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "+1 new" in md
    assert "~1 changed" in md
    assert "-1 removed" in md
    assert "0 unchanged" in md


# ---------------------------------------------------------------------------
# render_changelog: failures
# ---------------------------------------------------------------------------


def test_render_changelog_failures_none_when_all_ok():
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(d, date="2026-09-01", source_results=[_ok_result()])
    assert "## Failures\n\nnone" in md


def test_render_changelog_failures_lists_non_ok_sources():
    failed = HarvestResult(source="gdc", status="failed", error="HTTP 500")
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(
        d, date="2026-09-01", source_results=[_ok_result("openneuro"), failed]
    )

    failures_section = md.split("## Failures\n\n", 1)[1].split("\n\n", 1)[0]
    assert "openneuro" not in failures_section
    assert "`gdc`: failed — HTTP 500" in failures_section


# ---------------------------------------------------------------------------
# render_changelog: added / removed lists
# ---------------------------------------------------------------------------


def test_render_changelog_added_removed_bullet_format():
    added_rec = _record("openneuro:new1", name="New Dataset", url="https://x/new1")
    removed_rec = _record("openneuro:gone1", name="Gone Dataset", url="https://x/gone1")
    d = diff.diff_catalog([removed_rec], [added_rec])

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "- `openneuro:new1` — New Dataset — https://x/new1" in md
    assert "- `openneuro:gone1` — Gone Dataset — https://x/gone1" in md


def test_render_changelog_added_list_caps_at_200():
    added = [_record(f"openneuro:{i:04d}") for i in range(205)]
    d = Diff(added=added, removed=[], changed={})

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "## Added (205)" in md
    assert md.count("\n- `openneuro:") == 200
    assert "- … and 5 more" in md


def test_render_changelog_removed_list_caps_at_200():
    removed = [_record(f"openneuro:{i:04d}") for i in range(201)]
    d = Diff(added=[], removed=removed, changed={})

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "## Removed (201)" in md
    assert "- … and 1 more" in md


# ---------------------------------------------------------------------------
# render_changelog: changed section (name header, nested diffs, caps)
# ---------------------------------------------------------------------------


def test_render_changelog_changed_shows_id_and_name_then_nested_diffs():
    old_rec = _record("openneuro:ds1", name="Ds One", access="open")
    new_rec = _record("openneuro:ds1", name="Ds One", access="registration")
    d = diff.diff_catalog([old_rec], [new_rec])

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "- `openneuro:ds1` — Ds One" in md
    assert "  - `/access`: `open` → `registration`" in md


def test_render_changelog_changed_name_falls_back_when_name_itself_unchanged():
    # name is identical on both sides, so it never appears in the diff
    # tuples themselves -- render_changelog must still be able to label
    # the record's header line with it.
    old_rec = _record("openneuro:ds1", name="Ds One", access="open")
    new_rec = _record("openneuro:ds1", name="Ds One", access="registration")
    d = diff.diff_catalog([old_rec], [new_rec])

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "`openneuro:ds1` — Ds One" in md


def test_render_changelog_changed_paths_cap_at_10_per_record():
    old_rec = _record("openneuro:many", modalities=[f"M{i}" for i in range(15)])
    new_rec = _record("openneuro:many", modalities=[f"X{i}" for i in range(15)])
    d = diff.diff_catalog([old_rec], [new_rec])
    assert len(d.changed["openneuro:many"]) == 15

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert md.count("  - `/modalities/") == 10
    assert "  - … and 5 more" in md


def test_render_changelog_changed_falls_back_to_id_when_names_missing_entry():
    # A hand-built Diff with no `names` entry for a changed id (e.g. built
    # some way other than diff_catalog) still renders a header line --
    # the id itself, rather than a KeyError or a blank name.
    d = Diff(
        added=[],
        removed=[],
        changed={"a:1": [("/access", "open", "registration")]},
    )

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "- `a:1` — a:1" in md


def test_render_changelog_changed_records_cap_at_200():
    changed = {f"a:{i:04d}": [("/access", "open", "registration")] for i in range(205)}
    d = Diff(added=[], removed=[], changed=changed, names={k: k for k in changed})

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert "## Changed (205)" in md
    assert md.count("\n- `a:") == 200
    assert "- … and 5 more" in md


def test_render_changelog_changed_value_truncated_to_120_chars_by_renderer():
    long_old = "x" * 500
    long_new = "y" * 500
    old_rec = _record("openneuro:long", access="open")
    new_rec = _record("openneuro:long", access="open")
    old_rec["access_notes"] = long_old
    new_rec["access_notes"] = long_new

    d = diff.diff_catalog([old_rec], [new_rec])
    # Diff itself keeps the full, untruncated values.
    _path, stored_old, _stored_new = d.changed["openneuro:long"][0]
    assert stored_old == long_old
    assert len(stored_old) == 500

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    assert long_old not in md
    assert long_new not in md
    assert "x" * 120 in md


# ---------------------------------------------------------------------------
# render_changelog: enrichment
# ---------------------------------------------------------------------------


def test_render_changelog_enrichment_not_run_when_none():
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(
        d, date="2026-09-01", source_results=[], enrich_stats=None
    )
    assert "## Enrichment\n\nnot run" in md


def test_render_changelog_enrichment_stats_rendered_when_given():
    d = diff.diff_catalog([], [])
    stats = {
        "backend": "anthropic",
        "calls": 42,
        "cache_hits": 10,
        "guard_drops": 3,
        "failures": 1,
    }
    md = diff.render_changelog(
        d, date="2026-09-01", source_results=[], enrich_stats=stats
    )

    enrichment_section = md.split("## Enrichment\n\n", 1)[1].split("\n\n", 1)[0]
    assert "backend: `anthropic`" in enrichment_section
    assert "calls: 42" in enrichment_section
    assert "cache_hits: 10" in enrichment_section
    assert "guard_drops: 3" in enrichment_section
    assert "failures: 1" in enrichment_section


# ---------------------------------------------------------------------------
# render_changelog: validation warnings
# ---------------------------------------------------------------------------


def test_render_changelog_validation_warnings_count():
    d = diff.diff_catalog([], [])
    md = diff.render_changelog(d, date="2026-09-01", source_results=[], warnings=7)
    assert "## Validation warnings: 7" in md


# ---------------------------------------------------------------------------
# render_changelog: free text can never corrupt the markdown structure
# ---------------------------------------------------------------------------

# Every heading line this module ever emits starts with one of these.
_KNOWN_HEADING_PREFIXES = (
    "# ",
    "## Sources",
    "## Failures",
    "## Added",
    "## Removed",
    "## Changed",
    "## Enrichment",
    "## Validation warnings:",
)


def test_render_changelog_sources_multiline_error_stays_on_one_row():
    result = HarvestResult(
        source="gdc", status="failed", error="line1\nline2\r\nline3\ttabbed"
    )
    d = diff.diff_catalog([], [])

    md = diff.render_changelog(d, date="2026-09-01", source_results=[result])

    sources_table = md.split("## Sources\n\n", 1)[1].split("\n\n", 1)[0]
    table_lines = sources_table.split("\n")
    assert len(table_lines) == 3  # header + separator + exactly one data row
    assert "line1 line2 line3 tabbed" in table_lines[2]


def test_render_changelog_added_bullet_survives_nasty_name():
    nasty_name = "Weird | Name `with` backticks\nand a newline"
    rec = _record("openneuro:nasty", name=nasty_name, url="https://x/nasty")
    d = diff.diff_catalog([], [rec])

    md = diff.render_changelog(d, date="2026-09-01", source_results=[])

    added_section = md.split("## Added (1)\n\n", 1)[1].split("\n\n", 1)[0]
    lines = added_section.split("\n")
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("- `openneuro:nasty` — ")
    assert line.endswith("https://x/nasty")
    assert "Weird \\| Name 'with' backticks and a newline" in line


def test_render_changelog_nasty_free_text_cannot_forge_a_heading():
    nasty = "normal text\n\n# Fake Heading\n## Another fake\ntrailing"
    old_rec = _record("openneuro:nasty", name=nasty, access="open")
    new_rec = _record("openneuro:nasty", name=nasty, access=nasty)
    added_rec = _record("openneuro:added", name=nasty, url=nasty)
    failed = HarvestResult(source="gdc", status="failed", error=nasty)

    d = diff.diff_catalog([old_rec], [new_rec, added_rec])
    md = diff.render_changelog(
        d,
        date="2026-09-01",
        source_results=[failed],
        enrich_stats={
            "backend": nasty,
            "calls": 1,
            "cache_hits": 0,
            "guard_drops": 0,
            "failures": 0,
        },
    )

    for line in md.split("\n"):
        if line.startswith("#"):
            assert line.startswith(_KNOWN_HEADING_PREFIXES), line


# ---------------------------------------------------------------------------
# render_changelog: determinism
# ---------------------------------------------------------------------------


def test_render_changelog_is_deterministic():
    old = [_record("a:1"), _record("a:2")]
    new = [_record("a:1", access="registration"), _record("a:3")]
    d = diff.diff_catalog(old, new)
    kwargs = {
        "date": "2026-09-01",
        "source_results": [_ok_result("openneuro"), _ok_result("gdc")],
        "enrich_stats": {
            "backend": "anthropic",
            "calls": 1,
            "cache_hits": 1,
            "guard_drops": 0,
            "failures": 0,
        },
        "warnings": 2,
    }

    first = diff.render_changelog(d, **kwargs)
    second = diff.render_changelog(d, **kwargs)

    assert first == second


# ---------------------------------------------------------------------------
# write_changelog
# ---------------------------------------------------------------------------


def test_write_changelog_writes_dated_and_latest_with_identical_content(tmp_path):
    markdown = "# Refresh 2026-09-01\n\nsome content\n"

    dated_path, latest_path = diff.write_changelog(
        markdown, date="2026-09-01", out_dir=tmp_path
    )

    assert dated_path == tmp_path / "2026-09-01.md"
    assert latest_path == tmp_path / "latest.md"
    assert dated_path.read_text(encoding="utf-8") == markdown
    assert latest_path.read_text(encoding="utf-8") == markdown


def test_write_changelog_creates_out_dir_if_missing(tmp_path):
    out_dir = tmp_path / "nested" / "changelog"
    dated_path, _latest_path = diff.write_changelog(
        "content\n", date="2026-09-01", out_dir=out_dir
    )
    assert dated_path.exists()


# ---------------------------------------------------------------------------
# summary_title
# ---------------------------------------------------------------------------


def test_summary_title_format():
    added = [_record(f"a:new{i}") for i in range(14)]
    removed = [_record(f"a:gone{i}") for i in range(2)]
    changed = {
        f"a:changed{i}": [("/access", "open", "registration")] for i in range(37)
    }
    d = Diff(added=added, removed=removed, changed=changed)

    title = diff.summary_title(d, date="2026-09-01")

    assert title == "Monthly refresh 2026-09-01: +14 new, ~37 changed, -2 removed"


def test_summary_title_zero_counts():
    d = Diff(added=[], removed=[], changed={})
    assert diff.summary_title(d, date="2026-01-01") == (
        "Monthly refresh 2026-01-01: +0 new, ~0 changed, -0 removed"
    )
