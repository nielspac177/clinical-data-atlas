# Clinical Data Atlas — Design Spec (2026-08-22)

> The binding spec is **PROJECT_BRIEF.md** (user-supplied, verbatim) plus the decisions, verified endpoints, frozen schema and data contracts below (extracted from the approved implementation plan `docs/superpowers/plans/2026-08-22-clinical-data-atlas-phase0.md`). Where this file and the plan disagree, this file wins; where this file and PROJECT_BRIEF.md disagree, the decision table below records why.

## Decisions & assumptions (stated instead of blocking questions)
| # | Decision | Why / alternative |
|---|---|---|
| D1 | Repo at `~/clinical-data-atlas`, public GitHub repo `nielspac177/clinical-data-atlas`, Pages project site `https://nielspac177.github.io/clinical-data-atlas/` (custom domain later, `BASE_URL` configurable) | User asked for GitHub Pages/free hosting; Pages via Actions keeps bot-PR/merge-deploy separation |
| D2 | Brief says "Owner: Kelvin"; git identity is Niels Pacheco-Barrios. `PROJECT_BRIEF.md` kept verbatim; `CITATION.cff`/About use git identity; `MAINTAINER` lives in `atlas/config.py` + `site/assets/js/config.js` for a one-line change | Avoid fabricating attribution; reversible |
| D3 | Phase 0 sources = OpenNeuro, PhysioNet, GDC, TCIA (≈2,600 records). DHS/World Bank/Crossref come in Phases 1–2 per the brief's phase order | Follows brief |
| D4 | Endpoint corrections: PhysioNet `GET /api/v1/project/published/` (brief's `/rest/...` 404s); TCIA = NBIA v1 public endpoints + DataCite prefix `10.7937` for descriptions/licenses/DOIs (`getCollectionDescriptions` 500/401); DataCite uses `page[cursor]` paging; DHS `robots.txt` disallows `/rest/dhs/` → DHS harvest opt-in + documented | Verified live 2026-08-22 |
| D5 | Schema = pydantic v2 model, single source of truth → `docs/schema.json` + `docs/schema.md` generated; frozen v1 below | One definition for validation, docs and LLM output schema |
| D6 | LLM enrichment backend pluggable: `anthropic` SDK if `ANTHROPIC_API_KEY` → else `claude -p --json-schema` CLI → else rules-only; every LLM output cached per record in `data/raw/enrich/llm/<sha>.json` (committed) | Works locally without key, CI reuses cache, never blocks refresh; load the `claude-api` skill when implementing for current model ids |
| D7 | Raw cache = one pretty JSON per record + `manifest.json` per source; volatile fields stripped before hashing; >20 % listing shrink marks source failed and keeps previous data | Honest, reviewable diffs; guards partial API responses |
| D8 | `access` = least-restrictive tier at which substantive data is usable; `access_tiers[]` + `access_notes` carry nuance (GDC = open + application/dbGaP; PhysioNet Open→open, Restricted→registration, Credentialed→credentialed, Contributor Review→application) | Researcher-centric semantics, documented in schema.md |
| D9 | Site: vanilla ES modules, vendored `3d-force-graph.min.js` (sha256-pinned), system fonts, theme follows `prefers-color-scheme` (dark default) + persisted toggle, backbone-first graph, DOM-overlay labels, lazy `data/records/<source>/<native>.json`, changelog pre-rendered to HTML at build | Self-contained, fast, a11y-friendly |
| D10 | Code MIT; catalog metadata CC BY 4.0 (`data/LICENSE`) | Attribution keeps provenance attached when re-hosted |
| D11 | Execution: foundation tasks sequential; 4 source tasks in parallel (worktrees); enrich/graph/refresh and site tracks in parallel; then integration, real refresh, deploy; then adversarial quality rounds via the Workflow tool + self-paced `/loop` until exit criteria | User asked for adversarial team + loop |
| D12 | Creating the public repo, pushing, enabling Pages and Actions are part of this plan (user asked for deployment). `ANTHROPIC_API_KEY` secret is user-only (optional). The "Allow GitHub Actions to create and approve pull requests" toggle is set via `gh api` (repo-admin action; reported in the final summary) | Needed for monthly bot PRs |

## Verified endpoints (2026-08-22) — record in `docs/sources.md`
| Source | Endpoint (verified) | Count | Notes |
|---|---|---|---|
| OpenNeuro | POST `https://openneuro.org/crn/graphql` `datasets(first:100, after, orderBy:{created:descending})` nodes: `id created publishDate metadata{species studyDomain studyDesign studyLongitudinal dataProcessed ages modalities associatedPaperDOI grantFunderName dxStatus affirmedDefaced} latestSnapshot{tag created description{Name Authors License DatasetDOI Funding ReferencesAndLinks HowToAcknowledge} readme summary{subjects sessions modalities secondaryModalities tasks totalFiles size dataProcessed}}` | 1,864 | no auth; robots allow; SPA pages → GraphQL only; url `https://openneuro.org/datasets/<id>`; DOI `doi:10.18112/openneuro.<id>.vX` |
| PhysioNet | GET `https://physionet.org/api/v1/project/published/` (single 1.3 MB array) | 716 (532 latest; Database 586/Challenge 53/Software 62/Model 15) | keys: slug, version, core_doi, version_doi, is_latest_version, title, short_description, abstract(HTML), license{name}, dua{name}, access_policy (Open/Credentialed/Restricted/Contributor Review), resource_type, topics[], publish_date, main_storage_size, source_url |
| GDC | GET `https://api.gdc.cancer.gov/projects?size=100&expand=summary,summary.data_categories,summary.experimental_strategies,program` | 93 | fields project_id, name, primary_site[], disease_type[], program{name,dbgap_accession_number}, summary{case_count,file_count,file_size,data_categories[],experimental_strategies[]}, released, state; url `https://portal.gdc.cancer.gov/projects/<id>` |
| TCIA | NBIA v1 `…/nbia-api/services/v1/getCollectionValues` (156 public) + per collection `getModalityValues`, `getBodyPartValues`, `getPatient` (count/species/phantom); DataCite `https://api.datacite.org/dois?prefix=10.7937&page[size]=100&page[cursor]=1` (317 DOIs; title/description/url/rights/creators/relatedIdentifiers) | 156 (+ gated DataCite-only) | `getCollectionDescriptions` & `*ValuesAndCounts` need token → unusable; match NBIA↔DataCite by alt-title / parenthesised suffix / url slug; 5 ambiguous → prefer url kind `collection`, latest year |
| Crossref (Phase 1) | `https://api.crossref.org/journals/{issn}/works?rows=100&cursor=*&filter=from-index-date:…&select=…` | 8,870 / 12,940 | use from-index-date; never persist cursors; JATS abstracts |
| DHS (Phase 2, opt-in) | `https://api.dhsprogram.com/rest/dhs/surveys?f=json&perpage=100&page=N` | ~374 | robots.txt disallows `/rest/dhs/` → opt-in flag + note |
| World Bank NADA (Phase 2) | `https://microdata.worldbank.org/index.php/api/catalog/search?format=json&ps=100&page=N` | thousands | filter health-relevant |
| INEI ENDES | no NADA API (404) → curated; also DHS `PE` | — | |
| Ancillary | MeSH `https://id.nlm.nih.gov/mesh/lookup/descriptor?label=<x>&match=exact`; ROR `https://api.ror.org/v2/organizations?affiliation=<x>` | — | cached, offline-safe |

## Frozen schema v1 (pydantic model `atlas/schema.py`; JSONL one record per line, keys sorted)
Required unless marked O. Lists default `[]`.
- `id` `<source>:<source_native_id slug>` (regex `^[a-z_]+:[A-Za-z0-9._-]+$`); `source` enum `openneuro|physionet|gdc|tcia|scientific_data|data_in_brief|dhs|worldbank|curated`; `source_native_id`; `name`; `summary` (≤40 words, enforced); `url` (http/https)
- O `dataset_doi` (version-collapsed), O `version`, O `published` (ISO date)
- `domains[]` enum (17): `neurology, psychiatry, neuroscience, cardiology, oncology, pulmonology, critical_care, surgery, pediatrics, obstetrics_gynecology, infectious_disease, endocrinology_metabolism, gastroenterology_hepatology, nephrology_urology, musculoskeletal, public_health, other`
- `modalities[]` enum (33): `MRI, fMRI, dMRI, MRS, PET, SPECT, CT, xray, mammography, ultrasound, radiotherapy, pathology, EEG, MEG, iEEG, fNIRS, ECG, EMG, PPG, physiological_signals, wearable, eye_tracking, behavioral, genomics, transcriptomics, proteomics, EHR, clinical_notes, claims, registry, survey, clinical_tabular, other`
- `conditions[] {label, mesh_id?}`; O `keywords[]`; O `population`; `species` enum `human|animal|mixed|phantom|simulated|unknown`
- `sample_size` int|null + `sample_unit` enum `participants|cases|admissions|records|images|studies|series|households|surveys|other`|null; O `size_bytes`
- `countries[]` ISO-3166-1 alpha-2; `years {start,end}` ints|null
- `access` enum `open|registration|credentialed|application|purchase` (ordered; conflicts → more restrictive); O `access_tiers[]`; O `access_notes`; O `access_howto`
- O `license` (SPDX when mappable via `LICENSE_MAP`, else verbatim)
- `institutions[] {name, ror_id?, country?}`; `authors[] {name, orcid?}`; `papers[] {doi, title?, relation: describes|cites|is_cited_by|other}`; `related[] {id, relation: same_cohort|duplicate_of|derived_from|part_of}` (ids must exist)
- `record_status` `active|needs_review|removed`
- `provenance {harvested_via, harvested_at, last_verified, raw_hash?, enrichment {method: rules|llm|rules+llm|curated, model?, prompt_version?, at?, fields: {field: origin}}}`
Validation: errors = model failure, duplicate id, dangling related, summary > 40 words, bad url; warnings = empty domains/modalities, non-open access without access_notes, sample_size without unit. `atlas schema --export` writes `docs/schema.json` + `docs/schema.md`; CI fails on drift.

## Data contracts (consumed by the site — fixed)
- `data/graph/graph.json`: `{"nodes":[{id,type:dataset|modality|condition|institution|source,label,source?,domains?,access?,degree,backbone?:true}],"links":[{source,target,type}]}`; ids `modality:EEG`, `condition:<slug>`, `institution:<ror|slug>`, `source:<source>`; edges dataset→source/modality/condition/institution(top 3), dataset↔dataset (related). Backbone = all non-dataset nodes except institutions with degree<2; top-30 conditions flagged; ≈1.1 MB.
- `data/graph/search-index.json`: rows `{id,name,summary,source,domains,modalities,conditions(labels),countries,access,years,sample_size,sample_unit,url,species}` sorted by id.
- `data/graph/stats.json`: `{record_count, per_source, per_domain, per_modality, per_access, backbone_count}` (no dates).
- `data/catalog/catalog.jsonl` (+ `excluded.jsonl` with `{id, reason}`); site build splits into `_site/data/records/<source>/<native>.json`.
- `data/changelog/YYYY-MM-DD.md` + `latest.md`; site build renders newest 12 into `whats-new.html`.

