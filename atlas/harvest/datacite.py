"""Minimal DataCite REST client: every DOI registered under one prefix.

Not a `Harvester` -- DataCite is never a source on its own, it is where
`atlas.harvest.tcia` gets the descriptions, licenses, authors and DOIs that
the NBIA API doesn't expose. It lives here (rather than inside `tcia.py`)
because "list every DOI for a prefix" is prefix-agnostic and any later
source registering its DOIs with DataCite can reuse it unchanged.

Two things about `GET /dois` are worth knowing before reading the code:

- **Cursor paging is mandatory.** The obvious `page[number]=N` paging is
  capped and, worse, unstable: records shift between pages while you walk
  them, so a straight page-by-page crawl silently drops and duplicates
  DOIs. Starting at ``page[cursor]=1`` and then following ``links.next``
  verbatim pins a consistent snapshot.
- **The query string can't go through `urlencode`.** The parameter names
  contain brackets (``page[size]``), so `atlas.http.get_json`'s `params`
  kwarg would re-encode them into a form DataCite doesn't accept. The URL
  is therefore assembled here, with the brackets pre-encoded, and passed
  to `get_json` whole.

Because a silently-truncated listing would show up downstream as a wave of
"deleted" records, :func:`list_by_prefix` refuses to return a result it
can't vouch for: it asserts that no DOI came back twice and that the
number of records matches the ``meta.total`` the API itself reported.
"""

from __future__ import annotations

import urllib.parse

from atlas import http

API = "https://api.datacite.org/dois"


def _page_data(body: object) -> list | None:
    """The ``data`` array of a `GET /dois` response, or `None` when the
    request failed (`atlas.http.get_json` returns `None`) or the body
    isn't shaped like a DataCite page at all."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    return data if isinstance(data, list) else None


def list_by_prefix(prefix: str, *, page_size: int = 100) -> list[dict]:
    """Every DataCite record registered under `prefix`, cursor-paged.

    Returns the raw ``data`` entries (``{"id", "type", "attributes",
    "relationships"}``) in the order DataCite returned them.

    Raises `AssertionError` when the same DOI appears twice or when the
    number of records collected disagrees with the ``meta.total`` reported
    by the first page -- both mean the snapshot is inconsistent and must
    not be treated as a complete listing. Raises `RuntimeError` when a page
    request fails outright (`atlas.http.get_json` returning `None` after
    its retries) or comes back without a `data` array.
    """
    url = (
        f"{API}?prefix={urllib.parse.quote(prefix)}"
        f"&page%5Bsize%5D={page_size}&page%5Bcursor%5D=1"
    )

    records: list[dict] = []
    seen: set[str] = set()
    total: int | None = None

    while url:
        body = http.get_json(url)
        page = _page_data(body)
        if page is None:
            raise RuntimeError(f"DataCite page request failed or malformed: {url}")

        if total is None:
            total = (body.get("meta") or {}).get("total")

        for record in page:
            doi = (record.get("attributes") or {}).get("doi")
            if doi in seen:
                raise AssertionError(
                    f"DataCite returned duplicate DOI {doi!r} for prefix "
                    f"{prefix!r} -- the cursor paging snapshot is inconsistent"
                )
            seen.add(doi)
            records.append(record)

        url = (body.get("links") or {}).get("next")

    if total is not None and len(records) != total:
        raise AssertionError(
            f"DataCite prefix {prefix!r}: collected {len(records)} records but "
            f"meta.total says {total} -- the listing is incomplete"
        )
    return records
