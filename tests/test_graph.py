"""Tests for atlas.graph.build: graph.json + search-index.json + stats.json.

Conventions mirrored from tests/test_schema.py: a `_record` factory builds
a minimal-but-valid `Record` with sensible graph-relevant overrides, and
tests are grouped by the rule they exercise (node ids/labels, links,
degree, backbone, domains aggregation, ordering, search index, stats,
write_outputs determinism), ending with one whole-fixture contract test.
"""

from __future__ import annotations

import inspect
import json
import re
from collections import Counter

from atlas import config, io, vocab
from atlas.graph import build
from atlas.schema import Record

# --------------------------------------------------------------------------
# Fixture factory
# --------------------------------------------------------------------------


def _record(id_: str, *, source: str | None = None, **overrides: object) -> Record:
    """A minimal-but-valid `Record`, graph-relevant fields overridable.

    `source` defaults to the id's own prefix (`Record` requires `id` to
    start with `f"{source}:"`), so callers only need to pass it
    explicitly when deliberately varying the source for a fixed id.

    Dates are fixed to a real value (not omitted) so the "no dates in
    output" tests actually exercise something: if `build_graph` ever
    leaked `provenance`/`published` into an output, these dates would
    show up and the regex check would catch it.
    """
    prefix, native = id_.split(":", 1)
    source = source or prefix
    data: dict = {
        "id": id_,
        "source": source,
        "source_native_id": native,
        "name": f"Dataset {native}",
        "summary": "A short synthetic dataset summary for graph build tests.",
        "url": f"https://example.org/{native}",
        "species": "human",
        "sample_size": None,
        "sample_unit": None,
        "countries": [],
        "years": {},
        "access": "open",
        "record_status": "active",
        "provenance": {
            "harvested_via": "test",
            "harvested_at": "2026-01-01",
            "last_verified": "2026-01-01",
            "enrichment": {"method": "rules", "fields": {}},
        },
    }
    data.update(overrides)
    return Record.model_validate(data)


def _node_map(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"]}


def _link_set(graph: dict) -> set[tuple[str, str, str]]:
    return {(link["source"], link["target"], link["type"]) for link in graph["links"]}


# --------------------------------------------------------------------------
# Empty input
# --------------------------------------------------------------------------


def test_empty_records_produce_empty_graph_and_index():
    graph, index, stats = build.build_graph([])
    assert graph == {"nodes": [], "links": []}
    assert index == []
    assert stats["record_count"] == 0
    assert stats["node_count"] == 0
    assert stats["link_count"] == 0
    assert stats["backbone_count"] == 0
    assert stats["per_source"] == {}
    assert stats["per_domain"] == {}
    assert stats["per_modality"] == {}
    assert stats["per_access"] == {}


# --------------------------------------------------------------------------
# Dataset + source: node shape, ids, labels, links, degree, backbone
# --------------------------------------------------------------------------


def test_single_record_creates_dataset_and_source_nodes():
    record = _record("openneuro:ds001", source="openneuro", domains=["neurology"])
    graph, _index, _stats = build.build_graph([record])
    nodes = _node_map(graph)

    assert set(nodes) == {"openneuro:ds001", "source:openneuro"}

    dataset = nodes["openneuro:ds001"]
    assert dataset["type"] == "dataset"
    assert dataset["label"] == record.name
    assert dataset["source"] == "openneuro"
    assert dataset["domains"] == ["neurology"]
    assert dataset["access"] == "open"
    assert dataset["degree"] == 1
    assert "backbone" not in dataset  # datasets are never backbone

    source_node = nodes["source:openneuro"]
    assert source_node["type"] == "source"
    assert source_node["label"] == "OpenNeuro"
    assert source_node["degree"] == 1
    assert source_node["backbone"] is True
    assert "domains" not in source_node  # source nodes never get a domains key


def test_source_labels_match_human_names_map():
    expected = {
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
    records = [_record(f"{src}:x{i}", source=src) for i, src in enumerate(expected)]
    graph, _index, _stats = build.build_graph(records)
    nodes = _node_map(graph)
    for src, label in expected.items():
        assert nodes[f"source:{src}"]["label"] == label


def test_has_source_link_shape():
    record = _record("openneuro:ds001")
    graph, _index, _stats = build.build_graph([record])
    assert graph["links"] == [
        {
            "source": "openneuro:ds001",
            "target": "source:openneuro",
            "type": "has_source",
        }
    ]


def test_two_datasets_same_source_share_one_source_node_with_degree_two():
    records = [_record("openneuro:ds001"), _record("openneuro:ds002")]
    graph, _index, _stats = build.build_graph(records)
    nodes = _node_map(graph)
    assert nodes["source:openneuro"]["degree"] == 2
    assert len([n for n in graph["nodes"] if n["type"] == "source"]) == 1


# --------------------------------------------------------------------------
# Modality: id, label, always backbone, one edge per distinct value
# --------------------------------------------------------------------------


def test_modality_node_id_label_and_always_backbone():
    record = _record("openneuro:ds001", modalities=["EEG"])
    graph, _index, _stats = build.build_graph([record])
    nodes = _node_map(graph)
    modality = nodes["modality:EEG"]
    assert modality["type"] == "modality"
    assert modality["label"] == "EEG"
    assert modality["backbone"] is True
    assert ("openneuro:ds001", "modality:EEG", "has_modality") in _link_set(graph)


def test_modality_backbone_even_with_degree_one():
    # Unlike conditions/institutions, a modality is backbone regardless of
    # how many datasets touch it.
    record = _record("openneuro:ds001", modalities=["MEG"])
    graph, _index, _stats = build.build_graph([record])
    nodes = _node_map(graph)
    assert nodes["modality:MEG"]["degree"] == 1
    assert nodes["modality:MEG"]["backbone"] is True


def test_duplicate_modality_within_one_record_creates_one_edge():
    record = _record("openneuro:ds001", modalities=["EEG", "EEG"])
    graph, _index, _stats = build.build_graph([record])
    assert _link_set(graph) == {
        ("openneuro:ds001", "source:openneuro", "has_source"),
        ("openneuro:ds001", "modality:EEG", "has_modality"),
    }
    assert _node_map(graph)["modality:EEG"]["degree"] == 1


def test_every_modality_gets_its_own_edge():
    record = _record("openneuro:ds001", modalities=["EEG", "MRI", "CT"])
    graph, _index, _stats = build.build_graph([record])
    modality_links = {link for link in _link_set(graph) if link[2] == "has_modality"}
    assert modality_links == {
        ("openneuro:ds001", "modality:EEG", "has_modality"),
        ("openneuro:ds001", "modality:MRI", "has_modality"),
        ("openneuro:ds001", "modality:CT", "has_modality"),
    }


# --------------------------------------------------------------------------
# Condition: id (slug), label (first-seen casing), backbone iff degree >= 2
# --------------------------------------------------------------------------


def test_condition_id_is_slug_of_label():
    record = _record("openneuro:ds001", conditions=[{"label": "QIN Breast DCE-MRI"}])
    graph, _index, _stats = build.build_graph([record])
    assert "condition:qin-breast-dce-mri" in _node_map(graph)


def test_condition_with_degree_one_is_not_backbone():
    record = _record("openneuro:ds001", conditions=[{"label": "Rare Syndrome"}])
    graph, _index, _stats = build.build_graph([record])
    node = _node_map(graph)["condition:rare-syndrome"]
    assert node["degree"] == 1
    assert "backbone" not in node


def test_condition_with_degree_two_is_backbone():
    records = [
        _record("openneuro:ds001", conditions=[{"label": "Stroke"}]),
        _record("openneuro:ds002", conditions=[{"label": "Stroke"}]),
    ]
    graph, _index, _stats = build.build_graph(records)
    node = _node_map(graph)["condition:stroke"]
    assert node["degree"] == 2
    assert node["backbone"] is True


def test_condition_label_uses_first_seen_casing():
    records = [
        _record("openneuro:ds001", conditions=[{"label": "Epilepsy"}]),
        _record("openneuro:ds002", conditions=[{"label": "epilepsy"}]),
    ]
    graph, _index, _stats = build.build_graph(records)
    node = _node_map(graph)["condition:epilepsy"]
    assert node["label"] == "Epilepsy"
    assert node["degree"] == 2  # both records point at the same slug


def test_duplicate_condition_within_one_record_creates_one_edge():
    record = _record(
        "openneuro:ds001",
        conditions=[{"label": "Stroke"}, {"label": "stroke"}],
    )
    graph, _index, _stats = build.build_graph([record])
    condition_links = [
        link for link in graph["links"] if link["type"] == "has_condition"
    ]
    assert len(condition_links) == 1


# --------------------------------------------------------------------------
# Institution: ror-derived id, slug fallback, cap at 3, backbone iff >= 2
# --------------------------------------------------------------------------


def test_institution_id_from_ror_url_uses_last_path_segment():
    record = _record(
        "openneuro:ds001",
        institutions=[
            {"name": "Mass General Hospital", "ror_id": "https://ror.org/02mhbdp94"}
        ],
    )
    graph, _index, _stats = build.build_graph([record])
    nodes = _node_map(graph)
    assert "institution:02mhbdp94" in nodes
    assert nodes["institution:02mhbdp94"]["label"] == "Mass General Hospital"


def test_institution_id_from_bare_ror_id_used_as_is():
    record = _record(
        "openneuro:ds001",
        institutions=[{"name": "Some Institute", "ror_id": "03abcxy99"}],
    )
    graph, _index, _stats = build.build_graph([record])
    assert "institution:03abcxy99" in _node_map(graph)


def test_institution_id_without_ror_falls_back_to_slugified_name():
    record = _record(
        "openneuro:ds001", institutions=[{"name": "Tiny Lab", "ror_id": None}]
    )
    graph, _index, _stats = build.build_graph([record])
    assert "institution:tiny-lab" in _node_map(graph)


def test_only_first_three_institutions_get_edges():
    record = _record(
        "openneuro:ds001",
        institutions=[
            {"name": "Inst A"},
            {"name": "Inst B"},
            {"name": "Inst C"},
            {"name": "Inst D"},
            {"name": "Inst E"},
        ],
    )
    graph, _index, _stats = build.build_graph([record])
    nodes = _node_map(graph)
    for included in ("a", "b", "c"):
        assert f"institution:inst-{included}" in nodes
    for excluded in ("d", "e"):
        assert f"institution:inst-{excluded}" not in nodes
    institution_links = [
        link for link in graph["links"] if link["type"] == "has_institution"
    ]
    assert len(institution_links) == 3


def test_institution_with_degree_one_is_not_backbone():
    record = _record("openneuro:ds001", institutions=[{"name": "Tiny Lab"}])
    graph, _index, _stats = build.build_graph([record])
    node = _node_map(graph)["institution:tiny-lab"]
    assert node["degree"] == 1
    assert "backbone" not in node


def test_institution_with_degree_two_is_backbone():
    records = [
        _record("openneuro:ds001", institutions=[{"name": "Big Lab"}]),
        _record("openneuro:ds002", institutions=[{"name": "Big Lab"}]),
    ]
    graph, _index, _stats = build.build_graph(records)
    node = _node_map(graph)["institution:big-lab"]
    assert node["degree"] == 2
    assert node["backbone"] is True


# --------------------------------------------------------------------------
# related: undirected collapse, missing target skipped, removed skipped
# --------------------------------------------------------------------------


def test_related_link_declared_from_one_side_creates_one_undirected_link():
    records = [
        _record(
            "openneuro:ds001",
            related=[{"id": "openneuro:ds002", "relation": "same_cohort"}],
        ),
        _record("openneuro:ds002"),
    ]
    graph, _index, _stats = build.build_graph(records)
    related_links = [link for link in graph["links"] if link["type"] == "related"]
    assert related_links == [
        {"source": "openneuro:ds001", "target": "openneuro:ds002", "type": "related"}
    ]


def test_related_link_declared_from_both_sides_collapses_to_one():
    records = [
        _record(
            "openneuro:ds012",
            related=[{"id": "openneuro:ds013", "relation": "duplicate_of"}],
        ),
        _record(
            "openneuro:ds013",
            related=[{"id": "openneuro:ds012", "relation": "duplicate_of"}],
        ),
    ]
    graph, _index, _stats = build.build_graph(records)
    related_links = [link for link in graph["links"] if link["type"] == "related"]
    assert len(related_links) == 1
    assert related_links[0] == {
        "source": "openneuro:ds012",
        "target": "openneuro:ds013",
        "type": "related",
    }


def test_related_link_to_nonexistent_id_is_skipped():
    record = _record(
        "openneuro:ds001",
        related=[{"id": "openneuro:ghost", "relation": "same_cohort"}],
    )
    graph, _index, _stats = build.build_graph([record])
    assert graph["links"] == [
        {
            "source": "openneuro:ds001",
            "target": "source:openneuro",
            "type": "has_source",
        }
    ]


def test_related_link_counts_toward_dataset_degree():
    # degree is "computed over all links" -- has_source (1) + related (1)
    # for each of the two datasets involved.
    records = [
        _record(
            "openneuro:ds001",
            related=[{"id": "openneuro:ds002", "relation": "same_cohort"}],
        ),
        _record("openneuro:ds002"),
    ]
    graph, _index, _stats = build.build_graph(records)
    nodes = _node_map(graph)
    assert nodes["openneuro:ds001"]["degree"] == 2
    assert nodes["openneuro:ds002"]["degree"] == 2


def test_related_link_to_removed_record_is_skipped():
    records = [
        _record(
            "openneuro:ds001",
            related=[{"id": "openneuro:removed001", "relation": "same_cohort"}],
        ),
        _record("openneuro:removed001", record_status="removed"),
    ]
    graph, _index, _stats = build.build_graph(records)
    assert "openneuro:removed001" not in _node_map(graph)
    related_links = [link for link in graph["links"] if link["type"] == "related"]
    assert related_links == []


# --------------------------------------------------------------------------
# record_status: removed excluded from everything, needs_review included
# --------------------------------------------------------------------------


def test_removed_record_excluded_from_graph_index_and_stats():
    records = [
        _record("openneuro:ds001"),
        _record("openneuro:removed001", record_status="removed"),
    ]
    graph, index, stats = build.build_graph(records)
    assert "openneuro:removed001" not in _node_map(graph)
    assert all(row["id"] != "openneuro:removed001" for row in index)
    assert stats["record_count"] == 1


def test_needs_review_record_is_included():
    records = [_record("openneuro:ds001", record_status="needs_review")]
    graph, index, stats = build.build_graph(records)
    assert "openneuro:ds001" in _node_map(graph)
    assert index[0]["id"] == "openneuro:ds001"
    assert stats["record_count"] == 1


# --------------------------------------------------------------------------
# Domains aggregation on modality/condition/institution nodes
# --------------------------------------------------------------------------


def test_non_dataset_node_domain_is_dominant_domain_of_connected_datasets():
    records = [
        _record("openneuro:ds001", modalities=["EEG"], domains=["neurology"]),
        _record("openneuro:ds002", modalities=["EEG"], domains=["neurology"]),
        _record("openneuro:ds003", modalities=["EEG"], domains=["oncology"]),
    ]
    graph, _index, _stats = build.build_graph(records)
    assert _node_map(graph)["modality:EEG"]["domains"] == ["neurology"]


def test_non_dataset_node_domain_tie_break_uses_vocab_order():
    # Two domains tie at one dataset each -- the lower-indexed vocab
    # value must win, regardless of which one was seen first.
    lower, higher = vocab.DOMAINS[0], vocab.DOMAINS[4]
    records = [
        _record("openneuro:ds001", modalities=["EEG"], domains=[higher]),
        _record("openneuro:ds002", modalities=["EEG"], domains=[lower]),
    ]
    graph, _index, _stats = build.build_graph(records)
    assert _node_map(graph)["modality:EEG"]["domains"] == [lower]


def test_non_dataset_node_domain_is_empty_list_when_no_domains():
    record = _record("openneuro:ds001", modalities=["EEG"], domains=[])
    graph, _index, _stats = build.build_graph([record])
    assert _node_map(graph)["modality:EEG"]["domains"] == []


def test_dataset_domains_are_vocab_ordered_regardless_of_input_order():
    reversed_domains = list(reversed(vocab.DOMAINS[:3]))
    record = _record("openneuro:ds001", domains=reversed_domains)
    graph, _index, _stats = build.build_graph([record])
    assert _node_map(graph)["openneuro:ds001"]["domains"] == list(vocab.DOMAINS[:3])


# --------------------------------------------------------------------------
# Ordering: nodes by id string, links by (source, target, type)
# --------------------------------------------------------------------------


def test_nodes_are_sorted_by_id_string():
    records = [
        _record("physionet:p001", modalities=["MRI"]),
        _record("openneuro:ds001", modalities=["EEG"]),
    ]
    graph, _index, _stats = build.build_graph(records)
    ids = [n["id"] for n in graph["nodes"]]
    assert ids == sorted(ids)


def test_links_are_sorted_by_source_target_type():
    records = [
        _record("physionet:p001", modalities=["MRI"]),
        _record("openneuro:ds001", modalities=["EEG"]),
    ]
    graph, _index, _stats = build.build_graph(records)
    triples = [
        (link["source"], link["target"], link["type"]) for link in graph["links"]
    ]
    assert triples == sorted(triples)


# --------------------------------------------------------------------------
# search-index.json: shape, sort order, vocab-ordered lists
# --------------------------------------------------------------------------


def test_search_index_row_shape():
    record = _record(
        "openneuro:ds001",
        domains=["neurology"],
        modalities=["EEG"],
        conditions=[{"label": "Epilepsy"}],
        countries=["US"],
        years={"start": 2015, "end": 2018},
        sample_size=42,
        sample_unit="participants",
    )
    _graph, index, _stats = build.build_graph([record])
    assert index == [
        {
            "id": "openneuro:ds001",
            "name": record.name,
            "summary": record.summary,
            "source": "openneuro",
            "domains": ["neurology"],
            "modalities": ["EEG"],
            "conditions": ["Epilepsy"],
            "countries": ["US"],
            "access": "open",
            "years": {"start": 2015, "end": 2018},
            "sample_size": 42,
            "sample_unit": "participants",
            "url": record.url,
            "species": "human",
        }
    ]


def test_search_index_sorted_by_id():
    records = [_record("openneuro:ds002"), _record("openneuro:ds001")]
    _graph, index, _stats = build.build_graph(records)
    assert [row["id"] for row in index] == ["openneuro:ds001", "openneuro:ds002"]


def test_search_index_domains_and_modalities_are_vocab_ordered():
    # Input is deliberately reverse-of-vocab-order, so this only passes if
    # vocab order is actively enforced (not just coincidentally preserved).
    domains_in = list(reversed(vocab.DOMAINS[:3]))
    modalities_in = list(reversed(vocab.MODALITIES[:3]))
    record = _record("openneuro:ds001", domains=domains_in, modalities=modalities_in)
    _graph, index, _stats = build.build_graph([record])
    assert index[0]["domains"] == list(vocab.DOMAINS[:3])
    assert index[0]["modalities"] == list(vocab.MODALITIES[:3])


def test_search_index_years_nullable_fields_pass_through():
    record = _record("openneuro:ds001", years={})
    _graph, index, _stats = build.build_graph([record])
    assert index[0]["years"] == {"start": None, "end": None}


# --------------------------------------------------------------------------
# stats.json: shape, counts, no dates
# --------------------------------------------------------------------------


def test_stats_shape_and_counts():
    records = [
        _record("openneuro:ds001", domains=["neurology"], modalities=["EEG", "MRI"]),
        _record(
            "physionet:p001", domains=["neurology", "oncology"], modalities=["EEG"]
        ),
    ]
    graph, _index, stats = build.build_graph(records)
    assert stats == {
        "record_count": 2,
        "per_source": {"openneuro": 1, "physionet": 1},
        "per_domain": {"neurology": 2, "oncology": 1},
        "per_modality": {"EEG": 2, "MRI": 1},
        "per_access": {"open": 2},
        "backbone_count": sum(1 for n in graph["nodes"] if n.get("backbone")),
        "node_count": len(graph["nodes"]),
        "link_count": len(graph["links"]),
    }


def test_stats_has_no_extra_keys():
    _graph, _index, stats = build.build_graph([_record("openneuro:ds001")])
    assert set(stats) == {
        "record_count",
        "per_source",
        "per_domain",
        "per_modality",
        "per_access",
        "backbone_count",
        "node_count",
        "link_count",
    }


def test_removed_record_excluded_from_stats_counts():
    records = [
        _record("openneuro:ds001", domains=["neurology"]),
        _record("openneuro:removed001", record_status="removed", domains=["oncology"]),
    ]
    _graph, _index, stats = build.build_graph(records)
    assert stats["per_domain"] == {"neurology": 1}
    assert stats["per_source"] == {"openneuro": 1}


# --------------------------------------------------------------------------
# No dates anywhere in any output
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")


def test_no_iso_dates_leak_into_any_output():
    record = _record(
        "openneuro:ds001",
        published="2020-05-01",
        domains=["neurology"],
        modalities=["EEG"],
        conditions=[{"label": "Epilepsy"}],
        institutions=[
            {"name": "Mass General Hospital", "ror_id": "https://ror.org/02mhbdp94"}
        ],
    )
    graph, index, stats = build.build_graph([record])
    combined = json.dumps(graph) + json.dumps(index) + json.dumps(stats)
    assert not _DATE_RE.search(combined)


# --------------------------------------------------------------------------
# write_outputs: canonical json, atomic, deterministic
# --------------------------------------------------------------------------


def test_write_outputs_writes_three_canonical_json_files(tmp_path):
    graph, index, stats = build.build_graph([_record("openneuro:ds001")])
    build.write_outputs(graph, index, stats, out_dir=tmp_path)

    assert (tmp_path / "graph.json").read_text(encoding="utf-8") == io.canonical_json(
        graph
    )
    assert (tmp_path / "search-index.json").read_text(
        encoding="utf-8"
    ) == io.canonical_json(index)
    assert (tmp_path / "stats.json").read_text(encoding="utf-8") == io.canonical_json(
        stats
    )
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_outputs_twice_produces_identical_bytes(tmp_path):
    graph, index, stats = build.build_graph(
        [
            _record("openneuro:ds001", modalities=["EEG"], domains=["neurology"]),
            _record("physionet:p001", modalities=["MRI"], domains=["oncology"]),
        ]
    )
    build.write_outputs(graph, index, stats, out_dir=tmp_path)
    first = {
        name: (tmp_path / name).read_bytes()
        for name in ("graph.json", "search-index.json", "stats.json")
    }
    build.write_outputs(graph, index, stats, out_dir=tmp_path)
    second = {
        name: (tmp_path / name).read_bytes()
        for name in ("graph.json", "search-index.json", "stats.json")
    }
    assert first == second


def test_write_outputs_default_out_dir_is_config_graph():
    default = inspect.signature(build.write_outputs).parameters["out_dir"].default
    assert default == config.GRAPH


# --------------------------------------------------------------------------
# Whole-fixture contract test: ~40 records covering every node type,
# related links (declared once, twice, dangling, pointing at a removed
# record), institutions with/without ROR, and conditions at degree 1 vs
# >= 2. `backbone` count scaling ([20, 1500]) is a full-catalog sanity
# check (see the brief) that doesn't apply to a 40-record fixture; here
# we instead assert the exact backbone set, independently re-derived from
# the raw fixture data (never from the graph's own output).
# --------------------------------------------------------------------------

_FILLER_SOURCES = (
    "openneuro",
    "physionet",
    "gdc",
    "tcia",
    "scientific_data",
    "data_in_brief",
    "dhs",
    "worldbank",
    "curated",
)
_FILLER_MODALITIES = ("MRI", "CT", "genomics", "EHR", "ultrasound")
_FILLER_DOMAINS = ("neurology", "cardiology", "oncology", "public_health")


def _full_fixture_records() -> list[Record]:
    interesting = [
        # Epilepsy (slug "epilepsy"): 5 records -> degree 5, backbone.
        # ds001/ds002 also cover first-seen casing ("Epilepsy" wins).
        _record(
            "openneuro:ds001",
            modalities=["EEG"],
            domains=["neurology"],
            conditions=[{"label": "Epilepsy"}],
            institutions=[
                {"name": "Mass General Hospital", "ror_id": "https://ror.org/02mhbdp94"}
            ],
        ),
        _record(
            "openneuro:ds002",
            modalities=["EEG", "MRI"],
            domains=["neurology"],
            conditions=[{"label": "epilepsy"}],
            institutions=[
                {"name": "Mass General Hospital", "ror_id": "https://ror.org/02mhbdp94"}
            ],
        ),
        _record(
            "physionet:p003",
            modalities=["EEG"],
            domains=["neuroscience"],
            conditions=[{"label": "Epilepsy"}],
            institutions=[{"name": "Institute Bare", "ror_id": "03abcxy99"}],
        ),
        _record(
            "physionet:p004",
            modalities=["MRI"],
            domains=["neuroscience"],
            conditions=[{"label": "Epilepsy"}],
            institutions=[{"name": "Institute Bare", "ror_id": "03abcxy99"}],
        ),
        _record(
            "physionet:p005",
            modalities=["MRI"],
            domains=["neurology"],
            conditions=[{"label": "Epilepsy"}],
        ),
        # Stroke (slug "stroke"): 2 records -> degree 2, backbone (boundary).
        _record(
            "gdc:g006",
            modalities=["genomics"],
            domains=["oncology"],
            conditions=[{"label": "Stroke"}],
        ),
        _record(
            "gdc:g007",
            modalities=["genomics"],
            domains=["oncology"],
            conditions=[{"label": "Stroke"}],
        ),
        # Rare Syndrome: 1 record -> degree 1, NOT backbone. Also carries
        # "Tiny Lab" (no ROR, degree 1) -> NOT backbone.
        _record(
            "tcia:t008",
            modalities=["CT"],
            domains=["other"],
            conditions=[{"label": "Rare Syndrome"}],
            institutions=[{"name": "Tiny Lab"}],
        ),
        # Second Mass General appearance via a plain-name/ROR record (no
        # condition) -> Mass General's degree becomes 3.
        _record(
            "tcia:t009",
            modalities=["CT", "xray"],
            domains=["surgery"],
            institutions=[
                {"name": "Mass General Hospital", "ror_id": "https://ror.org/02mhbdp94"}
            ],
        ),
        # 5 institutions -> only the first 3 (record order) get edges.
        _record(
            "scientific_data:sd010",
            modalities=["survey"],
            domains=["pediatrics"],
            institutions=[
                {"name": "Inst A"},
                {"name": "Inst B"},
                {"name": "Inst C"},
                {"name": "Inst D"},
                {"name": "Inst E"},
            ],
        ),
        # related declared from one side only.
        _record(
            "data_in_brief:dib011",
            modalities=["registry"],
            domains=["public_health"],
            related=[{"id": "openneuro:ds001", "relation": "same_cohort"}],
        ),
        # related declared from both sides -> collapses to one link.
        _record(
            "openneuro:ds012",
            modalities=["MRI"],
            domains=["cardiology"],
            related=[{"id": "openneuro:ds013", "relation": "duplicate_of"}],
        ),
        _record(
            "openneuro:ds013",
            modalities=["MRI"],
            domains=["cardiology"],
            related=[{"id": "openneuro:ds012", "relation": "duplicate_of"}],
        ),
        # related to an id that doesn't exist anywhere -> skipped.
        _record(
            "openneuro:ds014",
            modalities=["PET"],
            domains=["oncology"],
            related=[{"id": "openneuro:ghost9999", "relation": "same_cohort"}],
        ),
        # related to an id that exists but is `removed` -> skipped.
        _record(
            "openneuro:ds015",
            modalities=["SPECT"],
            domains=["cardiology"],
            related=[{"id": "openneuro:removed001", "relation": "same_cohort"}],
        ),
        # excluded from every output.
        _record(
            "openneuro:removed001",
            modalities=["MRI"],
            domains=["neurology"],
            record_status="removed",
        ),
        # needs_review is included like any active record.
        _record(
            "physionet:needsreview001",
            modalities=["ECG"],
            domains=["cardiology"],
            record_status="needs_review",
        ),
    ]

    fillers = [
        _record(
            f"{_FILLER_SOURCES[i % len(_FILLER_SOURCES)]}:filler{i:03d}",
            modalities=[_FILLER_MODALITIES[i % len(_FILLER_MODALITIES)]],
            domains=[_FILLER_DOMAINS[i % len(_FILLER_DOMAINS)]],
        )
        for i in range(23)
    ]
    return interesting + fillers


def _expected_institution_key(ror_id: str | None, name: str) -> str:
    """Independent re-derivation of the institution-id rule (not a call
    into `atlas.graph.build`), so this test doesn't just check the code
    against itself."""
    if ror_id:
        return ror_id.rstrip("/").rsplit("/", 1)[-1]
    return io.slugify(name)


def test_full_fixture_contract():
    records = _full_fixture_records()
    assert len(records) == 40  # ~40 per the brief

    included = [r for r in records if r.record_status != "removed"]
    graph, index, stats = build.build_graph(records)

    node_ids = [n["id"] for n in graph["nodes"]]
    node_id_set = set(node_ids)

    # ids unique
    assert len(node_ids) == len(node_id_set)

    # every link endpoint exists as a node id
    for link in graph["links"]:
        assert link["source"] in node_id_set
        assert link["target"] in node_id_set

    # every non-removed dataset id is in the search index, and only those
    included_ids = {r.id for r in included}
    assert {row["id"] for row in index} == included_ids
    assert "openneuro:removed001" not in node_id_set

    # backbone key is never explicitly False anywhere
    assert all(n.get("backbone") is not False for n in graph["nodes"])

    # --- independently re-derived expected backbone set ---
    expected_source_ids = {f"source:{r.source}" for r in included}
    expected_modality_ids = {f"modality:{m}" for r in included for m in r.modalities}

    condition_counts: Counter = Counter()
    for r in included:
        condition_counts.update({io.slugify(c.label) for c in r.conditions})
    expected_backbone_condition_ids = {
        f"condition:{slug}" for slug, n in condition_counts.items() if n >= 2
    }
    expected_non_backbone_condition_ids = {
        f"condition:{slug}" for slug, n in condition_counts.items() if n < 2
    }

    institution_counts: Counter = Counter()
    for r in included:
        keys = {
            _expected_institution_key(inst.ror_id, inst.name)
            for inst in r.institutions[:3]
        }
        institution_counts.update(keys)
    expected_backbone_institution_ids = {
        f"institution:{key}" for key, n in institution_counts.items() if n >= 2
    }
    expected_non_backbone_institution_ids = {
        f"institution:{key}" for key, n in institution_counts.items() if n < 2
    }

    actual_backbone_ids = {n["id"] for n in graph["nodes"] if n.get("backbone")}

    assert expected_source_ids <= actual_backbone_ids
    assert expected_modality_ids <= actual_backbone_ids
    assert expected_backbone_condition_ids <= actual_backbone_ids
    assert expected_non_backbone_condition_ids.isdisjoint(actual_backbone_ids)
    assert expected_backbone_institution_ids <= actual_backbone_ids
    assert expected_non_backbone_institution_ids.isdisjoint(actual_backbone_ids)

    # no dataset is ever backbone
    dataset_ids = {n["id"] for n in graph["nodes"] if n["type"] == "dataset"}
    assert dataset_ids.isdisjoint(actual_backbone_ids)

    # the union of the independently-derived pieces accounts for the
    # entire backbone -- nothing unexpected snuck in.
    assert actual_backbone_ids == (
        expected_source_ids
        | expected_modality_ids
        | expected_backbone_condition_ids
        | expected_backbone_institution_ids
    )
    assert stats["backbone_count"] == len(actual_backbone_ids)

    # spot checks with human-readable names, for a reader of this test
    nodes = _node_map(graph)
    assert nodes["condition:epilepsy"]["degree"] == 5
    assert nodes["condition:epilepsy"]["backbone"] is True
    assert nodes["condition:epilepsy"]["label"] == "Epilepsy"
    assert nodes["condition:stroke"]["degree"] == 2
    assert nodes["condition:stroke"]["backbone"] is True
    assert nodes["condition:rare-syndrome"]["degree"] == 1
    assert "backbone" not in nodes["condition:rare-syndrome"]
    assert nodes["institution:02mhbdp94"]["degree"] == 3
    assert nodes["institution:02mhbdp94"]["backbone"] is True
    assert nodes["institution:03abcxy99"]["degree"] == 2
    assert nodes["institution:03abcxy99"]["backbone"] is True
    assert nodes["institution:tiny-lab"]["degree"] == 1
    assert "backbone" not in nodes["institution:tiny-lab"]

    # institution cap: only the first 3 of sd010's 5 institutions exist
    for letter in "abc":
        assert f"institution:inst-{letter}" in node_id_set
    for letter in "de":
        assert f"institution:inst-{letter}" not in node_id_set

    # related: one-sided, reciprocal-collapsed, dangling-skipped, removed-skipped
    related_links = {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == "related"
    }
    assert related_links == {
        ("data_in_brief:dib011", "openneuro:ds001"),
        ("openneuro:ds012", "openneuro:ds013"),
    }

    # nodes sorted by id, links sorted by (source, target, type)
    assert node_ids == sorted(node_ids)
    triples = [
        (link["source"], link["target"], link["type"]) for link in graph["links"]
    ]
    assert triples == sorted(triples)

    # node/link counts agree with the graph actually built
    assert stats["node_count"] == len(graph["nodes"])
    assert stats["link_count"] == len(graph["links"])
    assert stats["record_count"] == len(included)

    # no ISO dates anywhere in any of the three outputs
    combined = json.dumps(graph) + json.dumps(index) + json.dumps(stats)
    assert not _DATE_RE.search(combined)
