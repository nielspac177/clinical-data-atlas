"""Tests for `atlas.normalize.openneuro` against 5 real, trimmed OpenNeuro
GraphQL dataset envelopes captured live on 2026-08-22 (see
`docs/sources/openneuro.md` for the exact request and field notes).

Fixture envelopes live at `tests/fixtures/openneuro/records/<id>.json`.
Their `metadata.ages` / `summary.subjects` lists are truncated to <=5 items
and `readme` to <=500 chars per the project's fixture-size rules -- so
`sample_size` (== len(subjects)) is 5 for every fixture here by
construction, not because the mapping is hardcoded; a synthetic test below
uses a distinctly-sized subjects list to prove that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas import schema, vocab
from atlas.normalize import common, openneuro

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "openneuro" / "records"

HARVESTED_AT = "2026-08-22"


def load_envelope(native_id: str) -> dict:
    path = FIXTURES_DIR / f"{native_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_fixture(native_id: str, *, first_seen: str = "2026-01-01"):
    envelope = load_envelope(native_id)
    return openneuro.normalize(
        envelope, harvested_at=HARVESTED_AT, first_seen=first_seen
    )


ALL_FIXTURE_IDS = [
    "ds008082",
    "ds008477",
    "ds008159",
    "ds008704",
    "ds005516",
]


# ---------------------------------------------------------------------------
# SOURCE + registry wiring
# ---------------------------------------------------------------------------


def test_source_constant():
    assert openneuro.SOURCE == "openneuro"


# ---------------------------------------------------------------------------
# ds008082 -- COVID-19 EEG dataset: neurology domain keyword (studyDomain
# "Clinical/Neurological"), covid-19 condition alias, dual associatedPaperDOI
# with a comma + https prefixes, a duplicate DOI in ReferencesAndLinks
# (must be deduplicated, not double-counted), a full real author list.
# ---------------------------------------------------------------------------


def test_normalize_ds008082_covid_neurology_domain_and_paper_dedup():
    record = normalize_fixture("ds008082")
    assert isinstance(record, schema.Record)

    assert record.id == "openneuro:ds008082"
    assert record.source == "openneuro"
    assert record.source_native_id == "ds008082"
    assert record.name == "A COVID-19 survivors and close contacts EEG dataset"
    assert record.url == "https://openneuro.org/datasets/ds008082"

    # readme is real (non-placeholder) -> summary is the first 40 words of it.
    assert record.summary == (
        "July 1st 2026 Cuban Neuroscience Center This is a dataset containing "
        "173 EEGs including 86 healthy subjects and 87 COVID patients EEG was "
        "acquired using a 10-20 standard montage. Resting EEG was recorded "
        "for 8 minutes in all participants with"
    )
    assert len(record.summary.split()) <= 40

    assert record.species == "human"
    assert record.sample_size == 5  # len(subjects) after fixture truncation
    assert record.sample_unit == "participants"
    assert record.size_bytes == 1182337457

    assert record.modalities == ["EEG"]
    assert record.domains == ["neurology"]  # studyDomain "Clinical/Neurological"
    assert [c.label for c in record.conditions] == ["covid-19"]
    assert record.population == "Human; COVID-19 vs Controls"
    assert record.keywords == ["COVID"]

    assert record.license == "CC0-1.0"
    assert record.dataset_doi == "10.18112/openneuro.ds008082"
    assert record.version == "1.0.0"
    assert record.published.isoformat() == "2026-07-01"

    assert len(record.authors) == 28
    assert record.authors[0].name == "Ana Calzada-Reyes"
    assert record.authors[-1].name == "Pedro Valdés-Sosa"

    # associatedPaperDOI has two DOIs (comma-separated, https://doi.org/
    # prefixed); ReferencesAndLinks repeats the second one -- deduplicated.
    assert [p.doi for p in record.papers] == [
        "10.1038/s41597-021-00829-7",
        "10.1016/j.cnp.2026.02.007",
    ]
    assert {p.relation for p in record.papers} == {"describes"}

    assert record.access == "open"
    assert record.access_tiers == ["open"]
    assert record.record_status == "active"
    assert record.provenance.harvested_via == "api:openneuro-graphql"
    assert record.provenance.harvested_at.isoformat() == "2026-01-01"  # first_seen
    assert record.provenance.last_verified.isoformat() == HARVESTED_AT
    assert record.provenance.raw_hash is not None


# ---------------------------------------------------------------------------
# ds008477 -- Rat MEMRI dataset: non-human species, "Healthy / Control"
# condition alias, no papers (both source fields empty/null), a normal
# 5-author list, default neuroscience domain.
# ---------------------------------------------------------------------------


def test_normalize_ds008477_rat_species_and_healthy_control_condition():
    record = normalize_fixture("ds008477")

    assert record.name == (
        "Longitudinal MEMRI study of exercise and sexual behavior in female Wistar rats"
    )
    assert record.species == "animal"  # raw metadata.species == "Rat"
    assert record.sample_size == 5
    assert record.size_bytes == 1121424882
    assert record.modalities == ["MRI"]
    assert record.domains == ["neuroscience"]  # no neurology/psychiatry keyword
    assert [c.label for c in record.conditions] == ["healthy controls"]
    assert record.population == "Rat; Healthy / Control"
    assert record.keywords == []  # summary.tasks is empty

    assert record.license == "CC0-1.0"
    assert record.dataset_doi == "10.18112/openneuro.ds008477"
    assert record.version == "1.0.2"
    assert record.published.isoformat() == "2026-07-26"

    assert [a.name for a in record.authors] == [
        "Julissa Mendoza",
        "Raul G. Paredes",
        "Maria F. Barrera-Tenorio",
        "Lorena Gaytan-Tocaven",
        "Josue A. Aguilar-Moreno",
    ]
    # associatedPaperDOI == "" and ReferencesAndLinks is null -- no papers.
    assert record.papers == []


# ---------------------------------------------------------------------------
# ds008159 -- associatedPaperDOI + ReferencesAndLinks both cite the same
# companion preprint, and ReferencesAndLinks *also* cites an older version
# of the dataset's own DOI (a real self-citation quirk) -- must end up with
# exactly one paper, not two, and not the dataset citing itself.
# ---------------------------------------------------------------------------


def test_normalize_ds008159_self_doi_excluded_from_papers():
    record = normalize_fixture("ds008159")

    assert record.dataset_doi == "10.18112/openneuro.ds008159"
    assert record.version == "1.0.1"
    assert record.modalities == ["MRI", "fMRI"]
    assert record.domains == ["neuroscience"]
    assert record.conditions == []  # dxStatus is ""
    assert record.population == "Human; ages 21–33"

    # readme is exactly 40 words -> summary is the whole (unchanged) readme.
    assert len(record.summary.split()) == 40
    assert record.summary.startswith("Please cite the following reference")
    assert record.summary.endswith("716388.")

    assert record.keywords == ["yieldresist"]

    # Only the genuine companion-paper DOI survives: the same DOI appears
    # in both associatedPaperDOI and ReferencesAndLinks (deduplicated),
    # and ReferencesAndLinks' *other* DOI is this dataset's own (an older
    # ".v1.0.0" of "10.18112/openneuro.ds008159") -- excluded as self-citation.
    assert [p.doi for p in record.papers] == ["10.64898/2026.04.03.716388"]


# ---------------------------------------------------------------------------
# ds008704 -- an unfinished/template BIDS submission: every Authors entry
# is the stock placeholder, the readme is the stock "TODO:" boilerplate,
# and ReferencesAndLinks/associatedPaperDOI are placeholder text too.
# ---------------------------------------------------------------------------


def test_normalize_ds008704_placeholder_authors_and_readme_produce_template_summary():
    record = normalize_fixture("ds008704")

    assert record.name == "Quartet 3T data"  # trailing "\n" stripped
    assert record.modalities == ["MRI", "fMRI"]
    assert record.sample_size == 5

    # Every Authors entry ("TODO:", "First1 Last1", "First2 Last2", "...")
    # is a placeholder -> authors ends up empty, not full of garbage names.
    assert record.authors == []

    # readme is the stock "TODO: Provide description..." boilerplate ->
    # treated as a placeholder, so summary falls back to the template.
    assert record.summary == (
        "Quartet 3T data: MRI, fMRI dataset on OpenNeuro (5 participants)."
    )

    # keywords = tasks verbatim -- only Authors get placeholder-filtered,
    # not tasks/keywords, so the TODO-prefixed task names are kept as-is.
    assert record.keywords == [
        "TODO: full task name for volitional",
        "TODO: full task name for ambiguous",
        "TODO: full task name for hMTloc",
        "TODO: full task name for physical",
        "physical",
        "ambiguous",
        "volitional",
        "hMTloc",
    ]

    # associatedPaperDOI == "" and ReferencesAndLinks are placeholder
    # strings with no DOI-shaped text -- no papers fabricated from them.
    assert record.papers == []
    assert record.population == "Human; ages 25–37"
    assert record.license == "CC0-1.0"


# ---------------------------------------------------------------------------
# ds005516 -- metadata.species/studyDomain/dxStatus/associatedPaperDOI are
# all JSON `null` (not just empty strings) with affirmedDefaced=true, an
# unmapped license (verbatim fallback, not nulled out), and three distinct
# paper DOIs found only via ReferencesAndLinks (doi.org/dx.doi.org forms).
# ---------------------------------------------------------------------------


def test_normalize_ds005516_empty_species_affirmed_defaced_and_license_fallback():
    record = normalize_fixture("ds005516")

    assert record.species == "human"  # empty species + affirmedDefaced=true
    assert record.domains == ["neuroscience"]  # studyDomain/dxStatus both null
    assert record.conditions == []
    # species raw is null (not just deduced) -> omitted from free-text
    # population; dxStatus is null too -> only the age range remains.
    assert record.population == "ages 7–22"

    # "CC-BY-SA 4.0" isn't in vocab.LICENSE_MAP (exactly or normalized) --
    # per docs/schema.md, an unmapped-but-present license passes through
    # verbatim rather than being nulled out.
    assert common.license_to_spdx("CC-BY-SA 4.0") is None
    assert record.license == "CC-BY-SA 4.0"

    assert record.dataset_doi == "10.18112/openneuro.ds005516"
    assert record.modalities == ["EEG"]
    assert len(record.authors) == 8
    assert record.authors[0].name == "Seyed Yahya Shirazi"

    # All three papers come from ReferencesAndLinks (associatedPaperDOI is
    # null); one is a dx.doi.org-prefixed DOI, proving both doi.org and
    # dx.doi.org forms are recognized.
    assert [p.doi for p in record.papers] == [
        "10.1101/2024.10.03.615261",
        "10.1038/sdata.2017.181",
        "10.1038/sdata.2017.40",
    ]


# ---------------------------------------------------------------------------
# Schema validation across all 5 fixtures
# ---------------------------------------------------------------------------


def test_all_fixtures_normalize_to_schema_valid_records():
    records = [normalize_fixture(nid) for nid in ALL_FIXTURE_IDS]
    assert all(isinstance(r, schema.Record) for r in records)

    dumped = [r.model_dump(mode="json") for r in records]
    errors, _warnings = schema.validate_records(dumped)
    assert errors == []


# ---------------------------------------------------------------------------
# enrichment_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("native_id", ALL_FIXTURE_IDS)
def test_enrichment_text_non_empty_and_within_length_cap(native_id):
    envelope = load_envelope(native_id)
    text = openneuro.enrichment_text(envelope)
    assert isinstance(text, str)
    assert text.strip() != ""
    assert len(text) <= 1500


# ---------------------------------------------------------------------------
# no_snapshot exclusion (unpublished/draft dataset)
#
# None of the 5 live-captured fixtures above have this shape (a scan of
# ~1,000 real listing entries turned up zero with `latestSnapshot: null`),
# so this uses a small hand-built envelope with the documented shape
# instead -- see docs/sources/openneuro.md.
# ---------------------------------------------------------------------------


def test_normalize_excludes_dataset_with_no_latest_snapshot():
    envelope = {
        "source": "openneuro",
        "native_id": "ds999999",
        "harvest_method": "api:openneuro-graphql",
        "endpoints": ["https://openneuro.org/crn/graphql"],
        "payload": {
            "id": "ds999999",
            "created": "2026-01-01T00:00:00.000Z",
            "publishDate": None,
            "metadata": {
                "species": "",
                "studyDomain": "",
                "studyDesign": "",
                "studyLongitudinal": "",
                "dataProcessed": None,
                "ages": [],
                "modalities": [],
                "associatedPaperDOI": "",
                "grantFunderName": "",
                "dxStatus": "",
                "affirmedDefaced": False,
            },
            "latestSnapshot": None,
        },
    }
    outcome = openneuro.normalize(
        envelope, harvested_at=HARVESTED_AT, first_seen="2026-01-01"
    )
    assert isinstance(outcome, common.Excluded)
    assert outcome.native_id == "ds999999"
    assert outcome.reason == "no_snapshot"


# ---------------------------------------------------------------------------
# Synthetic edge cases not present in the 5 live fixtures
# ---------------------------------------------------------------------------


def _minimal_envelope(native_id: str = "ds000001", **overrides) -> dict:
    """A minimal-but-realistically-shaped envelope for synthetic-input
    tests of branches the 5 live fixtures don't happen to exercise."""
    metadata = {
        "species": "",
        "studyDomain": "",
        "studyDesign": "",
        "studyLongitudinal": "",
        "dataProcessed": None,
        "ages": [],
        "modalities": [],
        "associatedPaperDOI": "",
        "grantFunderName": "",
        "dxStatus": "",
        "affirmedDefaced": False,
    }
    metadata.update(overrides.pop("metadata", {}))
    description = {
        "Name": "Synthetic dataset",
        "Authors": ["Jane Doe"],
        "License": "CC0",
        "DatasetDOI": f"doi:10.18112/openneuro.{native_id}.v1.0.0",
        "Funding": None,
        "ReferencesAndLinks": None,
        "HowToAcknowledge": None,
    }
    description.update(overrides.pop("description", {}))
    summary = {
        "subjects": ["01", "02", "03", "04", "05", "06", "07"],
        "sessions": [],
        "modalities": ["mri"],
        "secondaryModalities": [],
        "tasks": [],
        "totalFiles": 10,
        "size": 12345,
        "dataProcessed": False,
    }
    summary.update(overrides.pop("summary", {}))
    payload = {
        "id": native_id,
        "created": "2026-01-01T00:00:00.000Z",
        "publishDate": "2026-01-02T00:00:00.000Z",
        "metadata": metadata,
        "latestSnapshot": {
            "tag": "1.0.0",
            "created": "2026-01-02T00:00:00.000Z",
            "description": description,
            "readme": "A short, non-placeholder readme for this dataset.",
            "summary": summary,
        },
    }
    return {
        "source": "openneuro",
        "native_id": native_id,
        "harvest_method": "api:openneuro-graphql",
        "endpoints": ["https://openneuro.org/crn/graphql"],
        "payload": payload,
    }


def test_sample_size_is_the_actual_subject_count_not_hardcoded():
    # 7 subjects here, vs. 5 in every live fixture above -- proves
    # sample_size tracks len(subjects) rather than a hardcoded constant.
    record = openneuro.normalize(
        _minimal_envelope(), harvested_at=HARVESTED_AT, first_seen="2026-01-01"
    )
    assert record.sample_size == 7


def test_species_unknown_when_empty_and_not_affirmed_defaced():
    record = openneuro.normalize(
        _minimal_envelope(metadata={"species": "", "affirmedDefaced": False}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.species == "unknown"


def test_species_human_when_empty_but_affirmed_defaced():
    record = openneuro.normalize(
        _minimal_envelope(metadata={"species": None, "affirmedDefaced": True}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.species == "human"


def test_unmapped_modality_value_goes_to_keywords_not_modalities():
    record = openneuro.normalize(
        _minimal_envelope(summary={"modalities": ["genetics"], "tasks": ["realtask"]}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.modalities == []
    assert record.keywords == ["realtask", "genetics"]


def test_domain_psychiatry_keyword_in_study_domain():
    record = openneuro.normalize(
        _minimal_envelope(
            metadata={"studyDomain": "Psychiatry and Behavioral Sciences"}
        ),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.domains == ["psychiatry"]


def test_domain_both_neurology_and_psychiatry_keywords_present():
    record = openneuro.normalize(
        _minimal_envelope(metadata={"studyDomain": "Neurology and Psychiatry"}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.domains == ["neurology", "psychiatry"]


def test_license_absent_entirely_is_null_never_assumed():
    record = openneuro.normalize(
        _minimal_envelope(description={"License": None}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.license is None


def test_case_insensitive_modality_values_are_still_mapped():
    record = openneuro.normalize(
        _minimal_envelope(summary={"modalities": ["MRI", "EEG"]}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.modalities == ["MRI", "EEG"]


def test_modalities_are_vocab_ordered_regardless_of_source_order():
    # secondaryModalities lists fMRI-mapping value after dMRI-mapping value;
    # output must still follow vocab.MODALITIES order (MRI, fMRI, dMRI, ...).
    record = openneuro.normalize(
        _minimal_envelope(
            summary={
                "modalities": ["mri"],
                "secondaryModalities": ["mri_diffusion", "mri_functional"],
            }
        ),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.modalities == ["MRI", "fMRI", "dMRI"]
    assert list(vocab.MODALITIES).index("MRI") < list(vocab.MODALITIES).index("fMRI")


def test_name_falls_back_to_native_id_when_name_is_blank():
    record = openneuro.normalize(
        _minimal_envelope(native_id="ds000002", description={"Name": "   "}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.name == "ds000002"


def test_population_is_none_when_no_parts_are_present():
    record = openneuro.normalize(
        _minimal_envelope(metadata={"species": "", "dxStatus": "", "ages": []}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.population is None


def test_population_single_age_value_has_no_dash():
    record = openneuro.normalize(
        _minimal_envelope(metadata={"species": "Human", "ages": [30, 30, None]}),
        harvested_at=HARVESTED_AT,
        first_seen="2026-01-01",
    )
    assert record.population == "Human; ages 30"


def test_readme_null_is_treated_as_empty_and_falls_back_to_template():
    envelope = _minimal_envelope()
    envelope["payload"]["latestSnapshot"]["readme"] = None
    record = openneuro.normalize(
        envelope, harvested_at=HARVESTED_AT, first_seen="2026-01-01"
    )
    assert record.summary.startswith("Synthetic dataset: MRI dataset on OpenNeuro")
