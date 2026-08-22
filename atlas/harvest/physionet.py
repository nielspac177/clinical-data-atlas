"""PhysioNet harvester: the published-projects listing, in one request.

PhysioNet publishes every project version it has ever hosted at a single
endpoint -- ``GET /api/v1/project/published/`` -- returning a flat JSON
array of *version* entries (one project can appear several times, once
per published version). There is no pagination and no per-record detail
endpoint to follow, so both :meth:`probe` and :meth:`harvest` are exactly
one request.

**Deviation from the task brief:** the brief's ``/rest/database-list/``-
style endpoints return 404. The real, verified endpoint is
``/api/v1/project/published/`` (see ``docs/sources/physionet.md``).

What gets written to :class:`~atlas.harvest.base.RawStore`, and what
doesn't:

- Only entries where ``is_latest_version`` is true are written -- an
  older version of a project already-published is redundant with its
  latest version for cataloging purposes, so it is skipped *here*, at
  harvest time, rather than merely excluded downstream. ``native_id`` is
  the entry's ``slug`` (stable across a project's versions; PhysioNet
  reuses the same slug and bumps ``version``/``version_doi`` instead).
- Every *resource type* the API reports (``Database``, ``Challenge``,
  ``Software``, ``Model``) is kept in raw, verbatim, payload un-filtered
  -- the raw cache is meant to be the honest, complete record of what
  PhysioNet published, independent of which of those the catalog itself
  goes on to use. It's ``atlas.normalize.physionet`` that narrows to
  dataset-shaped resources (``Database``/``Challenge``), excluding
  ``Software``/``Model`` with ``Excluded(reason="not_a_dataset")``.

No field on this endpoint is volatile (nothing here is a "last polled"
style stamp that changes independent of the data itself), so
``RawStore.write`` is called with the default ``volatile=()``.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from atlas import http
from atlas.harvest.base import Harvester, HarvestResult

LISTING_URL = "https://physionet.org/api/v1/project/published/"


def _result_from_manifest(source: str, manifest: dict, seconds: float) -> HarvestResult:
    """Build a `HarvestResult` from a `RawStore.finalize` manifest."""
    counts = manifest["counts"]
    return HarvestResult(
        source=source,
        status=manifest["status"],
        listed=counts["listed"],
        written=counts["written"],
        unchanged=counts["unchanged"],
        removed=counts["removed"],
        seconds=seconds,
        error=manifest["error"],
    )


class PhysioNetHarvester(Harvester):
    """Harvests PhysioNet's published-projects listing.

    ``fast`` has no effect: there is no per-record detail request to
    skip -- the listing entry *is* the whole record.
    """

    name = "physionet"
    harvest_method = "api:physionet-published"

    def probe(self) -> list[Any]:
        """One GET of the listing; assert it looks like the documented
        shape: a non-empty JSON array whose first element has ``slug``
        and ``access_policy``.

        Note this returns a ``list``, not a ``dict`` -- the base class's
        `probe` return annotation is a generic placeholder (see its
        docstring: "return the parsed body", whatever shape that is);
        PhysioNet's one endpoint happens to answer with a JSON array.
        """
        body = http.get_json(LISTING_URL)
        assert isinstance(body, list) and body, (
            f"expected a non-empty JSON array from {LISTING_URL}, got "
            f"{type(body).__name__ if body is not None else 'None (request failed)'}"
        )
        first = body[0]
        assert isinstance(first, dict) and "slug" in first, (
            "first listing entry is missing 'slug'"
        )
        assert "access_policy" in first, (
            "first listing entry is missing 'access_policy'"
        )
        return body

    def harvest(self, *, fast: bool = False, limit: int | None = None) -> HarvestResult:
        """Fetch the listing once, write one envelope per latest-version
        entry (up to `limit` of them, if given), then finalize.

        Never raises: any failure while fetching or walking the listing
        (a failed request, a response that isn't the expected JSON
        array) is caught and turned into a ``status="failed"``
        `HarvestResult`, with whatever was already written via
        `self.store.write` before the failure preserved by
        `RawStore.finalize`'s own failed-status handling.
        """
        started = time.monotonic()
        # A plain calendar date, not a timestamp -- matches every other
        # harvester's `harvested_at` convention (see m1-shared-context.md).
        harvested_at = date.today().isoformat()  # noqa: DTZ011
        listed_ids: set[str] = set()

        try:
            listing = http.get_json(LISTING_URL)
            if not isinstance(listing, list):
                got = type(listing).__name__ if listing is not None else "None"
                raise TypeError(
                    f"GET {LISTING_URL} did not return a JSON array (got {got})"
                )

            for entry in listing:
                if not isinstance(entry, dict) or not entry.get("is_latest_version"):
                    continue
                slug = entry.get("slug")
                if not slug:
                    continue
                self.store.write(
                    slug,
                    entry,
                    harvest_method=self.harvest_method,
                    endpoints=[LISTING_URL],
                    volatile=(),
                )
                listed_ids.add(slug)
                if limit is not None and len(listed_ids) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001 -- one broken source must not kill a refresh run
            manifest = self.store.finalize(
                listed_ids=listed_ids,
                harvested_at=harvested_at,
                endpoints=[LISTING_URL],
                status="failed",
                error=str(exc),
            )
            return _result_from_manifest(
                self.name, manifest, time.monotonic() - started
            )

        manifest = self.store.finalize(
            listed_ids=listed_ids,
            harvested_at=harvested_at,
            endpoints=[LISTING_URL],
            status="ok",
        )
        return _result_from_manifest(self.name, manifest, time.monotonic() - started)
