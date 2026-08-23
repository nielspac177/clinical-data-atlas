"""Tests for `atlas.normalize.physionet` against the 5 real, live-captured
fixtures under `tests/fixtures/physionet/records/` (see
`docs/sources/physionet.md` for how/when they were captured) plus a few
hand-built envelopes for branches the 5 real records can't exercise on
their own (a `Challenge`/`Model` resource type, an over-40-word
`short_description`, an unknown `access_policy`).
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from atlas import io, schema
from atlas.normalize import physionet
from atlas.normalize.common import Excluded

FIXTURES = Path(__file__).parent / "fixtures" / "physionet" / "records"

HARVESTED_AT = "2026-08-22"
FIRST_SEEN = "2026-01-15"


def _payload(slug: str) -> dict:
    return json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))


def _envelope(slug: str, payload: dict | None = None) -> dict:
    return {
        "source": "physionet",
        "native_id": slug,
        "harvest_method": "api:physionet-published",
        "endpoints": ["https://physionet.org/api/v1/project/published/"],
        "payload": payload if payload is not None else _payload(slug),
    }


def _normalize(slug: str, payload: dict | None = None):
    return physionet.normalize(
        _envelope(slug, payload), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )


DATASET_SLUGS = ["slpdb", "ct-ich", "eicu-crd", "hirid"]  # resource_type Database
SOFTWARE_SLUG = "wfdb-swig-matlab"
ALL_SLUGS = [*DATASET_SLUGS, SOFTWARE_SLUG]


# ---------------------------------------------------------------------------
# Module wiring
# ---------------------------------------------------------------------------


def test_source_constant():
    assert physionet.SOURCE == "physionet"


def test_registered_with_the_real_normalizer_registry():
    from atlas import normalize as normalize_pkg

    registry = normalize_pkg.get_normalizers()
    assert registry.get("physionet") == (physionet.normalize, physionet.enrichment_text)


# ---------------------------------------------------------------------------
# Software/Model -> Excluded(reason="not_a_dataset"); Database/Challenge kept
# ---------------------------------------------------------------------------


def test_software_resource_type_is_excluded():
    outcome = _normalize(SOFTWARE_SLUG)
    assert outcome == Excluded(native_id=SOFTWARE_SLUG, reason="not_a_dataset")


def test_model_resource_type_is_excluded():
    payload = _payload("slpdb")
    payload["resource_type"] = "Model"
    outcome = _normalize("slpdb", payload)
    assert outcome == Excluded(native_id="slpdb", reason="not_a_dataset")


@pytest.mark.parametrize("slug", DATASET_SLUGS)
def test_database_resource_type_is_kept(slug):
    outcome = _normalize(slug)
    assert isinstance(outcome, schema.Record)


def test_challenge_resource_type_is_kept_and_not_excluded():
    payload = _payload("slpdb")
    payload["resource_type"] = "Challenge"
    outcome = _normalize("slpdb", payload)
    assert isinstance(outcome, schema.Record)


# ---------------------------------------------------------------------------
# Every kept fixture validates against the canonical schema
# ---------------------------------------------------------------------------


def test_all_dataset_fixtures_normalize_to_schema_valid_records():
    records = [_normalize(slug) for slug in DATASET_SLUGS]
    assert all(isinstance(r, schema.Record) for r in records)
    errors, _warnings = schema.validate_records(
        [r.model_dump(mode="json") for r in records]
    )
    assert errors == []


# ---------------------------------------------------------------------------
# id / source / source_native_id / name / url
# ---------------------------------------------------------------------------


def test_id_source_and_native_id():
    record = _normalize("slpdb")
    assert record.id == "physionet:slpdb"
    assert record.source == "physionet"
    assert record.source_native_id == "slpdb"


def test_name_is_title():
    record = _normalize("slpdb")
    assert record.name == "MIT-BIH Polysomnographic Database"


def test_url_is_source_url():
    payload = _payload("slpdb")
    record = _normalize("slpdb")
    assert record.url == payload["source_url"]
    assert record.url.startswith("https://")


# ---------------------------------------------------------------------------
# summary: short_description vs. abstract vs. title fallback
# ---------------------------------------------------------------------------


def test_summary_uses_short_description_when_at_most_40_words():
    payload = _payload("slpdb")
    record = _normalize("slpdb")
    assert record.summary == payload["short_description"]
    assert schema.word_count(record.summary) <= 40


def test_summary_uses_short_description_at_exactly_40_words_boundary():
    payload = _payload("slpdb")
    exactly_40_words = " ".join(f"word{i}" for i in range(40))
    payload["short_description"] = exactly_40_words
    assert schema.word_count(exactly_40_words) == 40

    record = _normalize("slpdb", payload)

    assert record.summary == exactly_40_words


def test_summary_falls_back_to_abstract_when_short_description_is_empty():
    payload = _payload(SOFTWARE_SLUG)  # wfdb-swig-matlab has "" short_description
    assert payload["short_description"] == ""
    outcome = physionet.normalize(
        _envelope(SOFTWARE_SLUG), harvested_at=HARVESTED_AT, first_seen=FIRST_SEEN
    )
    # excluded (Software), but enrichment_text/summary logic is exercised
    # via a Database copy of the same payload below instead.
    assert isinstance(outcome, Excluded)

    payload_as_database = dict(payload, resource_type="Database")
    record = _normalize(SOFTWARE_SLUG, payload_as_database)
    expected = io.first_words(io.strip_html(payload["abstract"]), 40)
    assert record.summary == expected
    assert record.summary != ""


def test_summary_falls_back_to_abstract_when_short_description_over_40_words():
    long_short_description = " ".join(f"word{i}" for i in range(50))
    payload = _payload("slpdb")
    payload["short_description"] = long_short_description
    payload["abstract"] = "<p>" + " ".join(f"abs{i}" for i in range(60)) + "</p>"

    record = _normalize("slpdb", payload)

    assert record.summary == io.first_words(io.strip_html(payload["abstract"]), 40)
    assert schema.word_count(record.summary) == 40


def test_summary_falls_back_to_title_when_short_description_and_abstract_both_empty():
    payload = _payload("slpdb")
    payload["short_description"] = ""
    payload["abstract"] = ""
    payload["title"] = "Only The Title Remains"

    record = _normalize("slpdb", payload)

    assert record.summary == "Only The Title Remains"


# ---------------------------------------------------------------------------
# access / access_tiers / access_notes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug,expected_access",
    [
        ("slpdb", "open"),  # Open
        ("ct-ich", "registration"),  # Restricted
        ("eicu-crd", "credentialed"),  # Credentialed
        ("hirid", "application"),  # Contributor Review
    ],
)
def test_access_policy_mapping(slug, expected_access):
    record = _normalize(slug)
    assert record.access == expected_access
    assert record.access_tiers == [expected_access]


def test_access_notes_none_when_no_dua():
    payload = _payload("slpdb")
    assert payload["dua"] is None
    record = _normalize("slpdb")
    assert record.access_notes is None


def test_access_notes_is_dua_name_when_present():
    payload = _payload("ct-ich")
    record = _normalize("ct-ich")
    assert record.access_notes == payload["dua"]["name"]
    assert record.access_notes == "PhysioNet Restricted Health Data Use Agreement 1.5.0"


def test_unknown_access_policy_is_excluded_not_raised():
    payload = _payload("slpdb")
    payload["access_policy"] = "Something Else Entirely"

    outcome = _normalize("slpdb", payload)

    assert outcome == Excluded(
        native_id="slpdb", reason="unmapped_access_policy:Something Else Entirely"
    )


def test_missing_access_policy_is_excluded_not_raised():
    payload = _payload("slpdb")
    del payload["access_policy"]

    outcome = _normalize("slpdb", payload)

    assert outcome == Excluded(native_id="slpdb", reason="unmapped_access_policy:None")


# ---------------------------------------------------------------------------
# license: SPDX mapping vs. verbatim fallback
# ---------------------------------------------------------------------------


def test_license_maps_to_spdx_when_known():
    record = _normalize("slpdb")
    assert record.license == "ODC-By-1.0"


def test_license_falls_back_to_verbatim_name_when_unmapped():
    payload = _payload("ct-ich")
    record = _normalize("ct-ich")
    assert payload["license"]["name"] not in {}  # sanity: has a name at all
    assert record.license == "PhysioNet Restricted Health Data License 1.5.0"


# ---------------------------------------------------------------------------
# dataset_doi: core_doi preferred, version_doi fallback, cleaned/lowercased
# ---------------------------------------------------------------------------


def test_dataset_doi_falls_back_to_version_doi_when_core_doi_is_none():
    payload = _payload("slpdb")
    assert payload["core_doi"] is None
    record = _normalize("slpdb")
    assert record.dataset_doi == payload["version_doi"].lower()


def test_dataset_doi_prefers_core_doi_when_present():
    payload = _payload("ct-ich")
    assert payload["core_doi"] is not None
    assert payload["core_doi"] != payload["version_doi"]
    record = _normalize("ct-ich")
    assert record.dataset_doi == payload["core_doi"].lower()


def test_dataset_doi_none_when_neither_doi_present():
    payload = _payload("slpdb")
    payload["core_doi"] = None
    payload["version_doi"] = None
    record = _normalize("slpdb", payload)
    assert record.dataset_doi is None


# ---------------------------------------------------------------------------
# version / published / size_bytes passthrough
# ---------------------------------------------------------------------------


def test_version_published_and_size_bytes_passthrough():
    payload = _payload("slpdb")
    record = _normalize("slpdb")
    assert record.version == payload["version"] == "1.0.0"
    assert record.published == date(1999, 8, 3)
    assert record.size_bytes == payload["main_storage_size"] == 663056564


# ---------------------------------------------------------------------------
# keywords: topics, +"challenge" for Challenge resource type, deduped
# ---------------------------------------------------------------------------


def test_keywords_are_topics_verbatim_for_database():
    payload = _payload("slpdb")
    record = _normalize("slpdb")
    assert record.keywords == payload["topics"]
    assert "challenge" not in record.keywords


def test_keywords_append_challenge_for_challenge_resource_type():
    payload = _payload("slpdb")
    payload["resource_type"] = "Challenge"
    record = _normalize("slpdb", payload)
    assert record.keywords == [*payload["topics"], "challenge"]


def test_keywords_challenge_not_duplicated_if_already_a_topic():
    payload = _payload("slpdb")
    payload["resource_type"] = "Challenge"
    payload["topics"] = ["challenge", "sleep"]
    record = _normalize("slpdb", payload)
    assert record.keywords == ["challenge", "sleep"]
    assert record.keywords.count("challenge") == 1


# ---------------------------------------------------------------------------
# sample_size / sample_unit via regex_sample_size
# ---------------------------------------------------------------------------


def test_sample_size_participants_from_short_description():
    record = _normalize("slpdb")
    assert (record.sample_size, record.sample_unit) == (18, "participants")


def test_sample_size_admissions_family():
    record = _normalize("eicu-crd")
    assert (record.sample_size, record.sample_unit) == (200000, "admissions")


def test_sample_size_none_when_no_unit_word_matches():
    record = _normalize("ct-ich")
    assert (record.sample_size, record.sample_unit) == (None, None)


def test_sample_size_pulled_from_full_abstract_not_just_short_description():
    # hirid's short_description alone has no unit-word match; the count
    # only shows up once the (HTML-stripped) abstract is included too.
    record = _normalize("hirid")
    assert (record.sample_size, record.sample_unit) == (6500, "participants")


# ---------------------------------------------------------------------------
# species / domains / modalities / conditions / countries / years / people
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", DATASET_SLUGS)
def test_species_is_always_human(slug):
    assert _normalize(slug).species == "human"


@pytest.mark.parametrize("slug", DATASET_SLUGS)
def test_domains_modalities_conditions_are_empty_at_normalize_time(slug):
    record = _normalize(slug)
    assert record.domains == []
    assert record.modalities == []
    assert record.conditions == []


@pytest.mark.parametrize("slug", DATASET_SLUGS)
def test_countries_years_institutions_authors_are_empty(slug):
    record = _normalize(slug)
    assert record.countries == []
    assert record.years == schema.Years()
    assert record.institutions == []
    assert record.authors == []


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_provenance_fields():
    payload = _payload("slpdb")
    record = _normalize("slpdb")
    prov = record.provenance
    assert prov.harvested_via == "api:physionet-published"
    assert prov.harvested_at == date.fromisoformat(FIRST_SEEN)
    assert prov.last_verified == date.fromisoformat(HARVESTED_AT)
    assert prov.raw_hash == io.content_hash(payload)


def test_provenance_raw_hash_changes_if_payload_changes():
    payload = _payload("slpdb")
    record_a = _normalize("slpdb", payload)
    mutated = copy.deepcopy(payload)
    mutated["title"] = "Different Title"
    record_b = _normalize("slpdb", mutated)
    assert record_a.provenance.raw_hash != record_b.provenance.raw_hash


# ---------------------------------------------------------------------------
# record_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", DATASET_SLUGS)
def test_record_status_is_active(slug):
    assert _normalize(slug).record_status == "active"


# ---------------------------------------------------------------------------
# enrichment_text
# ---------------------------------------------------------------------------


def test_enrichment_text_combines_title_short_description_abstract_topics():
    payload = _payload("slpdb")
    text = physionet.enrichment_text(_envelope("slpdb"))
    assert payload["title"] in text
    assert payload["short_description"] in text
    assert io.strip_html(payload["abstract"]) in text
    for topic in payload["topics"]:
        assert topic in text
    assert "<p>" not in text and "</p>" not in text


def test_enrichment_text_is_capped_at_1500_chars():
    payload = _payload("slpdb")
    payload["abstract"] = "<p>" + "word " * 2000 + "</p>"
    text = physionet.enrichment_text(_envelope("slpdb", payload))
    assert len(text) == 1500


def test_enrichment_text_handles_missing_optional_fields():
    payload = {"title": "Bare Title", "slug": "bare"}
    text = physionet.enrichment_text(_envelope("bare", payload))
    assert text == "Bare Title"


def test_enrichment_text_never_raises_on_fully_empty_payload():
    text = physionet.enrichment_text(_envelope("empty", {}))
    assert text == ""
