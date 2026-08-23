"""Quality-loop ledger and scorecard (see docs/quality/README.md).

Hand-run maintenance tool; never imported by the pipeline.

    python -m atlas.tools.quality ingest ROUND_SUMMARY.json
    python -m atlas.tools.quality mark-fixed r1-site-01 r1-data-02 ...
    python -m atlas.tools.quality score --round 1 --commit abc1234 --dod pass
    python -m atlas.tools.quality table

``ingest`` appends the findings of one workflow round (the JSON the
``quality-round`` workflow returns) to ``docs/quality/findings.jsonl``:
confirmed -> open, refuted -> refuted, anything else -> note; ids listed in
``fixes[].fixed_ids`` are marked fixed straight away.  ``score`` computes the
per-lens and overall scores from the OPEN findings and records a round entry
in ``docs/quality/scorecard.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QUALITY = ROOT / "docs" / "quality"
FINDINGS = QUALITY / "findings.jsonl"
SCORECARD = QUALITY / "scorecard.json"

PENALTY = {"critical": 40, "high": 15, "medium": 5, "low": 1}
WEIGHTS = {
    "data": 25,
    "site": 20,
    "pipeline": 15,
    "performance": 10,
    "security": 10,
    "docs": 10,
    "dod": 10,
}
LENSES = [k for k in WEIGHTS if k != "dod"]
STATUSES = ("open", "fixed", "refuted", "note")


def read_findings(path: Path = FINDINGS) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_findings(rows: list[dict], path: Path = FINDINGS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    )


def ingest(summary: dict, rows: list[dict]) -> tuple[list[dict], dict]:
    """Merge one round summary into the ledger rows; returns (rows, counts)."""
    fixed_ids = {
        i for fx in summary.get("fixes") or [] for i in fx.get("fixed_ids") or []
    }
    by_id = {r["id"]: r for r in rows}
    counts = {"added": 0, "open": 0, "fixed": 0, "refuted": 0, "note": 0}
    for f in summary.get("findings") or []:
        status = {"confirmed": "open", "refuted": "refuted"}.get(
            f.get("status"), "note"
        )
        if f["id"] in fixed_ids:
            status = "fixed"
        row = {
            "id": f["id"],
            "round": f.get("round", summary.get("round")),
            "lens": f["lens"],
            "severity": f["severity"],
            "area": f.get("area"),
            "file": f.get("file"),
            "line": f.get("line"),
            "claim": f["claim"],
            "evidence": f.get("evidence", ""),
            "repro": f.get("repro", ""),
            "fix": f.get("fix", ""),
            "status": status,
            "age": 0,
            "fixed_round": summary.get("round") if status == "fixed" else None,
        }
        if f["id"] in by_id:
            by_id[f["id"]].update(row)
        else:
            rows.append(row)
            by_id[f["id"]] = row
            counts["added"] += 1
        counts[status] += 1
    return rows, counts


def mark(rows: list[dict], ids: list[str], status: str, round_no: int | None) -> int:
    n = 0
    for r in rows:
        if r["id"] in ids:
            r["status"] = status
            if status == "fixed":
                r["fixed_round"] = round_no
            n += 1
    return n


def age_open(rows: list[dict], round_no: int) -> None:
    for r in rows:
        if (
            r["status"] == "open"
            and r.get("round") is not None
            and r["round"] < round_no
        ):
            r["age"] = round_no - r["round"]


def score(rows: list[dict], *, dod_pass: bool) -> dict:
    open_rows = [r for r in rows if r["status"] == "open"]
    lens_scores = {}
    for lens in LENSES:
        pen = sum(PENALTY.get(r["severity"], 1) for r in open_rows if r["lens"] == lens)
        lens_scores[lens] = max(0, 100 - pen)
    lens_scores["dod"] = 100 if dod_pass else 0
    overall = sum(lens_scores[k] * w for k, w in WEIGHTS.items()) / sum(
        WEIGHTS.values()
    )
    open_by_sev = {s: sum(1 for r in open_rows if r["severity"] == s) for s in PENALTY}
    if open_by_sev["critical"]:
        overall = min(overall, 59)
    if open_by_sev["high"]:
        overall = min(overall, 84)
    return {"scores": lens_scores, "overall": round(overall, 1), "open": open_by_sev}


def render_table(entry: dict) -> str:
    lines = ["| lens | score |", "|---|---|"]
    for k, v in entry["scores"].items():
        lines.append(f"| {k} | {v} |")
    lines.append(f"| **overall** | **{entry['overall']}** |")
    o = entry["open"]
    lines.append("")
    lines.append(
        f"Open findings: critical {o['critical']}, high {o['high']}, medium {o['medium']}, low {o['low']}."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m atlas.tools.quality")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(
        "ingest", help="append a workflow round summary to findings.jsonl"
    )
    p.add_argument("summary", type=Path)
    p = sub.add_parser("mark-fixed", help="mark finding ids fixed")
    p.add_argument("ids", nargs="+")
    p.add_argument("--round", type=int, default=None)
    p = sub.add_parser("mark", help="set an explicit status on finding ids")
    p.add_argument("status", choices=STATUSES)
    p.add_argument("ids", nargs="+")
    p.add_argument("--round", type=int, default=None)
    p = sub.add_parser(
        "score", help="compute scores from open findings and record the round"
    )
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--commit", default="")
    p.add_argument("--dod", choices=["pass", "fail"], required=True)
    p.add_argument("--date", default=datetime.now(tz=UTC).date().isoformat())
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("table", help="print the open findings grouped by lens")
    a = ap.parse_args(argv)

    rows = read_findings()
    if a.cmd == "ingest":
        summary = json.loads(a.summary.read_text())
        rows, counts = ingest(summary, rows)
        write_findings(rows)
        print(json.dumps(counts))
        return 0
    if a.cmd == "mark-fixed":
        n = mark(rows, a.ids, "fixed", a.round)
        write_findings(rows)
        print(f"marked {n} fixed")
        return 0
    if a.cmd == "mark":
        n = mark(rows, a.ids, a.status, a.round)
        write_findings(rows)
        print(f"marked {n} {a.status}")
        return 0
    if a.cmd == "score":
        age_open(rows, a.round)
        entry = {
            "round": a.round,
            "date": a.date,
            "commit": a.commit,
            "dod": a.dod,
            **score(rows, dod_pass=a.dod == "pass"),
        }
        print(render_table(entry))
        if not a.dry_run:
            write_findings(rows)
            card = (
                json.loads(SCORECARD.read_text())
                if SCORECARD.exists()
                else {"rounds": []}
            )
            card["rounds"] = [
                r for r in card["rounds"] if r.get("round") != a.round
            ] + [entry]
            card["rounds"].sort(key=lambda r: r["round"])
            SCORECARD.write_text(json.dumps(card, indent=2, sort_keys=True) + "\n")
        return 0
    if a.cmd == "table":
        for lens in LENSES:
            rs = [r for r in rows if r["lens"] == lens and r["status"] == "open"]
            if rs:
                print(f"## {lens} ({len(rs)} open)")
                for r in sorted(rs, key=lambda r: list(PENALTY).index(r["severity"])):
                    print(
                        f"- [{r['severity']}] {r['id']} {r['file']}: {r['claim']} (age {r.get('age', 0)})"
                    )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
