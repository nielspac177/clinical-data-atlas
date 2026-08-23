"""Tests for `atlas.enrich.rules`: the deterministic term-table hints
engine (`hints()`, `RuleHits`) and its word-boundary regex helper (`_rx`).
"""

from __future__ import annotations

from atlas import vocab
from atlas.enrich import rules
from atlas.enrich.rules import RuleHits, hints

# ---------------------------------------------------------------------------
# _rx: word-boundary regex helper
# ---------------------------------------------------------------------------


def test_rx_matches_whole_word_case_insensitively():
    assert rules._rx("ecg").search("a 12-lead ECG recording")
    assert rules._rx("ecg").search("a 12-lead ecg recording")


def test_rx_stem_ending_in_magic_suffix_matches_longer_inflections():
    # "histopatholog" ends in "og" -> leading boundary only, so it matches
    # every -ology/-ological/-ologic inflection from one table entry.
    rx = rules._rx("histopatholog")
    assert rx.search("histopathology review")
    assert rx.search("histopathological grading")
    assert rx.search("histopathologic subtype")


def test_rx_non_stem_term_requires_a_trailing_boundary_too():
    # "cardiac" does not end in a magic suffix, so it only matches the
    # complete word, not an arbitrary continuation of it.
    rx = rules._rx("cardiac")
    assert rx.search("cardiac arrest")
    assert not rx.search("cardiacyzer")  # made-up continuation, must not match


def test_rx_does_not_match_substring_across_a_word_boundary():
    # The two false-positive traps this helper exists to fix.
    assert not rules._rx("rat").search("BraTS glioma dataset")
    assert not rules._rx("rats").search("BraTS glioma dataset")
    assert not rules._rx("pet").search("a machine learning competition")


def test_rx_multi_word_term_matches_across_whitespace():
    assert rules._rx("atrial fibrillation").search("chronic Atrial   Fibrillation case")


def test_rx_is_cached():
    first = rules._rx("epilepsy")
    second = rules._rx("epilepsy")
    assert first is second


# ---------------------------------------------------------------------------
# hints(): required scenarios from the task brief
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


def test_bare_pet_word_does_match():
    result = hints("a PET imaging study", source="curated")
    assert "PET" in result.modalities


def test_rat_hippocampus_recordings_is_animal_species():
    result = hints("rat hippocampus recordings", source="curated")
    assert result.species == "animal"


def test_rat_and_human_cortex_has_no_species_opinion():
    result = hints("rat and human cortex", source="curated")
    assert result.species is None


def test_no_animal_or_human_terms_has_no_species_opinion():
    result = hints("a dataset of brain MRI scans", source="curated")
    assert result.species is None


# ---------------------------------------------------------------------------
# Ordering and dedup: domains/modalities in vocab order, conditions sorted
# ---------------------------------------------------------------------------


def test_domains_are_deduped_and_returned_in_vocab_order():
    # Text mentions pulmonology and public_health terms before a
    # neurology one, but vocab.DOMAINS orders neurology first.
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
# Evidence: populated with the literal matched substring(s)
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


# ---------------------------------------------------------------------------
# raw_hints["keywords"]: folded into text for the ordinary term scan
# ---------------------------------------------------------------------------


def test_keywords_are_folded_into_the_text_scan():
    # "ECG" only appears as a keyword, never in the prose itself.
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
    # A topic string with no substring overlap with any general term-table
    # entry, so this can only fire through the PHYSIONET_TOPICS path.
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
    # A generic cancer word only -- the organ comes solely from the
    # structured keyword, so "lung neoplasms" can only appear through the
    # BODYPART_HINTS(+tcia) path, never a direct CONDITION_TERMS match.
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
# addition to the import-time asserts in rules.py itself.
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


def test_condition_labels_matching_vocab_condition_aliases_use_the_exact_string():
    # Where this module's canonical label set overlaps a label that
    # `vocab.CONDITION_ALIASES` also canonicalizes to, the strings must
    # agree byte-for-byte -- otherwise the two tables would silently
    # disagree about the same clinical concept.
    alias_values = set(vocab.CONDITION_ALIASES.values())
    overlap = alias_values & rules.CONDITION_LABELS
    assert len(overlap) >= 15  # sanity floor: most seed aliases are covered
    for label in overlap:
        assert label in rules.CONDITION_LABELS


def test_animal_and_human_terms_are_nonempty_string_tuples():
    assert rules.ANIMAL_TERMS and all(isinstance(t, str) for t in rules.ANIMAL_TERMS)
    assert rules.HUMAN_TERMS and all(isinstance(t, str) for t in rules.HUMAN_TERMS)


def test_no_duplicate_keys_were_collapsed_in_any_term_table():
    # A dict literal silently keeps the last value for a repeated key --
    # this would only catch a table that ended up suspiciously small, but
    # it is a cheap floor against that class of authoring mistake.
    assert len(rules.MODALITY_TERMS) > 60
    assert len(rules.DOMAIN_TERMS) > 60
    assert len(rules.CONDITION_TERMS) > 60


# ---------------------------------------------------------------------------
# RuleHits: plain dataclass shape
# ---------------------------------------------------------------------------


def test_rule_hits_is_a_plain_dataclass_with_the_documented_fields():
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
