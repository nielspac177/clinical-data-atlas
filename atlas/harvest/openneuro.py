"""OpenNeuro harvester: walks the public `datasets` GraphQL listing.

One POST endpoint, no auth, paged with `first`/`after` (`orderBy:
{created: descending}`), 100 datasets per page. Each page's `edges[].node`
is written to the raw store verbatim -- `node` already carries everything
`atlas.normalize.openneuro` needs (`metadata` + `latestSnapshot`), so
there is no separate per-record detail request to skip; `fast` is
accepted for interface compatibility with `Harvester` but has no effect
here. See `docs/sources/openneuro.md` for the exact verified query and
quirks.

Real-data quirk this harvester defends against (see
`tests/test_harvest_openneuro.py` and the live evidence in
`docs/sources/openneuro.md`): a small fraction of dataset nodes per page
(observed ~1-4%) fail to resolve server-side (GraphQL "Not Found" errors
resolving `metadata`/`latestSnapshot` for a handful of ids), and the
*entire* edge for that dataset comes back JSON `null` -- with no id
recoverable at all, alongside the page's otherwise-valid `data`. Such
edges are skipped, not fatal: nothing else in that page's response is
affected, and `pageInfo` is unaffected too, so pagination continues
normally.
"""

from __future__ import annotations

import datetime
import time
from typing import ClassVar

from atlas import http
from atlas.harvest.base import Harvester, HarvestResult

ENDPOINT = "https://openneuro.org/crn/graphql"
PAGE_SIZE = 100

_QUERY = """
query($first: Int!, $after: String) {
  datasets(first: $first, after: $after, orderBy: {created: descending}) {
    pageInfo { hasNextPage endCursor count }
    edges {
      node {
        id
        created
        publishDate
        metadata {
          species
          studyDomain
          studyDesign
          studyLongitudinal
          dataProcessed
          ages
          modalities
          associatedPaperDOI
          grantFunderName
          dxStatus
          affirmedDefaced
        }
        latestSnapshot {
          tag
          created
          description {
            Name
            Authors
            License
            DatasetDOI
            Funding
            ReferencesAndLinks
            HowToAcknowledge
          }
          readme
          summary {
            subjects
            sessions
            modalities
            secondaryModalities
            tasks
            totalFiles
            size
            dataProcessed
          }
        }
      }
    }
  }
}
"""


class OpenNeuroHarvester(Harvester):
    """Walks OpenNeuro's public GraphQL dataset listing, writing one raw
    envelope per dataset node."""

    name: ClassVar[str] = "openneuro"
    harvest_method: ClassVar[str] = "api:openneuro-graphql"

    def _fetch_page(self, first: int, after: str | None) -> dict | None:
        return http.post_json(
            ENDPOINT, {"query": _QUERY, "variables": {"first": first, "after": after}}
        )

    def probe(self) -> dict:
        result = self._fetch_page(1, None)
        assert result is not None, "OpenNeuro GraphQL request failed (no response)"
        data = result.get("data")
        assert isinstance(data, dict), f"no 'data' in GraphQL response: {result!r}"
        datasets = data.get("datasets")
        assert isinstance(datasets, dict), f"no 'datasets' in response data: {data!r}"
        edges = datasets.get("edges")
        assert isinstance(edges, list) and edges, (
            f"no dataset edges returned: {datasets!r}"
        )
        node = (edges[0] or {}).get("node")
        assert isinstance(node, dict) and node.get("id"), (
            f"first edge has no node.id: {edges[0]!r}"
        )
        return result

    def harvest(self, *, fast: bool = False, limit: int | None = None) -> HarvestResult:
        started = time.monotonic()
        harvested_at = datetime.date.today().isoformat()  # noqa: DTZ011 -- shared spec mandates this exact expression for harvested_at
        listed_ids: set[str] = set()
        after: str | None = None

        try:
            while True:
                result = self._fetch_page(PAGE_SIZE, after)
                if not isinstance(result, dict) or not isinstance(
                    result.get("data"), dict
                ):
                    raise RuntimeError(  # noqa: TRY004 -- an API/network failure, not a type-safety bug
                        f"OpenNeuro GraphQL request failed: {result!r}"
                    )
                datasets = result["data"]["datasets"]

                limit_reached = False
                for edge in datasets.get("edges") or []:
                    node = (edge or {}).get("node") if edge else None
                    native_id = node.get("id") if node else None
                    if not native_id:
                        # Server-side resolver failure for this dataset --
                        # nothing usable to write; see module docstring.
                        continue
                    listed_ids.add(native_id)
                    self.store.write(
                        native_id,
                        node,
                        harvest_method=self.harvest_method,
                        endpoints=[ENDPOINT],
                    )
                    if limit is not None and len(listed_ids) >= limit:
                        limit_reached = True
                        break

                if limit_reached:
                    break
                page_info = datasets.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")
        except Exception as exc:  # noqa: BLE001 -- one broken source must not kill the run
            manifest = self.store.finalize(
                listed_ids=listed_ids,
                harvested_at=harvested_at,
                endpoints=[ENDPOINT],
                status="failed",
                error=str(exc),
            )
            return HarvestResult(
                source=self.name,
                status="failed",
                listed=len(listed_ids),
                written=manifest["counts"]["written"],
                unchanged=manifest["counts"]["unchanged"],
                removed=manifest["counts"]["removed"],
                seconds=time.monotonic() - started,
                error=str(exc),
            )

        manifest = self.store.finalize(
            listed_ids=listed_ids,
            harvested_at=harvested_at,
            endpoints=[ENDPOINT],
            status="ok",
        )
        return HarvestResult(
            source=self.name,
            status="ok",
            listed=len(listed_ids),
            written=manifest["counts"]["written"],
            unchanged=manifest["counts"]["unchanged"],
            removed=manifest["counts"]["removed"],
            seconds=time.monotonic() - started,
        )
