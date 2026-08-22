"""Canonical controlled vocabularies for the schema (frozen v1).

Plain constants only — tuples for ordered/enumerated value sets, dicts for
lookup tables. Nothing here imports from :mod:`atlas.schema`; it is the
other way around, so this module has no dependencies beyond the stdlib and
stays safe to import from anywhere (docs generation, normalizers, tests).

Tuple order matters for two of these: ``ACCESS_ORDER`` is least-restrictive
to most-restrictive (used to resolve conflicting access signals), and every
tuple's order is what ``atlas schema --export`` renders into
``docs/schema.md``, so keep additions appended rather than reordered.
"""

from __future__ import annotations

# Clinical domain a dataset serves (17). `other` is the deliberate catch-all
# for anything that doesn't fit the other 16.
DOMAINS: tuple[str, ...] = (
    "neurology",
    "psychiatry",
    "neuroscience",
    "cardiology",
    "oncology",
    "pulmonology",
    "critical_care",
    "surgery",
    "pediatrics",
    "obstetrics_gynecology",
    "infectious_disease",
    "endocrinology_metabolism",
    "gastroenterology_hepatology",
    "nephrology_urology",
    "musculoskeletal",
    "public_health",
    "other",
)

# Data modality captured (33).
MODALITIES: tuple[str, ...] = (
    "MRI",
    "fMRI",
    "dMRI",
    "MRS",
    "PET",
    "SPECT",
    "CT",
    "xray",
    "mammography",
    "ultrasound",
    "radiotherapy",
    "pathology",
    "EEG",
    "MEG",
    "iEEG",
    "fNIRS",
    "ECG",
    "EMG",
    "PPG",
    "physiological_signals",
    "wearable",
    "eye_tracking",
    "behavioral",
    "genomics",
    "transcriptomics",
    "proteomics",
    "EHR",
    "clinical_notes",
    "claims",
    "registry",
    "survey",
    "clinical_tabular",
    "other",
)

# Access tiers (5), ordered least- to most-restrictive. `access` always
# resolves to the *least*-restrictive tier at which substantive data is
# usable; when a source presents conflicting signals, resolve to whichever
# tier sorts *later* in this tuple (decision D8).
ACCESS_ORDER: tuple[str, ...] = (
    "open",
    "registration",
    "credentialed",
    "application",
    "purchase",
)

# Unit that `sample_size` counts (10).
SAMPLE_UNITS: tuple[str, ...] = (
    "participants",
    "cases",
    "admissions",
    "records",
    "images",
    "studies",
    "series",
    "households",
    "surveys",
    "other",
)

# Species studied (6).
SPECIES: tuple[str, ...] = (
    "human",
    "animal",
    "mixed",
    "phantom",
    "simulated",
    "unknown",
)

# Harvester/source identifiers (9) — Phase 0 sources plus the curated
# catch-all; Phases 1-2 sources (dhs, worldbank) are already reserved here
# per the frozen schema even though their harvesters land later.
SOURCES: tuple[str, ...] = (
    "openneuro",
    "physionet",
    "gdc",
    "tcia",
    "scientific_data",
    "data_in_brief",
    "dhs",
    "worldbank",
    "curated",
)

# Lifecycle status of a catalog record (3).
RECORD_STATUS: tuple[str, ...] = (
    "active",
    "needs_review",
    "removed",
)

# How a linked paper relates to the dataset (4).
PAPER_RELATIONS: tuple[str, ...] = (
    "describes",
    "cites",
    "is_cited_by",
    "other",
)

# How a related atlas record relates to this one (4).
RELATED_RELATIONS: tuple[str, ...] = (
    "same_cohort",
    "duplicate_of",
    "derived_from",
    "part_of",
)

# How enrichment fields (domains/modalities/conditions/summary/...) were
# produced (4).
ENRICHMENT_METHODS: tuple[str, ...] = (
    "rules",
    "llm",
    "rules+llm",
    "curated",
)

# Free-text license strings (as seen verbatim from sources) -> SPDX
# identifier. Unmapped strings pass through verbatim (see
# `atlas.normalize.common.license_to_spdx`, Task 0.5).
LICENSE_MAP: dict[str, str] = {
    "CC0": "CC0-1.0",
    "Creative Commons Zero 1.0 Universal Public Domain Dedication": "CC0-1.0",
    "Creative Commons Attribution 4.0 International Public License": "CC-BY-4.0",
    "cc-by-4.0": "CC-BY-4.0",
    "cc-by-3.0": "CC-BY-3.0",
    "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International "
    "Public License": "CC-BY-NC-SA-4.0",
    "Creative Commons Attribution-ShareAlike 4.0 International Public License": (
        "CC-BY-SA-4.0"
    ),
    "cc-by-nc-4.0": "CC-BY-NC-4.0",
    "cc-by-nc-3.0": "CC-BY-NC-3.0",
    "cc-by-nc-nd-3.0": "CC-BY-NC-ND-3.0",
    "Open Data Commons Attribution License v1.0": "ODC-By-1.0",
    "Open Data Commons Open Database License v1.0": "ODbL-1.0",
    "MIT License": "MIT",
    "GNU General Public License version 3": "GPL-3.0-only",
}

# Lowercase source-reported condition label -> canonical label used for
# MeSH lookup. Seed set (~20 canonical targets); later tasks extend this
# table as new conditions show up during enrichment.
CONDITION_ALIASES: dict[str, str] = {
    "healthy / control": "healthy controls",
    "lung adenocarcinoma": "adenocarcinoma of lung",
    "glioblastoma multiforme": "glioblastoma",
    "parkinson's disease": "parkinson disease",
    "parkinson disease": "parkinson disease",
    "pd": "parkinson disease",
    "alzheimer's disease": "alzheimer disease",
    "afib": "atrial fibrillation",
    "atrial fibrillation": "atrial fibrillation",
    "covid": "covid-19",
    "covid-19": "covid-19",
    "sars-cov-2 infection": "covid-19",
    "tbi": "traumatic brain injury",
    "ms": "multiple sclerosis",
    "mdd": "depressive disorder, major",
    "major depression": "depressive disorder, major",
    "schizophrenia": "schizophrenia",
    "autism": "autism spectrum disorder",
    "asd": "autism spectrum disorder",
    "adhd": "attention deficit disorder with hyperactivity",
    "stroke": "stroke",
    "ischemic stroke": "stroke",
    "epilepsy": "epilepsy",
    "seizure disorder": "epilepsy",
    "heart failure": "heart failure",
    "chf": "heart failure",
    "sepsis": "sepsis",
    "breast cancer": "breast neoplasms",
    "lung cancer": "lung neoplasms",
    "obstructive sleep apnea": "sleep apnea, obstructive",
    "osa": "sleep apnea, obstructive",
}
