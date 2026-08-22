"""Build `data/graph/graph.json`, `search-index.json`, and `stats.json`.

`build_graph` turns the catalog's `Record`s into the three frozen
contracts the static site depends on (see the design spec's "Data
contracts" section):

- `graph.json`: `{"nodes": [...], "links": [...]}` for the 3D force
  graph. Every dataset gets its own node; each distinct source/modality/
  condition/institution *value* collapses to one shared node touched by
  every dataset that has it, so the graph shows structure instead of one
  dataset-shaped blob per record.
- `search-index.json`: one flat row per dataset for the filterable table.
- `stats.json`: small aggregate counts for the site's summary numbers.

`write_outputs` writes all three as `io.canonical_json` (compact, sorted
keys, one trailing newline) via `io.write_atomic`, so two runs over the
same input produce byte-identical files and a reader never sees a
half-written one.

Records with `record_status == "removed"` are excluded from every output
before any rule below runs; `needs_review` records are included like any
other active record.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from atlas import config, io, vocab
from atlas.schema import Record

# Human-readable label for each `atlas.vocab.SOURCES` value -- used as
# that source's node label; the raw value (e.g. "data_in_brief") is fine
# as an id but not as display text.
_SOURCE_LABELS: dict[str, str] = {
    "openneuro": "OpenNeuro",
    "physionet": "PhysioNet",
    "gdc": "NCI GDC",
    "tcia": "TCIA",
    "scientific_data": "Scientific Data",
    "data_in_brief": "Data in Brief",
    "dhs": "DHS Program",
    "worldbank": "World Bank Microdata",
    "curated": "Curated",
}

# Position of each vocab value in its tuple: keeps every list of vocab
# values in `atlas.vocab`'s fixed order (deterministic regardless of the
# order a record's fields happened to be populated in) and, for domain
# aggregation, breaks ties deterministically.
_DOMAIN_RANK: dict[str, int] = {value: i for i, value in enumerate(vocab.DOMAINS)}
_MODALITY_RANK: dict[str, int] = {value: i for i, value in enumerate(vocab.MODALITIES)}

# Node types that carry a `domains` summary (the dominant domain of the
# datasets connected to them) -- `source` deliberately excluded, per the
# contract: only modality/condition/institution nodes get one.
_DOMAIN_SUMMARY_TYPES = ("modality", "condition", "institution")


def _vocab_sorted(values: list[str], rank: dict[str, int]) -> list[str]:
    """`values`, deduplicated and ordered per `rank` (a vocab tuple's
    value -> index map), regardless of the order they arrived in."""
    return sorted(dict.fromkeys(values), key=lambda v: rank[v])


def _institution_key(ror_id: str | None, name: str) -> str:
    """The id fragment for an institution node: `ror_id`'s last URL path
    segment when it's a URL (`https://ror.org/02mhbdp94` -> `02mhbdp94`),
    `ror_id` itself when it's already bare, else `io.slugify(name)`."""
    if ror_id:
        return ror_id.rstrip("/").rsplit("/", 1)[-1]
    return io.slugify(name)


def _dominant_domain(counts: Counter) -> str:
    """The domain with the highest count in `counts`; ties broken by
    `atlas.vocab.DOMAINS` order for a deterministic pick."""
    return min(counts.items(), key=lambda kv: (-kv[1], _DOMAIN_RANK[kv[0]]))[0]


# --------------------------------------------------------------------------
# graph.json + search-index.json + stats.json
# --------------------------------------------------------------------------


def build_graph(records: list[Record]) -> tuple[dict, list[dict], dict]:
    """Build `(graph, search_index, stats)` from `records`.

    `graph = {"nodes": [...], "links": [...]}`: one node per dataset plus
    one shared node per distinct source/modality/condition/institution
    value, and one link per dataset<->attribute relationship
    (`has_source`/`has_modality`/`has_condition`/`has_institution`) plus
    one per related dataset pair (`related`, undirected -- see below).
    `degree` is the number of links touching each node, computed once
    over the complete link set. `backbone` (`True`, or the key omitted --
    never `False`) marks every source/modality node and every condition/
    institution node with degree >= 2; a dataset node never gets it.

    A record's own `modalities`/`conditions`/`institutions` are
    deduplicated before counting (repeated values contribute only one
    edge and one domain-count sample each), and only its first 3
    institutions (record order) get a `has_institution` edge at all.

    `related` edges collapse a pair declared from both sides into one
    edge (`source < target` by id string) and skip a `related.id` that
    isn't among the *included* records -- which is also how a `related`
    link pointing at a `removed` record disappears: `removed` records
    are filtered out before this id set is built, so they were never a
    valid target to begin with.
    """
    included = [r for r in records if r.record_status != "removed"]
    known_ids = {r.id for r in included}

    nodes: dict[str, dict] = {}
    links: set[tuple[str, str, str]] = set()
    condition_labels: dict[str, str] = {}
    institution_labels: dict[str, str] = {}
    node_domains: dict[str, Counter] = defaultdict(Counter)
    related_pairs: set[tuple[str, str]] = set()

    for record in included:
        nodes[record.id] = {
            "id": record.id,
            "type": "dataset",
            "label": record.name,
            "source": record.source,
            "domains": _vocab_sorted(record.domains, _DOMAIN_RANK),
            "access": record.access,
        }

        source_id = f"source:{record.source}"
        nodes.setdefault(
            source_id,
            {"id": source_id, "type": "source", "label": _SOURCE_LABELS[record.source]},
        )
        links.add((record.id, source_id, "has_source"))

        for modality in dict.fromkeys(record.modalities):
            modality_id = f"modality:{modality}"
            nodes.setdefault(
                modality_id, {"id": modality_id, "type": "modality", "label": modality}
            )
            links.add((record.id, modality_id, "has_modality"))
            node_domains[modality_id].update(record.domains)

        seen_conditions: dict[str, str] = {}
        for condition in record.conditions:
            slug = io.slugify(condition.label)
            seen_conditions.setdefault(slug, condition.label)
        for slug, label in seen_conditions.items():
            condition_id = f"condition:{slug}"
            condition_labels.setdefault(condition_id, label)
            nodes.setdefault(
                condition_id,
                {
                    "id": condition_id,
                    "type": "condition",
                    "label": condition_labels[condition_id],
                },
            )
            links.add((record.id, condition_id, "has_condition"))
            node_domains[condition_id].update(record.domains)

        seen_institutions: dict[str, str] = {}  # key -> name, first-seen in this record
        for institution in record.institutions[:3]:
            key = _institution_key(institution.ror_id, institution.name)
            seen_institutions.setdefault(key, institution.name)
        for key, name in seen_institutions.items():
            institution_id = f"institution:{key}"
            institution_labels.setdefault(institution_id, name)
            nodes.setdefault(
                institution_id,
                {
                    "id": institution_id,
                    "type": "institution",
                    "label": institution_labels[institution_id],
                },
            )
            links.add((record.id, institution_id, "has_institution"))
            node_domains[institution_id].update(record.domains)

        for rel in record.related:
            if rel.id in known_ids:
                related_pairs.add(tuple(sorted((record.id, rel.id))))

    for dataset_a, dataset_b in related_pairs:
        links.add((dataset_a, dataset_b, "related"))

    degree: Counter = Counter()
    for endpoint_a, endpoint_b, _type in links:
        degree[endpoint_a] += 1
        degree[endpoint_b] += 1

    for node_id, node in nodes.items():
        node["degree"] = degree[node_id]
        if node["type"] in _DOMAIN_SUMMARY_TYPES:
            counts = node_domains[node_id]
            node["domains"] = [_dominant_domain(counts)] if counts else []
        always_backbone = node["type"] in ("source", "modality")
        past_threshold = node["type"] in ("condition", "institution") and (
            node["degree"] >= 2
        )
        if always_backbone or past_threshold:
            node["backbone"] = True

    graph = {
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "links": [{"source": a, "target": b, "type": ty} for a, b, ty in sorted(links)],
    }

    search_index = _build_search_index(included)
    stats = _build_stats(included, graph)
    return graph, search_index, stats


def _build_search_index(records: list[Record]) -> list[dict]:
    """One row per record for the site's filterable table, sorted by id."""
    rows = [
        {
            "id": r.id,
            "name": r.name,
            "summary": r.summary,
            "source": r.source,
            "domains": _vocab_sorted(r.domains, _DOMAIN_RANK),
            "modalities": _vocab_sorted(r.modalities, _MODALITY_RANK),
            "conditions": [c.label for c in r.conditions],
            "countries": list(r.countries),
            "access": r.access,
            "years": {"start": r.years.start, "end": r.years.end},
            "sample_size": r.sample_size,
            "sample_unit": r.sample_unit,
            "url": r.url,
            "species": r.species,
        }
        for r in records
    ]
    rows.sort(key=lambda row: row["id"])
    return rows


def _build_stats(records: list[Record], graph: dict) -> dict:
    """Small aggregate counts for the site's summary numbers -- counts
    only, deliberately no dates (see the module docstring)."""
    backbone_count = sum(1 for node in graph["nodes"] if node.get("backbone"))
    return {
        "record_count": len(records),
        "per_source": dict(Counter(r.source for r in records)),
        "per_domain": dict(Counter(d for r in records for d in r.domains)),
        "per_modality": dict(Counter(m for r in records for m in r.modalities)),
        "per_access": dict(Counter(r.access for r in records)),
        "backbone_count": backbone_count,
        "node_count": len(graph["nodes"]),
        "link_count": len(graph["links"]),
    }


def write_outputs(
    graph: dict, index: list[dict], stats: dict, out_dir: Path = config.GRAPH
) -> None:
    """Write `graph.json`, `search-index.json`, and `stats.json` under
    `out_dir` as compact, sorted-key, trailing-newline JSON
    (`io.canonical_json`), atomically (`io.write_atomic`) -- a re-build
    over unchanged input reproduces byte-identical files, and a reader
    (or `git diff`) never sees a half-written one."""
    io.write_atomic(out_dir / "graph.json", io.canonical_json(graph))
    io.write_atomic(out_dir / "search-index.json", io.canonical_json(index))
    io.write_atomic(out_dir / "stats.json", io.canonical_json(stats))
