"""Live smoke tests against the real OpenNeuro GraphQL API.

Skipped unless `ATLAS_LIVE=1` (see `make test-live`); network calls here
are real and subject to the project's politeness delay (`atlas.http`).
"""

from __future__ import annotations

import os

import pytest

from atlas.harvest.base import RawStore
from atlas.harvest.openneuro import OpenNeuroHarvester

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ATLAS_LIVE") != "1",
        reason="live-marked tests only run with ATLAS_LIVE=1 (see `make test-live`)",
    ),
]


def test_probe_passes_against_the_real_api(tmp_path):
    harvester = OpenNeuroHarvester(store=RawStore("openneuro", root=tmp_path))
    result = harvester.probe()  # raises AssertionError if the shape is wrong
    node = result["data"]["datasets"]["edges"][0]["node"]
    assert node["id"]


def test_first_page_has_the_required_keys(tmp_path):
    harvester = OpenNeuroHarvester(store=RawStore("openneuro", root=tmp_path))
    result = harvester._fetch_page(5, None)

    datasets = result["data"]["datasets"]
    assert set(datasets["pageInfo"]) >= {"hasNextPage", "endCursor", "count"}
    assert datasets["pageInfo"]["count"] > 1000  # ~1,864 as of 2026-08-22

    edges = datasets["edges"]
    assert len(edges) == 5
    valid_nodes = [e["node"] for e in edges if e and e.get("node")]
    assert valid_nodes, "all 5 edges failed server-side -- unexpected"

    node = valid_nodes[0]
    assert set(node) >= {"id", "created", "publishDate", "metadata", "latestSnapshot"}
    assert set(node["metadata"]) >= {
        "species",
        "studyDomain",
        "dxStatus",
        "ages",
        "modalities",
        "associatedPaperDOI",
        "affirmedDefaced",
    }
    if node["latestSnapshot"]:
        snapshot = node["latestSnapshot"]
        assert set(snapshot) >= {"tag", "description", "readme", "summary"}
        assert set(snapshot["description"]) >= {
            "Name",
            "Authors",
            "License",
            "DatasetDOI",
            "ReferencesAndLinks",
        }
        assert set(snapshot["summary"]) >= {
            "subjects",
            "modalities",
            "secondaryModalities",
            "tasks",
            "size",
        }
