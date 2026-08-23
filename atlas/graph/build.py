"""Build `data/graph/graph.json`, `search-index.json`, and `stats.json`.

`build_graph` turns the catalog's `Record`s into the three frozen
contracts the static site depends on (see the design spec's "Data
contracts" section):

- `graph.json`: `{"nodes": [...], "links": [...]}` for the 3D force
  graph. Every dataset gets its own node; each distinct source/modality/
  condition/institution *value* collapses to one shared node touched by
  every dataset that has it, so the graph shows structure instead of one
  dataset-shaped blob per record.
- `search-index.json`: one flat row per dataset for the filterable
  table -- each row's `conditions` is that record's own condition labels
  verbatim, in record order (not deduplicated/canonicalized across
  records the way a `condition` graph node's label is).
- `stats.json`: small aggregate counts for the site's summary numbers.

The two outputs join on the dataset `id`: a `graph.json` dataset node's
`id` is the same string as the matching `search-index.json` row's `id`
(both are `record.id`, unchanged) -- a site page can look up one from
the other directly, no separate mapping needed.

`write_outputs` writes all three as `io.canonical_json` (compact, sorted
keys, one trailing newline) via `io.write_atomic`, so two runs over the
same input produce byte-identical files and a reader never sees a
half-written one.

Records with `record_status == "removed"` are excluded from every output
before any rule below runs; `needs_review` records are included like any
other active record. The remaining records are then processed in
`id`-sorted order (never the caller-supplied order) so that every output
-- including which casing wins for a condition/institution label shared
by more than one record -- is a pure function of the record *set*, not
of the order the caller happened to list them in.
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

# Import-time guard: every `vocab.SOURCES` value must have a human label
# and vice versa, so a new/renamed source can never silently produce a
# `KeyError` (or a stale, unused entry) here instead of a loud failure at
# import time.
assert set(_SOURCE_LABELS) == set(vocab.SOURCES), (
    "_SOURCE_LABELS must have exactly one entry per atlas.vocab.SOURCES "
    "value -- add/remove one to keep them in sync"
)

# Position of each vocab value in its tuple: keeps every list of vocab
# values in `atlas.vocab`'s fixed order (deterministic regardless of the
# order a record's fields happened to be populated in) and, for domain
# aggregation, breaks ties deterministically.
_DOMAIN_RANK: dict[str, int] = {value: i for i, value in enumerate(vocab.DOMAINS)}
_MODALITY_RANK: dict[str, int] = {value: i for i, value in enumerate(vocab.MODALITIES)}

# Node types that carry a `domains` summary: the dominant domain of the
# datasets connected to that node. Every non-dataset type gets one -- a
# dataset node's `domains` is its own field (set directly from the
# record), not a summary of anything it's connected to, so it's the only
# type outside this set.
_DOMAIN_SUMMARY_TYPES = ("source", "modality", "condition", "institution")


def _vocab_sorted(values: list[str], rank: dict[str, int]) -> list[str]:
    """`values`, deduplicated and ordered per `rank` (a vocab tuple's
    value -> index map), regardless of the order they arrived in."""
    return sorted(dict.fromkeys(values), key=lambda v: rank[v])


def _institution_key(ror_id: str | None, name: str) -> str:
    """The id fragment for an institution node: `ror_id`'s last URL path
    segment when it's a URL (`https://ror.org/02mhbdp94` -> `02mhbdp94`),
    `ror_id` itself when it's already bare, else `io.slugify(name)`.
    Surrounding whitespace on `ror_id` (and on the extracted segment) is
    stripped before use, since it's a source-reported field that reaches
    here unvalidated.

    Note: a bare-id record and a ROR-URL record describing the very same
    real-world institution produce two distinct institution nodes -- the
    two id schemes are never cross-referenced. Inherent to this id rule,
    not handled specially.
    """
    if ror_id:
        cleaned = ror_id.strip().rstrip("/")
        return cleaned.rsplit("/", 1)[-1].strip()
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

    A record's own `domains`/`modalities`/`conditions`/`institutions` are
    deduplicated before counting (repeated values contribute only one
    edge and one domain-count sample each), and only its first 3
    institutions (record order) get a `has_institution` edge at all.

    `related` edges collapse a pair declared from both sides into one
    edge (`source < target` by id string), skip a `related.id` that
    isn't among the *included* records -- which is also how a `related`
    link pointing at a `removed` record disappears: `removed` records
    are filtered out before this id set is built, so they were never a
    valid target to begin with -- and skip a record relating to itself
    (never a real edge, so never worth a self-loop).

    See the module docstring for why records are processed in id-sorted
    order rather than the order `records` arrived in.
    """
    included = sorted(
        (r for r in records if r.record_status != "removed"), key=lambda r: r.id
    )
    known_ids = {r.id for r in included}

    nodes: dict[str, dict] = {}
    links: set[tuple[str, str, str]] = set()
    # First-seen (in id-sorted order) label for each condition/institution
    # id -- deterministic because `included` is sorted before this loop.
    condition_labels: dict[str, str] = {}
    institution_labels: dict[str, str] = {}
    node_domains: dict[str, Counter] = defaultdict(Counter)
    related_pairs: set[tuple[str, str]] = set()
    # Each record's own deduplicated, vocab-ordered domains -- computed
    # once here and reused for the dataset node, the matching search-
    # index row, and every domain-count tally below, so none of them can
    # drift apart from recomputing the same thing differently.
    domains_by_dataset_id: dict[str, list[str]] = {}

    for record in included:
        domains = _vocab_sorted(record.domains, _DOMAIN_RANK)
        domains_by_dataset_id[record.id] = domains

        nodes[record.id] = {
            "id": record.id,
            "type": "dataset",
            "label": record.name,
            "source": record.source,
            "domains": domains,
            "access": record.access,
        }

        source_id = f"source:{record.source}"
        nodes.setdefault(
            source_id,
            {"id": source_id, "type": "source", "label": _SOURCE_LABELS[record.source]},
        )
        links.add((record.id, source_id, "has_source"))
        node_domains[source_id].update(domains)

        for modality in dict.fromkeys(record.modalities):
            modality_id = f"modality:{modality}"
            nodes.setdefault(
                modality_id, {"id": modality_id, "type": "modality", "label": modality}
            )
            links.add((record.id, modality_id, "has_modality"))
            node_domains[modality_id].update(domains)

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
            node_domains[condition_id].update(domains)

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
            node_domains[institution_id].update(domains)

        for rel in record.related:
            if rel.id in known_ids and rel.id != record.id:
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

    search_index = _build_search_index(included, domains_by_dataset_id)
    stats = _build_stats(included, graph)
    return graph, search_index, stats


def _build_search_index(
    records: list[Record], domains_by_dataset_id: dict[str, list[str]]
) -> list[dict]:
    """One row per record for the site's filterable table, sorted by id.

    `domains` reuses the same deduplicated, vocab-ordered list already
    computed once per record in `build_graph` (its
    `domains_by_dataset_id`) rather than recomputing it independently
    here, so the dataset node and this row can never drift apart from
    each other.
    """
    rows = [
        {
            "id": r.id,
            "name": r.name,
            "summary": r.summary,
            "source": r.source,
            "domains": domains_by_dataset_id[r.id],
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
    only, deliberately no dates (see the module docstring).

    `per_source`/`per_domain`/`per_modality`/`per_access` include only
    the values that actually occur among `records`: a vocab value with
    zero matching records is simply absent as a key, never present with
    an explicit `0`. Consumers should treat a missing key as count `0`,
    not as "unknown"."""
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
