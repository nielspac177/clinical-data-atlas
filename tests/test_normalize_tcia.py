"""Tests for `atlas.normalize.tcia`.

Envelope fixtures under `tests/fixtures/tcia/records/` are the exact output
of the harvester over `tests/fixtures/tcia/*.json`, so these tests pin the
whole NBIA+DataCite -> canonical `Record` mapping end to end. Synthetic
envelopes cover the branches the eight captured records don't reach.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.normalize import tcia
from atlas.schema import Record, word_count

FIXTURES = Path(__file__).parent / "fixtures" / "tcia"
RECORDS = FIXTURES / "records"

HARVESTED_AT = "2026-08-22"
FIRST_SEEN = "2026-01-15"


def envelope(native_id: str) -> dict:
    from atlas.harvest.base import _filename_for

    return json.loads((RECORDS / _filename_for(native_id)).read_text(encoding="utf-8"))


def normalize(native_id: str) -> Record:
    return tcia.normalize(
        envelope(native_id), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )


ALL_NATIVE_IDS = [
    "4D-Lung",
    "A091105",
    "ACNS0332",
    "ACRIN-6698",
    "ACRIN-Contralateral-Breast-MR",
    "c-nmc-2019",
    "tcga-gbm",
    "vestibular-schwannoma-mc-rc2",
]


def synthetic(
    *,
    native_id: str = "My-Collection",
    nbia: dict | None = None,
    datacite: dict | None = None,
) -> dict:
    return {
        "source": "tcia",
        "native_id": native_id,
        "harvest_method": "api:nbia+datacite",
        "endpoints": [],
        "payload": {"nbia": nbia, "datacite": datacite},
    }


def patients(n: int, **overrides) -> list[dict]:
    base = {"Phantom": "NO", "SpeciesDescription": "Homo sapiens"}
    base.update(overrides)
    return [dict(base, PatientId=f"p{i}") for i in range(n)]


# ---------------------------------------------------------------------------
# Every captured record validates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("native_id", ALL_NATIVE_IDS)
def test_every_fixture_normalizes_to_a_valid_record(native_id):
    record = normalize(native_id)
    Record.model_validate(record.model_dump(mode="json"))

    assert record.source == "tcia"
    assert record.source_native_id == native_id
    assert record.url.startswith("https://")
    assert word_count(record.summary) <= 40
    assert record.provenance.harvested_via == "api:nbia+datacite"
    assert record.provenance.raw_hash
    # DataCite reports only a publicationYear, never a full date.
    assert record.published is None
    assert record.years.start is None and record.years.end is None
    assert record.countries == []


def test_fixture_native_ids_are_the_expected_set():
    assert sorted(p.name for p in RECORDS.glob("*.json")) == sorted(
        f"{n}.json" for n in ALL_NATIVE_IDS
    )


# ---------------------------------------------------------------------------
# Identity, url, name, summary
# ---------------------------------------------------------------------------


def test_id_is_the_slugified_native_id():
    assert normalize("ACRIN-Contralateral-Breast-MR").id == (
        "tcia:acrin-contralateral-breast-mr"
    )


def test_name_and_url_come_from_datacite_when_matched():
    record = normalize("4D-Lung")
    assert record.name == "Data from 4D Lung Imaging of NSCLC Patients"
    assert record.url == "https://www.cancerimagingarchive.net/collection/4d-lung/"


def test_name_ignores_alternative_titles():
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [
                    {"title": "Alt", "titleType": "AlternativeTitle"},
                    {"title": "Main", "titleType": None},
                ],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.name == "Main"


def test_name_and_url_fall_back_when_there_is_no_datacite_match():
    record = tcia.normalize(
        synthetic(nbia={"collection": "My-Collection", "modalities": ["CT"]}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.name == "My-Collection"
    assert record.url == "https://www.cancerimagingarchive.net/collections/"


def test_summary_is_the_first_40_words_of_the_datacite_description():
    record = normalize("4D-Lung")
    assert record.summary.startswith(
        "This data collection consists of images acquired during chemoradiotherapy"
    )
    assert word_count(record.summary) == 40


def test_summary_strips_html_from_the_description():
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "Main"}],
                "descriptions": [{"description": "<p>Hello <b>there</b></p>"}],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.summary == "Hello there"


def test_summary_falls_back_to_a_template_built_from_nbia_facts():
    record = tcia.normalize(
        synthetic(
            nbia={
                "collection": "My-Collection",
                "modalities": ["CT", "MR"],
                "body_parts": ["LUNG"],
                "patients": patients(12),
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.summary == (
        "My-Collection: imaging collection on The Cancer Imaging Archive "
        "(MRI, CT; 12 subjects)."
    )


def test_summary_template_omits_facts_the_source_does_not_report():
    record = tcia.normalize(
        synthetic(nbia={"collection": "My-Collection"}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.summary == (
        "My-Collection: imaging collection on The Cancer Imaging Archive."
    )


# ---------------------------------------------------------------------------
# Modalities and keywords
# ---------------------------------------------------------------------------


def test_modalities_map_nbia_codes_in_vocabulary_order():
    assert normalize("4D-Lung").modalities == ["CT", "radiotherapy"]
    assert normalize("ACRIN-Contralateral-Breast-MR").modalities == ["MRI", "xray"]


def test_non_imaging_modality_codes_become_keywords():
    record = normalize("ACRIN-6698")
    assert record.modalities == ["MRI"]
    assert record.keywords == ["seg", "breast", "tspine"]


def test_body_parts_become_lowercase_keywords():
    assert normalize("ACNS0332").keywords == ["brain", "headneck", "spine"]


def test_every_documented_modality_code_maps_somewhere():
    codes = [
        "CR", "CT", "DX", "FUSION", "KO", "MG", "MR", "NM", "OT", "PR",
        "PT", "REG", "RF", "RTDOSE", "RTPLAN", "RTSTRUCT", "RWV", "SEG",
        "SR", "US", "XA", "SM",
    ]  # fmt: skip
    record = tcia.normalize(
        synthetic(nbia={"collection": "X", "modalities": codes}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.modalities == [
        "MRI",
        "PET",
        "SPECT",
        "CT",
        "xray",
        "mammography",
        "ultrasound",
        "radiotherapy",
        "pathology",
    ]
    assert record.keywords == [
        "fusion", "ko", "ot", "pr", "reg", "rwv", "seg", "sr",
    ]  # fmt: skip


def test_unknown_modality_codes_are_ignored():
    record = tcia.normalize(
        synthetic(nbia={"collection": "X", "modalities": ["CT", "ZZZ"]}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.modalities == ["CT"]
    assert record.keywords == []


# ---------------------------------------------------------------------------
# Sample size and species
# ---------------------------------------------------------------------------


def test_sample_size_counts_non_phantom_patients():
    record = normalize("4D-Lung")
    assert (record.sample_size, record.sample_unit) == (5, "participants")


def test_sample_size_is_null_when_the_patient_list_is_missing():
    record = normalize("tcga-gbm")
    assert record.sample_size is None
    assert record.sample_unit is None


def test_sample_size_excludes_phantoms():
    record = tcia.normalize(
        synthetic(
            nbia={
                "collection": "X",
                "patients": patients(3) + patients(2, Phantom="YES"),
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.sample_size == 3


@pytest.mark.parametrize(
    "people,expected",
    [
        (patients(2), "human"),
        (patients(2, SpeciesDescription="Canis familiaris"), "animal"),
        (
            patients(1) + patients(1, SpeciesDescription="Mus musculus"),
            "mixed",
        ),
        (patients(2, Phantom="YES"), "phantom"),
        (patients(2, SpeciesDescription=""), "unknown"),
        ([], "unknown"),
    ],
)
def test_species_from_patient_records(people, expected):
    record = tcia.normalize(
        synthetic(nbia={"collection": "X", "patients": people}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.species == expected


def test_species_is_unknown_without_a_patient_list():
    assert normalize("c-nmc-2019").species == "unknown"


# ---------------------------------------------------------------------------
# License
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rights_identifier,expected",
    [
        ("cc-by-4.0", "CC-BY-4.0"),
        ("cc-by-3.0", "CC-BY-3.0"),
        ("cc-by-nc-4.0", "CC-BY-NC-4.0"),
        ("cc-by-nc-3.0", "CC-BY-NC-3.0"),
        ("cc-by-nc-nd-3.0", "CC-BY-NC-ND-3.0"),
        ("cc0-1.0", "CC0-1.0"),
    ],
)
def test_license_maps_rights_identifiers_to_spdx(rights_identifier, expected):
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "rightsList": [
                    {"rights": "Whatever", "rightsIdentifier": rights_identifier}
                ],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.license == expected


def test_license_falls_back_to_the_verbatim_rights_text():
    assert normalize("A091105").license == "NCTN Data Archive License"


def test_license_is_null_without_a_rights_entry():
    assert normalize("vestibular-schwannoma-mc-rc2").license is None


# ---------------------------------------------------------------------------
# Access and record status
# ---------------------------------------------------------------------------


def test_nbia_listed_collections_are_open_and_active():
    record = normalize("4D-Lung")
    assert record.access == "open"
    assert record.access_tiers == ["open"]
    assert record.record_status == "active"


def test_datacite_only_records_with_restrictive_rights_need_an_application():
    record = normalize("tcga-gbm")
    assert record.access == "application"
    assert record.access_tiers == ["application"]
    assert record.record_status == "needs_review"


def test_datacite_only_records_otherwise_need_registration():
    record = normalize("c-nmc-2019")
    assert record.access == "registration"
    assert record.record_status == "needs_review"


@pytest.mark.parametrize(
    "rights,expected",
    [
        ("TCIA Limited Access License", "application"),
        ("NIH Controlled Data Access Policy", "application"),
        ("NCTN Data Archive License", "application"),
        ("TCIA Data Usage Policy", "application"),
        ("Creative Commons Attribution 4.0 International", "registration"),
    ],
)
def test_gated_access_tier_from_the_rights_text(rights, expected):
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "rightsList": [{"rights": rights}],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.access == expected


def test_an_nbia_collection_without_a_datacite_match_needs_review():
    record = tcia.normalize(
        synthetic(nbia={"collection": "My-Collection", "patients": patients(2)}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.access == "open"
    assert record.record_status == "needs_review"


def test_a_collection_with_harvest_errors_needs_review():
    record = tcia.normalize(
        synthetic(
            nbia={
                "collection": "My-Collection",
                "modalities": ["CT"],
                "errors": ["getPatient: boom"],
            },
            datacite={"doi": "10.7937/x", "titles": [{"title": "T"}]},
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.record_status == "needs_review"


# ---------------------------------------------------------------------------
# Authors, institutions, papers, DOI, version
# ---------------------------------------------------------------------------


def test_authors_carry_orcids_verbatim_where_datacite_has_them():
    record = normalize("4D-Lung")
    assert [a.name for a in record.authors] == [
        "Hugo, Geoffrey D.",
        "Weiss, Elisabeth",
        "Sleeman, William C.",
        "Balik, Salim",
        "Keall, Paul J.",
    ]
    assert record.authors[3].orcid == "https://orcid.org/0000-0003-0756-8953"
    assert record.authors[0].orcid is None


def test_non_orcid_name_identifiers_are_ignored():
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "creators": [
                    {
                        "name": "Doe, Jane",
                        "nameIdentifiers": [
                            {
                                "nameIdentifierScheme": "ISNI",
                                "nameIdentifier": "0000 0001 2281 955X",
                            }
                        ],
                    }
                ],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.authors[0].orcid is None


def test_institutions_come_from_creator_affiliations_deduped():
    record = normalize("vestibular-schwannoma-mc-rc2")
    assert [i.name for i in record.institutions] == [
        "King's College London",
        "King's College Hospital NHS Foundation Trust",
        "Walton Centre",
    ]


def test_institutions_accept_both_datacite_affiliation_shapes():
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "creators": [
                    {"name": "A", "affiliation": ["Plain String University"]},
                    {"name": "B", "affiliation": [{"name": "Dict Institute"}]},
                ],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert [i.name for i in record.institutions] == [
        "Plain String University",
        "Dict Institute",
    ]


def test_papers_map_related_identifier_relations():
    record = normalize("tcga-gbm")
    relations = {p.doi: p.relation for p in record.papers}
    assert relations["10.1093/neuonc/noz199"] == "other"  # IsReferencedBy
    assert relations["10.1371/journal.pone.0025451"] == "is_cited_by"


def test_papers_treat_describing_relations_as_describes():
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "relatedIdentifiers": [
                    {
                        "relatedIdentifierType": "DOI",
                        "relationType": "IsDescribedBy",
                        "relatedIdentifier": "10.1000/a",
                    },
                    {
                        "relatedIdentifierType": "DOI",
                        "relationType": "IsSupplementTo",
                        "relatedIdentifier": "10.1000/b",
                    },
                    {
                        "relatedIdentifierType": "PMID",
                        "relationType": "IsSupplementTo",
                        "relatedIdentifier": "12345678",
                    },
                    {
                        "relatedIdentifierType": "DOI",
                        "relationType": "IsCitedBy",
                        "relatedIdentifier": "not a doi",
                    },
                ],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert [(p.doi, p.relation) for p in record.papers] == [
        ("10.1000/a", "describes"),
        ("10.1000/b", "describes"),
    ]


def test_papers_are_deduped_and_capped_at_twenty():
    related = [
        {
            "relatedIdentifierType": "DOI",
            "relationType": "IsCitedBy",
            "relatedIdentifier": f"10.1000/p{i}",
        }
        for i in range(30)
    ]
    related.append(dict(related[0]))
    record = tcia.normalize(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "relatedIdentifiers": related,
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert len(record.papers) == 20
    assert len({p.doi for p in record.papers}) == 20


def test_dataset_doi_and_version_come_from_datacite():
    record = normalize("tcga-gbm")
    assert record.dataset_doi == "10.7937/k9/tcia.2016.rnyfuye9"
    assert record.version == "4"


def test_dataset_doi_and_version_are_null_without_datacite():
    record = tcia.normalize(
        synthetic(nbia={"collection": "X"}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.dataset_doi is None
    assert record.version is None


# ---------------------------------------------------------------------------
# Domains and conditions
# ---------------------------------------------------------------------------


def test_domains_default_to_oncology():
    assert normalize("4D-Lung").domains == ["oncology"]


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Data from 4D Lung Imaging", ["oncology"]),
        ("COVID-19-AR chest imaging", ["pulmonology", "infectious_disease"]),
        ("A normative healthy reference cohort", []),
        ("Pediatric-CT-SEG segmentations", ["oncology", "pediatrics"]),
        ("COVID-19 pedi cohort", ["pulmonology", "pediatrics", "infectious_disease"]),
    ],
)
def test_domain_overrides_are_driven_by_the_title(title, expected):
    record = tcia.normalize(
        synthetic(datacite={"doi": "10.7937/x", "titles": [{"title": title}]}),
        harvested_at=HARVESTED_AT,
        first_seen=FIRST_SEEN,
    )
    assert record.domains == expected


def test_conditions_are_empty_unless_the_title_names_one_unambiguously():
    assert normalize("4D-Lung").conditions == []
    assert [c.label for c in normalize("tcga-gbm").conditions] == ["glioblastoma"]
    assert all(c.mesh_id is None for c in normalize("tcga-gbm").conditions)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_dates_follow_the_first_seen_convention():
    record = normalize("4D-Lung")
    assert record.provenance.harvested_at.isoformat() == FIRST_SEEN
    assert record.provenance.last_verified.isoformat() == HARVESTED_AT
    assert record.provenance.enrichment.method == "rules"


# ---------------------------------------------------------------------------
# enrichment_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("native_id", ALL_NATIVE_IDS)
def test_enrichment_text_is_non_empty_and_bounded(native_id):
    text = tcia.enrichment_text(envelope(native_id))
    assert 0 < len(text) <= 1500


def test_enrichment_text_includes_title_description_body_parts_and_codes():
    text = tcia.enrichment_text(envelope("ACRIN-6698"))
    assert "ACRIN 6698/I-SPY2 Breast DWI" in text
    assert "American College of Radiology" in text
    assert "BREAST" in text
    assert "SEG" in text


def test_enrichment_text_truncates_a_very_long_description():
    text = tcia.enrichment_text(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "descriptions": [{"description": "word " * 2000}],
            }
        )
    )
    assert len(text) == 1500


def test_enrichment_text_strips_html():
    text = tcia.enrichment_text(
        synthetic(
            datacite={
                "doi": "10.7937/x",
                "titles": [{"title": "T"}],
                "descriptions": [{"description": "<p>Hello <b>there</b></p>"}],
            }
        )
    )
    assert "<p>" not in text
    assert "Hello there" in text


def test_normalizer_is_discovered_by_the_registry():
    from atlas.normalize import get_normalizers

    normalize_fn, enrichment_fn = get_normalizers()["tcia"]
    assert normalize_fn is tcia.normalize
    assert enrichment_fn is tcia.enrichment_text
