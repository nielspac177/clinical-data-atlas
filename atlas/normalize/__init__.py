"""Auto-discovery registry for source `normalize`/`enrichment_text` pairs.

Each source module (`atlas/normalize/<source>.py`) just defines module-
level `normalize` and `enrichment_text` callables (and, optionally, a
`SOURCE` string naming the source it normalizes -- falling back to the
module's own name when absent) -- it needs no registration call.
`get_normalizers()` finds them by importing every submodule of this
package (except `common` and any module starting with `_`) and
collecting the ones that define both callables. This mirrors
`atlas.harvest.get_registry()` (Ruling R2 from the phase-0 plan): several
source normalizers are built in parallel worktrees, and a shared
registration list (one line per source, appended by every task) would be
a guaranteed merge conflict; auto-discovery means no file here needs
editing when a new source module is added.

`NORMALIZERS` is the cache, populated on first call to `get_normalizers()`
and then mutated *in place* (`.clear()` + `.update()`, never reassigned)
on every later re-discovery -- so an early `from atlas.normalize import
NORMALIZERS` reference stays live rather than going stale the moment
`get_normalizers()` next repopulates it. It starts empty: reading
`atlas.normalize.NORMALIZERS` directly *before* calling
`get_normalizers()` at least once in this process will see `{}` even
once source modules exist, so callers should always go through
`get_normalizers()`.

`DISCOVERY_ERRORS` records, by module name, the last import error seen
for any source module that failed to import during the most recent
discovery -- see `_discover`.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Callable

NORMALIZERS: dict[str, tuple[Callable, Callable]] = {}
DISCOVERY_ERRORS: dict[str, str] = {}


def _discover() -> dict[str, tuple[Callable, Callable]]:
    """Import every source submodule of this package and collect the
    `(normalize, enrichment_text)` callable pairs from modules that
    define both, keyed by the module's `SOURCE` attribute (or its own
    name when `SOURCE` isn't set).

    A submodule that fails to import (e.g. a missing optional dependency,
    a syntax error introduced mid-refactor) is skipped, not fatal -- one
    broken source must not take down the whole registry, and therefore
    not the whole `refresh` run. Its error is recorded in
    `DISCOVERY_ERRORS` (keyed by module name) and printed once to stderr.

    Two different modules claiming the same source key *is* fatal
    (raises `RuntimeError`, naming both modules) -- that's an authoring
    mistake between two otherwise-working modules, not an environmental
    failure, and silently letting one shadow the other would mean a
    normalizer quietly never runs.
    """
    normalizers: dict[str, tuple[Callable, Callable]] = {}
    DISCOVERY_ERRORS.clear()
    for module_info in pkgutil.iter_modules(__path__):
        mod_name = module_info.name
        if mod_name == "common" or mod_name.startswith("_"):
            continue
        qualified_name = f"{__name__}.{mod_name}"
        try:
            module = importlib.import_module(qualified_name)
        except Exception as exc:  # noqa: BLE001 -- one broken source must not kill the run
            DISCOVERY_ERRORS[mod_name] = str(exc)
            print(
                f"[normalize] failed to import {qualified_name}: {exc}", file=sys.stderr
            )
            continue

        normalize_fn = getattr(module, "normalize", None)
        enrichment_text_fn = getattr(module, "enrichment_text", None)
        if not (callable(normalize_fn) and callable(enrichment_text_fn)):
            continue

        source = getattr(module, "SOURCE", mod_name)
        pair = (normalize_fn, enrichment_text_fn)
        existing = normalizers.get(source)
        if existing is not None and existing != pair:
            raise RuntimeError(
                f"normalize source {source!r} is claimed by both "
                f"{existing[0].__module__} and {normalize_fn.__module__}"
            )
        normalizers[source] = pair
    return normalizers


def get_normalizers() -> dict[str, tuple[Callable, Callable]]:
    """The `{source: (normalize_fn, enrichment_text_fn)}` registry,
    discovering it on first call and caching the result in
    `NORMALIZERS`.

    Note: while there are zero source modules (or none registering both
    callables), discovery legitimately returns `{}` every time -- there
    is nothing to cache yet, so this re-scans (cheaply; the package
    currently has no other submodules) until the first normalizer module
    exists.
    """
    if not NORMALIZERS:
        discovered = _discover()
        NORMALIZERS.clear()
        NORMALIZERS.update(discovered)
    return NORMALIZERS
