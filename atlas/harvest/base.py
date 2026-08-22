"""Harvester base class and the raw-record filesystem store.

Every source implements exactly one :class:`Harvester` subclass -- a cheap
one-request sanity check (`probe`) and the real pull (`harvest`) -- and
writes whatever it fetches through a :class:`RawStore`, which is the only
thing in this module that touches the filesystem (no network anywhere
here; that's `atlas.http`, called from each source's harvester).

Layout under ``data/raw/<source>/`` (see `atlas.config.RAW`)::

    records/<file>.json   one envelope per native id, `atlas.io.pretty_json`
    manifest.json         this source's last-known-good listing

Envelope (what's inside each ``records/<file>.json``)::

    {"source", "native_id", "harvest_method", "endpoints", "payload"}

`payload` is the source's response, stored verbatim -- no timestamps are
ever added here, so a record file's content changes only when the
*source's data* changes. That's also why change detection
(:meth:`RawStore.write`) hashes `payload` rather than comparing envelope
bytes: two fetches of otherwise-identical data can still differ in fields
a source stamps on every response (e.g. "last polled"), and `volatile` is
how a harvester tells `write()` to ignore those before hashing.

Manifest (``manifest.json``)::

    {"source", "harvested_at", "endpoints", "status", "error", "counts",
     "records": {native_id: {"hash", "first_seen"}}}

:meth:`RawStore.finalize` writes the manifest, once per harvest run, after
every listed record has gone through `write()`. It also carries the
"shrink guard": if a source's listing comes back suspiciously smaller
than last time (more than 20% fewer ids -- almost always a paging bug or
a rate limit cutting a response short, not a real mass deletion at the
source), it refuses to delete anything and reports the run as failed
instead.
"""

from __future__ import annotations

import copy
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from atlas import config, io

# ---------------------------------------------------------------------------
# HarvestResult
# ---------------------------------------------------------------------------


@dataclass
class HarvestResult:
    """Outcome of one :meth:`Harvester.harvest` call."""

    source: str
    status: str  # "ok" | "failed" | "skipped"
    listed: int = 0
    written: int = 0
    unchanged: int = 0
    removed: int = 0
    seconds: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------
# Dotted-path stripping (for `volatile`) and filename slugging
# ---------------------------------------------------------------------------


def _pop_dotted(obj: dict, dotted_path: str) -> None:
    """Remove `dotted_path` (e.g. "attributes.updated") from `obj` in
    place. Missing paths are ignored; only dict nesting is traversed -- a
    list anywhere along the path stops the walk silently rather than
    raising."""
    *parents, leaf = dotted_path.split(".")
    node = obj
    for part in parents:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(leaf, None)


def _strip_volatile(payload: dict, volatile: tuple[str, ...]) -> dict:
    """A deep copy of `payload` with every `volatile` dotted path
    removed, ready to hash. Returns `payload` itself, uncopied, when
    `volatile` is empty."""
    if not volatile:
        return payload
    stripped = copy.deepcopy(payload)
    for dotted_path in volatile:
        _pop_dotted(stripped, dotted_path)
    return stripped


_FILENAME_SAFE_RE = re.compile(r"[A-Za-z0-9._-]+")


def _filename_for(native_id: str) -> str:
    """The ``records/`` filename for `native_id`: itself, verbatim, when
    it's already filename-safe; :func:`atlas.io.slugify`'d otherwise. The
    envelope inside always carries the verbatim id regardless."""
    stem = (
        native_id if _FILENAME_SAFE_RE.fullmatch(native_id) else io.slugify(native_id)
    )
    return f"{stem}.json"


# ---------------------------------------------------------------------------
# RawStore
# ---------------------------------------------------------------------------


class RawStore:
    """Filesystem store for one source's raw harvested records."""

    def __init__(self, source: str, root: Path = config.RAW) -> None:
        self.source = source
        self.root = Path(root) / source
        self.records_dir = self.root / "records"
        self.manifest_path = self.root / "manifest.json"

        # Filename -> native id claimed so far *this run* (collision guard).
        self._filenames: dict[str, str] = {}
        # Native id -> {"hash", "first_seen"} for every write() call this run.
        self._run_records: dict[str, dict] = {}
        self._written = 0
        self._unchanged = 0
        # Snapshot of the previous manifest's records, frozen for the
        # duration of this run (finalize() writes the *new* manifest;
        # nothing else touches manifest.json in between).
        self._previous = self.existing()

    # -- reading ----------------------------------------------------------

    def _read_manifest(self) -> dict | None:
        if not self.manifest_path.exists():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def existing(self) -> dict[str, dict]:
        """The previous manifest's ``records`` map, or ``{}`` if this
        source has never been harvested (no manifest yet)."""
        manifest = self._read_manifest()
        return dict(manifest["records"]) if manifest else {}

    def load(self, native_id: str) -> dict:
        """The envelope previously written for `native_id`."""
        path = self.records_dir / _filename_for(native_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def load_all(self) -> dict[str, dict]:
        """Every envelope, keyed by verbatim native id.

        Uses the current manifest's record ids when a manifest exists;
        otherwise falls back to globbing ``records/*.json`` directly
        (e.g. before this source's first :meth:`finalize` call).
        """
        manifest = self._read_manifest()
        if manifest is not None:
            return {
                native_id: self.load(native_id) for native_id in manifest["records"]
            }

        envelopes: dict[str, dict] = {}
        for path in sorted(self.records_dir.glob("*.json")):
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelopes[envelope["native_id"]] = envelope
        return envelopes

    # -- writing ------------------------------------------------------------

    def _claim_filename(self, native_id: str) -> str:
        """Resolve and reserve the filename for `native_id` this run,
        raising if a *different* native id already claimed it."""
        filename = _filename_for(native_id)
        claimant = self._filenames.setdefault(filename, native_id)
        if claimant != native_id:
            raise ValueError(
                f"native ids {claimant!r} and {native_id!r} both map to "
                f"filename {filename!r} within this run -- rename one of "
                "them at the source"
            )
        return filename

    def write(
        self,
        native_id: str,
        payload: dict,
        *,
        harvest_method: str,
        endpoints: list[str],
        volatile: tuple[str, ...] = (),
    ) -> bool:
        """Write `payload` for `native_id` if it's new or changed.

        Change detection hashes `payload` with `volatile` dotted paths
        stripped first (see module docstring), so a source's own
        volatile fields never cause a rewrite by themselves. Returns
        True -- and (re)writes the record file atomically, storing
        `payload` verbatim -- when `native_id` is new or its stripped
        hash differs from what's already known; returns False, leaving
        the file untouched, when it matches.
        """
        filename = self._claim_filename(native_id)
        new_hash = io.content_hash(_strip_volatile(payload, volatile))

        prior = self._run_records.get(native_id) or self._previous.get(native_id)
        changed = prior is None or prior["hash"] != new_hash
        first_seen = prior["first_seen"] if prior else None
        self._run_records[native_id] = {"hash": new_hash, "first_seen": first_seen}

        if changed:
            envelope = {
                "source": self.source,
                "native_id": native_id,
                "harvest_method": harvest_method,
                "endpoints": list(endpoints),
                "payload": payload,
            }
            io.write_atomic(self.records_dir / filename, io.pretty_json(envelope))
            self._written += 1
        else:
            self._unchanged += 1
        return changed

    # -- finalizing ---------------------------------------------------------

    def finalize(
        self,
        *,
        listed_ids: set[str],
        harvested_at: str,
        endpoints: list[str],
        status: str,
        error: str | None = None,
    ) -> dict:
        """Write ``manifest.json`` for this run and return it.

        Three cases:

        - `status="failed"` (the harvest itself raised): nothing is ever
          deleted; `records` is the previous manifest merged with
          whatever *was* written before the failure.
        - otherwise, if the listing shrank more than 20% versus the
          previous manifest (a likely paging/rate-limit glitch, not a
          real mass deletion at the source): `status` is forced to
          "failed", an explanatory `error` is set, nothing is deleted,
          and `records` is kept exactly as the previous manifest had it.
        - otherwise: native ids no longer in `listed_ids` are deleted
          (file + manifest entry) and counted as `removed`.
        """
        previous = self._previous
        resolved_this_run = {
            native_id: {
                "hash": entry["hash"],
                "first_seen": entry["first_seen"] or harvested_at,
            }
            for native_id, entry in self._run_records.items()
        }
        removed = 0

        if status == "failed":
            records = {**previous, **resolved_this_run}
            final_status, final_error = "failed", error
        else:
            n_previous = len(previous)
            shrank = bool(previous) and len(listed_ids) < 0.8 * n_previous
            if shrank:
                records = dict(previous)
                final_status = "failed"
                final_error = (
                    f"listing shrank from {n_previous} to {len(listed_ids)} "
                    "(>20%); keeping previous records"
                )
            else:
                merged = {**previous, **resolved_this_run}
                stale_ids = [nid for nid in previous if nid not in listed_ids]
                for native_id in stale_ids:
                    (self.records_dir / _filename_for(native_id)).unlink(
                        missing_ok=True
                    )
                records = {
                    nid: entry for nid, entry in merged.items() if nid in listed_ids
                }
                removed = len(stale_ids)
                final_status, final_error = status, error

        manifest = {
            "source": self.source,
            "harvested_at": harvested_at,
            "endpoints": list(endpoints),
            "status": final_status,
            "error": final_error,
            "counts": {
                "listed": len(listed_ids),
                "written": self._written,
                "unchanged": self._unchanged,
                "removed": removed,
            },
            "records": records,
        }
        io.write_atomic(self.manifest_path, io.pretty_json(manifest))
        return manifest


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------


class Harvester(ABC):
    """One source's harvest logic: how to probe it and how to fetch
    everything from it into a :class:`RawStore`.

    Subclasses set `name` (the source id -- used for `data/raw/<name>/`
    and as the registry key in `atlas.harvest.get_registry`) and
    `harvest_method` (free text describing how records are obtained,
    e.g. "api" or "api+scrape" -- it ends up in every envelope and, via
    `normalize.common.make_provenance`, in every record's
    `provenance.harvested_via`).
    """

    name: ClassVar[str]
    harvest_method: ClassVar[str]

    def __init__(self, store: RawStore | None = None) -> None:
        self.store = store if store is not None else RawStore(self.name)

    @abstractmethod
    def probe(self) -> dict:
        """Make exactly one request against the source and return its
        parsed body. Raises AssertionError, with a message naming the
        field or shape that's wrong, if the response doesn't look like
        what this harvester expects -- catches upstream API changes
        early, independent of running a full harvest."""

    @abstractmethod
    def harvest(self, *, fast: bool = False, limit: int | None = None) -> HarvestResult:
        """Fetch every record from the source into `self.store` (one
        `self.store.write(...)` call per record), then call
        `self.store.finalize(...)` and return its result as a
        `HarvestResult`.

        `fast` lets a harvester skip optional per-record detail requests
        (e.g. use only the listing payload); `limit` caps how many
        records to fetch, for quick manual runs.
        """
