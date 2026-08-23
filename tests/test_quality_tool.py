"""Unit tests for the quality-loop ledger/scorecard helper (atlas/tools/quality.py)."""

from atlas.tools import quality


def _summary():
    return {
        "round": 1,
        "findings": [
            {
                "id": "r1-site-01",
                "lens": "site",
                "severity": "high",
                "area": "site",
                "file": "site/a.js",
                "claim": "x",
                "status": "confirmed",
            },
            {
                "id": "r1-site-02",
                "lens": "site",
                "severity": "low",
                "area": "site",
                "file": "site/b.js",
                "claim": "y",
                "status": "refuted",
            },
            {
                "id": "r1-data-01",
                "lens": "data",
                "severity": "critical",
                "area": "data",
                "file": "rec",
                "claim": "z",
                "status": "confirmed",
            },
            {
                "id": "r1-docs-01",
                "lens": "docs",
                "severity": "medium",
                "area": "docs",
                "file": "README.md",
                "claim": "w",
                "status": "note",
            },
        ],
        "fixes": [{"area": "site", "fixed_ids": ["r1-site-01"]}],
    }


def test_ingest_statuses_and_fixed():
    rows, counts = quality.ingest(_summary(), [])
    by = {r["id"]: r for r in rows}
    assert by["r1-site-01"]["status"] == "fixed"
    assert by["r1-site-02"]["status"] == "refuted"
    assert by["r1-data-01"]["status"] == "open"
    assert by["r1-docs-01"]["status"] == "note"
    assert counts["added"] == 4


def test_score_caps_and_weights():
    rows, _ = quality.ingest(_summary(), [])
    s = quality.score(rows, dod_pass=True)
    # one open critical in data -> data 60, others 100, dod 100 -> weighted 90, capped at 59
    assert s["scores"]["data"] == 60
    assert s["scores"]["site"] == 100
    assert s["overall"] == 59
    quality.mark(rows, ["r1-data-01"], "fixed", 2)
    s2 = quality.score(rows, dod_pass=False)
    assert s2["scores"]["dod"] == 0
    assert s2["overall"] == 90.0


def test_age_open_counts_rounds():
    rows, _ = quality.ingest(_summary(), [])
    quality.age_open(rows, 3)
    assert next(r for r in rows if r["id"] == "r1-data-01")["age"] == 2
