"""MeSH descriptor resolution: a source's free-text condition label ->
its MeSH descriptor id (e.g. ``"D004827"``), with a negative-caching,
on-disk cache.

Live endpoint (verified 2026-08-22): ``GET
https://id.nlm.nih.gov/mesh/lookup/descriptor?label=<label>&match=exact&
limit=10`` -> a JSON list of ``{"resource": "http://id.nlm.nih.gov/mesh/
D004827", "label": "Epilepsy"}``. ``match`` also supports ``startswith``
(``contains`` is too weak to trust unattended -- it is never used here).

:func:`resolve` tries three things, in order, stopping at the first hit:

1. An exact match on `label` itself.
2. An exact match on ``atlas.vocab.CONDITION_ALIASES[label.lower()]`` --
   the seed table of messy-source-string -> canonical-MeSH-ish-string,
   for labels a source spells in a way MeSH's exact match won't find
   (`"lung adenocarcinoma"` -> `"adenocarcinoma of lung"`).
3. A `startswith` match on whichever of the two strings above was tried
   last (the alias when one exists -- it is the cleaner, more
   MeSH-shaped string -- otherwise the raw label), accepted only when it
   returns *exactly one* hit. Zero hits means nothing to accept; more
   than one means the prefix is ambiguous and guessing would be a
   fabricated fact, so both are treated as "not found" rather than
   picking one arbitrarily.

A definitive "not found" (every step above tried and none produced a
usable hit) is cached as ``null`` right alongside a positive id, so a
condition MeSH genuinely has no descriptor for is only ever looked up
once. A network failure is different from "not found": if
``http.get_json`` returns ``None`` (every retry failed) or raises
:class:`atlas.http.OfflineError`, resolution is *unknown*, not
*negative* -- `resolve` returns ``None`` for this call but leaves the
cache untouched, so a later, connected run gets to try again.

Cache file (``atlas.config.RAW / "enrich" / "mesh.json"`` by default):
``atlas.io.pretty_json`` of ``{label.strip().lower(): "D…" | null}``,
written atomically and only when its content actually changed. An
in-process dict (:data:`_CACHE`, keyed by cache path) means a cache file
is read from disk at most once per path per process, however many labels
get resolved against it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from atlas import config, http, io
from atlas.vocab import CONDITION_ALIASES

MESH_LOOKUP_URL = "https://id.nlm.nih.gov/mesh/lookup/descriptor"
_LOOKUP_LIMIT = 10

_DEFAULT_CACHE_PATH = config.RAW / "enrich" / "mesh.json"

# cache_path -> {label_lowercased: "D…" | None}, loaded from disk at most
# once per path (see _load_cache).
_CACHE: dict[Path, dict[str, str | None]] = {}


def _cache_key(label: str) -> str:
    return label.strip().lower()


def _load_cache(cache_path: Path) -> dict[str, Any]:
    """`_CACHE[cache_path]`, reading it from disk on first use for this
    path (`{}` if the file is absent, unreadable, or not valid JSON)."""
    if cache_path not in _CACHE:
        try:
            raw = cache_path.read_text(encoding="utf-8")
        except OSError:
            _CACHE[cache_path] = {}
        else:
            try:
                _CACHE[cache_path] = json.loads(raw)
            except ValueError:
                _CACHE[cache_path] = {}
    return _CACHE[cache_path]


def _write_cache_if_changed(cache_path: Path, cache: dict[str, Any]) -> None:
    """Write `cache` to `cache_path` as pretty, sorted-key JSON -- but
    only if that differs from what is already on disk, so a batch that
    resolved nothing new never touches the file's mtime."""
    new_text = io.pretty_json(cache)
    try:
        current = cache_path.read_bytes()
    except OSError:
        current = None
    if current == new_text.encode("utf-8"):
        return
    io.write_atomic(cache_path, new_text)


def _descriptor_id(item: dict) -> str:
    """`"D004827"` from a lookup hit's `"resource"` URL."""
    return item["resource"].rsplit("/", 1)[-1]


def _lookup(label: str, match: str) -> list | None:
    return http.get_json(
        MESH_LOOKUP_URL,
        params={"label": label, "match": match, "limit": _LOOKUP_LIMIT},
    )


def _resolve_uncached(label: str) -> tuple[str | None, bool]:
    """`(descriptor_id, resolved)` for a `label` not already in the
    cache. `resolved` is `False` only when a lookup call outright failed
    (`http.get_json` returned `None`) -- the caller must not cache that
    as a negative, since it means "unknown", not "not found"."""
    exact_hits = _lookup(label, "exact")
    if exact_hits is None:
        return None, False
    if exact_hits:
        return _descriptor_id(exact_hits[0]), True

    alias = CONDITION_ALIASES.get(_cache_key(label))
    term = label
    if alias:
        alias_hits = _lookup(alias, "exact")
        if alias_hits is None:
            return None, False
        if alias_hits:
            return _descriptor_id(alias_hits[0]), True
        term = alias  # startswith against the cleaner, canonical term

    startswith_hits = _lookup(term, "startswith")
    if startswith_hits is None:
        return None, False
    if len(startswith_hits) == 1:
        return _descriptor_id(startswith_hits[0]), True

    return None, True  # genuinely not found: zero hits, or an ambiguous prefix


def _resolve_into(
    cache: dict[str, str | None], label: str, *, offline: bool
) -> tuple[str | None, bool]:
    """`(value, changed)`: resolve `label` against `cache`, mutating it
    in place on a fresh result. `changed` tells the caller whether a
    cache write is warranted."""
    key = _cache_key(label)
    if key in cache:
        return cache[key], False
    if offline:
        return None, False
    try:
        value, resolved = _resolve_uncached(label)
    except http.OfflineError:
        return None, False
    if not resolved:
        return None, False
    cache[key] = value
    return value, True


def resolve(
    label: str,
    *,
    cache_path: Path = _DEFAULT_CACHE_PATH,
    offline: bool = False,
) -> str | None:
    """The MeSH descriptor id for `label` (e.g. `"D004827"`), or `None`
    if it can't be resolved -- see the module docstring for the lookup
    order, negative-caching, and offline-safety rules. Never raises: an
    offline run or a failed request yields `None`, exactly like a
    genuine "not found".
    """
    cache = _load_cache(cache_path)
    value, changed = _resolve_into(cache, label, offline=offline)
    if changed:
        _write_cache_if_changed(cache_path, cache)
    return value


def resolve_many(
    labels: Iterable[str],
    *,
    cache_path: Path = _DEFAULT_CACHE_PATH,
    offline: bool = False,
) -> dict[str, str | None]:
    """`{label: resolve(label, ...)}` for every `label` in `labels`,
    sharing one cache load and writing at most once for the whole batch
    (rather than once per uncached label)."""
    cache = _load_cache(cache_path)
    results: dict[str, str | None] = {}
    any_changed = False
    for label in labels:
        value, changed = _resolve_into(cache, label, offline=offline)
        results[label] = value
        any_changed = any_changed or changed
    if any_changed:
        _write_cache_if_changed(cache_path, cache)
    return results
