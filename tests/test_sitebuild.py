"""Tests for `atlas.sitebuild` — assembling `site/` + `data/` into `_site/`.

Everything runs against `tests/fixtures/site/`, the synthetic catalog that
matches the fixed data contracts, so these tests never need a real harvest
and never touch the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from atlas import config, io, schema, sitebuild, vocab

FIXTURES = Path(__file__).parent / "fixtures" / "site"
SITE_DIR = config.ROOT / "site"

# Any `__UPPER_SNAKE__` token: the build must leave none behind.
PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")
ROOT_PATH_RE = re.compile(r'(?:href|src)="/')

MARKER_START = sitebuild.MARKER_START
MARKER_END = sitebuild.MARKER_END

BASE_URL = "/clinical-data-atlas/"
BUILD_ID = "abc1234"


def build(out: Path, **overrides: object) -> int:
    """Run the builder against the fixtures; return its exit code."""
    args = {
        "--base-url": BASE_URL,
        "--build": BUILD_ID,
        "--out": str(out),
        "--site-dir": str(SITE_DIR),
        "--data-dir": str(FIXTURES),
        "--catalog": str(FIXTURES / "catalog.jsonl"),
        "--changelog-dir": str(FIXTURES / "changelog"),
    }
    args.update({k: str(v) for k, v in overrides.items()})
    argv: list[str] = []
    for flag, value in args.items():
        argv += [flag, value]
    return sitebuild.main(argv)


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A successful build, shared by the assertions that only read it."""
    out = tmp_path / "_site"
    assert build(out) == 0
    return out


def text_files(root: Path) -> list[Path]:
    """Text assets the build owns — `vendor/` is third-party (see
    `test_vendor_bundle_is_copied_untouched`). The suffix set comes from
    production so the two cannot drift."""
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in sitebuild.TEXT_SUFFIXES
        and sitebuild.VENDOR_DIR not in p.relative_to(root).parts
    )


def tree_digest(root: Path) -> dict[str, str]:
    """`{relative path: sha256}` for every file — content, not timestamps."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_build_copies_the_site(built: Path) -> None:
    for name in (
        "index.html",
        "table.html",
        "whats-new.html",
        "about.html",
        "404.html",
        "robots.txt",
        "sitemap.xml",
        "assets/css/tokens.css",
        "assets/js/config.js",
        "assets/img/favicon.svg",
    ):
        assert (built / name).is_file(), name


def test_social_card_is_a_committed_1200x630_png(built: Path) -> None:
    """`og:image` has to be a raster.

    X, Slack, LinkedIn and iMessage all refuse an SVG card, so
    `site/assets/img/og.png` (rendered from `og.svg` by
    `python -m atlas.tools.og_png`, and committed) is what the tags point
    at. The SVG stays alongside it as the editable source.
    """
    png = built / "assets" / "img" / "og.png"
    assert png.is_file(), "run `make og` and commit site/assets/img/og.png"
    assert (built / "assets" / "img" / "og.svg").is_file()

    header = png.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR: width and height, big-endian, right after the signature.
    assert int.from_bytes(header[16:20], "big") == 1200
    assert int.from_bytes(header[20:24], "big") == 630

    tags = []
    for page in sorted(built.glob("*.html")):
        text = page.read_text()
        assert "og.svg" not in text, page.name
        tags += re.findall(r"<meta[^>]+(?:og:image|twitter:image)[^>]*>", text)

    assert len(tags) == 8  # og:image + twitter:image on all four real pages
    for tag in tags:
        assert f'content="{config.SITE_URL}assets/img/og.png"' in tag, tag


def test_build_copies_the_graph_data(built: Path) -> None:
    for name in ("graph.json", "search-index.json", "stats.json"):
        copied = json.loads((built / "data" / name).read_text())
        assert copied == json.loads((FIXTURES / name).read_text())


def test_no_placeholder_survives(built: Path) -> None:
    leftovers = {
        str(path.relative_to(built)): PLACEHOLDER_RE.findall(path.read_text())
        for path in text_files(built)
        if PLACEHOLDER_RE.search(path.read_text())
    }
    assert leftovers == {}


def test_placeholders_are_substituted(built: Path) -> None:
    index = (built / "index.html").read_text()
    assert f"?v={BUILD_ID}" in index
    assert f'href="{config.SITE_URL}"' in index

    js = (built / "assets" / "js" / "config.js").read_text()
    assert f'export const BUILD = "{BUILD_ID}";' in js
    assert f'export const BASE_URL = "{BASE_URL}";' in js
    assert f'export const MAINTAINER = "{config.MAINTAINER}";' in js

    about = (built / "about.html").read_text()
    assert config.MAINTAINER in about
    assert "2026-08-22" in about  # __UPDATED__, newest changelog date


def test_catalog_is_split_one_file_per_record(built: Path) -> None:
    lines = [
        line
        for line in (FIXTURES / "catalog.jsonl").read_text().splitlines()
        if line.strip()
    ]
    records = sorted((built / "data" / "records").rglob("*.json"))
    assert len(records) == len(lines)

    one = json.loads((built / "data/records/openneuro/ds000001.json").read_text())
    assert one["id"] == "openneuro:ds000001"
    assert one["name"] == "Resting-state MRI in focal epilepsy"

    # The slug is the part of the id after the first colon, and the
    # directory is the source.
    assert (built / "data/records/physionet/atlas-sepsis-ehr.json").is_file()


def test_changelog_is_rendered_between_the_markers(built: Path) -> None:
    page = (built / "whats-new.html").read_text()
    assert "<!--CHANGELOG:START-->" in page and "<!--CHANGELOG:END-->" in page

    body = page.split("<!--CHANGELOG:START-->")[1].split("<!--CHANGELOG:END-->")[0]
    assert '<h2><time datetime="2026-08-22">2026-08-22</time></h2>' in body
    assert body.count("<article>") == 1
    assert "First synthetic refresh" in body
    # `tables` and `fenced_code` extensions are enabled.
    assert "<table>" in body


def test_changelog_keeps_only_the_newest_twelve(tmp_path: Path) -> None:
    changelogs = tmp_path / "changelog"
    changelogs.mkdir()
    for month in range(1, 16):  # 15 entries, newest is 2026-15-01 by name
        (changelogs / f"2026-{month:02d}-01.md").write_text(f"Entry {month}.\n")
    (changelogs / "latest.md").write_text("A copy of the newest entry.\n")

    out = tmp_path / "_site"
    assert build(out, **{"--changelog-dir": changelogs}) == 0

    body = (out / "whats-new.html").read_text()
    assert body.count("<article>") == 12
    assert "Entry 15." in body and "Entry 4." in body
    assert "Entry 3." not in body
    # `latest.md` is a duplicate of the newest entry, never its own article.
    assert "A copy of the newest entry." not in body
    assert "2026-15-01" in body  # __UPDATED__ tracks the newest filename


def test_vendor_bundle_is_copied_untouched(built: Path) -> None:
    """Third-party bytes are sha256-pinned, so the build must not rewrite
    them — and three.js's own `__THREE__` tokens must not trip the
    placeholder guard."""
    source = SITE_DIR / "vendor" / "3d-force-graph.min.js"
    copied = built / "vendor" / "3d-force-graph.min.js"
    assert copied.read_bytes() == source.read_bytes()
    assert PLACEHOLDER_RE.search(copied.read_text())  # __THREE__ lives here


def changelog_body(tmp_path: Path, markdown_text: str) -> str:
    """Build with a single changelog entry; return the rendered block."""
    changelogs = tmp_path / "changelog"
    changelogs.mkdir(exist_ok=True)
    (changelogs / "2026-09-01.md").write_text(markdown_text)

    out = tmp_path / "_site"
    assert build(out, **{"--changelog-dir": changelogs}) == 0
    page = (out / "whats-new.html").read_text()
    return page.split(MARKER_START)[1].split(MARKER_END)[0]


def test_injected_tags_render_as_visible_text(tmp_path: Path) -> None:
    """Changelog prose is generated from harvested, submitter-controlled
    dataset names, and `markdown` passes raw HTML straight through."""
    body = changelog_body(
        tmp_path,
        "Added **<img src=x onerror=alert(1)>** and "
        "<script>alert(2)</script> and <svg onload=alert(3)></svg>.\n",
    )

    assert "<img" not in body
    assert "<script>" not in body
    assert "<svg" not in body
    assert "onerror" in body and "onload" in body  # present, but as text
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in body
    assert "<strong>" in body  # real markdown still renders


def test_code_and_quotes_survive_sanitizing(tmp_path: Path) -> None:
    """The sanitizer runs *after* rendering precisely so that markdown
    syntax involving angle brackets keeps working."""
    body = changelog_body(
        tmp_path,
        "Filter `age < 18` from the cohort.\n\n"
        "```\nif (a < b) then c > d\n```\n\n"
        "> Source note: counts are provisional.\n",
    )

    # Single-escaped, never double-escaped.
    assert "<code>age &lt; 18</code>" in body
    assert "&lt; b" in body and "&gt; d" in body
    assert "&amp;lt;" not in body and "&amp;gt;" not in body
    # The blockquote marker is still a blockquote.
    assert "<blockquote>" in body
    assert "counts are provisional" in body


def test_unsafe_link_targets_are_dropped(tmp_path: Path) -> None:
    body = changelog_body(
        tmp_path,
        "[bad](javascript:alert%281%29) and [worse](data:text/html;base64,PHN2Zz4=)\n\n"
        "[good](https://example.org/a?b=1&c=2)\n",
    )

    assert "javascript" not in body.lower()
    assert "data:text/html" not in body
    assert "<a>bad</a>" in body  # link text kept, target gone
    assert 'href="https://example.org/a?b=1&amp;c=2"' in body
    assert 'rel="noopener"' in body


def test_changelog_tables_keep_their_structure(tmp_path: Path) -> None:
    body = changelog_body(
        tmp_path, "| Source | Added |\n|---|---|\n| openneuro | 3 |\n"
    )
    for tag in ("<table>", "<thead>", "<tbody>", "<th>", "<td>"):
        assert tag in body, tag
    assert "openneuro" in body


def test_bare_ampersands_are_not_turned_into_entities() -> None:
    """`HTMLParser` matches `&[a-zA-Z]+` without needing a semicolon and
    without validating the name, so `Q&A` in a raw-HTML block arrives here
    as an "entity ref" called "A". Re-emitting it as `&name;` would invent
    a semicolon the author never wrote."""
    assert sitebuild.sanitize_html("<p>Q&A</p>") == "<p>Q&amp;A</p>"
    assert (
        sitebuild.sanitize_html("<p>AT&T and R&D</p>") == "<p>AT&amp;T and R&amp;D</p>"
    )
    # A bare ampersand before a space is plain data, not an entity ref.
    assert "Ben &amp; Jerry" in sitebuild.sanitize_html("<p>Ben & Jerry</p>")


def test_real_entities_and_numeric_refs_are_preserved() -> None:
    named = "<p>&amp; &lt; &gt; &quot; &copy; &mdash;</p>"
    assert sitebuild.sanitize_html(named) == named

    numeric = "<p>&#169; &#x2014; &#8212;</p>"
    assert sitebuild.sanitize_html(numeric) == numeric

    # Mixed: a real entity next to a bare ampersand.
    assert sitebuild.sanitize_html("<p>5 &lt; 6, Q&A</p>") == "<p>5 &lt; 6, Q&amp;A</p>"


def test_changelog_keeps_ampersands_in_raw_html(tmp_path: Path) -> None:
    """End to end: markdown escapes bare ampersands in prose itself, but
    passes raw inline HTML through untouched -- that is where the
    fabricated semicolon used to show up."""
    body = changelog_body(
        tmp_path, "Added <span>Q&A cohort from AT&T</span> and plain Q&A.\n"
    )
    assert "Q&amp;A cohort from AT&amp;T" in body
    assert "Q&A;" not in body
    assert "plain Q&amp;A" in body


def test_sanitizer_drops_attributes_and_balances_tags() -> None:
    """Unit-level checks that need no build."""
    dirty = (
        '<p class="x" onclick="alert(1)">hi</p>'
        '<a href="https://ok.test" target="_blank" onmouseover="x">link</a>'
        '<time datetime="2026-09-01">then</time>'
        "<p>unclosed"
    )
    clean = sitebuild.sanitize_html(dirty)

    assert "onclick" not in clean and "onmouseover" not in clean
    assert 'class="x"' not in clean and "target=" not in clean
    assert '<a href="https://ok.test" rel="noopener">link</a>' in clean
    assert '<time datetime="2026-09-01">then</time>' in clean
    # The input left a <p> open; the sanitizer closes it on the way out.
    assert clean.endswith("</p>")


def test_every_asset_reference_is_cache_busted(built: Path) -> None:
    """A stale CSS or JS file after a deploy is a support burden; every
    first-party asset reference must carry the build hash."""
    pattern = re.compile(r'(?:href|src)="([^"]*\.(?:css|js))(\?[^"]*)?"')
    checked = 0
    for page in sorted(built.glob("*.html")):
        for path, query in pattern.findall(page.read_text()):
            checked += 1
            assert query == f"?v={BUILD_ID}", f"{page.name} -> {path}{query}"
    assert checked >= 20  # 4 CSS + 1 JS per page, plus the vendor bundle


def test_missing_markers_fail_the_build(tmp_path: Path) -> None:
    site = tmp_path / "site"
    shutil.copytree(SITE_DIR, site)
    page = site / "whats-new.html"
    page.write_text(page.read_text().replace("<!--CHANGELOG:END-->", ""))

    assert build(tmp_path / "_site", **{"--site-dir": site}) == 1


def test_lastmod_and_urldate_appear_only_when_dated(tmp_path: Path) -> None:
    dated = tmp_path / "dated"
    assert build(dated) == 0
    assert "<lastmod>2026-08-22</lastmod>" in (dated / "sitemap.xml").read_text()
    assert "urldate = {2026-08-22}" in (dated / "about.html").read_text()
    assert "Updated 2026-08-22" in (dated / "about.html").read_text()

    undated = tmp_path / "undated"
    assert build(undated, **{"--changelog-dir": tmp_path / "nope"}) == 0
    sitemap = (undated / "sitemap.xml").read_text()
    about = (undated / "about.html").read_text()
    assert "lastmod" not in sitemap
    assert sitemap.count("<loc>") == 4  # still valid, still complete
    assert "urldate" not in about
    assert "Not yet published" in about
    assert "Updated" not in about.split('<footer class="site-footer">')[1]
    assert "<!--?" not in sitemap and "<!--?" not in about


def test_repo_url_placeholder_is_substituted(built: Path) -> None:
    for name in ("index.html", "table.html", "whats-new.html", "about.html"):
        page = (built / name).read_text()
        assert config.REPO_URL in page, name
    js = (built / "assets" / "js" / "config.js").read_text()
    assert f'export const REPO_URL = "{config.REPO_URL}";' in js


def test_every_page_has_exactly_one_h1(built: Path) -> None:
    for page in sorted(built.glob("*.html")):
        assert page.read_text().count("<h1") == 1, page.name


def test_catalog_fixture_matches_the_frozen_schema() -> None:
    """The fixture other site tasks build against must be a real catalog."""
    rows = io.read_jsonl(FIXTURES / "catalog.jsonl")
    errors, _warnings = schema.validate_records(rows)
    assert errors == []
    assert len(rows) == 6
    assert {row["source"] for row in rows} == {"openneuro", "physionet"}
    # All five access tiers are represented, so badge styling is exercised.
    assert {row["access"] for row in rows} == set(vocab.ACCESS_ORDER)


def test_nojekyll_is_written(built: Path) -> None:
    assert (built / ".nojekyll").is_file()


def test_page_asset_paths_are_relative(built: Path) -> None:
    # 404.html is excluded on purpose: GitHub Pages serves it for *any*
    # missing path, so its links must be absolute from BASE_URL.
    pages = [p for p in built.glob("*.html") if p.name != "404.html"]
    assert pages
    for page in pages:
        assert not ROOT_PATH_RE.search(page.read_text()), page.name


def test_404_links_are_prefixed_with_the_base_url(built: Path) -> None:
    page = (built / "404.html").read_text()
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', page)
    assert hrefs
    for href in hrefs:
        assert href.startswith(BASE_URL), href


def test_robots_points_at_the_absolute_sitemap(built: Path) -> None:
    robots = (built / "robots.txt").read_text()
    assert f"Sitemap: {config.SITE_URL}sitemap.xml" in robots
    assert "Allow: /" in robots

    sitemap = (built / "sitemap.xml").read_text()
    assert sitemap.count("<loc>") == 4
    assert f"<loc>{config.SITE_URL}</loc>" in sitemap


def test_build_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert build(first) == 0
    assert build(second) == 0
    assert tree_digest(first) == tree_digest(second)


def test_rebuilding_over_an_existing_tree_removes_stale_files(
    tmp_path: Path,
) -> None:
    out = tmp_path / "_site"
    assert build(out) == 0
    stale = out / "stale.html"
    stale.write_text("<p>from a previous build</p>")

    assert build(out) == 0
    assert not stale.exists()


# ---------------------------------------------------------------------------
# Degraded inputs and failures
# ---------------------------------------------------------------------------


def test_missing_data_warns_but_still_builds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "_site"
    code = build(
        out,
        **{
            "--data-dir": tmp_path / "nope",
            "--catalog": tmp_path / "nope" / "catalog.jsonl",
            "--changelog-dir": tmp_path / "nope",
        },
    )
    assert code == 0

    captured = capsys.readouterr()
    assert "warning" in (captured.out + captured.err).lower()

    # The pages still build, so the site can go up before the first harvest.
    assert (built_index := out / "index.html").is_file()
    assert not PLACEHOLDER_RE.search(built_index.read_text())
    assert not (out / "data" / "graph.json").exists()

    body = (out / "whats-new.html").read_text()
    assert "<article>" not in body
    assert (out / "about.html").read_text().count("__UPDATED__") == 0


def test_unknown_placeholder_fails_the_build(tmp_path: Path) -> None:
    site = tmp_path / "site"
    shutil.copytree(SITE_DIR, site)
    (site / "oops.html").write_text("<p>__MYSTERY_TOKEN__</p>")

    out = tmp_path / "_site"
    assert build(out, **{"--site-dir": site}) == 1


def test_module_runs_as_a_script(tmp_path: Path) -> None:
    """`python -m atlas.sitebuild ...` — the contract the Makefile uses."""
    out = tmp_path / "_site"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "atlas.sitebuild",
            "--base-url",
            BASE_URL,
            "--build",
            BUILD_ID,
            "--out",
            str(out),
            "--data-dir",
            str(FIXTURES),
            "--catalog",
            str(FIXTURES / "catalog.jsonl"),
            "--changelog-dir",
            str(FIXTURES / "changelog"),
        ],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (out / "index.html").is_file()
    assert result.stdout.strip()  # a one-line summary


def test_embedded_changelog_headings_are_demoted(tmp_path: Path) -> None:
    """A changelog file is a standalone document (it doubles as the monthly
    refresh's PR body), so it carries its own `#` title and `##` sections.
    Embedded in the What's new page it sits under that page's own `<h1>`
    and its `<article>`'s `<h2>` date, so every heading shifts down a
    level and the redundant title is dropped."""
    body = changelog_body(
        tmp_path,
        "# Monthly refresh 2026-09-01: +1 new, ~0 changed, -0 removed\n\n"
        "**+1 new, ~0 changed, -0 removed, 0 unchanged**\n\n"
        "## Sources\n\nAll four sources reported ok.\n\n"
        "### Failures\n\nnone\n",
    )

    # The entry's own title is dropped: the article's dated `<h2>` is the
    # heading a reader needs, and the counts repeat on the summary line.
    assert "<h1" not in body
    assert "Monthly refresh 2026-09-01" not in body
    assert '<h2><time datetime="2026-09-01">2026-09-01</time></h2>' in body
    assert "<h3>Sources</h3>" in body
    assert "<h4>Failures</h4>" in body
    assert "+1 new, ~0 changed, -0 removed, 0 unchanged" in body


def test_old_style_changelog_bodies_keep_a_gapless_outline(tmp_path: Path) -> None:
    """Entries written to the older convention -- no `#` title, sections
    starting at `###` -- must not land at `h4` under the article's `<h2>`.
    Headings shift by whatever it takes to put the body's own top level at
    `h3`, so the outline never skips a level (WCAG 1.3.1)."""
    body = changelog_body(
        tmp_path,
        "Intro line.\n\n### Summary\n\nText.\n\n#### Detail\n\nText.\n",
    )
    assert "<h3>Summary</h3>" in body
    assert "<h4>Detail</h4>" in body
    assert "<h5" not in body


def test_a_title_that_is_not_the_generated_one_survives_as_a_heading(
    tmp_path: Path,
) -> None:
    """The `# Monthly refresh <date>: …` line generated by
    `atlas.diff.render_changelog` is dropped -- the article's date already
    titles the entry. Any other `#` title is a hand-written one that says
    something the date does not, so it stays, demoted under the date."""
    body = changelog_body(
        tmp_path,
        "# Emergency correction: PhysioNet license fields\n\n"
        "## What happened\n\nText.\n",
    )
    assert "<h1" not in body
    assert "<h3>Emergency correction: PhysioNet license fields</h3>" in body
    assert "<h4>What happened</h4>" in body
