"""JSON serialization, atomic file writes, and small text utilities.

Everything here is pure and filesystem-only (no network), so it is safe to
import and call from anywhere: harvesters, normalizers, the enrich/graph
stages, and tests alike.

Two JSON renderings, one convention for hashing:

- :func:`canonical_json` is the *compact*, deterministic form used for
  hashing and for JSONL rows: sorted keys, no incidental whitespace, so two
  semantically-equal objects always serialize to the same bytes regardless
  of the order their keys were built in.
- :func:`pretty_json` is the *human* form (2-space indent) used for files a
  person is expected to open and read/diff, e.g. ``data/raw/<source>/
  records/<id>.json`` and the generated schema docs.
- :func:`content_hash` documents and fixes one convention so every caller
  gets the same digest for the same content: it is
  ``"sha256:" + sha256(canonical_json(obj) with its trailing newline
  stripped).hexdigest()`` -- equivalently, the sha256 of the compact,
  sorted-key JSON body with no trailing newline. Fixing this here (rather
  than at each call site) is what lets :mod:`atlas.harvest.base`'s
  shrink/change detection compare hashes across runs and machines.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# JSON: canonical (hashing/JSONL) and pretty (human-readable) renderings
# ---------------------------------------------------------------------------


def _canonical_body(obj: Any) -> str:
    """The compact, sorted-key JSON text shared by canonical_json and
    content_hash (without the trailing newline canonical_json adds)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json(obj: Any) -> str:
    """Compact, deterministic JSON: sorted keys, no extra whitespace,
    unescaped unicode, one trailing newline."""
    return _canonical_body(obj) + "\n"


def pretty_json(obj: Any) -> str:
    """Human-readable JSON: 2-space indent, sorted keys, unescaped
    unicode, one trailing newline."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def content_hash(obj: Any) -> str:
    """``"sha256:<hex>"`` of ``obj``'s canonical JSON body.

    See the module docstring for the exact convention (compact, sorted-key
    JSON, no trailing newline). Stable under key order and independent of
    whether a serialized copy on disk happens to end in a newline.
    """
    digest = hashlib.sha256(_canonical_body(obj).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Filesystem: atomic writes and JSONL
# ---------------------------------------------------------------------------


def write_atomic(path: Path | str, text: str) -> None:
    """Write `text` to `path` without ever leaving a half-written file.

    Writes to ``<path>.tmp`` first, then ``os.replace``s it into place --
    on POSIX and Windows alike that replace is atomic, so a reader never
    observes a partial file, and a crash mid-write leaves only the ``.tmp``
    sibling rather than a corrupt target. Creates parent directories as
    needed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def read_jsonl(path: Path | str) -> list[dict]:
    """Read a JSON-Lines file into a list of dicts, skipping blank lines."""
    path = Path(path)
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path | str, rows: Iterable[dict]) -> None:
    """Write `rows` as one :func:`canonical_json` line each, atomically."""
    text = "".join(canonical_json(row) for row in rows)
    write_atomic(path, text)


# ---------------------------------------------------------------------------
# Text utilities: slugs, HTML-to-text, word truncation
# ---------------------------------------------------------------------------

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9._-]+")


def slugify(s: str) -> str:
    """Lowercase, accent-stripped, filesystem/URL-safe slug.

    Lowercases, NFKD-normalizes and drops combining marks (accents), then
    collapses every run of characters outside ``[a-z0-9._-]`` to a single
    ``-`` and strips leading/trailing ``-``.
    """
    normalized = unicodedata.normalize("NFKD", s.lower())
    without_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return _SLUG_INVALID_RE.sub("-", without_accents).strip("-")


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    """Plain text from an HTML fragment: tags removed, entities unescaped,
    whitespace collapsed.

    ``<script>``/``<style>``/``<noscript>`` elements are dropped *with*
    their contents first -- source APIs (e.g. PhysioNet abstracts) embed
    HTML that can carry these, and their text is never part of the visible
    description. Remaining tags are then replaced with a single space (not
    deleted outright) so that block-level markup such as ``<p>a</p><p>b</p>``
    -- common in dataset descriptions -- reads as "a b" rather than the
    words running together as "ab"; the whitespace collapse below then
    absorbs the extra spacing this introduces around inline tags.
    """
    without_scripts = _SCRIPT_STYLE_RE.sub(" ", s)
    without_tags = _TAG_RE.sub(" ", without_scripts)
    unescaped = html.unescape(without_tags)
    return _WHITESPACE_RE.sub(" ", unescaped).strip()


def first_words(s: str, n: int = 40) -> str:
    """The first `n` whitespace-separated words of `s`, single-spaced,
    with no trailing ellipsis (a plain prefix, not a "preview")."""
    return " ".join(s.split()[:n])
