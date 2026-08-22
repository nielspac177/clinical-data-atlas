"""Auto-discovery registry for `Harvester` subclasses.

Each source module (`atlas/harvest/<source>.py`) just defines a
`Harvester` subclass with a `name` -- it needs no registration call.
`get_registry()` finds them by importing every submodule of this package
(except `base` and any module starting with `_`) and collecting their
`Harvester` subclasses. This is Ruling R2 from the phase-0 plan: several
source harvesters are built in parallel worktrees, and a shared
registration list (one line per source, appended by every task) would be
a guaranteed merge conflict; auto-discovery means no file here needs
editing when a new source module is added.

`REGISTRY` is the cache, populated on first call to `get_registry()` and
then mutated *in place* (`.clear()` + `.update()`, never reassigned) on
every later re-discovery -- so an early `from atlas.harvest import
REGISTRY` reference stays live rather than going stale the moment
`get_registry()` next repopulates it. It starts empty: reading
`atlas.harvest.REGISTRY` directly *before* calling `get_registry()` at
least once in this process will see `{}` even once source modules exist,
so callers should always go through `get_registry()`.

`DISCOVERY_ERRORS` records, by module name, the last import error seen
for any source module that failed to import during the most recent
discovery -- see `_discover`.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

from atlas.harvest.base import Harvester

REGISTRY: dict[str, type[Harvester]] = {}
DISCOVERY_ERRORS: dict[str, str] = {}


def _discover() -> dict[str, type[Harvester]]:
    """Import every source submodule of this package and collect their
    `Harvester` subclasses, keyed by `name`.

    A submodule that fails to import (e.g. a missing optional dependency,
    a syntax error introduced mid-refactor) is skipped, not fatal -- one
    broken source must not take down the whole registry, and therefore
    not the whole `refresh` run. Its error is recorded in
    `DISCOVERY_ERRORS` (keyed by module name) and printed once to stderr.

    Two different `Harvester` subclasses claiming the same `name` *is*
    fatal (raises `RuntimeError`, naming both modules) -- that's an
    authoring mistake between two otherwise-working modules, not an
    environmental failure, and silently letting one shadow the other
    would mean a harvester quietly never runs.
    """
    registry: dict[str, type[Harvester]] = {}
    DISCOVERY_ERRORS.clear()
    for module_info in pkgutil.iter_modules(__path__):
        mod_name = module_info.name
        if mod_name == "base" or mod_name.startswith("_"):
            continue
        qualified_name = f"{__name__}.{mod_name}"
        try:
            module = importlib.import_module(qualified_name)
        except Exception as exc:  # noqa: BLE001 -- one broken source must not kill the run
            DISCOVERY_ERRORS[mod_name] = str(exc)
            print(
                f"[harvest] failed to import {qualified_name}: {exc}", file=sys.stderr
            )
            continue
        for attr in vars(module).values():
            if not (
                isinstance(attr, type)
                and issubclass(attr, Harvester)
                and attr is not Harvester
                and getattr(attr, "name", "")
            ):
                continue
            existing = registry.get(attr.name)
            if existing is not None and existing is not attr:
                raise RuntimeError(
                    f"harvester name {attr.name!r} is claimed by both "
                    f"{existing.__module__} and {attr.__module__}"
                )
            registry[attr.name] = attr
    return registry


def get_registry() -> dict[str, type[Harvester]]:
    """The `{name: Harvester subclass}` registry, discovering it on first
    call and caching the result in `REGISTRY`.

    Note: while there are zero source modules, discovery legitimately
    returns `{}` every time -- there is nothing to cache yet, so this
    re-scans (cheaply; the package currently has no other submodules)
    until the first source module exists.
    """
    if not REGISTRY:
        discovered = _discover()
        REGISTRY.clear()
        REGISTRY.update(discovered)
    return REGISTRY
