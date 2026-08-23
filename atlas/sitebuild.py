"""Assemble the static site: `site/` + `data/` -> `_site/`.

    python -m atlas.sitebuild --base-url /clinical-data-atlas/ --build <sha> --out _site

The steps, in order: copy `site/` wholesale, copy the three generated graph
JSON files next to it as `data/`, split the catalog into one lazily-fetched
file per record, render the newest changelogs into `whats-new.html`, then
substitute the build-time placeholders (`__BUILD__`, `__BASE_URL__`,
`__SITE_URL__`, `__REPO_URL__`, `__UPDATED__`, `__MAINTAINER__`)
throughout. A placeholder that survives that pass fails the build rather
than shipping to a reader.

Missing pipeline output is a warning, not an error: the site must be
buildable before the first harvest has ever run.

Three conventions the rest of the repo relies on:

- A changelog file is named `YYYY-MM-DD.md` and does **not** repeat its own
  date as a heading — this module wraps each one in
  ``<article><h2><time datetime=…>…</time></h2>…</article>``. Bodies are
  written as standalone documents (a `#` title, `##` sections — the same
  markdown serves as the refresh PR's body) and `demote_headings` shifts
  them under that `<h2>` at embed time. `latest.md` is a duplicate of the
  newest entry and is skipped.
- A source line ending in `<!--?updated-->` survives only when there is a
  changelog date to substitute, and one ending in `<!--?not-updated-->`
  only when there isn't — so a `<lastmod>` element or a BibTeX `urldate`
  is omitted entirely rather than filled with a placeholder date.
- A record's per-file name is the part of its `id` after the first colon
  (`openneuro:ds000001` -> `records/openneuro/ds000001.json`); the schema's
  id pattern already guarantees that part is filename-safe.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from collections.abc import Sequence
from html.entities import html5
from html.parser import HTMLParser
from pathlib import Path

import markdown

from atlas import config, io

GRAPH_FILES = ("graph.json", "search-index.json", "stats.json")
TEXT_SUFFIXES = frozenset({".html", ".js", ".css", ".svg", ".txt", ".xml"})
CHANGELOG_LIMIT = 12
MARKER_START = "<!--CHANGELOG:START-->"
MARKER_END = "<!--CHANGELOG:END-->"
VENDOR_DIR = "vendor"
PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")
NO_CHANGELOG_HTML = '<p class="muted">No refreshes have been recorded yet.</p>'

# Line-level conditionals. A source line ending in `<!--?updated-->` is kept
# only when a changelog date exists (the marker is then stripped); one ending
# in `<!--?not-updated-->` is kept only when there is none. Both markers are
# HTML/XML comments, so an unbuilt source file still renders correctly.
IF_DATED = "<!--?updated-->"
IF_UNDATED = "<!--?not-updated-->"


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def copy_graph_data(data_dir: Path, out: Path) -> int:
    """Copy the generated graph JSON into `<out>/data/`.

    Looks in `<data_dir>/graph/` (the repo layout) and then in `<data_dir>`
    itself, so a flat directory of the three files works as-is for tests.
    """
    copied = 0
    for name in GRAPH_FILES:
        source = next(
            (p for p in (data_dir / "graph" / name, data_dir / name) if p.is_file()),
            None,
        )
        if source is None:
            _warn(f"{name} not found under {data_dir}; building without it")
            continue
        destination = out / "data" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied += 1
    return copied


def split_catalog(catalog: Path, out: Path) -> int:
    """Write one `data/records/<source>/<native>.json` per catalog row."""
    if not catalog.is_file():
        _warn(f"{catalog} not found; no per-record files written")
        return 0

    written = 0
    for row in io.read_jsonl(catalog):
        record_id = str(row.get("id", ""))
        source, _, native = record_id.partition(":")
        if not source or not native or "/" in native:
            _warn(f"skipping record with unusable id {record_id!r}")
            continue
        io.write_atomic(
            out / "data" / "records" / source / f"{native}.json",
            io.canonical_json(row),
        )
        written += 1
    return written


def changelog_entries(changelog_dir: Path) -> list[Path]:
    """The newest `CHANGELOG_LIMIT` entries, newest first."""
    if not changelog_dir.is_dir():
        _warn(f"{changelog_dir} not found; the What's new page will be empty")
        return []
    entries = sorted(
        (p for p in changelog_dir.glob("*.md") if p.name != "latest.md"),
        key=lambda p: p.name,
        reverse=True,
    )
    return entries[:CHANGELOG_LIMIT]


# Rendered-changelog sanitizer.
#
# `markdown` passes embedded HTML straight through, and changelog entries
# are generated from harvested dataset names -- strings a dataset submitter
# controls. Sanitizing the *rendered* HTML rather than escaping the markdown
# source is what keeps code spans, fenced blocks and blockquotes working:
# escaping the source turns `age < 18` into a visible `&lt;` and destroys
# the `> quote` marker, while the allowlist below still guarantees that no
# tag outside it reaches a reader as markup.
ALLOWED_TAGS = frozenset(
    {
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "strong", "em", "b", "i", "code", "pre",
        "blockquote", "a", "table", "thead", "tbody", "tr", "th", "td",
        "time", "article", "section", "span",
    }
)  # fmt: skip
VOID_TAGS = frozenset({"br", "hr"})
ALLOWED_ATTRS = {"a": frozenset({"href"}), "time": frozenset({"datetime"})}
SAFE_SCHEMES = ("http://", "https://")


class _Sanitizer(HTMLParser):
    """Reduce rendered markdown to `ALLOWED_TAGS`.

    Allowed tags survive with every attribute dropped except `href` on
    `<a>` (http/https only, always with `rel="noopener"`) and `datetime`
    on `<time>`. Anything else -- `<img>`, `<script>`, an event handler --
    is emitted as escaped text: visible to the reader, inert to the
    browser. Comments and declarations are dropped (the base class's
    no-op handlers), and open tags are tracked so the result stays
    balanced however unbalanced the input was.
    """

    def __init__(self) -> None:
        # convert_charrefs=False keeps `&lt;` from a code span intact instead
        # of decoding it to `<` and re-escaping it on the way out.
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._open: list[str] = []

    def _text(self, data: str) -> None:
        self._out.append(html.escape(data, quote=False))

    def _attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = ALLOWED_ATTRS.get(tag, frozenset())
        rendered: list[str] = []
        for name, value in attrs:
            if name not in allowed or value is None:
                continue
            if name == "href":
                url = value.strip()
                # An allowlist of schemes, so `javascript:`, `data:` and
                # obfuscations of them are dropped without pattern-matching.
                if not url.lower().startswith(SAFE_SCHEMES):
                    continue
                rendered.append(f' href="{html.escape(url)}" rel="noopener"')
            else:
                rendered.append(f' {name}="{html.escape(value)}"')
        return "".join(rendered)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self._text(self.get_starttag_text() or f"<{tag}>")
            return
        self._out.append(f"<{tag}{self._attributes(tag, attrs)}>")
        if tag not in VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self._text(self.get_starttag_text() or f"<{tag}/>")
            return
        self._out.append(f"<{tag}{self._attributes(tag, attrs)}>")
        if tag not in VOID_TAGS:
            self._out.append(f"</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            self._text(f"</{tag}>")
            return
        if tag not in self._open:
            return  # A stray close tag closes nothing.
        while self._open:
            current = self._open.pop()
            self._out.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        self._text(data)

    def handle_entityref(self, name: str) -> None:
        # HTMLParser reports the `&A` in `Q&A` as an entity ref named
        # "A" -- it matches `&[a-zA-Z]+` without requiring a semicolon and
        # without validating the name -- so re-emitting `&name;` blindly
        # invents punctuation: `Q&A` would reach the reader as `Q&A;`,
        # and so would AT&T, R&D and friends. Only real HTML5 entity
        # names survive as entities; anything else is a bare ampersand
        # followed by ordinary text.
        if f"{name};" in html5:
            self._out.append(f"&{name};")
        else:
            self._out.append("&amp;" + html.escape(name, quote=False))

    def handle_charref(self, name: str) -> None:
        self._out.append(f"&#{name};")

    def result(self) -> str:
        self.close()
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)


def sanitize_html(rendered: str) -> str:
    """Reduce rendered markdown to the changelog tag allowlist."""
    parser = _Sanitizer()
    parser.feed(rendered)
    return parser.result()


# Heading demotion. Applied to *sanitized* HTML, where the allowlist has
# already dropped every heading attribute and escaped anything that only
# looked like a tag -- so a heading is always a bare `<hN>`/`</hN>` and a
# `<h1>` inside a code span is `&lt;h1&gt;` text, which these never touch.
_HEADING_RE = re.compile(r"<(/?)h([1-6])(\s[^>]*)?>")
_LEADING_H1_RE = re.compile(r"\A\s*<h1(?:\s[^>]*)?>(.*?)</h1>\s*", re.DOTALL)
# The title `atlas.diff.render_changelog` generates: "Monthly refresh
# 2026-09-01: +1 new, …" from `summary_title`, or a bare "Refresh <date>"
# when no title was passed. Only this shape is dropped -- see
# `demote_headings`.
_GENERATED_TITLE_RE = re.compile(r"\A(?:Monthly refresh|Refresh)\s+\d{4}-\d{2}-\d{2}\b")

# The level the deepest-outdented heading in an embedded body should land
# at: one below the article's own `<h2>` date.
_EMBEDDED_TOP_LEVEL = 3


def _heading_text(inner_html: str) -> str:
    """A heading's plain text, for matching only -- inline markup and
    entities are irrelevant to whether this is the generated title."""
    return html.unescape(re.sub(r"<[^>]+>", "", inner_html)).strip()


def _shift(match: re.Match[str], by: int) -> str:
    slash, level, attrs = match.group(1), int(match.group(2)), match.group(3) or ""
    return f"<{slash}h{min(level + by, 6)}{attrs}>"


def demote_headings(body: str) -> str:
    """Re-level a standalone document's headings so it can be embedded
    under a heading of its own.

    A changelog entry is a complete markdown document -- it doubles as the
    monthly refresh's PR body (`atlas.diff.render_changelog`,
    `.github/workflows/monthly-refresh.yml`) -- so it opens with an `#`
    title and uses `##` sections. On the What's new page it lands inside an
    `<article>` under the page's own `<h1>` and the article's dated `<h2>`,
    where that structure would ship a second `<h1>` and an outline that
    jumps around.

    Two steps:

    1. A leading `<h1>` is dropped **only if it is the generated title**
       (`_GENERATED_TITLE_RE`), which says nothing the article's `<h2>`
       date doesn't already say -- its counts are repeated verbatim by the
       summary line right beneath it. A hand-written title ("Emergency
       correction: PhysioNet license fields") is kept: it carries meaning
       the date cannot.
    2. Every heading then shifts by however much it takes to put the
       body's own top level at `h3` -- one below the article's `<h2>`. A
       shift proportional to the body, rather than a flat one, is what
       keeps an older entry that starts at `###` (the convention this
       module documented until Task 3.5) from landing at `h4` and skipping
       a level, which is a WCAG 1.3.1 heading-order violation. Relative
       depth inside the body is preserved either way. `h6` is the floor, so
       a document already six deep merges its two deepest levels rather
       than emitting an `<h7>`.

    Doing this at embed time rather than in the writer means changelogs
    committed by earlier refreshes render correctly too, without anyone
    regenerating them.
    """
    leading = _LEADING_H1_RE.match(body)
    if leading and _GENERATED_TITLE_RE.match(_heading_text(leading.group(1))):
        body = body[leading.end() :]

    levels = [int(m.group(2)) for m in _HEADING_RE.finditer(body) if not m.group(1)]
    if not levels:
        return body
    shift = max(0, _EMBEDDED_TOP_LEVEL - min(levels))
    if not shift:
        return body
    return _HEADING_RE.sub(lambda m: _shift(m, shift), body)


def render_changelog(entries: Sequence[Path]) -> str:
    """Render each entry as its own dated, sanitized `<article>`, with its
    headings demoted to sit under the article's `<h2>` date."""
    renderer = markdown.Markdown(extensions=["tables", "fenced_code"])
    articles = []
    for path in entries:
        date = path.stem
        renderer.reset()
        body = demote_headings(
            sanitize_html(renderer.convert(path.read_text(encoding="utf-8")))
        )
        articles.append(
            f'<article>\n<h2><time datetime="{date}">{date}</time></h2>\n'
            f"{body}\n</article>"
        )
    return "\n".join(articles)


def insert_changelog(page: Path, rendered_html: str) -> bool:
    """Replace everything between the markers in `page`. False if absent."""
    text = page.read_text(encoding="utf-8")
    start = text.find(MARKER_START)
    end = text.find(MARKER_END)
    if start == -1 or end == -1 or end < start:
        return False
    body = rendered_html or NO_CHANGELOG_HTML
    head = text[: start + len(MARKER_START)]
    page.write_text(f"{head}\n{body}\n{text[end:]}", encoding="utf-8")
    return True


def _text_files(out: Path) -> list[Path]:
    """Every text asset the build owns.

    `vendor/` is excluded: those bytes are third-party and sha256-pinned,
    they carry no placeholders of ours, and three.js legitimately contains
    `__THREE__`-shaped tokens that would otherwise trip the guard below.
    """
    return sorted(
        p
        for p in out.rglob("*")
        if p.is_file()
        and p.suffix in TEXT_SUFFIXES
        and VENDOR_DIR not in p.relative_to(out).parts
    )


def resolve_conditionals(text: str, *, dated: bool) -> str:
    """Apply the `IF_DATED` / `IF_UNDATED` line markers (see their docs)."""
    keep, drop = (IF_DATED, IF_UNDATED) if dated else (IF_UNDATED, IF_DATED)
    lines = [line for line in text.splitlines(keepends=True) if drop not in line]
    return "".join(line.replace(keep, "") for line in lines)


def substitute(out: Path, values: dict[str, str], *, dated: bool) -> None:
    """Resolve conditional lines, then replace every placeholder, in every
    text asset under `out`."""
    for path in _text_files(out):
        text = path.read_text(encoding="utf-8")
        replaced = resolve_conditionals(text, dated=dated)
        for token, value in values.items():
            replaced = replaced.replace(token, value)
        if replaced != text:
            path.write_text(replaced, encoding="utf-8")


def remaining_placeholders(out: Path) -> list[str]:
    """`"<relative path>: __TOKEN__"` for every placeholder left behind."""
    found: list[str] = []
    for path in _text_files(out):
        for token in sorted(set(PLACEHOLDER_RE.findall(path.read_text("utf-8")))):
            found.append(f"{path.relative_to(out)}: {token}")
    return found


def build(
    *,
    site_dir: Path,
    data_dir: Path,
    catalog: Path,
    changelog_dir: Path,
    out: Path,
    base_url: str,
    build_id: str,
) -> int:
    """Build the site into `out`. Returns a process exit code."""
    if not site_dir.is_dir():
        print(f"error: site directory {site_dir} not found", file=sys.stderr)
        return 1

    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(site_dir, out)
    site_files = sum(1 for p in out.rglob("*") if p.is_file())

    data_files = copy_graph_data(data_dir, out)
    records = split_catalog(catalog, out)

    entries = changelog_entries(changelog_dir)
    whats_new = out / "whats-new.html"
    if whats_new.is_file() and not insert_changelog(
        whats_new, render_changelog(entries)
    ):
        print(
            f"error: {MARKER_START}/{MARKER_END} markers missing from {whats_new.name}",
            file=sys.stderr,
        )
        return 1

    prefix = base_url if base_url.endswith("/") else f"{base_url}/"
    updated = entries[0].stem if entries else ""
    values = {
        "__BUILD__": build_id,
        "__BASE_URL__": prefix,
        "__SITE_URL__": config.SITE_URL,
        "__REPO_URL__": config.REPO_URL,
        "__MAINTAINER__": config.MAINTAINER,
    }
    # Every `__UPDATED__` sits on an `IF_DATED` line, so when there is no
    # date the token is removed rather than filled -- and an unmarked use
    # added later trips the placeholder guard instead of shipping a
    # stand-in date. The undated wording lives once, in the page itself.
    if updated:
        values["__UPDATED__"] = updated
    substitute(out, values, dated=bool(updated))

    if leftovers := remaining_placeholders(out):
        print("error: unsubstituted placeholders remain:", file=sys.stderr)
        for leftover in leftovers:
            print(f"  {leftover}", file=sys.stderr)
        return 1

    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(
        f"sitebuild: {site_files} site files, {data_files} data files, "
        f"{records} records, {len(entries)} changelogs -> {out} "
        f"(base={prefix} build={build_id})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m atlas.sitebuild",
        description="Assemble the static site into an output directory.",
    )
    parser.add_argument("--base-url", required=True, help="path prefix, e.g. /atlas/")
    parser.add_argument("--build", required=True, help="short commit hash")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--site-dir", type=Path, default=config.ROOT / "site")
    parser.add_argument("--data-dir", type=Path, default=config.DATA)
    parser.add_argument(
        "--catalog", type=Path, default=config.CATALOG / "catalog.jsonl"
    )
    parser.add_argument("--changelog-dir", type=Path, default=config.CHANGELOG)
    args = parser.parse_args(argv)

    return build(
        site_dir=args.site_dir,
        data_dir=args.data_dir,
        catalog=args.catalog,
        changelog_dir=args.changelog_dir,
        out=args.out,
        base_url=args.base_url,
        build_id=args.build,
    )


if __name__ == "__main__":
    raise SystemExit(main())
