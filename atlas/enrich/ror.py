"""ROR (Research Organization Registry) affiliation resolution: a source's
free-text institution name -> its ROR id, canonical name, and country,
with a negative-caching, on-disk cache.

Live endpoint (verified 2026-08-22): ``GET https://api.ror.org/v2/
organizations?affiliation=<name>`` -> ``{"items": [{"organization": {"id":
"https://ror.org/02mhbdp94", "name": "...", "locations": [{"geonames_
details": {"country_code": "US", ...}}]}, "score": 1.0, "chosen": true,
...}], ...}``. ROR's own affiliation matcher does the fuzzy work; this
module only has to decide whether to *trust* its top answer.

:func:`resolve` trusts the top (`items[0]`) result only when ROR marked
it `"chosen": true` *and* its `"score"` is at least 0.9 -- both a low
score and "not chosen" mean ROR itself wasn't confident, and guessing
past that would be a fabricated affiliation. Anything less than that
bar resolves to `None`, exactly like "not found" resolves in
`atlas.enrich.mesh` -- including the same distinction between a
definitive negative (cached as `null`) and a network failure
(`http.get_json` returned `None`, or raised `atlas.http.OfflineError`:
*unknown*, left uncached so a later run can retry).

Cache file (``atlas.config.RAW / "enrich" / "ror.json"`` by default):
``atlas.io.pretty_json`` of ``{name.strip().lower(): {"ror_id", "name",
"country"} | null}``, written atomically and only when its content
actually changed. An in-process dict (:data:`_CACHE`, keyed by cache
path) means a cache file is read from disk at most once per path per
process, however many names get resolved against it.

Deliberately not sharing a helper module with `atlas.enrich.mesh` even
though the caching/offline plumbing below is nearly identical: several
Phase 0 tasks land `atlas/enrich/*.py` modules in parallel worktrees, and
`atlas/enrich/__init__.py` is kept a bare one-line docstring for exactly
that reason (see its own docstring). A private third module here would
just move the merge-conflict risk rather than remove it, for ~30 lines
of overlap.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from atlas import config, http, io

ROR_AFFILIATION_URL = "https://api.ror.org/v2/organizations"
_MIN_SCORE = 0.9

_DEFAULT_CACHE_PATH = config.RAW / "enrich" / "ror.json"

# cache_path -> {name_lowercased: {"ror_id", "name", "country"} | None},
# loaded from disk at most once per path (see _load_cache).
_CACHE: dict[Path, dict[str, Any]] = {}


def _cache_key(name: str) -> str:
    return name.strip().lower()


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


def _country_of(organization: dict) -> str | None:
    """The first location's `geonames_details.country_code`, or `None`
    when `organization` has no locations at all."""
    locations = organization.get("locations") or []
    if not locations:
        return None
    details = (locations[0] or {}).get("geonames_details") or {}
    return details.get("country_code")


def _resolve_uncached(name: str) -> tuple[dict | None, bool]:
    """`(value, resolved)` for a `name` not already in the cache.
    `resolved` is `False` only when the request outright failed
    (`http.get_json` returned `None`) -- the caller must not cache that
    as a negative, since it means "unknown", not "not found"."""
    result = http.get_json(ROR_AFFILIATION_URL, params={"affiliation": name})
    if result is None:
        return None, False

    items = result.get("items") or []
    if not items:
        return None, True

    top = items[0]
    if not (top.get("chosen") is True and (top.get("score") or 0) >= _MIN_SCORE):
        return None, True

    organization = top.get("organization") or {}
    value = {
        "ror_id": organization.get("id"),
        "name": organization.get("name"),
        "country": _country_of(organization),
    }
    return value, True


def _resolve_into(
    cache: dict[str, Any], name: str, *, offline: bool
) -> tuple[dict | None, bool]:
    """`(value, changed)`: resolve `name` against `cache`, mutating it in
    place on a fresh result. `changed` tells the caller whether a cache
    write is warranted."""
    key = _cache_key(name)
    if key in cache:
        return cache[key], False
    if offline:
        return None, False
    try:
        value, resolved = _resolve_uncached(name)
    except http.OfflineError:
        return None, False
    if not resolved:
        return None, False
    cache[key] = value
    return value, True


def resolve(
    name: str,
    *,
    cache_path: Path = _DEFAULT_CACHE_PATH,
    offline: bool = False,
) -> dict | None:
    """`{"ror_id", "name", "country"}` for `name`'s best ROR affiliation
    match, or `None` if ROR wasn't confident (or nothing matched at all)
    -- see the module docstring for the acceptance threshold,
    negative-caching, and offline-safety rules. Never raises: an offline
    run or a failed request yields `None`, exactly like a genuine
    "not found".
    """
    cache = _load_cache(cache_path)
    value, changed = _resolve_into(cache, name, offline=offline)
    if changed:
        _write_cache_if_changed(cache_path, cache)
    return value


def resolve_many(
    names: Iterable[str],
    *,
    cache_path: Path = _DEFAULT_CACHE_PATH,
    offline: bool = False,
) -> dict[str, dict | None]:
    """`{name: resolve(name, ...)}` for every `name` in `names`, sharing
    one cache load and writing at most once for the whole batch (rather
    than once per uncached name)."""
    cache = _load_cache(cache_path)
    results: dict[str, dict | None] = {}
    any_changed = False
    for name in names:
        value, changed = _resolve_into(cache, name, offline=offline)
        results[name] = value
        any_changed = any_changed or changed
    if any_changed:
        _write_cache_if_changed(cache_path, cache)
    return results
