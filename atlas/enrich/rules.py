"""Deterministic, network-free classification hints: modality/domain/
condition/species signals mined from free text with cached word-boundary
regexes, plus two source-specific lookups (`PHYSIONET_TOPICS`,
`BODYPART_HINTS`) keyed off structured keywords rather than prose.

This is a coarse, high-recall pre-filter, not a verdict: `hints()` feeds
`atlas.enrich.classify` (Task 2.2) alongside the LLM pass, and
`merge.apply_enrichment` (Task 2.4) reconciles the two. Every value it
returns carries `evidence` -- the literal substring(s) matched in the
input text -- so a human (or a later guard) can audit why a value was
assigned; nothing here is invented, only pattern-matched.

The `_rx` word-boundary helper is ported from the user's prior project
(`neurodatahub/scripts/lib/lexicon.py`, `_rx`): plain substring matching
is a precision bug in both directions -- "rats" matching inside "BraTS"
mislabels a brain-tumor imaging dataset as animal-only, "pet" matching
inside "competition" invents a PET-imaging modality out of nothing.
Word boundaries fix both. Terms that are themselves a word stem meant to
match every inflected suffix (e.g. "histopatholog" -> histopatholog{y,
ical,ic,ist}) get a leading boundary only; everything else gets both, so
it matches whole words/phrases. Which stems qualify is fixed, not
guessed: only stems *ending* in `at|ic|og|am|ell|hal` get the leading-
only treatment (see `_rx`). A handful of the source lists in this
module's brief write a stem that does not end that way (e.g.
"electroencephalogra"); rather than special-case the helper, this module
spells out the concrete inflected forms instead (`electroencephalogram`,
`electroencephalography`, `electroencephalographic`) -- same coverage,
without weakening the one fixed rule everything else relies on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlas import vocab

# ---------------------------------------------------------------------------
# Word-boundary regex helper (ported from neurodatahub/scripts/lib/lexicon.py)
# ---------------------------------------------------------------------------

_STEM_SUFFIXES = ("at", "ic", "og", "am", "ell", "hal")
_RX_CACHE: dict[str, re.Pattern[str]] = {}


def _rx(term: str) -> re.Pattern[str]:
    """Case-insensitive word-boundary regex for `term`, cached.

    A leading `\\b` always applies. A trailing `\\b` applies too *unless*
    `term` ends in one of `_STEM_SUFFIXES` -- those are word stems meant
    to also match their own longer inflections (`"histopatholog"` ->
    "histopathology", "histopathological", ...). Internal spaces become
    `\\s+` so a two-word term still matches across a run of whitespace.
    """
    cached = _RX_CACHE.get(term)
    if cached is not None:
        return cached
    tail = "" if term.endswith(_STEM_SUFFIXES) else r"\b"
    pattern = re.compile(
        r"\b" + re.escape(term).replace(r"\ ", r"\s+") + tail, re.IGNORECASE
    )
    _RX_CACHE[term] = pattern
    return pattern


# ---------------------------------------------------------------------------
# MODALITY_TERMS: free-text term -> atlas.vocab.MODALITIES value
# ---------------------------------------------------------------------------

MODALITY_TERMS: dict[str, str] = {
    # ECG
    "ecg": "ECG",
    "electrocardiogram": "ECG",
    "electrocardiography": "ECG",
    "electrocardiographic": "ECG",
    "holter": "ECG",
    "rr interval": "ECG",
    # EEG
    "eeg": "EEG",
    "electroencephalogram": "EEG",
    "electroencephalography": "EEG",
    "electroencephalographic": "EEG",
    # MEG
    "meg": "MEG",
    "magnetoencephalography": "MEG",
    # iEEG
    "ieeg": "iEEG",
    "ecog": "iEEG",
    "intracranial eeg": "iEEG",
    "stereo-eeg": "iEEG",
    "seeg": "iEEG",
    # EMG
    "emg": "EMG",
    "electromyography": "EMG",
    # PPG
    "ppg": "PPG",
    "photoplethysmogram": "PPG",
    "photoplethysmography": "PPG",
    "photoplethysmographic": "PPG",
    # fNIRS
    "fnirs": "fNIRS",
    "nirs": "fNIRS",
    # fMRI
    "fmri": "fMRI",
    "functional mri": "fMRI",
    "bold": "fMRI",
    # dMRI
    "dti": "dMRI",
    "diffusion mri": "dMRI",
    "dwi": "dMRI",
    # MRS
    "mrs": "MRS",
    "spectroscopy": "MRS",
    # MRI
    "mri": "MRI",
    "magnetic resonance": "MRI",
    # PET
    "pet": "PET",
    # SPECT
    "spect": "SPECT",
    # CT
    "ct": "CT",
    "computed tomography": "CT",
    # xray
    "x-ray": "xray",
    "xray": "xray",
    "radiograph": "xray",
    "radiographs": "xray",
    "radiography": "xray",
    "radiographic": "xray",
    "chest radiograph": "xray",
    # mammography
    "mammogram": "mammography",
    "mammography": "mammography",
    "mammographic": "mammography",
    # ultrasound
    "ultrasound": "ultrasound",
    "sonogram": "ultrasound",
    "sonography": "ultrasound",
    "sonographic": "ultrasound",
    "echocardiogram": "ultrasound",
    "echocardiography": "ultrasound",
    "echocardiographic": "ultrasound",
    # pathology
    "pathology": "pathology",
    "histopatholog": "pathology",
    "whole slide": "pathology",
    "h&e": "pathology",
    # genomics
    "genome": "genomics",
    "genomes": "genomics",
    "genomic": "genomics",
    "wgs": "genomics",
    "wes": "genomics",
    "exome": "genomics",
    "snp": "genomics",
    "gwas": "genomics",
    # transcriptomics
    "rna-seq": "transcriptomics",
    "transcriptome": "transcriptomics",
    "transcriptomic": "transcriptomics",
    "gene expression": "transcriptomics",
    # proteomics
    "proteome": "proteomics",
    "proteomic": "proteomics",
    # EHR
    "ehr": "EHR",
    "electronic health record": "EHR",
    "electronic health records": "EHR",
    "mimic": "EHR",
    "eicu": "EHR",
    "clinical database": "EHR",
    # clinical_notes
    "clinical notes": "clinical_notes",
    "discharge summary": "clinical_notes",
    "discharge summaries": "clinical_notes",
    "radiology report": "clinical_notes",
    "radiology reports": "clinical_notes",
    # claims
    "claims": "claims",
    "billing": "claims",
    "insurance": "claims",
    # registry
    "registry": "registry",
    # survey
    "survey": "survey",
    "questionnaire": "survey",
    "interview": "survey",
    # wearable
    "wearable": "wearable",
    "accelerometer": "wearable",
    "actigraphy": "wearable",
    "actigraph": "wearable",
    "actigraphic": "wearable",
    "smartwatch": "wearable",
    "gait sensor": "wearable",
    # eye_tracking
    "eye tracking": "eye_tracking",
    "eye-tracking": "eye_tracking",
    "gaze": "eye_tracking",
    # behavioral
    "behavioral": "behavioral",
    "behavioural": "behavioral",
    "reaction time": "behavioral",
    "cognitive task": "behavioral",
    # physiological_signals
    "polysomnography": "physiological_signals",
    "polysomnogram": "physiological_signals",
    "psg": "physiological_signals",
    "vital signs": "physiological_signals",
    "waveform": "physiological_signals",
    "waveforms": "physiological_signals",
    "multiparameter": "physiological_signals",
    "icu monitor": "physiological_signals",
    "icu monitoring": "physiological_signals",
    # radiotherapy
    "radiotherapy": "radiotherapy",
    "rtstruct": "radiotherapy",
    "dose": "radiotherapy",
}

# ---------------------------------------------------------------------------
# DOMAIN_TERMS: free-text term -> atlas.vocab.DOMAINS value
# ---------------------------------------------------------------------------

DOMAIN_TERMS: dict[str, str] = {
    # neurology
    "epilepsy": "neurology",
    "epileptic": "neurology",
    "seizure": "neurology",
    "seizures": "neurology",
    "stroke": "neurology",
    "parkinson": "neurology",
    "parkinsonism": "neurology",
    "alzheimer": "neurology",
    "dementia": "neurology",
    "multiple sclerosis": "neurology",
    "migraine": "neurology",
    "neuropathy": "neurology",
    "neuropathic": "neurology",
    "neuropathies": "neurology",
    "tbi": "neurology",
    "brain": "neurology",
    # psychiatry
    "depression": "psychiatry",
    "depressive": "psychiatry",
    "schizophrenia": "psychiatry",
    "schizophrenic": "psychiatry",
    "bipolar": "psychiatry",
    "anxiety": "psychiatry",
    "ptsd": "psychiatry",
    "autism": "psychiatry",
    "adhd": "psychiatry",
    "psychiatric": "psychiatry",
    "psychiatry": "psychiatry",
    "psychiatrist": "psychiatry",
    # neuroscience
    "cognitive": "neuroscience",
    "cognition": "neuroscience",
    "memory task": "neuroscience",
    "perception": "neuroscience",
    "language": "neuroscience",
    "attention": "neuroscience",
    # cardiology
    "cardiac": "cardiology",
    "cardiology": "cardiology",
    "cardiovascular": "cardiology",
    "arrhythmia": "cardiology",
    "arrhythmias": "cardiology",
    "arrhythmic": "cardiology",
    "atrial fibrillation": "cardiology",
    "heart failure": "cardiology",
    "myocardial": "cardiology",
    "myocarditis": "cardiology",
    "myocardium": "cardiology",
    "blood pressure": "cardiology",
    "hypertension": "cardiology",
    "ecg": "cardiology",
    # oncology
    "cancer": "oncology",
    "tumor": "oncology",
    "tumour": "oncology",
    "carcinoma": "oncology",
    "glioma": "oncology",
    "leukemia": "oncology",
    "lymphoma": "oncology",
    "oncolog": "oncology",
    "neoplasm": "oncology",
    "neoplasms": "oncology",
    "metastasis": "oncology",
    "metastases": "oncology",
    "metastatic": "oncology",
    "metastasize": "oncology",
    # pulmonology
    "copd": "pulmonology",
    "asthma": "pulmonology",
    "pneumonia": "pulmonology",
    "pulmonary": "pulmonology",
    "lung": "pulmonology",
    "respiratory": "pulmonology",
    "respirator": "pulmonology",
    # critical_care
    "icu": "critical_care",
    "intensive care": "critical_care",
    "critical care": "critical_care",
    "sepsis": "critical_care",
    "mechanical ventilation": "critical_care",
    # surgery
    "surgery": "surgery",
    "surgical": "surgery",
    "surgeries": "surgery",
    "operative": "surgery",
    "postoperative": "surgery",
    "anesthesia": "surgery",
    "anesthetic": "surgery",
    "anaesthesia": "surgery",
    "anaesthetic": "surgery",
    # pediatrics
    "pediatric": "pediatrics",
    "paediatric": "pediatrics",
    "neonatal": "pediatrics",
    "neonate": "pediatrics",
    "neonates": "pediatrics",
    "infant": "pediatrics",
    "infants": "pediatrics",
    "children": "pediatrics",
    # obstetrics_gynecology
    "pregnancy": "obstetrics_gynecology",
    "pregnant": "obstetrics_gynecology",
    "pregnancies": "obstetrics_gynecology",
    "obstetric": "obstetrics_gynecology",
    "gynecolog": "obstetrics_gynecology",
    "gynaecolog": "obstetrics_gynecology",
    "fetal": "obstetrics_gynecology",
    "maternal": "obstetrics_gynecology",
    # infectious_disease
    "covid": "infectious_disease",
    "covid-19": "infectious_disease",
    "sars-cov-2": "infectious_disease",
    "hiv": "infectious_disease",
    "tuberculosis": "infectious_disease",
    "malaria": "infectious_disease",
    "influenza": "infectious_disease",
    "infection": "infectious_disease",
    # endocrinology_metabolism
    "diabetes": "endocrinology_metabolism",
    "diabetic": "endocrinology_metabolism",
    "obesity": "endocrinology_metabolism",
    "thyroid": "endocrinology_metabolism",
    "metabolic": "endocrinology_metabolism",
    # gastroenterology_hepatology
    "liver": "gastroenterology_hepatology",
    "hepatitis": "gastroenterology_hepatology",
    "hepatic": "gastroenterology_hepatology",
    "hepatocellular": "gastroenterology_hepatology",
    "gastrointestinal": "gastroenterology_hepatology",
    "gastroenterology": "gastroenterology_hepatology",
    "crohn's disease": "gastroenterology_hepatology",
    "crohn disease": "gastroenterology_hepatology",
    "colitis": "gastroenterology_hepatology",
    "pancreatitis": "gastroenterology_hepatology",
    "pancreatic": "gastroenterology_hepatology",
    # nephrology_urology
    "kidney": "nephrology_urology",
    "renal": "nephrology_urology",
    "dialysis": "nephrology_urology",
    "urolog": "nephrology_urology",
    "prostate": "nephrology_urology",
    # musculoskeletal
    "orthopedic": "musculoskeletal",
    "orthopaedic": "musculoskeletal",
    "fracture": "musculoskeletal",
    "osteoarthritis": "musculoskeletal",
    "spine": "musculoskeletal",
    "musculoskeletal": "musculoskeletal",
    # public_health
    "population health": "public_health",
    "public health": "public_health",
    "household survey": "public_health",
    "demographic and health": "public_health",
    "nhanes": "public_health",
    "epidemiolog": "public_health",
}

# ---------------------------------------------------------------------------
# CONDITION_TERMS: free-text term -> canonical condition label.
#
# This module's *own* canonical label set -- atlas/vocab.py has no
# controlled condition vocabulary (Condition.label is free text), and
# this task does not edit vocab.py. Where a term already has a home in
# `vocab.CONDITION_ALIASES` (source-reported label -> canonical label,
# used to canonicalize an *explicit* condition string), this table uses
# that exact canonical string so the two tables never disagree on the
# same concept. `CONDITION_LABELS` (below) is the resulting set, and
# `BODYPART_HINTS` targets are asserted to be a subset of it.
# ---------------------------------------------------------------------------

CONDITION_TERMS: dict[str, str] = {
    # neurology
    "epilepsy": "epilepsy",
    "epileptic": "epilepsy",
    "seizure": "epilepsy",
    "seizures": "epilepsy",
    "seizure disorder": "epilepsy",
    "stroke": "stroke",
    "ischemic stroke": "stroke",
    "hemorrhagic stroke": "stroke",
    "cerebral infarction": "stroke",
    "parkinson's disease": "parkinson disease",
    "parkinson disease": "parkinson disease",
    "parkinsonism": "parkinson disease",
    "alzheimer's disease": "alzheimer disease",
    "alzheimer disease": "alzheimer disease",
    "dementia": "dementia",
    "multiple sclerosis": "multiple sclerosis",
    "traumatic brain injury": "traumatic brain injury",
    "tbi": "traumatic brain injury",
    "migraine": "migraine",
    "migraines": "migraine",
    "amyotrophic lateral sclerosis": "amyotrophic lateral sclerosis",
    "als": "amyotrophic lateral sclerosis",
    "motor neuron disease": "amyotrophic lateral sclerosis",
    "huntington's disease": "huntington disease",
    "huntington disease": "huntington disease",
    "cerebral palsy": "cerebral palsy",
    "hydrocephalus": "hydrocephalus",
    "spinal cord injury": "spinal cord injury",
    "concussion": "concussion",
    "mild traumatic brain injury": "concussion",
    "mtbi": "concussion",
    "tinnitus": "tinnitus",
    "intracranial aneurysm": "intracranial aneurysm",
    "intracranial aneurysms": "intracranial aneurysm",
    "cerebral aneurysm": "intracranial aneurysm",
    "cerebral aneurysms": "intracranial aneurysm",
    "brain aneurysm": "intracranial aneurysm",
    "brain aneurysms": "intracranial aneurysm",
    "subarachnoid hemorrhage": "subarachnoid hemorrhage",
    "subarachnoid haemorrhage": "subarachnoid hemorrhage",
    "sah": "subarachnoid hemorrhage",
    "cerebral hemorrhage": "cerebral hemorrhage",
    "intracerebral hemorrhage": "cerebral hemorrhage",
    "intracranial hemorrhage": "cerebral hemorrhage",
    "brain hemorrhage": "cerebral hemorrhage",
    # oncology (organ-specific; bare "cancer"/"tumor" stay in DOMAIN_TERMS
    # only -- they don't say *which* neoplasm)
    "glioma": "glioma",
    "gliomas": "glioma",
    "glioblastoma": "glioblastoma",
    "glioblastoma multiforme": "glioblastoma",
    "meningioma": "meningioma",
    "meningiomas": "meningioma",
    "brain tumor": "brain neoplasms",
    "brain tumour": "brain neoplasms",
    "brain neoplasm": "brain neoplasms",
    "brain cancer": "brain neoplasms",
    "breast cancer": "breast neoplasms",
    "breast carcinoma": "breast neoplasms",
    "breast neoplasm": "breast neoplasms",
    "breast tumor": "breast neoplasms",
    "lung cancer": "lung neoplasms",
    "lung carcinoma": "lung neoplasms",
    "lung neoplasm": "lung neoplasms",
    "lung tumor": "lung neoplasms",
    "lung adenocarcinoma": "adenocarcinoma of lung",
    "prostate cancer": "prostatic neoplasms",
    "prostatic neoplasm": "prostatic neoplasms",
    "prostate carcinoma": "prostatic neoplasms",
    "colorectal cancer": "colorectal neoplasms",
    "colon cancer": "colorectal neoplasms",
    "rectal cancer": "colorectal neoplasms",
    "colorectal neoplasm": "colorectal neoplasms",
    "kidney cancer": "kidney neoplasms",
    "renal cell carcinoma": "kidney neoplasms",
    "kidney neoplasm": "kidney neoplasms",
    "liver cancer": "liver neoplasms",
    "hepatocellular carcinoma": "liver neoplasms",
    "liver neoplasm": "liver neoplasms",
    "head and neck cancer": "head and neck neoplasms",
    "head and neck neoplasm": "head and neck neoplasms",
    "pancreatic cancer": "pancreatic neoplasms",
    "pancreatic neoplasm": "pancreatic neoplasms",
    "bladder cancer": "urinary bladder neoplasms",
    "urinary bladder neoplasm": "urinary bladder neoplasms",
    "cervical cancer": "uterine cervical neoplasms",
    "cervical neoplasm": "uterine cervical neoplasms",
    "ovarian cancer": "ovarian neoplasms",
    "ovarian neoplasm": "ovarian neoplasms",
    "esophageal cancer": "esophageal neoplasms",
    "esophageal neoplasm": "esophageal neoplasms",
    "gastric cancer": "stomach neoplasms",
    "stomach cancer": "stomach neoplasms",
    "stomach neoplasm": "stomach neoplasms",
    "thyroid cancer": "thyroid neoplasms",
    "thyroid neoplasm": "thyroid neoplasms",
    "leukemia": "leukemia",
    "leukaemia": "leukemia",
    "lymphoma": "lymphoma",
    "hodgkin lymphoma": "lymphoma",
    "non-hodgkin lymphoma": "lymphoma",
    "melanoma": "melanoma",
    # cardiology
    "atrial fibrillation": "atrial fibrillation",
    "afib": "atrial fibrillation",
    "heart failure": "heart failure",
    "congestive heart failure": "heart failure",
    "chf": "heart failure",
    "myocardial infarction": "myocardial infarction",
    "heart attack": "myocardial infarction",
    "hypertension": "hypertension",
    "high blood pressure": "hypertension",
    # critical_care / infectious_disease
    "sepsis": "sepsis",
    "septic shock": "sepsis",
    "covid-19": "covid-19",
    "covid": "covid-19",
    "sars-cov-2": "covid-19",
    "coronavirus disease": "covid-19",
    # pulmonology
    "obstructive sleep apnea": "sleep apnea, obstructive",
    "obstructive sleep apnoea": "sleep apnea, obstructive",
    "sleep apnea": "sleep apnea, obstructive",
    "osa": "sleep apnea, obstructive",
    "asthma": "asthma",
    "copd": "copd",
    "chronic obstructive pulmonary disease": "copd",
    "pneumonia": "pneumonia",
    # psychiatry
    "major depression": "depressive disorder, major",
    "major depressive disorder": "depressive disorder, major",
    "mdd": "depressive disorder, major",
    "schizophrenia": "schizophrenia",
    "schizophrenic": "schizophrenia",
    "bipolar disorder": "bipolar disorder",
    "autism spectrum disorder": "autism spectrum disorder",
    "autism": "autism spectrum disorder",
    "autistic": "autism spectrum disorder",
    "attention deficit hyperactivity disorder": (
        "attention deficit disorder with hyperactivity"
    ),
    "attention-deficit/hyperactivity disorder": (
        "attention deficit disorder with hyperactivity"
    ),
    "adhd": "attention deficit disorder with hyperactivity",
    "anxiety disorder": "anxiety disorders",
    "anxiety disorders": "anxiety disorders",
    "generalized anxiety disorder": "anxiety disorders",
    "post-traumatic stress disorder": "post-traumatic stress disorder",
    "posttraumatic stress disorder": "post-traumatic stress disorder",
    "ptsd": "post-traumatic stress disorder",
    # endocrinology_metabolism
    "diabetes mellitus": "diabetes mellitus",
    "type 2 diabetes": "diabetes mellitus",
    "type 1 diabetes": "diabetes mellitus",
    "diabetes": "diabetes mellitus",
    "obesity": "obesity",
    "obese": "obesity",
    # nephrology_urology
    "chronic kidney disease": "chronic kidney disease",
    "ckd": "chronic kidney disease",
    "end-stage renal disease": "chronic kidney disease",
    "esrd": "chronic kidney disease",
    # healthy
    "healthy controls": "healthy controls",
    "healthy control": "healthy controls",
    "healthy volunteers": "healthy controls",
    "normal controls": "healthy controls",
}

# The canonical condition label set this module can ever emit (from
# CONDITION_TERMS directly, plus BODYPART_HINTS -- asserted below to be a
# subset, so the two tables never drift apart).
CONDITION_LABELS: frozenset[str] = frozenset(CONDITION_TERMS.values())

# ---------------------------------------------------------------------------
# Species: ANIMAL_TERMS / HUMAN_TERMS
# ---------------------------------------------------------------------------

# Plain term tuples, not value tables -- the only output is the fixed
# string "animal" (see `hints()`), never a per-term value.
ANIMAL_TERMS: tuple[str, ...] = (
    "mouse",
    "mice",
    "murine",
    "rat",
    "rats",
    "rodent",
    "rodents",
    "zebrafish",
    "drosophila",
    "macaque",
    "macaques",
    "nonhuman primate",
    "non-human primate",
    "primate",
    "marmoset",
    "baboon",
    "porcine",
    "swine",
    "rabbit",
    "canine",
    "feline",
    "ferret",
    "xenopus",
    "c. elegans",
)

HUMAN_TERMS: tuple[str, ...] = (
    "human",
    "humans",
    "patient",
    "patients",
    "participant",
    "participants",
    "subject",
    "subjects",
    "volunteer",
    "volunteers",
    "clinical",
)

# ---------------------------------------------------------------------------
# BODYPART_HINTS: NBIA BodyPartExamined -> condition label, gated on a
# cancer-mention term also being present in the text (see `hints()`).
# ---------------------------------------------------------------------------

_CANCER_MENTION_TERMS: tuple[str, ...] = (
    "cancer",
    "tumor",
    "tumour",
    "lesion",
    "carcinoma",
)

BODYPART_HINTS: dict[str, str] = {
    "BREAST": "breast neoplasms",
    "LUNG": "lung neoplasms",
    "PROSTATE": "prostatic neoplasms",
    "BRAIN": "brain neoplasms",
    "KIDNEY": "kidney neoplasms",
    "LIVER": "liver neoplasms",
    "COLON": "colorectal neoplasms",
    "HEAD AND NECK": "head and neck neoplasms",
    "HEADNECK": "head and neck neoplasms",
    "PANCREAS": "pancreatic neoplasms",
    "BLADDER": "urinary bladder neoplasms",
    "CERVIX": "uterine cervical neoplasms",
    "OVARY": "ovarian neoplasms",
    "ESOPHAGUS": "esophageal neoplasms",
    "STOMACH": "stomach neoplasms",
    "THYROID": "thyroid neoplasms",
}

# ---------------------------------------------------------------------------
# PHYSIONET_TOPICS: exact (lowercased) PhysioNet topic string -> a
# ("modality"|"domain", vocab value) hint, applied only for source ==
# "physionet" (see `hints()`).
# ---------------------------------------------------------------------------

PHYSIONET_TOPICS: dict[str, tuple[str, str]] = {
    "intensive care unit": ("domain", "critical_care"),
    "icu": ("domain", "critical_care"),
    "critical care": ("domain", "critical_care"),
    "sepsis": ("domain", "critical_care"),
    "mechanical ventilation": ("domain", "critical_care"),
    "ecg": ("modality", "ECG"),
    "electrocardiogram": ("modality", "ECG"),
    "electrocardiography": ("modality", "ECG"),
    "arrhythmia": ("domain", "cardiology"),
    "cardiology": ("domain", "cardiology"),
    "cardiovascular": ("domain", "cardiology"),
    "heart failure": ("domain", "cardiology"),
    "blood pressure": ("domain", "cardiology"),
    "hemodynamics": ("modality", "physiological_signals"),
    "mimic": ("modality", "EHR"),
    "eicu": ("modality", "EHR"),
    "electronic health records": ("modality", "EHR"),
    "clinical notes": ("modality", "clinical_notes"),
    "sleep": ("modality", "physiological_signals"),
    "polysomnography": ("modality", "physiological_signals"),
    "multiparameter": ("modality", "physiological_signals"),
    "vital signs": ("modality", "physiological_signals"),
    "waveform": ("modality", "physiological_signals"),
    "respiration": ("modality", "physiological_signals"),
    "ppg": ("modality", "PPG"),
    "photoplethysmography": ("modality", "PPG"),
    "eeg": ("modality", "EEG"),
    "electroencephalography": ("modality", "EEG"),
    "neuroelectric and myoelectric": ("modality", "EEG"),
    "emg": ("modality", "EMG"),
    "gait": ("modality", "wearable"),
    "accelerometer": ("modality", "wearable"),
    "physical activity": ("modality", "wearable"),
    "wearable": ("modality", "wearable"),
    "neonatal": ("domain", "pediatrics"),
    "pediatrics": ("domain", "pediatrics"),
    "covid-19": ("domain", "infectious_disease"),
    "gene expression": ("modality", "transcriptomics"),
    "genetics": ("modality", "genomics"),
    "x-ray": ("modality", "xray"),
    "surgery": ("domain", "surgery"),
    "anesthesia": ("domain", "surgery"),
    "psychiatry": ("domain", "psychiatry"),
    "public health": ("domain", "public_health"),
    "diabetes": ("domain", "endocrinology_metabolism"),
}

# ---------------------------------------------------------------------------
# Import-time invariants: every table target is a real vocab value (or, for
# conditions, part of this module's own canonical label set), and the
# regex cache is warmed for every term now rather than on first search.
# ---------------------------------------------------------------------------

for _value in MODALITY_TERMS.values():
    assert _value in vocab.MODALITIES, f"MODALITY_TERMS target {_value!r} not in vocab"

for _value in DOMAIN_TERMS.values():
    assert _value in vocab.DOMAINS, f"DOMAIN_TERMS target {_value!r} not in vocab"

for _kind, _target in PHYSIONET_TOPICS.values():
    if _kind == "modality":
        assert _target in vocab.MODALITIES, (
            f"PHYSIONET_TOPICS modality target {_target!r} not in vocab"
        )
    elif _kind == "domain":
        assert _target in vocab.DOMAINS, (
            f"PHYSIONET_TOPICS domain target {_target!r} not in vocab"
        )
    else:
        raise AssertionError(
            f"PHYSIONET_TOPICS kind must be modality/domain, got {_kind!r}"
        )

for _label in BODYPART_HINTS.values():
    assert _label in CONDITION_LABELS, (
        f"BODYPART_HINTS target {_label!r} not in CONDITION_LABELS"
    )

assert "animal" in vocab.SPECIES
assert "human" in vocab.SPECIES

for _term in (
    *MODALITY_TERMS,
    *DOMAIN_TERMS,
    *CONDITION_TERMS,
    *ANIMAL_TERMS,
    *HUMAN_TERMS,
    *_CANCER_MENTION_TERMS,
):
    _rx(_term)  # force compilation now, not on first hints() call

del _value, _kind, _target, _label, _term


# ---------------------------------------------------------------------------
# hints()
# ---------------------------------------------------------------------------


@dataclass
class RuleHits:
    """Deterministic classification signal for one record's text.

    `evidence` is flat across all three categories (a domain, modality,
    and condition value never collide as strings), mapping each assigned
    value to the literal substring(s) that matched it in the input.
    """

    domains: list[str]
    modalities: list[str]
    conditions: list[str]
    species: str | None
    evidence: dict[str, list[str]]


def hints(text: str, *, source: str, raw_hints: dict | None = None) -> RuleHits:
    """Mine deterministic domain/modality/condition/species hints from
    `text` (already HTML-stripped free text: name + description +
    keywords).

    `raw_hints={"keywords": [...]}` is folded into the text for the
    regular term-table scan (a no-op if the caller's `text` already
    includes them), and additionally drives two exact-match lookups that
    would be too noisy as free-text substring rules: `PHYSIONET_TOPICS`
    (only when `source == "physionet"`, matching a keyword against a
    known topic tag verbatim) and `BODYPART_HINTS` (only when `source ==
    "tcia"`, and only when the text also mentions a cancer term --
    `BodyPartExamined=BRAIN` alone says nothing about malignancy).

    Rules only add what they find evidence for: an empty match set is a
    perfectly valid outcome (e.g. non-clinical prose), never defaulted to
    a guess. `domains`/`modalities` come back deduped in vocab order;
    `conditions` deduped and alphabetically sorted (conditions have no
    controlled vocab ordering to follow).
    """
    text = text or ""
    raw_hints = raw_hints or {}
    keywords = [str(k) for k in (raw_hints.get("keywords") or [])]
    full_text = f"{text} {' '.join(keywords)}" if keywords else text

    evidence: dict[str, list[str]] = {}

    def record(value: str, matched: str) -> None:
        bucket = evidence.setdefault(value, [])
        if matched not in bucket:
            bucket.append(matched)

    def scan(table: dict[str, str]) -> set[str]:
        found: set[str] = set()
        for term, value in table.items():
            match = _rx(term).search(full_text)
            if match:
                found.add(value)
                record(value, match.group(0))
        return found

    modality_hits = scan(MODALITY_TERMS)
    domain_hits = scan(DOMAIN_TERMS)
    condition_hits = scan(CONDITION_TERMS)

    if source == "physionet":
        for keyword in keywords:
            topic = PHYSIONET_TOPICS.get(keyword.strip().lower())
            if topic is None:
                continue
            kind, value = topic
            (modality_hits if kind == "modality" else domain_hits).add(value)
            record(value, keyword)

    if source == "tcia":
        cancer_mentioned = any(
            _rx(term).search(full_text) for term in _CANCER_MENTION_TERMS
        )
        if cancer_mentioned:
            for keyword in keywords:
                label = BODYPART_HINTS.get(keyword.strip().upper())
                if label is None:
                    continue
                condition_hits.add(label)
                record(label, keyword)

    animal_hit = any(_rx(term).search(full_text) for term in ANIMAL_TERMS)
    human_hit = any(_rx(term).search(full_text) for term in HUMAN_TERMS)
    species = "animal" if animal_hit and not human_hit else None

    domains = [d for d in vocab.DOMAINS if d in domain_hits]
    modalities = [m for m in vocab.MODALITIES if m in modality_hits]
    conditions = sorted(condition_hits)

    ordered_evidence = {
        value: evidence[value] for value in (*domains, *modalities, *conditions)
    }

    return RuleHits(
        domains=domains,
        modalities=modalities,
        conditions=conditions,
        species=species,
        evidence=ordered_evidence,
    )
