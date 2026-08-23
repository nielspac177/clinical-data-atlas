"""Tests for Task 2.4: `atlas.enrich.dedupe` (keys, clustering, title-only
flagging, same-cohort links) and `atlas.enrich.merge` (cluster merge
policy, enrichment application).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from atlas.enrich import dedupe, merge
from atlas.normalize.common import Excluded
from atlas.schema import Author, Condition, Institution, Paper, Record, Related


def _record(
    *,
    id: str,
    source: str,
    source_native_id: str | None = None,
    name: str = "Example dataset",
    url: str | None = None,
    **overrides: object,
) -> Record:
    """A minimal, valid `Record` for dedupe/merge tests; every field can
    be overridden via keyword (including nested ones like `provenance`)."""
    native_id = (
        source_native_id if source_native_id is not None else id.split(":", 1)[1]
    )
    data: dict = {
        "id": id,
        "source": source,
        "source_native_id": native_id,
        "name": name,
        "summary": "A short summary used across the dedupe test suite.",
        "url": url or f"https://example.org/{native_id}",
        "dataset_doi": None,
        "species": "human",
        "sample_size": None,
        "sample_unit": None,
        "years": {},
        "access": "open",
        "record_status": "active",
        "provenance": {
            "harvested_via": "api",
            "harvested_at": "2026-08-01",
            "last_verified": "2026-08-22",
            "enrichment": {"method": "rules", "fields": {}},
        },
    }
    data.update(overrides)
    return Record.model_validate(data)


@dataclass
class _RuleHitsStub:
    """Tiny stand-in for `atlas.enrich.rules.RuleHits` -- duck-typed, per
    the task brief, so this test module never imports the real thing."""

    domains: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    species: str | None = None
    evidence: dict = field(default_factory=dict)


# ===========================================================================
# dedupe.keys() -- individual key extraction
# ===========================================================================


def test_doi_key_lowercases_and_collapses_version():
    assert (
        dedupe.doi_key("10.18112/OpenNeuro.ds000001.v1.0.0")
        == "10.18112/openneuro.ds000001"
    )


def test_doi_key_strips_doi_url_prefix():
    assert dedupe.doi_key("https://doi.org/10.1000/xyz123") == "10.1000/xyz123"


def test_doi_key_none_for_missing_or_invalid():
    assert dedupe.doi_key(None) is None
    assert dedupe.doi_key("not a doi") is None


def test_normalize_url_drops_scheme_www_query_fragment_trailing_slash():
    url = "HTTPS://WWW.OpenNeuro.org/datasets/DS000001/?utm=1#section"
    assert dedupe.normalize_url(url) == "openneuro.org/datasets/DS000001"


def test_normalize_url_lowercases_host_only_preserves_path_case():
    assert dedupe.normalize_url("https://Example.ORG/Path/Mixed-Case") == (
        "example.org/Path/Mixed-Case"
    )


def test_normalize_url_bare_host_no_path():
    assert dedupe.normalize_url("https://openneuro.org/") == "openneuro.org"


def test_normalize_url_none_for_missing():
    assert dedupe.normalize_url(None) is None
    assert dedupe.normalize_url("") is None


def test_accession_key_openneuro_from_url_and_from_native_id():
    from_url = dedupe.accession_key(
        "https://openneuro.org/datasets/ds000117/versions/1.0.0"
    )
    from_id = dedupe.accession_key(None, "ds000117", None)
    assert from_url == "openneuro:ds000117"
    assert from_id == "openneuro:ds000117"


def test_accession_key_physionet_slug():
    url = "https://physionet.org/content/mimiciv/2.2/"
    assert dedupe.accession_key(url) == "physionet:mimiciv"


def test_accession_key_gdc_project_id():
    assert (
        dedupe.accession_key(None, "TCGA-BRCA", "TCGA-BRCA breast cancer")
        == "gdc:TCGA-BRCA"
    )
    assert dedupe.accession_key(None, "BEATAML1.0", None) == "gdc:BEATAML1.0"


def test_accession_key_tcia_collection_slug():
    url = "https://www.cancerimagingarchive.net/collection/tcga-gbm/"
    assert dedupe.accession_key(url) == "tcia:tcga-gbm"


def test_accession_key_none_when_nothing_matches():
    assert (
        dedupe.accession_key("https://example.org/whatever", "abc123", "A Study")
        is None
    )


def test_title_fingerprint_lowercase_stopwords_sorted_dedup():
    fp = dedupe.title_fingerprint("The Stroke Imaging Dataset for Stroke Patients")
    # stopwords dropped ("the", "for"); "stroke" deduped; sorted alphabetically.
    assert fp == "imaging patients stroke"


def test_title_fingerprint_order_independent():
    a = dedupe.title_fingerprint("Alpha Beta Gamma")
    b = dedupe.title_fingerprint("Gamma, Beta! Alpha.")
    assert a == b == "alpha beta gamma"


def test_title_fingerprint_none_for_missing_or_only_stopwords():
    assert dedupe.title_fingerprint(None) is None
    assert dedupe.title_fingerprint("The Data For") is None


def test_keys_returns_all_four():
    record = _record(
        id="openneuro:ds000001",
        source="openneuro",
        name="Example Dataset",
        url="https://openneuro.org/datasets/ds000001",
        dataset_doi="10.18112/openneuro.ds000001.v1.0.0",
    )
    result = dedupe.keys(record)
    assert result == {
        "doi": "10.18112/openneuro.ds000001",
        "url": "openneuro.org/datasets/ds000001",
        "accession": "openneuro:ds000001",
        "title": "example",  # "dataset" is a stopword, dropped
    }


# ===========================================================================
# dedupe.cluster()
# ===========================================================================


def test_cluster_doi_match_merges():
    a = _record(
        id="curated:some-copy",
        source="curated",
        url="https://example.org/mirror/x",
        dataset_doi="10.1000/xyz",
    )
    b = _record(
        id="openneuro:ds000009",
        source="openneuro",
        url="https://openneuro.org/datasets/ds000009",
        dataset_doi="10.1000/xyz.v2.0.0",  # same DOI, version-suffixed
    )
    clusters = dedupe.cluster([a, b])
    assert len(clusters) == 1
    assert {r.id for r in clusters[0]} == {a.id, b.id}


def test_cluster_url_match_merges():
    a = _record(
        id="curated:x",
        source="curated",
        url="http://www.Example.org/data/x/",
    )
    b = _record(
        id="scientific_data:y",
        source="scientific_data",
        url="https://example.org/data/x",
    )
    clusters = dedupe.cluster([a, b])
    assert len(clusters) == 1
    assert {r.id for r in clusters[0]} == {a.id, b.id}


def test_cluster_accession_match_merges_openneuro_url_vs_id():
    a = _record(
        id="curated:mirror-ds117",
        source="curated",
        source_native_id="mirror-ds117",
        url="https://example.org/mirrors/ds000117-copy",  # mentions accession only via name below
        name="A mirror of ds000117",
    )
    b = _record(
        id="openneuro:ds000117",
        source="openneuro",
        source_native_id="ds000117",
        url="https://openneuro.org/datasets/ds000117",
    )
    clusters = dedupe.cluster([a, b])
    assert len(clusters) == 1
    assert {r.id for r in clusters[0]} == {a.id, b.id}


def test_cluster_title_only_does_not_merge_but_is_flagged():
    a = _record(
        id="curated:a",
        source="curated",
        url="https://example.org/a",
        name="Stroke Imaging Cohort",
    )
    b = _record(
        id="scientific_data:b",
        source="scientific_data",
        url="https://example.org/b",
        name="Stroke Imaging Cohort",
    )
    clusters = dedupe.cluster([a, b])
    assert sorted(len(c) for c in clusters) == [1, 1]

    flagged = dedupe.flag_title_collisions([a, b])
    assert flagged == {a.id, b.id}


def test_flag_title_collisions_same_source_not_flagged():
    a = _record(
        id="curated:a", source="curated", url="https://example.org/a", name="Same Title"
    )
    b = _record(
        id="curated:b", source="curated", url="https://example.org/b", name="Same Title"
    )
    assert dedupe.flag_title_collisions([a, b]) == set()


def test_cluster_singletons_included_and_sorted_deterministically():
    a = _record(id="tcia:zzz", source="tcia", url="https://example.org/z")
    b = _record(id="gdc:aaa", source="gdc", url="https://example.org/a")
    clusters_1 = dedupe.cluster([a, b])
    clusters_2 = dedupe.cluster([b, a])
    assert clusters_1 == clusters_2
    assert [c[0].id for c in clusters_1] == ["gdc:aaa", "tcia:zzz"]


# ===========================================================================
# dedupe.link_same_cohort()
# ===========================================================================


def test_link_same_cohort_tcga_gbm_both_ways():
    gdc = _record(
        id="gdc:TCGA-GBM",
        source="gdc",
        source_native_id="TCGA-GBM",
        url="https://portal.gdc.cancer.gov/projects/TCGA-GBM",
    )
    tcia = _record(
        id="tcia:tcga-gbm",
        source="tcia",
        source_native_id="TCGA-GBM",
        url="https://www.cancerimagingarchive.net/collection/tcga-gbm/",
    )
    updated = dedupe.link_same_cohort([gdc, tcia])
    updated_by_id = {r.id: r for r in updated}

    assert updated_by_id["gdc:TCGA-GBM"].related == [
        Related(id="tcia:tcga-gbm", relation="same_cohort")
    ]
    assert updated_by_id["tcia:tcga-gbm"].related == [
        Related(id="gdc:TCGA-GBM", relation="same_cohort")
    ]
    # order preserved
    assert [r.id for r in updated] == [gdc.id, tcia.id]


def test_link_same_cohort_different_cohort_not_linked():
    gdc = _record(id="gdc:TCGA-GBM", source="gdc", source_native_id="TCGA-GBM")
    tcia = _record(id="tcia:cptac-gbm", source="tcia", source_native_id="CPTAC-GBM")
    updated = dedupe.link_same_cohort([gdc, tcia])
    assert all(r.related == [] for r in updated)


def test_link_same_cohort_dedups_existing_related():
    gdc = _record(
        id="gdc:TCGA-LUAD",
        source="gdc",
        source_native_id="TCGA-LUAD",
        related=[{"id": "tcia:tcga-luad", "relation": "same_cohort"}],
    )
    tcia = _record(id="tcia:tcga-luad", source="tcia", source_native_id="TCGA-LUAD")
    updated = dedupe.link_same_cohort([gdc, tcia])
    updated_by_id = {r.id: r for r in updated}
    assert updated_by_id["gdc:TCGA-LUAD"].related == [
        Related(id="tcia:tcga-luad", relation="same_cohort")
    ]
    assert updated_by_id["tcia:tcga-luad"].related == [
        Related(id="gdc:TCGA-LUAD", relation="same_cohort")
    ]


def test_link_same_cohort_no_gdc_or_tcia_records_returns_same_list():
    a = _record(id="openneuro:ds1", source="openneuro")
    result = dedupe.link_same_cohort([a])
    assert result == [a]


# ===========================================================================
# merge.merge_cluster()
# ===========================================================================


def test_merge_cluster_singleton_returns_unchanged_no_exclusions():
    a = _record(id="openneuro:ds1", source="openneuro")
    merged, excluded = merge.merge_cluster([a])
    assert merged == a
    assert excluded == []


def test_merge_cluster_primary_priority_openneuro_over_curated():
    curated = _record(id="curated:z", source="curated")
    openneuro = _record(id="openneuro:ds1", source="openneuro")
    merged, excluded = merge.merge_cluster([curated, openneuro])
    assert merged.id == "openneuro:ds1"
    assert excluded == [
        Excluded(native_id="curated:z", reason="merged_into:openneuro:ds1")
    ]


def test_merge_cluster_tie_break_lexicographically_smaller_id():
    a = _record(id="curated:b-second", source="curated")
    b = _record(id="curated:a-first", source="curated")
    merged, excluded = merge.merge_cluster([a, b])
    assert merged.id == "curated:a-first"
    assert excluded == [
        Excluded(native_id="curated:b-second", reason="merged_into:curated:a-first")
    ]


def test_merge_cluster_access_conflict_more_restrictive_wins():
    primary = _record(id="openneuro:ds1", source="openneuro", access="open")
    secondary = _record(id="curated:z", source="curated", access="credentialed")
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.access == "credentialed"


def test_merge_cluster_access_tiers_union_in_vocab_order():
    primary = _record(id="openneuro:ds1", source="openneuro", access_tiers=["open"])
    secondary = _record(
        id="curated:z", source="curated", access_tiers=["application", "registration"]
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.access_tiers == ["open", "registration", "application"]


def test_merge_cluster_secondary_doi_becomes_describes_paper():
    primary = _record(id="openneuro:ds1", source="openneuro")
    secondary = _record(
        id="scientific_data:paper1",
        source="scientific_data",
        dataset_doi="10.1038/s41597-020-00000-0",
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.papers == [
        Paper(doi="10.1038/s41597-020-00000-0", relation="describes")
    ]


def test_merge_cluster_secondary_doi_not_duplicated_if_already_present():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        papers=[{"doi": "10.1038/s41597-020-00000-0", "relation": "describes"}],
    )
    secondary = _record(
        id="scientific_data:paper1",
        source="scientific_data",
        dataset_doi="10.1038/s41597-020-00000-0",
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.papers == [
        Paper(doi="10.1038/s41597-020-00000-0", relation="describes")
    ]


def test_merge_cluster_list_fields_union_preserve_primary_order_then_secondary():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        domains=["neurology", "psychiatry"],
        keywords=["mri"],
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        domains=["psychiatry", "oncology"],
        keywords=["mri", "t1"],
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.domains == ["neurology", "psychiatry", "oncology"]
    assert merged.keywords == ["mri", "t1"]


def test_merge_cluster_conditions_dedupe_by_label_keeps_mesh_id():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        conditions=[{"label": "Stroke", "mesh_id": "D020521"}],
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        conditions=[{"label": "stroke"}, {"label": "Epilepsy"}],
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.conditions == [
        Condition(label="Stroke", mesh_id="D020521"),
        Condition(label="Epilepsy"),
    ]


def test_merge_cluster_institutions_and_authors_dedupe_by_name():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        institutions=[{"name": "MIT"}],
        authors=[{"name": "Ada Lovelace"}],
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        institutions=[{"name": "MIT"}, {"name": "Stanford"}],
        authors=[{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}],
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.institutions == [
        Institution(name="MIT"),
        Institution(name="Stanford"),
    ]
    assert merged.authors == [Author(name="Ada Lovelace"), Author(name="Grace Hopper")]


def test_merge_cluster_scalar_fields_fill_from_secondary_only_when_primary_empty():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        license=None,
        sample_size=None,
        sample_unit=None,
        version="v1",
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        license="CC0-1.0",
        sample_size=42,
        sample_unit="participants",
        version="v2",
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.license == "CC0-1.0"
    assert merged.sample_size == 42
    assert merged.sample_unit == "participants"
    assert merged.version == "v1"  # primary's own value wins, never overwritten


def test_merge_cluster_record_status_needs_review_if_any_member_is():
    primary = _record(id="openneuro:ds1", source="openneuro", record_status="active")
    secondary = _record(id="curated:z", source="curated", record_status="needs_review")
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.record_status == "needs_review"


def test_merge_cluster_merged_from_note_in_provenance():
    primary = _record(id="openneuro:ds1", source="openneuro")
    secondary_a = _record(id="curated:b", source="curated")
    secondary_b = _record(id="curated:a", source="curated")
    merged, excluded = merge.merge_cluster([primary, secondary_a, secondary_b])
    assert merged.provenance.enrichment.fields["merged_from"] == "curated:a,curated:b"
    assert {e.native_id for e in excluded} == {"curated:a", "curated:b"}
    assert all(e.reason == "merged_into:openneuro:ds1" for e in excluded)


def test_merge_cluster_no_data_loss_secondary_fields_all_present():
    """Self-review: nothing from a secondary should vanish silently."""
    primary = _record(id="openneuro:ds1", source="openneuro", domains=["neurology"])
    secondary = _record(
        id="curated:z",
        source="curated",
        domains=["oncology"],
        modalities=["MRI"],
        keywords=["k1"],
        countries=["US"],
        institutions=[{"name": "Stanford"}],
        authors=[{"name": "Grace Hopper"}],
        related=[{"id": "openneuro:ds2", "relation": "derived_from"}],
        dataset_doi="10.1000/secondary-doi",
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert "oncology" in merged.domains
    assert "MRI" in merged.modalities
    assert "k1" in merged.keywords
    assert "US" in merged.countries
    assert any(i.name == "Stanford" for i in merged.institutions)
    assert any(a.name == "Grace Hopper" for a in merged.authors)
    assert any(r.id == "openneuro:ds2" for r in merged.related)
    assert any(p.doi == "10.1000/secondary-doi" for p in merged.papers)


def test_merge_cluster_is_deterministic_regardless_of_input_order():
    primary = _record(id="openneuro:ds1", source="openneuro", domains=["neurology"])
    secondary_a = _record(id="curated:b", source="curated", domains=["oncology"])
    secondary_b = _record(id="curated:a", source="curated", domains=["cardiology"])
    merged_1, excluded_1 = merge.merge_cluster([primary, secondary_a, secondary_b])
    merged_2, excluded_2 = merge.merge_cluster([secondary_b, secondary_a, primary])
    assert merged_1 == merged_2
    assert excluded_1 == excluded_2


# ===========================================================================
# merge.apply_enrichment()
# ===========================================================================


def test_apply_enrichment_union_and_vocab_order_domains_modalities():
    record = _record(
        id="openneuro:ds1", source="openneuro", domains=["oncology"], modalities=["CT"]
    )
    rule_hits = _RuleHitsStub(domains=["neurology"], modalities=["MRI"])
    llm_out = {
        "domains": ["psychiatry"],
        "modalities": [],
        "conditions": [],
        "countries": [],
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    # vocab.DOMAINS order: neurology, psychiatry, neuroscience, cardiology, oncology, ...
    assert result.domains == ["neurology", "psychiatry", "oncology"]
    # vocab.MODALITIES order: MRI, fMRI, dMRI, MRS, PET, SPECT, CT, ...
    assert result.modalities == ["MRI", "CT"]


def test_apply_enrichment_never_overwrites_source_facts():
    record = _record(
        id="openneuro:ds1",
        source="openneuro",
        name="Original Name",
        url="https://openneuro.org/datasets/ds1",
        access="open",
        license="CC0-1.0",
        sample_size=10,
        sample_unit="participants",
        species="human",
        dataset_doi="10.1000/original",
        version="v1",
    )
    rule_hits = _RuleHitsStub(
        species="animal"
    )  # should be ignored: species already known
    llm_out = {
        "domains": [],
        "modalities": [],
        "conditions": [],
        "countries": [],
        "summary": "A wildly different summary that should not replace facts.",
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result.name == "Original Name"
    assert result.url == "https://openneuro.org/datasets/ds1"
    assert result.access == "open"
    assert result.license == "CC0-1.0"
    assert result.sample_size == 10
    assert result.sample_unit == "participants"
    assert result.species == "human"  # not "animal" -- already known, never overwritten
    assert result.dataset_doi == "10.1000/original"
    assert result.version == "v1"


def test_apply_enrichment_species_fill_when_unknown():
    record = _record(id="openneuro:ds1", source="openneuro", species="unknown")
    rule_hits = _RuleHitsStub(species="animal")
    result = merge.apply_enrichment(record, None, rule_hits)
    assert result.species == "animal"
    assert result.provenance.enrichment.fields["species"] == "rules"


def test_apply_enrichment_summary_from_llm_else_kept():
    record = _record(
        id="openneuro:ds1", source="openneuro", summary="Old summary text here."
    )
    rule_hits = _RuleHitsStub()
    llm_out = {
        "domains": [],
        "modalities": [],
        "conditions": [],
        "countries": [],
        "summary": "New summary from the LLM classify step.",
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result.summary == "New summary from the LLM classify step."
    assert result.provenance.enrichment.fields["summary"] == "llm"

    llm_out_no_summary = {
        "domains": [],
        "modalities": [],
        "conditions": [],
        "countries": [],
        "summary": None,
    }
    result2 = merge.apply_enrichment(record, llm_out_no_summary, rule_hits)
    assert result2.summary == "Old summary text here."


def test_apply_enrichment_population_fill_only_when_empty():
    record = _record(id="openneuro:ds1", source="openneuro", population=None)
    rule_hits = _RuleHitsStub()
    llm_out = {
        "domains": [],
        "modalities": [],
        "conditions": [],
        "countries": [],
        "population": "Adults with stroke.",
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result.population == "Adults with stroke."

    record_with_pop = _record(
        id="openneuro:ds2", source="openneuro", population="Children with epilepsy."
    )
    result2 = merge.apply_enrichment(record_with_pop, llm_out, rule_hits)
    assert (
        result2.population == "Children with epilepsy."
    )  # existing kept, not overwritten


def test_apply_enrichment_countries_union():
    record = _record(id="openneuro:ds1", source="openneuro", countries=["US"])
    rule_hits = _RuleHitsStub()
    llm_out = {
        "domains": [],
        "modalities": [],
        "conditions": [],
        "countries": ["PE", "US"],
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result.countries == ["PE", "US"]
    assert result.provenance.enrichment.fields["countries"] == "llm"


def test_apply_enrichment_conditions_dedupe_keep_mesh_id_and_add_new():
    record = _record(
        id="openneuro:ds1",
        source="openneuro",
        conditions=[{"label": "Stroke", "mesh_id": "D020521"}],
    )
    rule_hits = _RuleHitsStub(conditions=["stroke", "Epilepsy"])
    llm_out = {
        "domains": [],
        "modalities": [],
        "conditions": [{"label": "epilepsy"}, "Migraine"],
        "countries": [],
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result.conditions == [
        Condition(label="Stroke", mesh_id="D020521"),
        Condition(label="Epilepsy"),
        Condition(label="Migraine"),
    ]
    # both rules and llm contributed new labels here (rules: Epilepsy; llm: Migraine)
    # -> llm wins the attribution for the conditions field.
    assert result.provenance.enrichment.fields["conditions"] == "llm"


def test_apply_enrichment_method_rules_only():
    record = _record(id="openneuro:ds1", source="openneuro", domains=[])
    rule_hits = _RuleHitsStub(domains=["neurology"])
    result = merge.apply_enrichment(record, None, rule_hits)
    assert result.provenance.enrichment.method == "rules"
    assert result.provenance.enrichment.fields["domains"] == "rules"
    assert result.provenance.enrichment.at == date.today()  # noqa: DTZ011


def test_apply_enrichment_method_rules_plus_llm():
    record = _record(id="openneuro:ds1", source="openneuro", domains=[])
    rule_hits = _RuleHitsStub(domains=["neurology"])
    llm_out = {
        "domains": ["psychiatry"],
        "modalities": [],
        "conditions": [],
        "countries": [],
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result.provenance.enrichment.method == "rules+llm"


def test_apply_enrichment_no_change_leaves_record_untouched():
    record = _record(id="openneuro:ds1", source="openneuro")
    rule_hits = _RuleHitsStub()
    result = merge.apply_enrichment(record, None, rule_hits)
    assert result == record
    assert result.provenance.enrichment.at is None


def test_apply_enrichment_curated_method_is_never_touched():
    record = _record(
        id="curated:z",
        source="curated",
        domains=[],
        provenance={
            "harvested_via": "curated",
            "harvested_at": "2026-08-01",
            "last_verified": "2026-08-22",
            "enrichment": {"method": "curated", "fields": {}},
        },
    )
    rule_hits = _RuleHitsStub(domains=["neurology"])
    llm_out = {
        "domains": ["psychiatry"],
        "modalities": [],
        "conditions": [],
        "countries": [],
    }
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result == record
    assert result.domains == []
    assert result.provenance.enrichment.method == "curated"
