"""Tests for `atlas.enrich.rules`: the deterministic term-table hints
engine (`hints()`, `RuleHits`) and its word-boundary regex helper (`_rx`).
"""

from __future__ import annotations

import dataclasses

import pytest

from atlas import vocab
from atlas.enrich import rules
from atlas.enrich.rules import RuleHits, hints

# ---------------------------------------------------------------------------
# _rx: word-boundary regex helper -- stemming is explicit (trailing `*`),
# never inferred from a term's own spelling (Ruling R14).
# ---------------------------------------------------------------------------


def test_rx_matches_whole_word_case_insensitively():
    assert rules._rx("ecg").search("a 12-lead ECG recording")
    assert rules._rx("ecg").search("a 12-lead ecg recording")


def test_rx_explicit_stem_matches_longer_inflections():
    # A trailing "*" is the only thing that makes a term a prefix stem.
    rx = rules._rx("histopatholog*")
    assert rx.search("histopathology review")
    assert rx.search("histopathological grading")
    assert rx.search("histopathologic subtype")


def test_rx_without_trailing_star_requires_a_whole_word():
    # No "*" -> both boundaries, matches only the complete word.
    rx = rules._rx("cardiac")
    assert rx.search("cardiac arrest")
    assert not rx.search("cardiacyzer")  # made-up continuation, must not match


def test_rx_strips_only_the_trailing_star_not_the_whole_term():
    rx = rules._rx("diabet*")
    assert rx.search("diabetes")
    assert rx.search("diabetic")
    assert not rx.search("diab")  # body is "diabet", not a shorter prefix


def test_rx_does_not_match_substring_across_a_word_boundary():
    # The two false-positive traps this helper exists to fix.
    assert not rules._rx("pet").search("a machine learning competition")


def test_rx_multi_word_term_matches_across_whitespace():
    assert rules._rx("atrial fibrillation").search("chronic Atrial   Fibrillation case")


def test_rx_is_cached():
    first = rules._rx("epilep*")
    second = rules._rx("epilep*")
    assert first is second


def test_rx_every_table_term_has_at_most_one_trailing_star():
    # A "*" anywhere but the final character is a silent no-op bug: it
    # would be escaped as a literal asterisk that never appears in real
    # text, so the whole entry could never match anything.
    for table in (rules.MODALITY_TERMS, rules.DOMAIN_TERMS, rules.CONDITION_TERMS):
        for term in table:
            if "*" in term:
                assert term.endswith("*"), f"{term!r} has a non-trailing '*'"
                assert term.count("*") == 1, f"{term!r} has more than one '*'"
    for term in (*rules.ANIMAL_TERMS, *rules.HUMAN_TERMS):
        if "*" in term:
            assert term.endswith("*"), f"{term!r} has a non-trailing '*'"


def test_no_stem_body_is_shorter_than_six_characters():
    # A short stem body is the class of bug fix round 2 found twice
    # ("metasta*" matched "metastable", "infant*" matched "infantry"): the
    # shorter the body, the more likely it accidentally prefixes some
    # unrelated English word. Every stem must be >= 6 chars unless the
    # exact term is in rules._SHORT_STEM_ALLOWLIST (empty right now -- see
    # that constant's comment for the policy on adding to it).
    offenders = []
    for table in (rules.MODALITY_TERMS, rules.DOMAIN_TERMS, rules.CONDITION_TERMS):
        for term in table:
            if term.endswith("*") and term not in rules._SHORT_STEM_ALLOWLIST:
                body = term[:-1]
                if len(body) < rules._MIN_STEM_BODY_LENGTH:
                    offenders.append(term)
    for term in rules.ANIMAL_TERMS:
        if term.endswith("*") and term not in rules._SHORT_STEM_ALLOWLIST:
            body = term[:-1]
            if len(body) < rules._MIN_STEM_BODY_LENGTH:
                offenders.append(term)
    assert offenders == []


def test_require_raises_on_a_deliberately_short_stem_body():
    with pytest.raises(RuntimeError):
        rules._require(len("ab") >= rules._MIN_STEM_BODY_LENGTH, "too short")


# ---------------------------------------------------------------------------
# Fix round 2: two Important false positives introduced by round-1 stem
# consolidation (a stem body safe-looking in isolation still needs a real
# audit against plausible unrelated English words, not just a length check).
# ---------------------------------------------------------------------------


def test_infantry_text_is_not_flagged_as_pediatrics():
    # "infant*" matched "infantry"/"infantryman"/"infanta" (no trailing
    # boundary). Now replaced with exact whole-word forms.
    result = hints(
        "Combat exposure among infantry veterans and PTSD outcomes",
        source="curated",
    )
    assert "pediatrics" not in result.domains


def test_infant_infants_infancy_still_match_pediatrics():
    for text in (
        "an infant born preterm",
        "a cohort of infants",
        "outcomes in early infancy",
    ):
        assert "pediatrics" in hints(text, source="curated").domains


def test_infant_is_a_whole_word_term_not_a_stem():
    assert "infant" in rules.DOMAIN_TERMS
    assert "infant*" not in rules.DOMAIN_TERMS


def test_metastable_alloy_text_is_not_flagged_as_oncology():
    # "metasta*" matched "metastable"/"metastability"/"metastannate" (no
    # trailing boundary). Now split at the real metastasis/metastatic
    # divergence point, both bodies well clear of "metastable".
    result = hints("A metastable phase was observed in the alloy", source="curated")
    assert "oncology" not in result.domains


def test_metastasis_family_still_matches_oncology():
    for text in (
        "widespread metastasis was observed",
        "multiple metastases were found",
        "a metastatic tumor",
        "the cancer began to metastasize",
    ):
        assert "oncology" in hints(text, source="curated").domains


def test_metasta_stem_no_longer_exists():
    assert "metasta*" not in rules.DOMAIN_TERMS
    assert "metastas*" in rules.DOMAIN_TERMS
    assert "metastat*" in rules.DOMAIN_TERMS


def test_genome_genomic_split_still_covers_both_families():
    for text in ("genome sequencing data", "genomic analysis", "genomics pipeline"):
        assert "genomics" in hints(text, source="curated").modalities


def test_autism_autistic_split_still_covers_both_tables():
    result = hints("autistic children with autism spectrum disorder", source="curated")
    assert "psychiatry" in result.domains
    assert "autism spectrum disorder" in result.conditions


def test_hepato_split_covers_liver_terms_including_hepato_compounds():
    for text in (
        "acute hepatitis diagnosis",
        "hepatic function tests",
        "hepatorenal syndrome",
        "hepatobiliary imaging",
        "hepatotoxicity screening",
    ):
        assert "gastroenterology_hepatology" in hints(text, source="curated").domains


# ---------------------------------------------------------------------------
# CRITICAL fix regression: bare "rat" silently matched as a prefix (no
# trailing boundary) because it happened to end in "at" under the old
# inferred-stemming rule, so "heart rate" was flagged as an animal study.
# ---------------------------------------------------------------------------


def test_heart_rate_is_not_flagged_as_animal():
    result = hints(
        "Heart rate variability recordings during overnight sleep monitoring.",
        source="curated",
    )
    assert result.species is None


def test_mortality_and_response_rate_are_not_flagged_as_animal():
    result = hints("Mortality rate and response rate across cohorts.", source="curated")
    assert result.species is None


def test_accurate_is_not_flagged_as_animal():
    result = hints("This is a highly accurate classifier.", source="curated")
    assert result.species is None


def test_rats_plural_is_flagged_as_animal():
    result = hints("Neural recordings from rats in a maze task.", source="curated")
    assert result.species == "animal"


def test_rat_model_is_flagged_as_animal():
    result = hints("A rat model of traumatic brain injury.", source="curated")
    assert result.species == "animal"


def test_bare_rat_and_bare_mouse_are_not_in_animal_terms():
    # Ruling R14: both were removed outright, not just re-guarded, since
    # each is common enough as a standalone word to need a qualifier.
    assert "rat" not in rules.ANIMAL_TERMS
    assert "mouse" not in rules.ANIMAL_TERMS


# ---------------------------------------------------------------------------
# hints(): required scenarios from the original task brief
# ---------------------------------------------------------------------------


def test_ecg_icu_text_hits_ecg_modality_and_two_domains():
    result = hints("12-lead ECG recordings from ICU patients", source="curated")
    assert "ECG" in result.modalities
    assert {"cardiology", "critical_care"} <= set(result.domains)


def test_brats_glioma_mri_hits_condition_and_modality_with_no_animal_flag():
    result = hints("BraTS glioma MRI", source="curated")
    assert "glioma" in result.conditions
    assert "MRI" in result.modalities
    assert result.species is None  # "rats" must not be found inside "BraTS"


def test_pet_modality_does_not_match_inside_competition():
    result = hints("a machine learning competition", source="curated")
    assert "PET" not in result.modalities


def test_rat_and_human_cortex_has_no_species_opinion():
    result = hints("rat and human cortex", source="curated")
    assert result.species is None


def test_animal_signal_is_cancelled_by_a_real_human_match():
    # A term that genuinely exists in ANIMAL_TERMS ("rat brain"), so this
    # exercises the human-cancels-animal branch, not just "nothing matched".
    result = hints(
        "recordings from a rat brain alongside human control subjects",
        source="curated",
    )
    assert result.species is None
    assert "rat brain" in result.evidence["species"]
    assert "human" in result.evidence["species"]


def test_no_animal_or_human_terms_has_no_species_opinion():
    result = hints("a dataset of brain MRI scans", source="curated")
    assert result.species is None


# ---------------------------------------------------------------------------
# IMPORTANT #2: ambiguous single-word keys replaced with unambiguous
# compound terms (ecog, mimic, pet, bold, claims).
# ---------------------------------------------------------------------------


def test_bare_ecog_alone_no_longer_matches():
    result = hints("Intracranial recordings using ECoG electrodes.", source="curated")
    assert result.modalities == []


def test_ecog_recording_and_grid_still_match_ieeg():
    assert (
        "iEEG" in hints("an ECoG recording from the grid", source="curated").modalities
    )
    assert "iEEG" in hints("placement of the ECoG grid", source="curated").modalities


def test_electrocorticography_stem_matches_ieeg():
    result = hints("electrocorticography of the temporal lobe", source="curated")
    assert "iEEG" in result.modalities


def test_mimic_database_variants_match_ehr():
    for text in (
        "the MIMIC-III database",
        "data from MIMIC-IV",
        "the MIMIC-CXR dataset",
    ):
        assert "EHR" in hints(text, source="curated").modalities


def test_bare_mimic_no_longer_matches_mimicry():
    result = hints("A study of butterfly mimicry.", source="curated")
    assert "EHR" not in result.modalities


def test_pet_imaging_terms_match_pet_modality():
    for text in (
        "a PET imaging study",
        "an FDG-PET tracer study",
        "combined PET/CT acquisition",
        "amyloid PET in early diagnosis",
    ):
        assert "PET" in hints(text, source="curated").modalities


def test_bare_pet_no_longer_matches_anything():
    assert hints("a PET study", source="curated").modalities == []
    assert hints("a machine learning competition", source="curated").modalities == []


def test_bold_fmri_terms_match_fmri_modality():
    assert "fMRI" in hints("BOLD fMRI signal analysis", source="curated").modalities
    assert "fMRI" in hints("the BOLD contrast is measured", source="curated").modalities


def test_bare_bold_as_plain_english_does_not_match_fmri():
    result = hints("the bold text below explains the method", source="curated")
    assert "fMRI" not in result.modalities


def test_claims_compound_terms_match_claims_modality():
    result = hints("insurance claims data used for cost analysis", source="curated")
    assert "claims" in result.modalities


def test_bare_claims_word_alone_no_longer_matches():
    assert hints("the paper claims a new result", source="curated").modalities == []


# ---------------------------------------------------------------------------
# IMPORTANT #2: animal terms replaced with qualified mouse/rat phrases.
# ---------------------------------------------------------------------------


def test_qualified_mouse_terms_are_flagged_as_animal():
    for text in (
        "mice injected with a viral vector",
        "a knockout mouse model of the disease",
        "recordings from mouse cortex",
        "a transgenic mouse line",
    ):
        assert hints(text, source="curated").species == "animal"


def test_bare_mouse_word_alone_does_not_flag_animal():
    # No qualifying context and no other animal term present.
    result = hints("a mouse click was recorded for each trial", source="curated")
    assert result.species is None


def test_named_rat_strains_are_flagged_as_animal():
    for text in ("sprague-dawley rats", "a wistar rat colony", "long-evans rats"):
        assert hints(text, source="curated").species == "animal"


def test_rodent_stem_matches_rodent_and_rodents():
    assert hints("a rodent model of disease", source="curated").species == "animal"
    assert hints("several rodents were studied", source="curated").species == "animal"


# ---------------------------------------------------------------------------
# IMPORTANT #3: neuroscience bare words dropped; dose -> radiotherapy
# tightened.
# ---------------------------------------------------------------------------


def test_bare_neuroscience_words_are_no_longer_domain_terms():
    for term in ("perception", "language", "attention"):
        assert term not in rules.DOMAIN_TERMS


def test_qualified_neuroscience_phrases_still_match():
    for text in (
        "a working memory task",
        "a study of visual perception",
        "speech perception in noise",
        "language comprehension deficits",
        "an attention task battery",
    ):
        assert "neuroscience" in hints(text, source="curated").domains


def test_unrelated_language_mention_does_not_trigger_neuroscience():
    result = hints(
        "a natural language processing model for text classification",
        source="curated",
    )
    assert "neuroscience" not in result.domains


def test_bare_dose_is_no_longer_a_radiotherapy_term():
    assert "dose" not in rules.MODALITY_TERMS


def test_drug_dose_does_not_trigger_radiotherapy():
    result = hints("the recommended dose is 10mg twice daily", source="curated")
    assert "radiotherapy" not in result.modalities


def test_radiotherapy_dose_terms_still_match():
    for text in (
        "radiation dose distribution for treatment planning",
        "dosimetry of the treatment plan",
        "the dose-volume histogram",
    ):
        assert "radiotherapy" in hints(text, source="curated").modalities


# ---------------------------------------------------------------------------
# Ordering and dedup: domains/modalities in vocab order, conditions sorted
# ---------------------------------------------------------------------------


def test_domains_are_deduped_and_returned_in_vocab_order():
    result = hints(
        "public health survey covering asthma and stroke patients",
        source="curated",
    )
    assert result.domains == ["neurology", "pulmonology", "public_health"]
    assert [vocab.DOMAINS.index(d) for d in result.domains] == sorted(
        vocab.DOMAINS.index(d) for d in result.domains
    )


def test_modalities_are_deduped_when_two_terms_hit_the_same_value():
    result = hints("mri and magnetic resonance imaging study", source="curated")
    assert result.modalities == ["MRI"]
    assert set(result.evidence["MRI"]) == {"mri", "magnetic resonance"}


def test_conditions_are_deduped_and_alphabetically_sorted():
    result = hints(
        "stroke and epilepsy and glioma in the same cohort", source="curated"
    )
    assert result.conditions == ["epilepsy", "glioma", "stroke"]


# ---------------------------------------------------------------------------
# Evidence: populated with the literal matched substring(s), including the
# dedicated "species" key.
# ---------------------------------------------------------------------------


def test_evidence_is_populated_with_matched_substrings():
    result = hints("12-lead ECG recordings from ICU patients", source="curated")
    assert result.evidence["ECG"] == ["ECG"]
    assert result.evidence["cardiology"] == ["ECG"]
    assert result.evidence["critical_care"] == ["ICU"]


def test_evidence_has_no_entry_for_unmatched_values():
    result = hints("a machine learning competition", source="curated")
    assert result.evidence == {}


def test_evidence_key_order_matches_output_list_order():
    result = hints(
        "public health survey covering asthma and stroke patients",
        source="curated",
    )
    assert list(result.evidence.keys())[: len(result.domains)] == result.domains


def test_species_evidence_present_when_animal_matches():
    result = hints("rat model of disease", source="curated")
    assert result.species == "animal"
    assert result.evidence["species"] == ["rat model"]


def test_species_evidence_absent_when_nothing_matches():
    result = hints("a machine learning competition", source="curated")
    assert "species" not in result.evidence


def test_species_evidence_present_even_when_cancelled_by_human():
    result = hints("rat brain and human cortex", source="curated")
    assert result.species is None
    assert set(result.evidence["species"]) == {"rat brain", "human"}


def test_species_evidence_is_the_last_key_in_ordered_evidence():
    result = hints(
        "12-lead ECG recordings from a rat model in ICU patients", source="curated"
    )
    assert list(result.evidence.keys())[-1] == "species"


# ---------------------------------------------------------------------------
# raw_hints["keywords"]: folded into text for the ordinary term scan
# ---------------------------------------------------------------------------


def test_keywords_are_folded_into_the_text_scan():
    result = hints(
        "A recordings archive.", source="curated", raw_hints={"keywords": ["ECG"]}
    )
    assert "ECG" in result.modalities


def test_missing_raw_hints_defaults_cleanly():
    result = hints("a dataset", source="curated")
    assert isinstance(result, RuleHits)


def test_raw_hints_without_keywords_key_does_not_crash():
    result = hints("a dataset", source="curated", raw_hints={})
    assert result.modalities == []


def test_empty_text_returns_an_empty_but_valid_result():
    result = hints("", source="curated")
    assert result == RuleHits(
        domains=[], modalities=[], conditions=[], species=None, evidence={}
    )


# ---------------------------------------------------------------------------
# PHYSIONET_TOPICS: exact keyword match, gated to source == "physionet"
# ---------------------------------------------------------------------------


def test_physionet_topic_keyword_hits_its_mapped_modality():
    result = hints(
        "A recordings archive.",
        source="physionet",
        raw_hints={"keywords": ["neuroelectric and myoelectric"]},
    )
    assert result.modalities == ["EEG"]
    assert result.evidence["EEG"] == ["neuroelectric and myoelectric"]


def test_physionet_topic_keyword_hits_its_mapped_domain():
    result = hints(
        "A recordings archive.",
        source="physionet",
        raw_hints={"keywords": ["intensive care unit"]},
    )
    assert "critical_care" in result.domains


def test_physionet_topic_lookup_is_case_insensitive_on_the_keyword():
    result = hints(
        "A recordings archive.",
        source="physionet",
        raw_hints={"keywords": ["ECG"]},  # table key is lowercase "ecg"
    )
    assert "ECG" in result.modalities


def test_physionet_topics_are_gated_to_physionet_source():
    result = hints(
        "A recordings archive.",
        source="openneuro",
        raw_hints={"keywords": ["neuroelectric and myoelectric"]},
    )
    assert result.modalities == []
    assert result.evidence == {}


# ---------------------------------------------------------------------------
# BODYPART_HINTS: gated to source == "tcia" AND a cancer-mention term
# ---------------------------------------------------------------------------


def test_bodypart_hint_fires_with_tcia_source_and_cancer_mention():
    result = hints(
        "A collection of CT scans for tumor research.",
        source="tcia",
        raw_hints={"keywords": ["LUNG"]},
    )
    assert result.conditions == ["lung neoplasms"]
    assert result.evidence["lung neoplasms"] == ["LUNG"]


def test_bodypart_hint_is_blocked_without_a_cancer_mention():
    result = hints(
        "A collection of chest CT scans.",
        source="tcia",
        raw_hints={"keywords": ["LUNG"]},
    )
    assert "lung neoplasms" not in result.conditions


def test_bodypart_hint_is_gated_to_tcia_source():
    result = hints(
        "A collection of CT scans for tumor research.",
        source="openneuro",
        raw_hints={"keywords": ["LUNG"]},
    )
    assert "lung neoplasms" not in result.conditions


def test_bodypart_hint_lookup_is_case_insensitive_on_the_keyword():
    result = hints("tumor case", source="tcia", raw_hints={"keywords": ["lung"]})
    assert result.conditions == ["lung neoplasms"]


def test_headneck_variants_map_to_the_same_condition_label():
    for bodypart in ("HEAD AND NECK", "HEADNECK"):
        result = hints("tumor case", source="tcia", raw_hints={"keywords": [bodypart]})
        assert result.conditions == ["head and neck neoplasms"]


# ---------------------------------------------------------------------------
# Every table target is a real vocab value (or, for conditions, part of
# this module's own canonical label set) -- enumerated explicitly here in
# addition to the import-time checks in rules.py itself.
# ---------------------------------------------------------------------------


def test_every_modality_terms_target_is_in_vocab():
    for term, value in rules.MODALITY_TERMS.items():
        assert value in vocab.MODALITIES, (
            f"{term!r} -> {value!r} not in vocab.MODALITIES"
        )


def test_every_domain_terms_target_is_in_vocab():
    for term, value in rules.DOMAIN_TERMS.items():
        assert value in vocab.DOMAINS, f"{term!r} -> {value!r} not in vocab.DOMAINS"


def test_every_physionet_topic_target_is_in_the_right_vocab():
    for topic, (kind, value) in rules.PHYSIONET_TOPICS.items():
        if kind == "modality":
            assert value in vocab.MODALITIES, f"{topic!r} -> {value!r} not a modality"
        else:
            assert kind == "domain"
            assert value in vocab.DOMAINS, f"{topic!r} -> {value!r} not a domain"


def test_every_bodypart_hint_target_is_in_condition_labels():
    for bodypart, label in rules.BODYPART_HINTS.items():
        assert label in rules.CONDITION_LABELS, (
            f"{bodypart!r} -> {label!r} not canonical"
        )


def test_condition_terms_covers_at_least_120_canonical_labels():
    assert len(rules.CONDITION_LABELS) >= 120


def test_every_condition_alias_value_is_a_condition_terms_label():
    # The FULL set, not a sample: every canonical label vocab.py's
    # CONDITION_ALIASES ever resolves to must be producible by this
    # module's own CONDITION_TERMS, byte-for-byte, or the two tables
    # would silently disagree about the same clinical concept.
    for alias_label in vocab.CONDITION_ALIASES.values():
        assert alias_label in rules.CONDITION_LABELS, (
            f"vocab.CONDITION_ALIASES value {alias_label!r} "
            "is missing from rules.CONDITION_LABELS"
        )


def test_animal_and_human_terms_are_nonempty_string_tuples():
    assert rules.ANIMAL_TERMS and all(isinstance(t, str) for t in rules.ANIMAL_TERMS)
    assert rules.HUMAN_TERMS and all(isinstance(t, str) for t in rules.HUMAN_TERMS)


def test_no_duplicate_keys_were_collapsed_in_any_term_table():
    # A dict literal silently keeps the last value for a repeated key --
    # this would only catch a table that ended up suspiciously small, but
    # it is a cheap floor against that class of authoring mistake.
    assert len(rules.MODALITY_TERMS) > 60
    assert len(rules.DOMAIN_TERMS) > 60
    assert len(rules.CONDITION_TERMS) > 100


# ---------------------------------------------------------------------------
# Import-time invariants raise RuntimeError, not bare AssertionError, and
# survive python -O (assertions stripped) -- exercised directly against
# the same `_require` helper `rules` uses at import time.
# ---------------------------------------------------------------------------


def test_require_helper_raises_runtime_error_on_failure():
    with pytest.raises(RuntimeError, match="boom"):
        rules._require(False, "boom")


def test_require_helper_is_a_noop_on_success():
    rules._require(True, "unreachable")  # must not raise


# ---------------------------------------------------------------------------
# RuleHits: frozen dataclass with the documented fields
# ---------------------------------------------------------------------------


def test_rule_hits_is_a_dataclass_with_the_documented_fields():
    result = RuleHits(
        domains=["neurology"],
        modalities=["MRI"],
        conditions=["glioma"],
        species=None,
        evidence={"neurology": ["brain"]},
    )
    assert result.domains == ["neurology"]
    assert result.modalities == ["MRI"]
    assert result.conditions == ["glioma"]
    assert result.species is None
    assert result.evidence == {"neurology": ["brain"]}


def test_rule_hits_is_frozen():
    result = hints("a dataset", source="curated")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.species = "animal"
