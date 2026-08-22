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

`REGISTRY` is the cache, populated on first call to `get_registry()`. It
is a plain module-level dict that starts empty -- reading
`atlas.harvest.REGISTRY` directly *before* calling `get_registry()` at
least once in this process will see `{}` even once source modules exist,
so callers should always go through `get_registry()`.
"""

from __future__ import annotations

import importlib
import pkgutil

from atlas.harvest.base import Harvester

REGISTRY: dict[str, type[Harvester]] = {}


def _discover() -> dict[str, type[Harvester]]:
    """Import every source submodule of this package and collect their
    `Harvester` subclasses, keyed by `name`."""
    registry: dict[str, type[Harvester]] = {}
    for module_info in pkgutil.iter_modules(__path__):
        mod_name = module_info.name
        if mod_name == "base" or mod_name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod_name}")
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, Harvester)
                and attr is not Harvester
                and getattr(attr, "name", "")
            ):
                registry[attr.name] = attr
    return registry


def get_registry() -> dict[str, type[Harvester]]:
    """The `{name: Harvester subclass}` registry, discovering it (and
    caching the result in `REGISTRY`) on first call.

    Note: while there are zero source modules, discovery legitimately
    returns `{}` every time -- there is nothing to cache yet, so this
    re-scans (cheaply; the package currently has no other submodules)
    until the first source module exists.
    """
    global REGISTRY
    if not REGISTRY:
        REGISTRY = _discover()
    return REGISTRY
