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

# Hard stop on the `links.next` walk. At the default page size that is
# 20,000 records -- far beyond any prefix this project reads -- so hitting
# it means the cursor is looping, not that a prefix is genuinely huge.
MAX_PAGES = 200


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

    Raises `AssertionError` when the same DOI appears twice, when the
    number of records collected disagrees with the ``meta.total`` reported
    by the first page, or when the `links.next` walk runs past
    `MAX_PAGES` -- each means the snapshot is inconsistent (or the cursor
    is looping) and must not be treated as a complete listing. Raises
    `RuntimeError` when a page request fails outright
    (`atlas.http.get_json` returning `None` after its retries) or comes
    back without a `data` array.
    """
    url = (
        f"{API}?prefix={urllib.parse.quote(prefix)}"
        f"&page%5Bsize%5D={page_size}&page%5Bcursor%5D=1"
    )

    records: list[dict] = []
    seen: set[str] = set()
    total: int | None = None

    for _ in range(MAX_PAGES):
        body = http.get_json(url)
        page = _page_data(body)
        if page is None:
            raise RuntimeError(f"DataCite page request failed or malformed: {url}")

        if total is None:
            total = (body.get("meta") or {}).get("total")

        for record in page:
            # DataCite's `id` is the DOI, and unlike `attributes.doi` it is
            # always present -- keying the duplicate check on it means two
            # records that merely *lack* a DOI can't look like a duplicate
            # pair (`None` seen twice).
            key = record.get("id") or (record.get("attributes") or {}).get("doi")
            if key is not None:
                if key in seen:
                    raise AssertionError(
                        f"DataCite returned duplicate DOI {key!r} for prefix "
                        f"{prefix!r} -- the cursor paging snapshot is inconsistent"
                    )
                seen.add(key)
            records.append(record)

        next_url = (body.get("links") or {}).get("next")
        # An empty page still carrying a `next` link is DataCite's cursor
        # walking past the end; without this the loop would spin until the
        # page cap.
        if not next_url or not page:
            break
        url = next_url
    else:
        raise AssertionError(
            f"DataCite prefix {prefix!r}: still paging after {MAX_PAGES} pages "
            f"({len(records)} records) -- refusing to follow `links.next` further"
        )

    if total is not None and len(records) != total:
        raise AssertionError(
            f"DataCite prefix {prefix!r}: collected {len(records)} records but "
            f"meta.total says {total} -- the listing is incomplete"
        )
    return records
