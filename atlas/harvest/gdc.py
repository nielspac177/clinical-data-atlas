"""NCI Genomic Data Commons (GDC) projects harvester.

One endpoint: ``GET https://api.gdc.cancer.gov/projects``, paged
defensively via ``from=`` against ``data.pagination.total`` -- verified
live 2026-08-22: 93 projects, comfortably inside one ``size=100`` page, so
in practice this harvester makes exactly one request. The paging loop
exists only so a future project count exceeding 100 doesn't silently drop
projects; see ``docs/sources/gdc.md``.

Every hit in ``data.hits[]`` is one GDC "project" (e.g. ``"TCGA-LUAD"``,
``"TARGET-AML"``): its native id is the project's own ``project_id``, and
the envelope payload is the hit dict verbatim. ``expand=summary,
summary.data_categories,summary.experimental_strategies,program`` already
pulls in everything this harvester (and the normalizer) needs, so there is
no separate per-project detail request -- ``fast`` has nothing to skip.

``summary.file_count``/``summary.file_size`` change as GDC ingests more
files for an already-released project; per the verified-endpoint notes
they are treated as content, not volatile, so no ``volatile=`` paths are
passed to :meth:`RawStore.write`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import ClassVar

from atlas import http
from atlas.harvest.base import Harvester, HarvestResult

BASE_URL = "https://api.gdc.cancer.gov/projects"
EXPAND = "summary,summary.data_categories,summary.experimental_strategies,program"
PAGE_SIZE = 100


def _params(size: int, from_: int) -> dict:
    return {"size": size, "from": from_, "expand": EXPAND, "format": "json"}


def _today() -> str:
    """Today's ISO date, anchored to UTC rather than the harvesting
    machine's local clock -- this pipeline runs unattended (monthly CI
    refresh), so "today" needs to be the same regardless of which
    timezone the runner happens to be in."""
    return datetime.now(UTC).date().isoformat()


def _page_url(size: int, from_: int) -> str:
    """The exact URL :func:`atlas.http.get_json` requests for these
    params -- built the same way `get_json` itself builds it (via
    :func:`atlas.http.qs`), so this is only used for the `endpoints`
    bookkeeping on envelopes/manifests, never for the request itself."""
    return f"{BASE_URL}?{http.qs(**_params(size, from_))}"


class GDCHarvester(Harvester):
    """Harvests every GDC project listing hit as one envelope each,
    native id = ``project_id``, payload = the hit dict verbatim."""

    name: ClassVar[str] = "gdc"
    harvest_method: ClassVar[str] = "api:gdc-projects"

    def probe(self) -> dict:
        """One ``size=1`` request; asserts the shape this harvester and
        normalizer rely on: ``data.hits[0].project_id`` and
        ``data.pagination.total`` both present."""
        body = http.get_json(BASE_URL, params=_params(1, 0))
        if not isinstance(body, dict):
            # Tryceratops would rather this be TypeError, but
            # Harvester.probe()'s contract is specifically AssertionError.
            raise AssertionError(  # noqa: TRY004
                "GDC projects probe: request failed or non-JSON body"
            )

        data = body.get("data")
        if not isinstance(data, dict):
            raise AssertionError("GDC projects probe: response missing 'data'")  # noqa: TRY004

        hits = data.get("hits")
        if not isinstance(hits, list) or not hits:
            raise AssertionError("GDC projects probe: 'data.hits' is missing or empty")
        if not hits[0].get("project_id"):
            raise AssertionError("GDC projects probe: hits[0] missing 'project_id'")

        pagination = data.get("pagination")
        if not isinstance(pagination, dict) or "total" not in pagination:
            raise AssertionError(
                "GDC projects probe: response missing 'data.pagination.total'"
            )
        return body

    def harvest(self, *, fast: bool = False, limit: int | None = None) -> HarvestResult:
        """Page through ``data.hits[]`` (``size=100``, ``from=`` advancing
        by however many hits the previous page actually returned) until
        ``data.pagination.total`` is reached or `limit` records have been
        listed, writing one record per ``project_id`` as it goes.

        `fast` has nothing to skip here -- see the module docstring.
        """
        started = time.time()
        listed_ids: set[str] = set()
        endpoints: list[str] = []
        from_ = 0

        try:
            while True:
                url = _page_url(PAGE_SIZE, from_)
                body = http.get_json(BASE_URL, params=_params(PAGE_SIZE, from_))
                if body is None:
                    raise RuntimeError(f"GDC projects request failed: {url}")
                endpoints.append(url)

                data = body["data"]
                hits = data["hits"]
                total = data["pagination"]["total"]

                for hit in hits:
                    native_id = hit["project_id"]
                    if native_id not in listed_ids:
                        listed_ids.add(native_id)
                        self.store.write(
                            native_id,
                            hit,
                            harvest_method=self.harvest_method,
                            endpoints=[url],
                        )
                    if limit is not None and len(listed_ids) >= limit:
                        break

                reached_limit = limit is not None and len(listed_ids) >= limit
                from_ += len(hits)
                if reached_limit or not hits or from_ >= total:
                    break
        except Exception as exc:  # noqa: BLE001 -- one broken source must not kill a refresh
            manifest = self.store.finalize(
                listed_ids=listed_ids,
                harvested_at=_today(),
                endpoints=endpoints,
                status="failed",
                error=str(exc),
            )
            return HarvestResult(
                source=self.name,
                status="failed",
                listed=manifest["counts"]["listed"],
                written=manifest["counts"]["written"],
                unchanged=manifest["counts"]["unchanged"],
                removed=manifest["counts"]["removed"],
                seconds=time.time() - started,
                error=str(exc),
            )

        manifest = self.store.finalize(
            listed_ids=listed_ids,
            harvested_at=_today(),
            endpoints=endpoints,
            status="ok",
        )
        return HarvestResult(
            source=self.name,
            status="ok",
            listed=manifest["counts"]["listed"],
            written=manifest["counts"]["written"],
            unchanged=manifest["counts"]["unchanged"],
            removed=manifest["counts"]["removed"],
            seconds=time.time() - started,
        )
