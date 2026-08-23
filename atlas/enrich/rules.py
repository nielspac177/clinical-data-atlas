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
inside "competition" invents a PET-imaging modality out of nothing. Word
boundaries fix both -- but stemming (when a term should also match its
own longer inflections) must be an explicit, per-term choice, never
inferred from a term's own spelling. An earlier version of this module
inferred it from a term's last two letters (stems ending `at|ic|og|am|
ell|hal` got a leading boundary only); `"rat"` ends in `"at"`, so it
silently qualified and `\\brat` (no trailing boundary) matched "rate",
"ratio", "rational" -- a real false positive on real text ("heart rate
variability" flagged as an animal study), not a hypothetical one, and
the same inference had already silently done the same thing to `"ecog"`
and `"mimic"` (see Ruling R14; both replaced below with unambiguous
compound terms instead of being re-blessed as stems). Stemming is now
spelled out per term with a trailing `*` in the table (see `_rx`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlas import vocab

# ---------------------------------------------------------------------------
# Word-boundary regex helper (ported from neurodatahub/scripts/lib/lexicon.py)
# ---------------------------------------------------------------------------

_RX_CACHE: dict[str, re.Pattern[str]] = {}


def _rx(term: str) -> re.Pattern[str]:
    """Case-insensitive word-boundary regex for `term`, cached.

    Stemming is explicit, never inferred (Ruling R14): a term ending in a
    literal trailing `*` is a prefix stem -- leading `\\b` only, so it
    also matches its own longer inflections (`"histopatholog*"` matches
    "histopathology", "histopathological", "histopathologic", ...).
    Every other term is a whole word/phrase: `\\b` on both sides, so it
    matches exactly that word or phrase and nothing it happens to be a
    substring of. Internal spaces become `\\s+` so a multi-word term
    still matches across a run of whitespace.
    """
    cached = _RX_CACHE.get(term)
    if cached is not None:
        return cached
    is_stem = term.endswith("*")
    body = term[:-1] if is_stem else term
    tail = "" if is_stem else r"\b"
    pattern = re.compile(
        r"\b" + re.escape(body).replace(r"\ ", r"\s+") + tail, re.IGNORECASE
    )
    _RX_CACHE[term] = pattern
    return pattern


# ---------------------------------------------------------------------------
# MODALITY_TERMS: free-text term -> atlas.vocab.MODALITIES value
# ---------------------------------------------------------------------------

MODALITY_TERMS: dict[str, str] = {
    # ECG
    "ecg": "ECG",
    "electrocardiogra*": "ECG",
    "holter": "ECG",
    "rr interval": "ECG",
    # EEG
    "eeg": "EEG",
    "electroencephalogra*": "EEG",
    # MEG
    "meg": "MEG",
    "magnetoencephalography": "MEG",
    # iEEG -- bare "ecog" removed (Ruling R14: it silently matched as a
    # prefix under the old inferred-stemming rule); replaced with
    # unambiguous compound terms.
    "ieeg": "iEEG",
    "electrocorticograph*": "iEEG",
    "ecog recording*": "iEEG",
    "ecog grid*": "iEEG",
    "intracranial eeg": "iEEG",
    "stereo-eeg": "iEEG",
    "seeg": "iEEG",
    # EMG
    "emg": "EMG",
    "electromyography": "EMG",
    # PPG
    "ppg": "PPG",
    "photoplethysmogra*": "PPG",
    # fNIRS
    "fnirs": "fNIRS",
    "nirs": "fNIRS",
    # fMRI -- bare "bold" removed (too common an English word); replaced
    # with the compound forms that actually mean the BOLD fMRI signal.
    "fmri": "fMRI",
    "functional mri": "fMRI",
    "bold signal": "fMRI",
    "bold fmri": "fMRI",
    "bold contrast": "fMRI",
    "bold response*": "fMRI",
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
    # PET -- bare "pet" removed (matches the common English word); every
    # remaining term names the imaging modality unambiguously.
    "positron emission tomography": "PET",
    "pet/ct": "PET",
    "pet-ct": "PET",
    "fdg-pet": "PET",
    "fdg pet": "PET",
    "pet scan*": "PET",
    "pet imaging": "PET",
    "pet tracer*": "PET",
    "amyloid pet": "PET",
    "tau pet": "PET",
    "pet-mr*": "PET",
    # SPECT
    "spect": "SPECT",
    # CT
    "ct": "CT",
    "computed tomography": "CT",
    # xray
    "x-ray": "xray",
    "xray": "xray",
    "radiograph*": "xray",
    "chest radiograph": "xray",
    # mammography
    "mammogra*": "mammography",
    # ultrasound
    "ultrasound": "ultrasound",
    "sonogra*": "ultrasound",
    "echocardiogra*": "ultrasound",
    # pathology
    "pathology": "pathology",
    "histopatholog*": "pathology",
    "whole slide": "pathology",
    "h&e": "pathology",
    # genomics
    "genom*": "genomics",
    "wgs": "genomics",
    "wes": "genomics",
    "exome": "genomics",
    "snp": "genomics",
    "gwas": "genomics",
    # transcriptomics
    "rna-seq": "transcriptomics",
    "transcriptom*": "transcriptomics",
    "gene expression": "transcriptomics",
    # proteomics
    "proteom*": "proteomics",
    # EHR -- bare "mimic" removed (silently matched as a prefix under the
    # old rule, e.g. inside "mimicry"); replaced with the actual MIMIC
    # database name variants.
    "ehr": "EHR",
    "electronic health record*": "EHR",
    "mimic-iii": "EHR",
    "mimic-iv": "EHR",
    "mimic iii": "EHR",
    "mimic iv": "EHR",
    "mimic database": "EHR",
    "mimic-cxr": "EHR",
    "mimic iv-ed": "EHR",
    "eicu": "EHR",
    "clinical database": "EHR",
    # clinical_notes
    "clinical notes": "clinical_notes",
    "discharge summar*": "clinical_notes",
    "radiology report*": "clinical_notes",
    # claims -- bare "claims" removed (too generic on its own); "billing"
    # and "insurance" are kept as-is (not flagged as false positives).
    "insurance claims": "claims",
    "claims data": "claims",
    "medical claims": "claims",
    "administrative claims": "claims",
    "billing claims": "claims",
    "claims database*": "claims",
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
    "actigraph*": "wearable",
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
    "cognitive task*": "behavioral",
    # physiological_signals
    "polysomnogra*": "physiological_signals",
    "psg": "physiological_signals",
    "vital signs": "physiological_signals",
    "waveform*": "physiological_signals",
    "multiparameter": "physiological_signals",
    "icu monitor*": "physiological_signals",
    # radiotherapy -- bare "dose" removed (far too generic on its own);
    # replaced with radiotherapy-specific dose terminology.
    "radiotherapy": "radiotherapy",
    "rtstruct": "radiotherapy",
    "radiation dose": "radiotherapy",
    "dose distribution*": "radiotherapy",
    "dosimetr*": "radiotherapy",
    "rtdose": "radiotherapy",
    "dose-volume": "radiotherapy",
    "treatment planning": "radiotherapy",
}

# ---------------------------------------------------------------------------
# DOMAIN_TERMS: free-text term -> atlas.vocab.DOMAINS value
# ---------------------------------------------------------------------------

DOMAIN_TERMS: dict[str, str] = {
    # neurology
    "epilep*": "neurology",
    "seizure*": "neurology",
    "stroke": "neurology",
    "parkinson": "neurology",
    "parkinsonism": "neurology",
    "alzheimer": "neurology",
    "dementia": "neurology",
    "multiple sclerosis": "neurology",
    "migraine": "neurology",
    "neuropath*": "neurology",
    "tbi": "neurology",
    "brain": "neurology",
    # psychiatry
    "depression": "psychiatry",
    "depressive": "psychiatry",
    "schizophren*": "psychiatry",
    "bipolar": "psychiatry",
    "anxiety": "psychiatry",
    "ptsd": "psychiatry",
    "autis*": "psychiatry",
    "adhd": "psychiatry",
    "psychiatr*": "psychiatry",
    # neuroscience -- bare "perception"/"language"/"attention" removed
    # (too generic in a corpus that also indexes ML/data-science papers);
    # kept as qualified multi-word phrases instead.
    "cognit*": "neuroscience",
    "memory task": "neuroscience",
    "working memory": "neuroscience",
    "visual perception": "neuroscience",
    "speech perception": "neuroscience",
    "language comprehension": "neuroscience",
    "attention task*": "neuroscience",
    # cardiology
    "cardiac": "cardiology",
    "cardiology": "cardiology",
    "cardiovascular": "cardiology",
    "arrhythmi*": "cardiology",
    "atrial fibrillation": "cardiology",
    "heart failure": "cardiology",
    "myocardi*": "cardiology",
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
    "oncolog*": "oncology",
    "neoplasm*": "oncology",
    "metasta*": "oncology",
    # pulmonology
    "copd": "pulmonology",
    "asthma": "pulmonology",
    "pneumonia": "pulmonology",
    "pulmonary": "pulmonology",
    "lung": "pulmonology",
    "respirator*": "pulmonology",
    # critical_care
    "icu": "critical_care",
    "intensive care": "critical_care",
    "critical care": "critical_care",
    "sepsis": "critical_care",
    "mechanical ventilation": "critical_care",
    # surgery
    "surger*": "surgery",
    "surgical": "surgery",
    "operative": "surgery",
    "postoperative": "surgery",
    "anesthe*": "surgery",
    "anaesthe*": "surgery",
    # pediatrics
    "pediatric*": "pediatrics",
    "paediatric*": "pediatrics",
    "neonat*": "pediatrics",
    "infant*": "pediatrics",
    "children": "pediatrics",
    # obstetrics_gynecology
    "pregnan*": "obstetrics_gynecology",
    "obstetric*": "obstetrics_gynecology",
    "gynecolog*": "obstetrics_gynecology",
    "gynaecolog*": "obstetrics_gynecology",
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
    "diabet*": "endocrinology_metabolism",
    "obesity": "endocrinology_metabolism",
    "thyroid": "endocrinology_metabolism",
    "metabolic": "endocrinology_metabolism",
    # gastroenterology_hepatology
    "liver": "gastroenterology_hepatology",
    "hepat*": "gastroenterology_hepatology",
    "gastrointestinal": "gastroenterology_hepatology",
    "gastroenterology": "gastroenterology_hepatology",
    "crohn's disease": "gastroenterology_hepatology",
    "crohn disease": "gastroenterology_hepatology",
    "colitis": "gastroenterology_hepatology",
    "pancreat*": "gastroenterology_hepatology",
    # nephrology_urology
    "kidney": "nephrology_urology",
    "renal": "nephrology_urology",
    "dialysis": "nephrology_urology",
    "urolog*": "nephrology_urology",
    "prostate": "nephrology_urology",
    # musculoskeletal
    "orthopedic*": "musculoskeletal",
    "orthopaedic*": "musculoskeletal",
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
    "epidemiolog*": "public_health",
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
# same concept -- every value of `vocab.CONDITION_ALIASES` is asserted to
# appear here (see the import-time checks below). Labels are lowercase;
# most follow plain clinical usage, a few follow MeSH's own inverted
# form where that inversion is the real heading (e.g. "depressive
# disorder, major", "diabetes mellitus, type 2", "arrhythmia, cardiac").
# `CONDITION_LABELS` (below) is the resulting canonical set, and
# `BODYPART_HINTS` targets are asserted to be a subset of it.
# ---------------------------------------------------------------------------

CONDITION_TERMS: dict[str, str] = {
    # neurology
    "epilep*": "epilepsy",
    "seizure*": "epilepsy",
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
    "migraine*": "migraine",
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
    "intracranial aneurysm*": "intracranial aneurysm",
    "cerebral aneurysm*": "intracranial aneurysm",
    "brain aneurysm*": "intracranial aneurysm",
    "subarachnoid hemorrhage": "subarachnoid hemorrhage",
    "subarachnoid haemorrhage": "subarachnoid hemorrhage",
    "sah": "subarachnoid hemorrhage",
    "cerebral hemorrhage": "cerebral hemorrhage",
    "intracerebral hemorrhage": "cerebral hemorrhage",
    "intracranial hemorrhage": "cerebral hemorrhage",
    "brain hemorrhage": "cerebral hemorrhage",
    "essential tremor": "essential tremor",
    "dystonia": "dystonia",
    "peripheral neuropath*": "peripheral neuropathy",
    "narcolepsy": "narcolepsy",
    "restless legs syndrome": "restless legs syndrome",
    "restless leg syndrome": "restless legs syndrome",
    "guillain-barre syndrome": "guillain-barre syndrome",
    "guillain barre syndrome": "guillain-barre syndrome",
    "myasthenia gravis": "myasthenia gravis",
    # oncology (organ-specific; bare "cancer"/"tumor" stay in DOMAIN_TERMS
    # only -- they don't say *which* neoplasm)
    "glioma*": "glioma",
    "glioblastoma": "glioblastoma",
    "glioblastoma multiforme": "glioblastoma",
    "meningioma*": "meningioma",
    "brain tumor": "brain neoplasms",
    "brain tumour": "brain neoplasms",
    "brain neoplasm*": "brain neoplasms",
    "brain cancer": "brain neoplasms",
    "breast cancer": "breast neoplasms",
    "breast carcinoma": "breast neoplasms",
    "breast neoplasm*": "breast neoplasms",
    "breast tumor": "breast neoplasms",
    "lung cancer": "lung neoplasms",
    "lung carcinoma": "lung neoplasms",
    "lung neoplasm*": "lung neoplasms",
    "lung tumor": "lung neoplasms",
    "lung adenocarcinoma": "adenocarcinoma of lung",
    "prostate cancer": "prostatic neoplasms",
    "prostatic neoplasm*": "prostatic neoplasms",
    "prostate carcinoma": "prostatic neoplasms",
    "colorectal cancer": "colorectal neoplasms",
    "colon cancer": "colorectal neoplasms",
    "rectal cancer": "colorectal neoplasms",
    "colorectal neoplasm*": "colorectal neoplasms",
    "kidney cancer": "kidney neoplasms",
    "renal cell carcinoma": "kidney neoplasms",
    "kidney neoplasm*": "kidney neoplasms",
    "liver cancer": "liver neoplasms",
    "hepatocellular carcinoma": "liver neoplasms",
    "liver neoplasm*": "liver neoplasms",
    "head and neck cancer": "head and neck neoplasms",
    "head and neck neoplasm*": "head and neck neoplasms",
    "pancreatic cancer": "pancreatic neoplasms",
    "pancreatic neoplasm*": "pancreatic neoplasms",
    "bladder cancer": "urinary bladder neoplasms",
    "urinary bladder neoplasm*": "urinary bladder neoplasms",
    "cervical cancer": "uterine cervical neoplasms",
    "cervical neoplasm*": "uterine cervical neoplasms",
    "ovarian cancer": "ovarian neoplasms",
    "ovarian neoplasm*": "ovarian neoplasms",
    "esophageal cancer": "esophageal neoplasms",
    "esophageal neoplasm*": "esophageal neoplasms",
    "gastric cancer": "stomach neoplasms",
    "stomach cancer": "stomach neoplasms",
    "stomach neoplasm*": "stomach neoplasms",
    "thyroid cancer": "thyroid neoplasms",
    "thyroid neoplasm*": "thyroid neoplasms",
    "leukemia": "leukemia",
    "leukaemia": "leukemia",
    "lymphoma*": "lymphoma",
    "hodgkin lymphoma": "lymphoma",
    "non-hodgkin lymphoma": "lymphoma",
    "melanoma": "melanoma",
    "sarcoma*": "sarcoma",
    "neuroblastoma": "neuroblastoma",
    "multiple myeloma": "multiple myeloma",
    "skin cancer": "skin neoplasms",
    "skin neoplasm*": "skin neoplasms",
    "testicular cancer": "testicular neoplasms",
    "testicular neoplasm*": "testicular neoplasms",
    "endometrial cancer": "endometrial neoplasms",
    "endometrial neoplasm*": "endometrial neoplasms",
    "bone cancer": "bone neoplasms",
    "bone neoplasm*": "bone neoplasms",
    "osteosarcoma": "bone neoplasms",
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
    "coronary artery disease": "coronary artery disease",
    "coronary heart disease": "coronary artery disease",
    "cardiomyopath*": "cardiomyopathy",
    "aortic aneurysm*": "aortic aneurysm",
    "peripheral vascular disease": "peripheral vascular disease",
    "peripheral artery disease": "peripheral vascular disease",
    "venous thromboembolism": "venous thromboembolism",
    "deep vein thrombosis": "venous thromboembolism",
    "dvt": "venous thromboembolism",
    "pulmonary embolism": "pulmonary embolism",
    "pulmonary embolus": "pulmonary embolism",
    "cardiac arrest": "cardiac arrest",
    "out-of-hospital cardiac arrest": "cardiac arrest",
    "pericarditis": "pericarditis",
    "endocarditis": "endocarditis",
    "infective endocarditis": "endocarditis",
    "cardiac arrhythmia": "arrhythmia, cardiac",
    # critical_care / infectious_disease
    "sepsis": "sepsis",
    "septic shock": "sepsis",
    "covid-19": "covid-19",
    "covid": "covid-19",
    "sars-cov-2": "covid-19",
    "coronavirus disease": "covid-19",
    "hiv infection*": "hiv infections",
    "hiv/aids": "hiv infections",
    "tuberculosis": "tuberculosis",
    "influenza": "influenza, human",
    "flu": "influenza, human",
    "hepatitis c": "hepatitis c",
    "hepatitis c virus": "hepatitis c",
    "hcv": "hepatitis c",
    "hepatitis b": "hepatitis b",
    "hepatitis b virus": "hepatitis b",
    "hbv": "hepatitis b",
    "malaria": "malaria",
    "multiple organ failure": "multiple organ failure",
    "multi-organ failure": "multiple organ failure",
    "multiple organ dysfunction syndrome": "multiple organ failure",
    "delirium": "delirium",
    "icu delirium": "delirium",
    # pulmonology
    "obstructive sleep apnea": "sleep apnea, obstructive",
    "obstructive sleep apnoea": "sleep apnea, obstructive",
    "sleep apnea": "sleep apnea, obstructive",
    "osa": "sleep apnea, obstructive",
    "asthma": "asthma",
    "copd": "copd",
    "chronic obstructive pulmonary disease": "copd",
    "pneumonia": "pneumonia",
    "pulmonary fibrosis": "pulmonary fibrosis",
    "pulmonary hypertension": "pulmonary hypertension",
    "bronchiectasis": "bronchiectasis",
    "cystic fibrosis": "cystic fibrosis",
    "pneumothorax": "pneumothorax",
    "acute respiratory distress syndrome": "acute respiratory distress syndrome",
    "respiratory distress syndrome": "acute respiratory distress syndrome",
    "ards": "acute respiratory distress syndrome",
    # psychiatry
    "major depression": "depressive disorder, major",
    "major depressive disorder": "depressive disorder, major",
    "mdd": "depressive disorder, major",
    "schizophren*": "schizophrenia",
    "bipolar disorder": "bipolar disorder",
    "autism spectrum disorder": "autism spectrum disorder",
    "autis*": "autism spectrum disorder",
    "attention deficit hyperactivity disorder": (
        "attention deficit disorder with hyperactivity"
    ),
    "attention-deficit/hyperactivity disorder": (
        "attention deficit disorder with hyperactivity"
    ),
    "adhd": "attention deficit disorder with hyperactivity",
    "anxiety disorder*": "anxiety disorders",
    "generalized anxiety disorder": "anxiety disorders",
    "post-traumatic stress disorder": "post-traumatic stress disorder",
    "posttraumatic stress disorder": "post-traumatic stress disorder",
    "ptsd": "post-traumatic stress disorder",
    "substance use disorder": "substance-related disorders",
    "substance abuse": "substance-related disorders",
    "substance-related disorder*": "substance-related disorders",
    "alcoholism": "alcoholism",
    "alcohol use disorder": "alcoholism",
    "alcohol dependence": "alcoholism",
    "opioid use disorder": "opioid-related disorders",
    "opioid addiction": "opioid-related disorders",
    "opioid dependence": "opioid-related disorders",
    "obsessive-compulsive disorder": "obsessive-compulsive disorder",
    "ocd": "obsessive-compulsive disorder",
    "panic disorder": "panic disorder",
    "insomnia": "insomnia",
    "chronic insomnia": "insomnia",
    # endocrinology_metabolism
    "diabetes mellitus": "diabetes mellitus",
    "diabetes": "diabetes mellitus",
    "type 1 diabetes": "diabetes mellitus, type 1",
    "type 1 diabetes mellitus": "diabetes mellitus, type 1",
    "type 2 diabetes": "diabetes mellitus, type 2",
    "type 2 diabetes mellitus": "diabetes mellitus, type 2",
    "obesity": "obesity",
    "obese": "obesity",
    "hypothyroidism": "hypothyroidism",
    "hyperthyroidism": "hyperthyroidism",
    "metabolic syndrome": "metabolic syndrome",
    # gastroenterology_hepatology
    "crohn's disease": "crohn disease",
    "crohn disease": "crohn disease",
    "ulcerative colitis": "colitis, ulcerative",
    "peptic ulcer": "peptic ulcer",
    "peptic ulcer disease": "peptic ulcer",
    "liver cirrhosis": "liver cirrhosis",
    "cirrhosis": "liver cirrhosis",
    "fatty liver disease": "fatty liver",
    "nonalcoholic fatty liver disease": "fatty liver",
    "nafld": "fatty liver",
    "irritable bowel syndrome": "irritable bowel syndrome",
    "ibs": "irritable bowel syndrome",
    "celiac disease": "celiac disease",
    "coeliac disease": "celiac disease",
    # nephrology_urology
    "chronic kidney disease": "chronic kidney disease",
    "ckd": "chronic kidney disease",
    "end-stage renal disease": "chronic kidney disease",
    "esrd": "chronic kidney disease",
    "acute kidney injury": "acute kidney injury",
    "aki": "acute kidney injury",
    "nephrolithiasis": "nephrolithiasis",
    "kidney stone*": "nephrolithiasis",
    "urinary tract infection*": "urinary tract infections",
    "uti": "urinary tract infections",
    "polycystic kidney disease": "polycystic kidney disease",
    "nephrotic syndrome": "nephrotic syndrome",
    "benign prostatic hyperplasia": "benign prostatic hyperplasia",
    "bph": "benign prostatic hyperplasia",
    # pediatrics
    "premature birth": "premature birth",
    "preterm birth": "premature birth",
    "prematurity": "premature birth",
    "congenital heart defect*": "congenital heart defects",
    "congenital heart disease": "congenital heart defects",
    "sudden infant death syndrome": "sudden infant death syndrome",
    "sids": "sudden infant death syndrome",
    # obstetrics_gynecology
    "pre-eclampsia": "pre-eclampsia",
    "preeclampsia": "pre-eclampsia",
    "gestational diabetes": "diabetes, gestational",
    "endometriosis": "endometriosis",
    # musculoskeletal
    "osteoarthritis": "osteoarthritis",
    "rheumatoid arthritis": "arthritis, rheumatoid",
    "osteoporosis": "osteoporosis",
    "low back pain": "low back pain",
    "lower back pain": "low back pain",
    # healthy
    "healthy control*": "healthy controls",
    "healthy volunteer*": "healthy controls",
    "normal control*": "healthy controls",
}

# The canonical condition label set this module can ever emit (from
# CONDITION_TERMS directly, plus BODYPART_HINTS -- asserted below to be a
# subset, so the two tables never drift apart).
CONDITION_LABELS: frozenset[str] = frozenset(CONDITION_TERMS.values())

# ---------------------------------------------------------------------------
# Species: ANIMAL_TERMS / HUMAN_TERMS
# ---------------------------------------------------------------------------

# Plain term tuples, not value tables -- the only output is the fixed
# string "animal" (see `hints()`), never a per-term value. Bare "mouse"
# and "rat" are deliberately absent (Ruling R14): "rat" is exactly the
# term that exposed the inferred-stemming bug (it silently matched as a
# prefix inside "rate"/"ratio"/"rational"), and both single words are
# common enough on their own that requiring a qualifying word ("rat
# model", "mouse brain", a named strain) is worth the small recall cost.
ANIMAL_TERMS: tuple[str, ...] = (
    "mice",
    "murine",
    "mouse model*",
    "mouse brain",
    "mouse cortex",
    "transgenic mouse",
    "mouse hippocamp*",
    "knockout mouse",
    "mouse strain*",
    "rats",
    "rat model*",
    "rat brain",
    "rat cortex",
    "rat hippocamp*",
    "sprague-dawley",
    "sprague dawley",
    "wistar",
    "long-evans",
    "rodent*",
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
# conditions, part of this module's own canonical label set), every
# `vocab.CONDITION_ALIASES` value is covered, and the regex cache is warmed
# for every term now rather than on first search. `_require` raises
# instead of using bare `assert` so these checks run the same way whether
# or not Python is invoked with `-O` (which strips assertions).
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


for _value in MODALITY_TERMS.values():
    _require(
        _value in vocab.MODALITIES, f"MODALITY_TERMS target {_value!r} not in vocab"
    )

for _value in DOMAIN_TERMS.values():
    _require(_value in vocab.DOMAINS, f"DOMAIN_TERMS target {_value!r} not in vocab")

for _kind, _target in PHYSIONET_TOPICS.values():
    if _kind == "modality":
        _require(
            _target in vocab.MODALITIES,
            f"PHYSIONET_TOPICS modality target {_target!r} not in vocab",
        )
    elif _kind == "domain":
        _require(
            _target in vocab.DOMAINS,
            f"PHYSIONET_TOPICS domain target {_target!r} not in vocab",
        )
    else:
        raise RuntimeError(
            f"PHYSIONET_TOPICS kind must be modality/domain, got {_kind!r}"
        )

for _label in BODYPART_HINTS.values():
    _require(
        _label in CONDITION_LABELS,
        f"BODYPART_HINTS target {_label!r} not in CONDITION_LABELS",
    )

for _alias_label in vocab.CONDITION_ALIASES.values():
    _require(
        _alias_label in CONDITION_LABELS,
        f"vocab.CONDITION_ALIASES value {_alias_label!r} missing from CONDITION_TERMS",
    )

_require("animal" in vocab.SPECIES, "'animal' missing from vocab.SPECIES")
_require("human" in vocab.SPECIES, "'human' missing from vocab.SPECIES")

for _term in (
    *MODALITY_TERMS,
    *DOMAIN_TERMS,
    *CONDITION_TERMS,
    *ANIMAL_TERMS,
    *HUMAN_TERMS,
    *_CANCER_MENTION_TERMS,
):
    _rx(_term)  # force compilation now, not on first hints() call

del _value, _kind, _target, _label, _alias_label, _term


# ---------------------------------------------------------------------------
# hints()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleHits:
    """Deterministic classification signal for one record's text.

    `evidence` is flat across domains/modalities/conditions (a value
    never collides as a string across those three categories), mapping
    each assigned value to the literal substring(s) that matched it in
    the input, plus one extra key, `"species"`, holding whichever
    animal/human substrings were matched -- present whenever either kind
    matched, regardless of whether `species` ended up `"animal"` or
    `None` (e.g. both an animal and a human term present cancels the
    call, but the evidence for *why* is still worth keeping). A value
    type: frozen, so a `RuleHits` cannot be mutated in place once built.
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

    animal_matches: list[str] = []
    for term in ANIMAL_TERMS:
        match = _rx(term).search(full_text)
        if match and match.group(0) not in animal_matches:
            animal_matches.append(match.group(0))

    human_matches: list[str] = []
    for term in HUMAN_TERMS:
        match = _rx(term).search(full_text)
        if match and match.group(0) not in human_matches:
            human_matches.append(match.group(0))

    species = "animal" if animal_matches and not human_matches else None
    for matched in (*animal_matches, *human_matches):
        record("species", matched)

    domains = [d for d in vocab.DOMAINS if d in domain_hits]
    modalities = [m for m in vocab.MODALITIES if m in modality_hits]
    conditions = sorted(condition_hits)

    ordered_keys = [*domains, *modalities, *conditions]
    if "species" in evidence:
        ordered_keys.append("species")
    ordered_evidence = {value: evidence[value] for value in ordered_keys}

    return RuleHits(
        domains=domains,
        modalities=modalities,
        conditions=conditions,
        species=species,
        evidence=ordered_evidence,
    )
