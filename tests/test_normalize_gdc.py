"""Tests for `atlas.normalize.gdc` (NCI GDC projects normalizer).

The four "released and open" fixtures (`TCGA-LUAD`, `TCGA-GBM`,
`TARGET-AML`, `MATCH-S1`) exercise the mapping against real, if trimmed,
GDC data; `CGCI-BLGSP` (live `state="submitted"`) exercises the
not-released exclusion with a real record instead of a synthetic one.
Small hand-built envelopes cover mapping branches the five real fixtures
don't happen to hit (e.g. Proteome Profiling, a CCDI project, a project
with no dbGaP accession anywhere).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from atlas.normalize import gdc
from atlas.normalize.common import Excluded
from atlas.schema import Record, validate_records

FIXTURES = Path(__file__).parent / "fixtures" / "gdc"

HARVESTED_AT = "2026-08-22"
FIRST_SEEN = "2026-08-01"


def _load(native_id: str) -> dict:
    return json.loads((FIXTURES / "records" / f"{native_id}.json").read_text())


def _envelope(payload: dict) -> dict:
    return {
        "source": "gdc",
        "native_id": payload["project_id"],
        "harvest_method": "api:gdc-projects",
        "endpoints": ["https://api.gdc.cancer.gov/projects?size=100&from=0"],
        "payload": payload,
    }


def _minimal_payload(**overrides) -> dict:
    base = {
        "project_id": "TEST-1",
        "name": "Test Sarcoma",
        "primary_site": ["Bone"],
        "disease_type": ["Test Sarcoma"],
        "released": True,
        "state": "open",
        "program": {"name": "TEST", "dbgap_accession_number": None},
        "summary": {
            "case_count": 10,
            "data_categories": [],
            "experimental_strategies": [],
        },
    }
    base.update(overrides)
    return base


def normalize(native_id: str) -> Record | Excluded:
    return gdc.normalize(
        _load(native_id), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )


# ---------------------------------------------------------------------------
# SOURCE
# ---------------------------------------------------------------------------


def test_source_constant():
    assert gdc.SOURCE == "gdc"


# ---------------------------------------------------------------------------
# Every non-excluded fixture normalizes to a valid Record
# ---------------------------------------------------------------------------


def test_every_released_open_fixture_normalizes_and_validates():
    for native_id in ["TCGA-LUAD", "TCGA-GBM", "TARGET-AML", "MATCH-S1"]:
        result = normalize(native_id)
        assert isinstance(result, Record), native_id
        errors, warnings = validate_records([result.model_dump(mode="json")])
        assert errors == [], (native_id, errors)
        assert warnings == [], (native_id, warnings)


# ---------------------------------------------------------------------------
# not_released exclusion
# ---------------------------------------------------------------------------


def test_state_not_open_is_excluded_as_not_released():
    # Real live data: CGCI-BLGSP is released=True but state="submitted".
    result = normalize("CGCI-BLGSP")
    assert isinstance(result, Excluded)
    assert result.native_id == "CGCI-BLGSP"
    assert result.reason == "not_released"


def test_released_false_is_excluded_as_not_released():
    payload = copy.deepcopy(_load("TCGA-LUAD")["payload"])
    payload["released"] = False
    result = gdc.normalize(
        _envelope(payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    assert isinstance(result, Excluded)
    assert result.reason == "not_released"


def test_released_true_but_state_closed_is_excluded():
    payload = _minimal_payload(released=True, state="legacy")
    result = gdc.normalize(
        _envelope(payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    assert isinstance(result, Excluded)
    assert result.reason == "not_released"


# ---------------------------------------------------------------------------
# id / url / name
# ---------------------------------------------------------------------------


def test_id_url_and_name():
    result = normalize("TCGA-LUAD")
    assert result.id == "gdc:TCGA-LUAD"
    assert result.source == "gdc"
    assert result.source_native_id == "TCGA-LUAD"
    assert result.url == "https://portal.gdc.cancer.gov/projects/TCGA-LUAD"
    assert result.name == "Lung Adenocarcinoma (TCGA-LUAD)"


# ---------------------------------------------------------------------------
# summary template + word limit
# ---------------------------------------------------------------------------


def test_summary_template_content():
    result = normalize("TCGA-LUAD")
    assert result.summary == (
        "Lung Adenocarcinoma: TCGA project in the NCI Genomic Data Commons "
        "with 585 cases; primary sites: Bronchus and lung; data: "
        "Biospecimen, Clinical, Sequencing Reads."
    )
    assert len(result.summary.split()) <= 40


def test_summary_top_three_data_categories_by_case_count_with_stable_tie_break():
    # TCGA-GBM's first five (post-trim) data_categories by case_count:
    # Simple Nucleotide Variation 601, Sequencing Reads 428,
    # Biospecimen 617, Clinical 617, Copy Number Variation 600.
    # Top 3 by case_count, ties broken by original order: Biospecimen (617,
    # earlier in the list) then Clinical (617) then Simple Nucleotide
    # Variation (601).
    result = normalize("TCGA-GBM")
    assert "data: Biospecimen, Clinical, Simple Nucleotide Variation." in result.summary


def test_summary_is_trimmed_to_forty_words_for_a_long_primary_site_list():
    payload = _minimal_payload(
        name="Widespread Neoplasm",
        primary_site=[f"Site {i}" for i in range(30)],
        summary={
            "case_count": 5,
            "data_categories": [
                {"data_category": "Clinical", "case_count": 5, "file_count": 5}
            ],
            "experimental_strategies": [],
        },
    )
    result = gdc.normalize(
        _envelope(payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    assert isinstance(result, Record)
    assert len(result.summary.split()) == 40


# ---------------------------------------------------------------------------
# sample_size / sample_unit
# ---------------------------------------------------------------------------


def test_sample_size_and_unit():
    result = normalize("TARGET-AML")
    assert result.sample_size == 2492
    assert result.sample_unit == "cases"


# ---------------------------------------------------------------------------
# modalities
# ---------------------------------------------------------------------------


def test_modalities_from_data_categories_and_pathology_strategies():
    # CGCI-BLGSP is excluded (not released/open) but its data_categories
    # (Simple Nucleotide Variation, Sequencing Reads, Biospecimen,
    # Clinical, Copy Number Variation) + a "Tissue Slide" experimental
    # strategy are still a good real fixture for exercising both
    # data_category- and experimental_strategy-derived modalities: build
    # it via the payload directly rather than through normalize() (which
    # would just exclude it).
    payload = _load("CGCI-BLGSP")["payload"]
    modalities = gdc._modalities(payload["summary"])
    # vocab.MODALITIES order: "pathology" sorts before "genomics", which
    # sorts before "clinical_tabular".
    assert modalities == ["pathology", "genomics", "clinical_tabular"]


def test_modalities_are_deduplicated_and_vocab_ordered():
    result = normalize("TCGA-LUAD")
    # clinical_tabular sorts after genomics in vocab.MODALITIES.
    assert result.modalities == ["genomics", "clinical_tabular"]
    assert len(result.modalities) == len(set(result.modalities))


def test_transcriptome_profiling_maps_to_transcriptomics():
    result = normalize("MATCH-S1")
    assert "transcriptomics" in result.modalities


def test_proteome_profiling_maps_to_proteomics():
    payload = _minimal_payload(
        summary={
            "case_count": 3,
            "data_categories": [
                {
                    "data_category": "Proteome Profiling",
                    "case_count": 3,
                    "file_count": 3,
                }
            ],
            "experimental_strategies": [],
        }
    )
    result = gdc.normalize(
        _envelope(payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    assert result.modalities == ["proteomics"]


def test_experimental_strategy_without_a_modality_mapping_contributes_none():
    payload = _minimal_payload(
        summary={
            "case_count": 3,
            "data_categories": [],
            "experimental_strategies": [
                {"experimental_strategy": "WGS", "case_count": 3, "file_count": 3}
            ],
        }
    )
    result = gdc.normalize(
        _envelope(payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    assert result.modalities == []
    assert "WGS" in result.keywords


# ---------------------------------------------------------------------------
# conditions
# ---------------------------------------------------------------------------


def test_disease_phrase_name_becomes_sole_condition_with_alias():
    result = normalize("TCGA-GBM")
    # "Glioblastoma Multiforme".lower() is aliased to "glioblastoma".
    assert [c.label for c in result.conditions] == ["glioblastoma"]


def test_disease_phrase_name_without_an_alias_passes_through_lowercased():
    result = normalize("TARGET-AML")
    assert [c.label for c in result.conditions] == ["acute myeloid leukemia"]


def test_study_title_name_uses_disease_type_entries_instead():
    # "Genomic Characterization CS-MATCH-0007 Arm S1" matches the
    # MATCH|Arm|Characterization|Phase|Study study-title pattern.
    result = normalize("MATCH-S1")
    labels = [c.label for c in result.conditions]
    disease_type = _load("MATCH-S1")["payload"]["disease_type"]
    assert labels == [d.lower() for d in disease_type]
    assert len(labels) == len(disease_type)


def test_study_title_pattern_matches_phase_and_study_keywords():
    payload = _minimal_payload(name="A Phase II Trial of Something")
    assert gdc._STUDY_NAME_RE.search(payload["name"])
    payload2 = _minimal_payload(name="Cohort Study of Something")
    assert gdc._STUDY_NAME_RE.search(payload2["name"])


# ---------------------------------------------------------------------------
# keywords: primary_site + program name + experimental strategies
# ---------------------------------------------------------------------------


def test_keywords_include_primary_sites_program_name_and_all_strategies():
    result = normalize("TCGA-LUAD")
    payload = _load("TCGA-LUAD")["payload"]
    for site in payload["primary_site"]:
        assert site in result.keywords
    assert "TCGA" in result.keywords
    for entry in payload["summary"]["experimental_strategies"]:
        assert entry["experimental_strategy"] in result.keywords


def test_pathology_strategy_is_a_keyword_in_addition_to_a_modality():
    # CGCI-BLGSP itself is excluded (not open); its payload still has a
    # real "Tissue Slide" strategy, so exercise the keyword helper
    # directly the way `normalize` uses it, rather than through the
    # full (excluding) `normalize()` call.
    payload = _load("CGCI-BLGSP")["payload"]
    keywords = gdc._keywords(
        payload["primary_site"], payload["program"]["name"], payload["summary"]
    )
    assert "Tissue Slide" in keywords
    assert "pathology" in gdc._modalities(payload["summary"])


# ---------------------------------------------------------------------------
# domains
# ---------------------------------------------------------------------------


def test_domains_default_to_oncology_only():
    result = normalize("MATCH-S1")
    assert result.domains == ["oncology"]


def test_domains_add_pediatrics_for_target_program():
    result = normalize("TARGET-AML")
    assert result.domains == ["oncology", "pediatrics"]


def test_domains_add_pediatrics_for_ccdi_program():
    payload = _minimal_payload(program={"name": "CCDI", "dbgap_accession_number": None})
    result = gdc.normalize(
        _envelope(payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    assert result.domains == ["oncology", "pediatrics"]


# ---------------------------------------------------------------------------
# access / access_tiers / access_notes
# ---------------------------------------------------------------------------


def test_access_and_access_tiers_are_fixed():
    result = normalize("TCGA-LUAD")
    assert result.access == "open"
    assert result.access_tiers == ["open", "application"]


def test_access_notes_uses_program_level_phs_when_project_level_is_null():
    # TCGA-LUAD: dbgap_accession_number is null at project level,
    # "phs000178" at program level.
    result = normalize("TCGA-LUAD")
    assert result.access_notes == (
        "Open tier: clinical, biospecimen, masked somatic mutations, "
        "expression. Controlled tier via dbGaP phs000178."
    )


def test_access_notes_prefers_project_level_phs_over_program_level():
    # MATCH-S1: dbgap_accession_number is "phs002153" at project level,
    # program-level is null.
    result = normalize("MATCH-S1")
    assert result.access_notes.endswith("dbGaP phs002153.")


def test_access_notes_has_no_phs_when_neither_level_reports_one():
    payload = _minimal_payload()
    payload.pop("dbgap_accession_number", None)  # absent outright, not just null
    payload["program"] = {"name": "TEST", "dbgap_accession_number": None}
    result = gdc.normalize(
        _envelope(payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    assert result.access_notes == (
        "Open tier: clinical, biospecimen, masked somatic mutations, "
        "expression. Controlled tier via dbGaP."
    )


# ---------------------------------------------------------------------------
# license / species / institutions / authors / years / countries
# ---------------------------------------------------------------------------


def test_fields_with_no_source_signal_are_left_null_or_empty():
    result = normalize("TCGA-LUAD")
    assert result.license is None
    assert result.species == "human"
    assert result.institutions == []
    assert result.authors == []
    assert result.papers == []
    assert result.years.start is None
    assert result.years.end is None
    # The API never states a country; must not be assumed to be "US".
    assert result.countries == []
    assert result.record_status == "active"


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_provenance_fields():
    result = normalize("TCGA-LUAD")
    assert result.provenance.harvested_via == "api:gdc-projects"
    assert str(result.provenance.harvested_at) == FIRST_SEEN
    assert str(result.provenance.last_verified) == HARVESTED_AT
    assert result.provenance.raw_hash is not None
    assert result.provenance.raw_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# enrichment_text
# ---------------------------------------------------------------------------


def test_enrichment_text_contains_expected_components():
    envelope = _load("TARGET-AML")
    text = gdc.enrichment_text(envelope)
    assert "Acute Myeloid Leukemia" in text
    assert "Hematopoietic and reticuloendothelial systems" in text
    assert "Myeloid Leukemias" in text
    assert "TARGET" in text
    assert "Sequencing Reads" in text
    assert "RNA-Seq" in text


def test_enrichment_text_is_capped_at_fifteen_hundred_chars():
    payload = _minimal_payload(
        primary_site=[
            f"Site number {i} with a fairly long descriptive name" for i in range(60)
        ],
    )
    text = gdc.enrichment_text(_envelope(payload))
    assert len(text) <= 1500


def test_enrichment_text_for_every_fixture_is_non_empty_and_bounded():
    for native_id in ["TCGA-LUAD", "TCGA-GBM", "TARGET-AML", "MATCH-S1", "CGCI-BLGSP"]:
        text = gdc.enrichment_text(_load(native_id))
        assert 0 < len(text) <= 1500
