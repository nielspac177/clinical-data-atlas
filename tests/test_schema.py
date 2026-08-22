"""Tests for the frozen schema v1: atlas/vocab.py + atlas/schema.py.

Also covers the ``atlas schema`` CLI (export/check/plain) since "schema
must work fully" is part of this task's contract, not just the model.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from atlas import cli, schema, vocab


def _minimal_record(**overrides: object) -> dict:
    """A minimal record dict that validates as-is; override fields as needed."""
    record: dict = {
        "id": "openneuro:ds000001",
        "source": "openneuro",
        "source_native_id": "ds000001",
        "name": "Example dataset",
        "summary": "A short example dataset summary used across the schema test suite.",
        "url": "https://openneuro.org/datasets/ds000001",
        "species": "human",
        "sample_size": None,
        "sample_unit": None,
        "countries": ["US"],
        "years": {"start": 2015, "end": 2018},
        "access": "open",
        "record_status": "active",
        "provenance": {
            "harvested_via": "graphql",
            "harvested_at": "2026-08-01",
            "last_verified": "2026-08-22",
            "enrichment": {"method": "rules", "fields": {}},
        },
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# Record: minimal valid record + extra="forbid"
# --------------------------------------------------------------------------


def test_minimal_valid_record_validates():
    record = schema.Record.model_validate(_minimal_record())
    assert record.id == "openneuro:ds000001"
    assert record.domains == []
    assert record.modalities == []
    assert record.years.start == 2015
    assert record.provenance.enrichment.method == "rules"


def test_extra_field_is_forbidden():
    with pytest.raises(ValidationError):
        schema.Record.model_validate(_minimal_record(bogus_field="nope"))


# --------------------------------------------------------------------------
# summary: <=40 words enforced
# --------------------------------------------------------------------------


def test_summary_of_40_words_is_valid():
    summary = " ".join(["word"] * 40)
    record = schema.Record.model_validate(_minimal_record(summary=summary))
    assert schema.word_count(record.summary) == 40


def test_summary_of_41_words_is_error():
    summary = " ".join(["word"] * 41)
    with pytest.raises(ValidationError):
        schema.Record.model_validate(_minimal_record(summary=summary))


# --------------------------------------------------------------------------
# id: format + must start with "<source>:"
# --------------------------------------------------------------------------


def test_id_not_matching_source_prefix_is_error():
    # Syntactically valid id (matches the general pattern) but for the
    # wrong source: source stays "openneuro", id claims "physionet:".
    with pytest.raises(ValidationError):
        schema.Record.model_validate(_minimal_record(id="physionet:mimic-iv"))


def test_id_bad_format_is_error():
    with pytest.raises(ValidationError):
        schema.Record.model_validate(_minimal_record(id="OpenNeuro ds000001"))


# --------------------------------------------------------------------------
# countries: ISO-3166-1 alpha-2
# --------------------------------------------------------------------------


def test_countries_lowercase_is_error():
    with pytest.raises(ValidationError):
        schema.Record.model_validate(_minimal_record(countries=["usa"]))


def test_countries_valid_alpha2_passes():
    record = schema.Record.model_validate(_minimal_record(countries=["US", "PE"]))
    assert record.countries == ["US", "PE"]


# --------------------------------------------------------------------------
# url: must start with http(s)
# --------------------------------------------------------------------------


def test_url_without_http_scheme_is_error():
    with pytest.raises(ValidationError):
        schema.Record.model_validate(_minimal_record(url="ftp://example.com/data"))


# --------------------------------------------------------------------------
# required strings must be non-empty (min_length=1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["id", "name", "summary", "url", "source_native_id"])
def test_required_string_fields_reject_empty_string(field):
    with pytest.raises(ValidationError):
        schema.Record.model_validate(_minimal_record(**{field: ""}))


# --------------------------------------------------------------------------
# sample_size / sample_unit / years: required (nullable), not defaulted
# --------------------------------------------------------------------------


def test_sample_size_is_required():
    record = _minimal_record()
    del record["sample_size"]
    with pytest.raises(ValidationError):
        schema.Record.model_validate(record)


def test_sample_unit_is_required():
    record = _minimal_record()
    del record["sample_unit"]
    with pytest.raises(ValidationError):
        schema.Record.model_validate(record)


def test_years_is_required():
    record = _minimal_record()
    del record["years"]
    with pytest.raises(ValidationError):
        schema.Record.model_validate(record)


def test_sample_size_and_sample_unit_may_be_explicitly_null():
    record = schema.Record.model_validate(
        _minimal_record(sample_size=None, sample_unit=None)
    )
    assert record.sample_size is None
    assert record.sample_unit is None


def test_years_start_and_end_default_to_none_when_omitted():
    record = schema.Record.model_validate(_minimal_record(years={}))
    assert record.years.start is None
    assert record.years.end is None


# --------------------------------------------------------------------------
# Institution.country: same ISO-3166-1 alpha-2 constraint as Record.countries
# --------------------------------------------------------------------------


def test_institution_country_lowercase_is_error():
    with pytest.raises(ValidationError):
        schema.Institution.model_validate({"name": "Example U.", "country": "usa"})


def test_institution_country_valid_alpha2_passes():
    inst = schema.Institution.model_validate({"name": "Example U.", "country": "US"})
    assert inst.country == "US"


# --------------------------------------------------------------------------
# validate_records: duplicate ids, dangling related
# --------------------------------------------------------------------------


def test_duplicate_ids_is_error():
    errors, _warnings = schema.validate_records([_minimal_record(), _minimal_record()])
    assert any("duplicate id" in e for e in errors)


def test_dangling_related_id_is_error():
    record = _minimal_record(
        related=[{"id": "openneuro:doesnotexist", "relation": "same_cohort"}]
    )
    errors, _warnings = schema.validate_records([record])
    assert any("does not match any record" in e for e in errors)


def test_related_id_present_among_records_is_not_dangling():
    rec1 = _minimal_record(
        id="openneuro:ds000001",
        related=[{"id": "openneuro:ds000002", "relation": "same_cohort"}],
    )
    rec2 = _minimal_record(id="openneuro:ds000002")
    errors, _warnings = schema.validate_records([rec1, rec2])
    assert errors == []


def test_related_id_pointing_to_a_record_that_failed_validation_is_dangling():
    # A references B, but B itself fails validation (bad url) — B never
    # becomes a known id, so A's reference to it must be reported dangling
    # too, not silently accepted just because *some* dict with that id was
    # present in the input batch.
    rec_a = _minimal_record(
        id="openneuro:ds000001",
        related=[{"id": "openneuro:ds000002", "relation": "same_cohort"}],
    )
    rec_b_invalid = _minimal_record(id="openneuro:ds000002", url="not-a-url")
    errors, _warnings = schema.validate_records([rec_a, rec_b_invalid])
    assert any(
        "openneuro:ds000001" in e and "does not match any record" in e for e in errors
    )


def test_validate_records_uses_index_when_no_usable_id():
    errors, _warnings = schema.validate_records([{}])
    assert errors
    assert any("index 0" in e for e in errors)


# --------------------------------------------------------------------------
# validate_records: warnings
# --------------------------------------------------------------------------


def test_access_open_without_notes_has_no_warning():
    errors, warnings = schema.validate_records([_minimal_record(access="open")])
    assert errors == []
    assert not any("access_notes" in w for w in warnings)


def test_access_credentialed_without_notes_warns():
    errors, warnings = schema.validate_records([_minimal_record(access="credentialed")])
    assert errors == []
    assert any("access_notes" in w for w in warnings)


def test_access_credentialed_with_notes_has_no_warning():
    record = _minimal_record(
        access="credentialed", access_notes="Requires PhysioNet credentialing."
    )
    errors, warnings = schema.validate_records([record])
    assert errors == []
    assert not any("access_notes" in w for w in warnings)


def test_sample_size_without_unit_warns():
    record = _minimal_record(sample_size=42, sample_unit=None)
    errors, warnings = schema.validate_records([record])
    assert errors == []
    assert any("sample_unit" in w for w in warnings)


def test_sample_size_with_unit_has_no_warning():
    record = _minimal_record(sample_size=42, sample_unit="participants")
    _errors, warnings = schema.validate_records([record])
    assert not any("sample_unit" in w for w in warnings)


def test_empty_domains_and_modalities_warn():
    _errors, warnings = schema.validate_records([_minimal_record()])
    assert any("domains" in w for w in warnings)
    assert any("modalities" in w for w in warnings)


def test_populated_domains_and_modalities_have_no_warning():
    record = _minimal_record(domains=["neurology"], modalities=["MRI"])
    _errors, warnings = schema.validate_records([record])
    assert not any("domains" in w for w in warnings)
    assert not any("modalities" in w for w in warnings)


# --------------------------------------------------------------------------
# word_count
# --------------------------------------------------------------------------


def test_word_count_splits_on_whitespace():
    assert schema.word_count("one two three") == 3
    assert schema.word_count("  extra   whitespace  ") == 2
    assert schema.word_count("") == 0


# --------------------------------------------------------------------------
# vocab: frozen counts/order (17 domains, 33 modalities, 5 ordered tiers, ...)
# --------------------------------------------------------------------------


def test_vocab_domains_has_17_unique_entries():
    assert len(vocab.DOMAINS) == 17
    assert len(set(vocab.DOMAINS)) == 17


def test_vocab_modalities_has_33_unique_entries():
    assert len(vocab.MODALITIES) == 33
    assert len(set(vocab.MODALITIES)) == 33


def test_vocab_access_order_is_frozen_and_ordered():
    assert vocab.ACCESS_ORDER == (
        "open",
        "registration",
        "credentialed",
        "application",
        "purchase",
    )


def test_vocab_species_and_sample_units():
    assert set(vocab.SPECIES) == {
        "human",
        "animal",
        "mixed",
        "phantom",
        "simulated",
        "unknown",
    }
    assert len(vocab.SAMPLE_UNITS) == 10
    assert len(set(vocab.SAMPLE_UNITS)) == 10


def test_vocab_license_map_spot_checks():
    assert vocab.LICENSE_MAP["MIT License"] == "MIT"
    assert vocab.LICENSE_MAP["cc-by-4.0"] == "CC-BY-4.0"
    assert vocab.LICENSE_MAP["CC0"] == "CC0-1.0"
    assert (
        vocab.LICENSE_MAP[
            "Creative Commons Zero 1.0 Universal Public Domain Dedication"
        ]
        == "CC0-1.0"
    )
    assert vocab.LICENSE_MAP["GNU General Public License version 3"] == "GPL-3.0-only"


def test_vocab_condition_aliases_spot_checks():
    assert vocab.CONDITION_ALIASES["parkinson's disease"] == "parkinson disease"
    assert vocab.CONDITION_ALIASES["covid"] == "covid-19"
    assert vocab.CONDITION_ALIASES["breast cancer"] == "breast neoplasms"
    assert vocab.CONDITION_ALIASES["osa"] == "sleep apnea, obstructive"
    assert vocab.CONDITION_ALIASES["multiple sclerosis"] == "multiple sclerosis"


def test_vocab_condition_aliases_excludes_ambiguous_abbreviations():
    # "asd"/"pd"/"ms" have competing, non-neuro/psych expansions (atrial
    # septal defect, peritoneal dialysis/panic disorder, mitral stenosis)
    # and must not be guessed at; the unambiguous abbreviations stay.
    for ambiguous in ("asd", "pd", "ms"):
        assert ambiguous not in vocab.CONDITION_ALIASES
    for unambiguous in ("afib", "tbi", "mdd", "chf", "osa", "adhd"):
        assert unambiguous in vocab.CONDITION_ALIASES


# --------------------------------------------------------------------------
# export_json_schema
# --------------------------------------------------------------------------


def test_export_json_schema_title_is_record():
    assert schema.export_json_schema()["title"] == "Record"


def test_export_json_schema_required_fields():
    required = set(schema.export_json_schema()["required"])
    assert required == {
        "id",
        "source",
        "source_native_id",
        "name",
        "summary",
        "url",
        "species",
        "sample_size",
        "sample_unit",
        "years",
        "access",
        "record_status",
        "provenance",
    }


# --------------------------------------------------------------------------
# Docs drift: docs/schema.json / docs/schema.md must match the model
# --------------------------------------------------------------------------


def test_docs_schema_json_matches_export():
    # Byte-level text comparison (not just parsed-JSON equality) so this
    # also catches formatting drift (indent, key order, trailing newline) —
    # the same exact text `atlas schema --export` would write.
    on_disk = schema.SCHEMA_JSON_PATH.read_text(encoding="utf-8")
    assert on_disk == schema.export_json_schema_text()


def test_docs_schema_md_matches_render():
    assert schema.SCHEMA_MD_PATH.read_text(encoding="utf-8") == schema.render_markdown()


# --------------------------------------------------------------------------
# CLI: atlas schema [--export|--check]
# --------------------------------------------------------------------------


def test_cli_schema_plain_prints_json_schema_to_stdout(capsys):
    assert cli.main(["schema"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["title"] == "Record"


def test_cli_schema_export_writes_both_files(tmp_path, monkeypatch):
    json_path = tmp_path / "schema.json"
    md_path = tmp_path / "schema.md"
    monkeypatch.setattr(schema, "SCHEMA_JSON_PATH", json_path)
    monkeypatch.setattr(schema, "SCHEMA_MD_PATH", md_path)

    assert cli.main(["schema", "--export"]) == 0

    assert json_path.read_text(encoding="utf-8") == schema.export_json_schema_text()
    assert md_path.read_text(encoding="utf-8") == schema.render_markdown()


def test_cli_schema_check_passes_when_in_sync(tmp_path, monkeypatch):
    json_path = tmp_path / "schema.json"
    md_path = tmp_path / "schema.md"
    monkeypatch.setattr(schema, "SCHEMA_JSON_PATH", json_path)
    monkeypatch.setattr(schema, "SCHEMA_MD_PATH", md_path)

    cli.main(["schema", "--export"])

    assert cli.main(["schema", "--check"]) == 0


def test_cli_schema_check_detects_drift(tmp_path, monkeypatch):
    json_path = tmp_path / "schema.json"
    md_path = tmp_path / "schema.md"
    json_path.write_text("{}", encoding="utf-8")
    md_path.write_text("stale content", encoding="utf-8")
    monkeypatch.setattr(schema, "SCHEMA_JSON_PATH", json_path)
    monkeypatch.setattr(schema, "SCHEMA_MD_PATH", md_path)

    assert cli.main(["schema", "--check"]) == 1


def test_cli_no_args_prints_help_and_exits_zero(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "schema" in out
