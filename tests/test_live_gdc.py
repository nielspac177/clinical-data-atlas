"""Live smoke test for the real GDC projects API.

Two opt-ins are required to actually hit the network, matching the
pattern in `tests/test_network_guard.py` / `tests/test_http.py`:

- `@pytest.mark.live` + the `skipif` below, so `ATLAS_LIVE=1` (as set by
  `make test-live`) is required even when a bare `pytest -m live` is run.
- `atlas.config.OFFLINE` monkeypatched to `False`: `tests/conftest.py`
  sets `ATLAS_OFFLINE=1` via `os.environ.setdefault` before any test
  module (or `atlas.config`) is imported, so `config.OFFLINE` is `True`
  for the whole process regardless of `ATLAS_LIVE` -- the `live` marker
  only lifts `conftest.py`'s *socket* guard, not this independent check
  in `atlas.http`. Flipping the config attribute (read live, per
  `atlas.http`'s module docstring) is how a `live` test opts back in.
"""

from __future__ import annotations

import os

import pytest

from atlas import config
from atlas.harvest import gdc
from atlas.harvest.base import RawStore

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ATLAS_LIVE") != "1",
        reason="live-marked tests only run with ATLAS_LIVE=1 (see `make test-live`)",
    ),
]


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)


def test_probe_against_the_real_api():
    body = gdc.GDCHarvester().probe()
    hits = body["data"]["hits"]
    assert hits[0]["project_id"]
    assert body["data"]["pagination"]["total"] > 0


def test_harvest_a_few_real_projects(tmp_path):
    store = RawStore("gdc", root=tmp_path)
    result = gdc.GDCHarvester(store=store).harvest(limit=3)

    assert result.status == "ok"
    assert result.listed == 3
    assert result.written == 3

    envelopes = store.load_all()
    assert len(envelopes) == 3
    for native_id, envelope in envelopes.items():
        payload = envelope["payload"]
        assert payload["project_id"] == native_id
        assert "summary" in payload
        assert "program" in payload
