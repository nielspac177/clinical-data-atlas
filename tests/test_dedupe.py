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
# dedupe.doi_key() / normalize_url() / url_key()
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


def test_url_key_passes_through_a_specific_two_segment_page():
    url = "https://physionet.org/content/mimiciv/2.2/"
    assert dedupe.url_key(url) == "physionet.org/content/mimiciv/2.2"


def test_url_key_none_for_bare_host_or_single_segment():
    assert dedupe.url_key("https://physionet.org/") is None
    assert dedupe.url_key("https://physionet.org/content/") is None  # 1 segment


def test_url_key_blocks_listing_roots_even_with_two_path_segments():
    # microdata.worldbank.org's catalog root itself has 2 segments, so the
    # segment-count rule alone wouldn't block it -- the explicit
    # LISTING_ROOTS entry is what does.
    assert dedupe.url_key("https://microdata.worldbank.org/index.php/catalog") is None
    assert (
        dedupe.url_key("https://microdata.worldbank.org/index.php/catalog/12345")
        == "microdata.worldbank.org/index.php/catalog/12345"
    )


def test_url_key_none_for_missing_url():
    assert dedupe.url_key(None) is None


# ===========================================================================
# dedupe.accession_key() -- dispatched by source + url shape, never by name
# ===========================================================================


def test_accession_key_openneuro_from_own_source_native_id():
    assert dedupe.accession_key("openneuro", "ds000117", None) == "openneuro:ds000117"


def test_accession_key_openneuro_from_url_for_a_different_source():
    url = "https://openneuro.org/datasets/ds000117/versions/1.0.0"
    assert dedupe.accession_key("curated", "mirror-1", url) == "openneuro:ds000117"


def test_accession_key_physionet_from_own_source_native_id():
    assert dedupe.accession_key("physionet", "mimiciv", None) == "physionet:mimiciv"


def test_accession_key_physionet_from_url_for_a_different_source():
    url = "https://physionet.org/content/mimiciv/2.2/"
    assert (
        dedupe.accession_key("scientific_data", "paper-1", url) == "physionet:mimiciv"
    )


def test_accession_key_gdc_from_own_source_native_id_preserves_case():
    assert dedupe.accession_key("gdc", "TCGA-BRCA", None) == "gdc:TCGA-BRCA"


def test_accession_key_gdc_from_url_for_a_different_source():
    url = "https://portal.gdc.cancer.gov/projects/TCGA-BRCA"
    assert dedupe.accession_key("curated", "mirror-2", url) == "gdc:TCGA-BRCA"


def test_accession_key_tcia_from_own_source_native_id():
    assert dedupe.accession_key("tcia", "TCGA-GBM", None) == "tcia:tcga-gbm"


def test_accession_key_tcia_from_url_for_a_different_source():
    url = "https://www.cancerimagingarchive.net/collection/tcga-gbm/"
    assert dedupe.accession_key("curated", "mirror-3", url) == "tcia:tcga-gbm"


def test_accession_key_own_source_native_id_wins_even_if_url_looks_like_another_repo():
    # Source dispatch happens first: a gdc-sourced record's own native id
    # is authoritative regardless of what its url contains.
    url = "https://www.cancerimagingarchive.net/collection/tcga-gbm/"
    assert dedupe.accession_key("gdc", "TCGA-GBM", url) == "gdc:TCGA-GBM"


def test_accession_key_none_when_nothing_matches():
    assert (
        dedupe.accession_key("curated", "abc123", "https://example.org/whatever")
        is None
    )
    assert dedupe.accession_key("curated", "abc123", None) is None


# ===========================================================================
# dedupe.title_fingerprint()
# ===========================================================================


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
    # A curated pointer whose url is the real OpenNeuro page (with a
    # version segment the openneuro-sourced record's own url lacks) --
    # the two share an accession despite different exact urls.
    a = _record(
        id="curated:mirror-ds117",
        source="curated",
        source_native_id="mirror-ds117",
        url="https://openneuro.org/datasets/ds000117/versions/1.0.0",
        name="A curated pointer to the OpenNeuro page",
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


# ---------------------------------------------------------------------------
# Critical regressions (fix round 1, controller ruling R19): keys must
# never be derivable from free text or a shared listing page.
# ---------------------------------------------------------------------------


def test_c1_tcia_listing_fallback_url_does_not_merge_unrelated_records():
    a = _record(
        id="tcia:collection-a",
        source="tcia",
        source_native_id="collection-a",
        url="https://www.cancerimagingarchive.net/collections/",
    )
    b = _record(
        id="tcia:collection-b",
        source="tcia",
        source_native_id="collection-b",
        url="https://www.cancerimagingarchive.net/collections/",
    )
    clusters = dedupe.cluster([a, b])
    assert sorted(len(c) for c in clusters) == [1, 1]


def test_c2_gdc_and_tcia_same_cohort_are_two_clusters_then_linked():
    gdc = _record(
        id="gdc:TCGA-GBM",
        source="gdc",
        source_native_id="TCGA-GBM",
        name="Glioblastoma Multiforme (TCGA-GBM)",
        url="https://portal.gdc.cancer.gov/projects/TCGA-GBM",
    )
    tcia = _record(
        id="tcia:tcga-gbm",
        source="tcia",
        source_native_id="TCGA-GBM",
        name="Genomic Data Commons: TCGA-GBM related collection [TCGA-GBM]",
        url="https://www.cancerimagingarchive.net/collection/tcga-gbm/",
    )
    clusters = dedupe.cluster([gdc, tcia])
    assert sorted(len(c) for c in clusters) == [1, 1]

    linked = dedupe.link_same_cohort([gdc, tcia])
    linked_by_id = {r.id: r for r in linked}
    assert linked_by_id["gdc:TCGA-GBM"].related == [
        Related(id="tcia:tcga-gbm", relation="same_cohort")
    ]
    assert linked_by_id["tcia:tcga-gbm"].related == [
        Related(id="gdc:TCGA-GBM", relation="same_cohort")
    ]


def test_c3_short_gdc_program_codes_do_not_match_inside_unrelated_titles():
    a = _record(
        id="curated:a",
        source="curated",
        url="https://example.org/a",
        name="NIH FMRI study of working memory",
    )
    b = _record(
        id="curated:b",
        source="curated",
        url="https://example.org/b",
        name="FMRIB Software Library analysis pipeline TRIO scanner MATCHED cohort",
    )
    clusters = dedupe.cluster([a, b])
    assert sorted(len(c) for c in clusters) == [1, 1]


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


def test_merge_cluster_access_escalation_carries_notes_and_howto():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        access="open",
        access_notes=None,
        access_howto=None,
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        access="credentialed",
        access_notes="Requires a signed DUA.",
        access_howto="Apply via the portal.",
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.access == "credentialed"
    assert merged.access_notes == "Requires a signed DUA."
    assert merged.access_howto == "Apply via the portal."


def test_merge_cluster_access_escalation_falls_back_to_primary_notes():
    """An escalating secondary with no notes of its own must not blank the
    primary's: `access` becomes the more restrictive tier, but the only
    prose anyone wrote about access survives the merge."""
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        access="open",
        access_notes="Open download; some subjects need a DUA.",
        access_howto="Ask the maintainers.",
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        access="credentialed",
        access_notes=None,
        access_howto=None,
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.access == "credentialed"
    assert merged.access_notes == "Open download; some subjects need a DUA."
    assert merged.access_howto == "Ask the maintainers."


def test_merge_cluster_access_no_escalation_keeps_primary_notes():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        access="open",
        access_notes="Primary's own note.",
        access_howto="Primary's own howto.",
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        access="open",
        access_notes="Secondary's note.",
        access_howto="Secondary's howto.",
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.access_notes == "Primary's own note."
    assert merged.access_howto == "Primary's own howto."


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


def test_merge_cluster_papers_dedup_via_doi_key_keeps_original_string():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        papers=[{"doi": "10.1038/S41597-020-00000-0.v1.0.0", "relation": "describes"}],
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        # same doi as primary's paper once doi_key-normalized (lowercased,
        # version-collapsed) -- but reported with different case/suffix.
        dataset_doi="10.1038/s41597-020-00000-0",
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    # deduped via dedupe.doi_key, but the primary's original string survives verbatim.
    assert merged.papers == [
        Paper(doi="10.1038/S41597-020-00000-0.v1.0.0", relation="describes")
    ]


def test_merge_cluster_domains_modalities_vocab_ordered():
    primary = _record(
        id="openneuro:ds1", source="openneuro", domains=["oncology"], modalities=["CT"]
    )
    secondary = _record(
        id="curated:z", source="curated", domains=["neurology"], modalities=["MRI"]
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    # vocab.DOMAINS order (neurology, ..., oncology, ...) -- not insertion order.
    assert merged.domains == ["neurology", "oncology"]
    # vocab.MODALITIES order (MRI, ..., CT, ...) -- not insertion order.
    assert merged.modalities == ["MRI", "CT"]


def test_merge_cluster_other_list_fields_union_preserve_primary_order_then_secondary():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        keywords=["mri"],
        countries=["US"],
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        keywords=["mri", "t1"],
        countries=["PE", "US"],
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.keywords == ["mri", "t1"]
    assert merged.countries == ["US", "PE"]


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


def test_merge_cluster_related_drops_self_loops_within_cluster():
    primary = _record(id="openneuro:ds1", source="openneuro")
    secondary = _record(
        id="curated:z",
        source="curated",
        related=[
            {"id": "openneuro:ds1", "relation": "duplicate_of"},  # points at primary
            {"id": "openneuro:ds-other", "relation": "derived_from"},  # unrelated, kept
        ],
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.related == [Related(id="openneuro:ds-other", relation="derived_from")]


def test_merge_cluster_related_drops_links_to_any_cluster_member():
    primary = _record(id="openneuro:ds1", source="openneuro")
    secondary_a = _record(
        id="curated:a",
        source="curated",
        related=[
            {"id": "curated:b", "relation": "duplicate_of"}
        ],  # points at fellow secondary
    )
    secondary_b = _record(id="curated:b", source="curated")
    merged, _ = merge.merge_cluster([primary, secondary_a, secondary_b])
    assert merged.related == []


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


def test_merge_cluster_fills_species_population_size_bytes_years_from_secondary():
    primary = _record(
        id="openneuro:ds1",
        source="openneuro",
        species="unknown",
        population=None,
        size_bytes=None,
        years={"start": None, "end": None},
    )
    secondary = _record(
        id="curated:z",
        source="curated",
        species="animal",
        population="Laboratory mice.",
        size_bytes=123456,
        years={"start": 2018, "end": 2020},
    )
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.species == "animal"
    assert merged.population == "Laboratory mice."
    assert merged.size_bytes == 123456
    assert merged.years.start == 2018
    assert merged.years.end == 2020


def test_merge_cluster_species_stays_when_primary_already_known():
    primary = _record(id="openneuro:ds1", source="openneuro", species="human")
    secondary = _record(id="curated:z", source="curated", species="animal")
    merged, _ = merge.merge_cluster([primary, secondary])
    assert merged.species == "human"


def test_merge_cluster_years_fills_start_and_end_independently_from_different_secondaries():
    primary = _record(
        id="openneuro:ds1", source="openneuro", years={"start": 2015, "end": None}
    )
    secondary_a = _record(
        id="curated:a", source="curated", years={"start": 1999, "end": None}
    )
    secondary_b = _record(
        id="curated:b", source="curated", years={"start": None, "end": 2021}
    )
    merged, _ = merge.merge_cluster([primary, secondary_a, secondary_b])
    # primary already has a start -> kept; end fills from whichever secondary has one.
    assert merged.years.start == 2015
    assert merged.years.end == 2021


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
        species="animal",
        population="A secondary population note.",
        size_bytes=999,
        years={"start": 2001, "end": 2002},
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
    # primary.species defaults to "human" (known) in this test's _record(),
    # so it -- correctly -- is not overwritten by the secondary's "animal".
    assert merged.population == "A secondary population note."
    assert merged.size_bytes == 999
    assert merged.years.start == 2001
    assert merged.years.end == 2002


def test_merge_cluster_is_deterministic_regardless_of_input_order():
    primary = _record(id="openneuro:ds1", source="openneuro", domains=["neurology"])
    secondary_a = _record(id="curated:b", source="curated", domains=["oncology"])
    secondary_b = _record(id="curated:a", source="curated", domains=["cardiology"])
    merged_1, excluded_1 = merge.merge_cluster([primary, secondary_a, secondary_b])
    merged_2, excluded_2 = merge.merge_cluster([secondary_b, secondary_a, primary])
    assert merged_1 == merged_2
    assert excluded_1 == excluded_2


# ===========================================================================
# merge.build_alias_map() / merge.remap_related()
# ===========================================================================


def test_build_alias_map_from_excluded_merged_into_reasons():
    excluded = [
        Excluded(native_id="curated:a", reason="merged_into:openneuro:ds1"),
        Excluded(native_id="curated:b", reason="merged_into:openneuro:ds1"),
        Excluded(native_id="physionet:x", reason="not_a_dataset"),
    ]
    assert merge.build_alias_map(excluded) == {
        "curated:a": "openneuro:ds1",
        "curated:b": "openneuro:ds1",
    }


def test_remap_related_rewrites_drops_dangling_dedups_no_self_loops():
    survivor = _record(id="openneuro:ds1", source="openneuro")
    other = _record(
        id="curated:other",
        source="curated",
        related=[
            {"id": "curated:a", "relation": "duplicate_of"},  # aliases to survivor
            {
                "id": "curated:b",
                "relation": "duplicate_of",
            },  # aliases to survivor too -> dedup
            {
                "id": "openneuro:ds1",
                "relation": "derived_from",
            },  # already survivor -> dedup
            {"id": "unknown:ghost", "relation": "part_of"},  # dangling -> dropped
        ],
    )
    alias_map = {"curated:a": "openneuro:ds1", "curated:b": "openneuro:ds1"}
    updated = merge.remap_related([survivor, other], alias_map)
    updated_other = next(r for r in updated if r.id == "curated:other")
    assert updated_other.related == [
        Related(id="openneuro:ds1", relation="duplicate_of")
    ]


def test_remap_related_drops_self_loop_created_by_remap():
    survivor = _record(
        id="openneuro:ds1",
        source="openneuro",
        related=[{"id": "curated:a", "relation": "duplicate_of"}],  # aliases to itself
    )
    alias_map = {"curated:a": "openneuro:ds1"}
    updated = merge.remap_related([survivor], alias_map)
    updated_survivor = next(r for r in updated if r.id == "openneuro:ds1")
    assert updated_survivor.related == []


def test_remap_related_no_change_returns_same_record_object():
    record = _record(id="openneuro:ds1", source="openneuro")
    updated = merge.remap_related([record], {})
    assert updated == [record]
    assert updated[0] is record


def test_remap_related_end_to_end_with_build_alias_map():
    primary = _record(id="openneuro:ds1", source="openneuro")
    secondary = _record(id="curated:z", source="curated")
    _, excluded = merge.merge_cluster([primary, secondary])

    other = _record(
        id="openneuro:ds2",
        source="openneuro",
        related=[{"id": "curated:z", "relation": "derived_from"}],
    )
    alias_map = merge.build_alias_map(excluded)
    updated = merge.remap_related([primary, other], alias_map)
    updated_other = next(r for r in updated if r.id == "openneuro:ds2")
    assert updated_other.related == [
        Related(id="openneuro:ds1", relation="derived_from")
    ]


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


def test_apply_enrichment_llm_pass_with_zero_changes_leaves_record_identical():
    """I7: `.at`/`.method` are stamped only when `fields_changed` is
    non-empty -- a confirming LLM pass (llm_out provided but proposing
    nothing new) must not bump the method to "rules+llm" or touch `.at`.
    """
    record = _record(id="openneuro:ds1", source="openneuro")
    rule_hits = _RuleHitsStub()
    llm_out = {"domains": [], "modalities": [], "conditions": [], "countries": []}
    result = merge.apply_enrichment(record, llm_out, rule_hits)
    assert result == record
    assert result.provenance.enrichment.method == "rules"
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


def test_apply_enrichment_invalid_rule_domain_is_not_counted_as_a_change():
    """M10: attribution is computed from the post-vocab-filter delta --
    a rule hit that isn't a real `vocab.DOMAINS` member is silently
    dropped by the vocab filter and must not itself count as "rules
    changed domains" (which would otherwise stamp `.at`/`.method` for a
    change nobody can actually see)."""
    record = _record(id="openneuro:ds1", source="openneuro", domains=["oncology"])
    rule_hits = _RuleHitsStub(domains=["not-a-real-domain"])
    result = merge.apply_enrichment(record, None, rule_hits)
    assert result == record
    assert "domains" not in result.provenance.enrichment.fields


def test_apply_enrichment_accepts_none_rule_hits_as_empty():
    record = _record(id="openneuro:ds1", source="openneuro", domains=[])
    llm_out = {
        "domains": ["neurology"],
        "modalities": [],
        "conditions": [],
        "countries": [],
    }
    result = merge.apply_enrichment(record, llm_out, None)
    assert result.domains == ["neurology"]
    assert result.provenance.enrichment.fields["domains"] == "llm"


def test_apply_enrichment_none_rule_hits_and_none_llm_out_is_a_no_op():
    record = _record(id="openneuro:ds1", source="openneuro")
    result = merge.apply_enrichment(record, None, None)
    assert result == record
