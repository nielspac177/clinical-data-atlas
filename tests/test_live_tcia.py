"""Live smoke checks for TCIA's two upstream APIs.

Marked `live`, so the socket guard in `tests/conftest.py` lets them through
and they only run via `make test-live` (`ATLAS_LIVE=1 pytest -m live`).
Two requests total: one NBIA probe and one DataCite page.
"""

from __future__ import annotations

import pytest

from atlas import config, http
from atlas.harvest import datacite, tcia

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live():
    if not config.LIVE:
        pytest.skip("live tests need ATLAS_LIVE=1")


def test_probe_against_the_real_nbia_api():
    body = tcia.TciaHarvester().probe()

    assert len(body) > 100, f"expected >100 public collections, got {len(body)}"
    assert "TCGA-LUAD" in {entry["Collection"] for entry in body}


def test_one_real_datacite_page_has_the_fields_the_normalizer_reads():
    url = (
        f"{datacite.API}?prefix={tcia.DOI_PREFIX}&page%5Bsize%5D=25&page%5Bcursor%5D=1"
    )
    body = http.get_json(url)

    assert body is not None, f"DataCite request failed: {url}"
    assert body["meta"]["total"] > 200
    assert len(body["data"]) == 25
    assert body["links"]["next"], "expected a cursor `links.next` to follow"

    attributes = body["data"][0]["attributes"]
    for field in ("doi", "titles", "url", "publicationYear", "creators"):
        assert field in attributes, f"DataCite attributes lost {field!r}"
