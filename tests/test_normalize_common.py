"""Tests for `atlas.normalize.common` (pure helpers shared by every
source normalizer) and the `atlas.normalize` auto-discovery registry
(`atlas/normalize/__init__.py`, mirroring `atlas.harvest.get_registry`).
"""

from __future__ import annotations

import types
from datetime import date
from types import SimpleNamespace

import pytest

from atlas import normalize as normalize_pkg
from atlas import vocab
from atlas.normalize import common

# ---------------------------------------------------------------------------
# clean_doi
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1000/xyz123", "10.1000/xyz123"),
        ("DOI:10.1000/xyz123", "10.1000/xyz123"),
        ("doi: 10.1000/xyz123", "10.1000/xyz123"),
        ("https://doi.org/10.1000/xyz123", "10.1000/xyz123"),
        ("http://dx.doi.org/10.1000/XYZ123", "10.1000/xyz123"),
        ("  10.1000/xyz123  ", "10.1000/xyz123"),
        ("10.123456789/long-registrant-code", "10.123456789/long-registrant-code"),
        ("not a doi", None),
        ("10.123/too-short-registrant", None),  # only 3 digits, needs 4-9
        ("", None),
        (None, None),
    ],
)
def test_clean_doi(raw, expected):
    assert common.clean_doi(raw) == expected


# ---------------------------------------------------------------------------
# collapse_version_doi
# ---------------------------------------------------------------------------


def test_collapse_version_doi_strips_openneuro_style_suffix():
    assert (
        common.collapse_version_doi("10.18112/openneuro.ds008711.v1.0.0")
        == "10.18112/openneuro.ds008711"
    )


@pytest.mark.parametrize(
    "doi,expected",
    [
        ("10.18112/openneuro.ds000001.v2", "10.18112/openneuro.ds000001"),
        ("10.18112/openneuro.ds000001.v10.2", "10.18112/openneuro.ds000001"),
        ("10.1000/no-version-suffix", "10.1000/no-version-suffix"),
    ],
)
def test_collapse_version_doi_variants(doi, expected):
    assert common.collapse_version_doi(doi) == expected


# ---------------------------------------------------------------------------
# year_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "date_str,expected",
    [
        ("2020-05-01", 2020),
        ("2020", 2020),
        ("2020-05-01T00:00:00Z", 2020),
        ("  2019-01-01", 2019),
        ("unknown", None),
        ("", None),
        (None, None),
    ],
)
def test_year_of(date_str, expected):
    assert common.year_of(date_str) == expected


# ---------------------------------------------------------------------------
# regex_sample_size
# ---------------------------------------------------------------------------


def test_regex_sample_size_matches_number_before_unit_word():
    assert common.regex_sample_size("recordings from 18 subjects") == (
        18,
        "participants",
    )


def test_regex_sample_size_returns_none_for_no_match():
    assert common.regex_sample_size("no numbers here") is None


def test_regex_sample_size_returns_none_when_two_unit_families_present():
    # "subjects" -> participants, "recordings" -> records: two families.
    assert common.regex_sample_size("18 subjects and 200 recordings") is None


def test_regex_sample_size_returns_none_when_same_family_has_conflicting_numbers():
    # "patients" and "volunteers" are both the participants family, but
    # 50 != 45 -- ambiguous.
    text = "the cohort had 50 patients, later revised to 45 volunteers"
    assert common.regex_sample_size(text) is None


def test_regex_sample_size_allows_same_family_repeated_with_same_number():
    text = "50 patients enrolled; 50 patients completed follow-up"
    assert common.regex_sample_size(text) == (50, "participants")


def test_regex_sample_size_strips_thousands_separators():
    assert common.regex_sample_size("1,234 patients") == (1234, "participants")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("312 admissions recorded", (312, "admissions")),
        ("a set of 40 images", (40, "images")),
        ("data from 12 recordings", (12, "records")),
        ("12 records total", (12, "records")),
        ("20 PATIENTS enrolled", (20, "participants")),
        ("5 volunteers", (5, "participants")),
        ("5 individuals", (5, "participants")),
    ],
)
def test_regex_sample_size_unit_family_mapping(text, expected):
    assert common.regex_sample_size(text) == expected


# ---------------------------------------------------------------------------
# license_to_spdx
# ---------------------------------------------------------------------------


def test_license_to_spdx_exact_match():
    assert common.license_to_spdx("CC0") == "CC0-1.0"


def test_license_to_spdx_case_and_whitespace_normalized_match():
    assert common.license_to_spdx("  cc0  ") == "CC0-1.0"
    assert common.license_to_spdx("MIT   LICENSE") == "MIT"


def test_license_to_spdx_unmapped_returns_none():
    assert common.license_to_spdx("Some Bespoke License") is None


def test_license_to_spdx_empty_or_none_returns_none():
    assert common.license_to_spdx("") is None
    assert common.license_to_spdx(None) is None


# ---------------------------------------------------------------------------
# make_provenance
# ---------------------------------------------------------------------------


def test_make_provenance_swaps_first_seen_and_harvested_at():
    prov = common.make_provenance(
        via="api",
        harvested_at="2026-08-20",
        first_seen="2026-01-01",
        raw_hash="sha256:abc",
    )
    # Provenance.harvested_at is the *first-seen* date (never changes);
    # last_verified is *this run's* date -- the opposite of the parameter
    # names, per the schema's documented semantics.
    assert prov.harvested_via == "api"
    assert prov.harvested_at == date(2026, 1, 1)
    assert prov.last_verified == date(2026, 8, 20)
    assert prov.raw_hash == "sha256:abc"


def test_make_provenance_allows_no_raw_hash():
    prov = common.make_provenance(
        via="scrape", harvested_at="2026-01-01", first_seen="2026-01-01", raw_hash=None
    )
    assert prov.raw_hash is None


def test_make_provenance_enrichment_placeholder_is_schema_valid_and_empty():
    prov = common.make_provenance(
        via="api", harvested_at="2026-01-01", first_seen="2026-01-01", raw_hash=None
    )
    assert prov.enrichment.method in vocab.ENRICHMENT_METHODS
    assert prov.enrichment.fields == {}
    assert prov.enrichment.model is None
    assert prov.enrichment.at is None


def test_make_provenance_is_keyword_only():
    with pytest.raises(TypeError):
        common.make_provenance("api", "2026-01-01", "2026-01-01", None)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Excluded
# ---------------------------------------------------------------------------


def test_excluded_carries_native_id_and_reason():
    excluded = common.Excluded(native_id="d1", reason="non-human species")
    assert excluded.native_id == "d1"
    assert excluded.reason == "non-human species"
    assert excluded == common.Excluded(native_id="d1", reason="non-human species")


# ---------------------------------------------------------------------------
# atlas.normalize registry: auto-discovery (mirrors atlas.harvest.get_registry)
# ---------------------------------------------------------------------------


def test_get_normalizers_is_empty_with_no_source_modules():
    assert normalize_pkg.get_normalizers() == {}


def test_discover_registers_module_defining_both_callables_keyed_by_source_attr(
    monkeypatch,
):
    monkeypatch.setattr(normalize_pkg, "NORMALIZERS", {})

    def fake_normalize(envelope, *, harvested_at, first_seen):
        raise NotImplementedError

    def fake_enrichment_text(envelope):
        raise NotImplementedError

    fake_module = types.ModuleType("atlas.normalize.fake_source")
    fake_module.normalize = fake_normalize
    fake_module.enrichment_text = fake_enrichment_text
    fake_module.SOURCE = "fake_source"

    fake_listing = [
        SimpleNamespace(name="fake_source"),
        SimpleNamespace(name="common"),
        SimpleNamespace(name="_private"),
    ]

    def fake_import_module(name):
        assert name == "atlas.normalize.fake_source", f"must not import {name!r}"
        return fake_module

    monkeypatch.setattr(
        normalize_pkg.pkgutil, "iter_modules", lambda path: fake_listing
    )
    monkeypatch.setattr(normalize_pkg.importlib, "import_module", fake_import_module)

    registry = normalize_pkg.get_normalizers()

    assert registry == {"fake_source": (fake_normalize, fake_enrichment_text)}


def test_discover_falls_back_to_module_name_when_source_attr_missing(monkeypatch):
    monkeypatch.setattr(normalize_pkg, "NORMALIZERS", {})

    def fake_normalize(envelope, *, harvested_at, first_seen):
        raise NotImplementedError

    def fake_enrichment_text(envelope):
        raise NotImplementedError

    fake_module = types.ModuleType("atlas.normalize.no_source_attr")
    fake_module.normalize = fake_normalize
    fake_module.enrichment_text = fake_enrichment_text
    # deliberately no SOURCE attribute

    monkeypatch.setattr(
        normalize_pkg.pkgutil,
        "iter_modules",
        lambda path: [SimpleNamespace(name="no_source_attr")],
    )
    monkeypatch.setattr(
        normalize_pkg.importlib, "import_module", lambda name: fake_module
    )

    registry = normalize_pkg.get_normalizers()

    assert registry == {"no_source_attr": (fake_normalize, fake_enrichment_text)}


def test_module_missing_one_callable_is_not_registered(monkeypatch):
    monkeypatch.setattr(normalize_pkg, "NORMALIZERS", {})

    fake_module = types.ModuleType("atlas.normalize.half_defined")
    fake_module.normalize = lambda envelope, **kw: None  # enrichment_text missing
    fake_module.SOURCE = "half_defined"

    monkeypatch.setattr(
        normalize_pkg.pkgutil,
        "iter_modules",
        lambda path: [SimpleNamespace(name="half_defined")],
    )
    monkeypatch.setattr(
        normalize_pkg.importlib, "import_module", lambda name: fake_module
    )

    registry = normalize_pkg.get_normalizers()

    assert registry == {}


def test_get_normalizers_caches_result_across_calls_once_non_empty(monkeypatch):
    monkeypatch.setattr(normalize_pkg, "NORMALIZERS", {})

    def fake_normalize(envelope, *, harvested_at, first_seen):
        raise NotImplementedError

    def fake_enrichment_text(envelope):
        raise NotImplementedError

    fake_module = types.ModuleType("atlas.normalize.cached_source")
    fake_module.normalize = fake_normalize
    fake_module.enrichment_text = fake_enrichment_text
    fake_module.SOURCE = "cached_source"

    call_count = 0

    def fake_import_module(name):
        nonlocal call_count
        call_count += 1
        return fake_module

    monkeypatch.setattr(
        normalize_pkg.pkgutil,
        "iter_modules",
        lambda path: [SimpleNamespace(name="cached_source")],
    )
    monkeypatch.setattr(normalize_pkg.importlib, "import_module", fake_import_module)

    first = normalize_pkg.get_normalizers()
    second = normalize_pkg.get_normalizers()

    assert first == second == {"cached_source": (fake_normalize, fake_enrichment_text)}
    assert call_count == 1  # discovery only ran once; second call served from cache
