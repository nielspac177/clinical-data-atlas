"""Live smoke tests for the PhysioNet harvester + normalizer against the
real API. Only runs with `ATLAS_LIVE=1` (see `make test-live`) -- offline
CI/unit runs skip this file entirely, same pattern as
`tests/test_network_guard.py::test_live_marked_tests_bypass_the_guard`.

Writes go through a `tmp_path`-rooted `RawStore`, never `data/raw/
physionet/`, so this is safe to run repeatedly without touching the
committed raw cache.
"""

from __future__ import annotations

import json
import os

import pytest

from atlas.harvest.base import RawStore
from atlas.harvest.physionet import PhysioNetHarvester
from atlas.normalize import physionet
from atlas.normalize.common import Excluded
from atlas.schema import Record, validate_records

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ATLAS_LIVE") != "1",
        reason="live-marked tests only run with ATLAS_LIVE=1 (see `make test-live`)",
    ),
]


def test_probe_against_real_api():
    body = PhysioNetHarvester().probe()
    assert isinstance(body, list)
    assert len(body) > 100  # 716 at verification time; allow drift either way
    first = body[0]
    assert "slug" in first
    assert "access_policy" in first


def test_harvest_limit_smoke_writes_and_normalizes_cleanly(tmp_path):
    store = RawStore("physionet", root=tmp_path)
    result = PhysioNetHarvester(store=store).harvest(limit=5)

    assert result.status == "ok"
    assert 1 <= result.written <= 5
    assert result.error is None

    envelopes = store.load_all()
    assert envelopes  # at least one record made it through

    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    harvested_at = manifest["harvested_at"]

    for native_id, envelope in envelopes.items():
        first_seen = manifest["records"][native_id]["first_seen"]
        outcome = physionet.normalize(
            envelope, harvested_at=harvested_at, first_seen=first_seen
        )
        assert isinstance(outcome, (Record, Excluded))
        if isinstance(outcome, Record):
            errors, _warnings = validate_records([outcome.model_dump(mode="json")])
            assert errors == []
