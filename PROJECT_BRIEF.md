# Clinical Data Atlas — Claude Code Build Prompt
> **How to use this file:** Save it as `PROJECT_BRIEF.md` in an empty folder (your future repo), open Claude Code there, and start with:
> *"Read PROJECT_BRIEF.md and build Phase 0. Verify every API against its live endpoint before writing the harvester for it. Show me the schema for approval before harvesting."*
> Then work phase by phase — one phase per session is a good rhythm. This brief is written to be the project's standing context, so also keep it (or a trimmed version) as `CLAUDE.md` so every future session reads it automatically.
---
## Project
Build **Clinical Data Atlas**: a public, living, explorable catalog of open and gated clinical & neuroscience datasets, presented as an interactive 3D knowledge graph plus a filterable table, refreshed monthly. Same concept as https://datasets.neuro2.ai/ but widened from neuroimaging to the whole clinical landscape.
**Owner:** Kelvin. **Status:** greenfield. **This repo is the single source of truth** for harvest scripts, raw per-source metadata, the merged catalog, and the static site.
## Non-negotiable principles
1. **Metadata only, never data.** The atlas catalogs and links out. It never downloads, rehosts, or redistributes any dataset's actual data. Restricted sources (ADNI, PPMI, NIS, NSQIP, SEER…) are cataloged from their public documentation pages only.
2. **APIs first, polite scraping second, never against terms of service.** Set a descriptive User-Agent, rate-limit all requests, respect robots.txt.
3. **One canonical schema** for every record, whatever the source.
4. **Provenance on every record:** source, harvest method, date last verified.
5. **Reproducible refresh:** the entire pipeline must rerun with one command (`make refresh` or `python -m atlas.refresh`) and produce a diff of what changed.
6. **Verify before building:** every API named below must be checked against its live endpoint before its harvester is written — if an endpoint has changed or is wrong in this brief, adapt and note it in `docs/sources.md`.
## Repo structure
```
atlas/
  harvest/          # one module per source (openneuro.py, physionet.py, ...)
  normalize/        # mapping raw source records -> canonical schema
  enrich/           # LLM classification, summaries, dedupe
  graph/            # build nodes+edges JSON and search index
  refresh.py        # orchestrates the whole pipeline, writes the diff
data/
  raw/<source>/     # raw harvested metadata, committed (it IS the provenance)
  catalog/          # merged canonical catalog (JSONL), the product
  graph/            # graph.json + search-index.json consumed by the site
  changelog/        # one markdown diff per monthly run
site/               # static site (no backend) — deployed to GitHub/Cloudflare Pages
docs/
  sources.md        # per-source notes: endpoint, quirks, last verified
  schema.md         # canonical schema documentation
PROJECT_BRIEF.md    # this file
```
## Canonical schema (v1 — propose refinements before Phase 0 harvest, then freeze)
Each catalog record (JSONL, one per dataset):
- `id` — stable slug, `<source>:<source_native_id>`
- `name`, `summary` (≤ 40 words, plain language)
- `source` — e.g. `openneuro`, `physionet`, `scientific_data`, `curated`
- `url` — official dataset home (outbound link; the atlas's whole job)
- `domains[]` — clinical domain(s): neurology, cardiology, oncology, surgery, public_health, …
- `modalities[]` — controlled vocabulary: MRI, fMRI, EEG, ECG, CT, PET, pathology, genomics, EHR, claims, survey, registry, wearable, …
- `conditions[]` — `{label, mesh_id?}` — code to MeSH where resolvable
- `population` — free text (e.g. "US adults 18+", "PD patients + controls")
- `sample_size` — integer or null; `sample_unit` (participants, admissions, records, images)
- `countries[]` — ISO 3166 alpha-2
- `years` — `{start, end}` of data collection, nulls allowed
- `access` — one of `open | registration | credentialed | application | purchase`
- `license` — SPDX id or free text as stated by the source
- `institutions[]` — `{name, ror_id?}`
- `authors[]` — `{name, orcid?}` (may be empty for national datasets)
- `papers[]` — `{doi, title}` — data descriptor and key citations
- `provenance` — `{harvested_via, harvested_at, last_verified}`
Graph node types: **dataset, modality, condition, institution, source**. Edges: dataset→modality, dataset→condition, dataset→institution, dataset→source, dataset→paper (papers as node type is optional, decide in Phase 1).
## Sources
### Tier A — automated harvesters (rerun monthly)
| Source | Route to try first | Notes |
|---|---|---|
| OpenNeuro | Public GraphQL API at openneuro.org | ~1,400 BIDS datasets; modality from BIDS metadata |
| PhysioNet | Published project index / REST endpoint on physionet.org | Include access tier (open vs credentialed, e.g. MIMIC) |
| Scientific Data | Crossref API filtered by ISSN `2052-4463`, type journal-article | Every article is a Data Descriptor; extract deposit links from abstract/metadata; LLM filters clinical relevance |
| Data in Brief | Crossref API filtered by ISSN `2352-3409` | Huge and multi-domain — LLM classification decides which articles earn a node |
| NCI GDC | GDC REST API (api.gdc.cancer.gov) | Projects = datasets (TCGA-*, TARGET, CPTAC…) |
| TCIA | TCIA REST API | ~200 imaging collections; map to `modalities` from collection metadata |
| DHS Program | DHS API (api.dhsprogram.com) | Surveys across 90+ countries — this family includes ENDES-like surveys |
| World Bank Microdata | NADA catalog API (microdata.worldbank.org, DDI metadata) | National household/health surveys; filter to health-relevant |
### Tier B — curated once, monitored monthly
NHANES (CDC; cycle-based releases), PPMI and ADNI (LONI IDA; application access), HCUP NIS (AHRQ; purchase + DUA), ACS NSQIP (participant sites), SEER (signed access), ENDES (INEI Peru microdata portal — NADA-based, may be partially harvestable), plus expansion candidates later (UK Biobank, MIDRC, cBioPortal).
For each: a rich hand-written record in `data/raw/curated/` (same schema) including an `access_howto` field — one paragraph on how a researcher actually gets the data. The monthly monitor checks each source's release/news page and flags changes rather than auto-rewriting entries.
### Enrichment (runs on all new records)
- Classify: clinical vs out-of-scope (journals only), `domains`, `modalities`, `conditions`.
- Summarize: the ≤40-word `summary`.
- Dedupe: same dataset appearing via repository AND journal descriptor merges into one record (repository wins as primary `url`; the paper goes to `papers[]`). Match on DOI links, title similarity, author overlap.
## The site (static, no backend)
- **3D graph view:** `3d-force-graph` (three.js), fed `data/graph/graph.json`. Default view = backbone only (sources, modalities, conditions, top institutions — a few hundred nodes); clicking expands a node's dataset neighborhood; search jumps to a dataset's ego network. Color = clinical domain; node size = degree; side panel shows the full record with outbound link and access badge.
- **Table view:** client-side filterable/searchable table (domain, modality, country, access tier, years) over `search-index.json`.
- **What's new page:** renders the latest files in `data/changelog/`.
- **About/methodology page:** what the atlas is, what it isn't (metadata only), how to cite, how to propose a dataset (GitHub issue template with schema-shaped form).
- Dark + light theme; responsive; no horizontal page scroll.
- Deploy: GitHub Pages or Cloudflare Pages from the repo; custom domain when ready.
## Phases — build in this order, get approval between phases
**Phase 0 — prototype.** Propose & freeze schema → harvesters for OpenNeuro, PhysioNet, GDC, TCIA → normalize + enrich → graph build → first working site with graph + table on this ~2,000-dataset core. *Done when: `refresh` runs end-to-end with one command and the site is clickable locally.*
**Phase 1 — journals.** Crossref harvesters for Scientific Data and Data in Brief → LLM clinical-relevance filter → dedupe against Phase 0 records. *Done when: journal records are in the catalog, spot-checking 30 random records shows >90% correctly classified, and no obvious repository/journal duplicates survive.*
**Phase 2 — curated layer.** Write Tier B entries (NHANES, PPMI, ADNI, NIS, NSQIP, SEER, ENDES, DHS/World Bank families) with `access_howto` → build the monthly monitor that checks their release pages. *Done when: every Tier B source has a complete, hand-verified record.*
**Phase 3 — launch & automate.** About page, citation guidance, issue-template submission form, custom domain, deploy → wire the monthly refresh (GitHub Actions cron calling `refresh`, or an external scheduler) → changelog email/report. *Done when: a monthly run completes unattended and produces a human-readable diff.*
## Working agreements for Claude Code
- Python 3.11+, minimal dependencies, typed where it helps. Every harvester independently runnable and independently failable — one broken source must not kill the refresh; it gets reported in the diff instead.
- Cache raw API responses in `data/raw/` so reruns are cheap and diffs are honest.
- Small commits per source/feature; conventional messages ("harvest: add TCIA collections").
- Write `docs/sources.md` as you go — endpoint used, auth needs, quirks, verified date.
- Never fabricate a record. If a source can't be verified, mark it and move on.
- Tests: at minimum, schema validation on every catalog record and a smoke test per harvester against a cached fixture.
