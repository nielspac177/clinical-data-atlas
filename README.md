# Clinical Data Atlas

[![CI](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/ci.yml/badge.svg)](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/ci.yml)
[![Deploy Pages](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/deploy-pages.yml/badge.svg)](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/deploy-pages.yml)
[![Code: MIT](https://img.shields.io/badge/code-MIT-blue.svg)](LICENSE)
[![Data: CC BY 4.0](https://img.shields.io/badge/data-CC%20BY%204.0-blue.svg)](data/LICENSE)

**A living, explorable catalog of open and gated clinical & neuroscience
datasets.** Clinical data is not hard to find because it is hidden — it is
hard to find because it is scattered across repositories, journal data
descriptors and institutional portals, each with its own vocabulary for the
same ideas. The atlas harvests those sources through their public APIs,
normalizes every result into one canonical record, and publishes the whole
thing twice over: as a 3D knowledge graph of how sources, modalities,
conditions and institutions connect, and as a filterable table of the same
records. It refreshes itself monthly, and the data files — not the website —
are the product.

**→ [nielspac177.github.io/clinical-data-atlas](https://nielspac177.github.io/clinical-data-atlas/)**

## What it is

- **2,655 datasets** from four repositories, described in [one schema](docs/schema.md):
  17 clinical domains, 33 modalities, conditions resolved against MeSH,
  institutions against ROR, countries as ISO codes, and five access tiers
  from `open` to `purchase`.
- **A 3D graph view** that starts from a backbone of sources, modalities,
  conditions and institutions, and expands a node's dataset neighborhood on
  click — plus a **table view** with the same records, the same detail
  panel, and full keyboard access.
- **Provenance on every field.** Each record names how it was harvested,
  when, when a refresh last confirmed it, and a hash of the raw API
  response it came from — and that raw response is committed to this repo.
- **Rules-first enrichment.** An LLM is used only where the rules can't
  decide (classification, the ≤40-word summary), each output is cached per
  record, and every enriched field records its own origin. Nothing is
  invented: a sample size the source doesn't state stays `null`.
- **One-command reproducibility.** `make refresh` reruns harvest →
  normalize → enrich → graph → diff end to end and writes a human-readable
  changelog of what moved. One broken source is reported in that diff
  rather than killing the run.
- **A static site with no backend** — vanilla ES modules, one vendored
  (SHA-256-pinned) 3D graph library, dark and light themes, no tracking.

## What it isn't

- **It never hosts or redistributes dataset contents.** Metadata only — no
  images, no waveforms, no patient records, not even copies of small ones.
  Every entry links out to the source's own page, and that is where access
  happens.
- **It is not an access broker.** Gated resources are described from their
  public documentation; the atlas cannot grant or speed up access to any of
  them.
- **It is not a review or a ranking.** Inclusion is not an endorsement and
  says nothing about a dataset's quality or fitness for your question.
- **It is not a substitute for the source.** Access terms and licenses
  change; always read the dataset's own documentation before planning work
  around it.

## Sources (Phase 0)

| Source | What it covers | Route | Catalog records |
|---|---|---|---|
| [OpenNeuro](docs/sources/openneuro.md) | BIDS neuroimaging datasets — MRI, fMRI, EEG, MEG, iEEG, PET | Public GraphQL API | 1,858 |
| [PhysioNet](docs/sources/physionet.md) | Physiological signal, waveform and clinical databases, incl. credentialed ones (MIMIC, eICU) | Published-projects REST API | 464 |
| [NCI GDC](docs/sources/gdc.md) | Cancer genomics programs — TCGA, TARGET, CPTAC, MATCH | Projects REST API | 92 |
| [TCIA](docs/sources/tcia.md) | Cancer imaging collections, public and gated | NBIA v1 + DataCite | 241 |
| | | **Total** | **2,655** |

Counts are the committed snapshot in `data/catalog/catalog.jsonl`. Every
endpoint was verified by hand against the live API before its harvester was
written; endpoints, auth, politeness, robots.txt findings, quirks, verified
dates and the deviations from the original brief are all in
[`docs/sources.md`](docs/sources.md), with per-source detail (including full
raw→canonical field mappings) under [`docs/sources/`](docs/sources/).

Journal data descriptors (Crossref: *Scientific Data*, *Data in Brief*) and
a hand-curated tier for resources with no API at all (NHANES, ADNI, PPMI,
HCUP NIS, SEER, DHS, World Bank microdata) are Phase 1 and 2 —
see `docs/sources.md § Tier B / Phase 1–2 candidates` and
[`PROJECT_BRIEF.md`](PROJECT_BRIEF.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11 (the project
pins 3.12).

```bash
git clone https://github.com/nielspac177/clinical-data-atlas
cd clinical-data-atlas

uv sync --all-groups        # or: uv sync --locked --group dev  (skips Playwright)
make test                   # unit tests — no network, no browser
make serve BASE_URL=/       # build the site into _site/ and serve it at :8080
```

Then open <http://127.0.0.1:8080/>. `BASE_URL=/` matters locally: the
default (`/clinical-data-atlas/`) is the path the site is deployed under on
GitHub Pages, and it is baked into the build's data-fetch prefix, so a
root-served local build needs the override.

Refreshing the catalog from the live APIs is a separate, slower step:

```bash
make refresh                 # harvest -> normalize -> enrich -> graph -> diff
make refresh SOURCE=tcia     # just one source, end to end
```

A full refresh talks to four APIs (~3 minutes of politely rate-limited
requests for TCIA alone) and, if enrichment is enabled, spends LLM calls on
records whose classification isn't cached. `uv run atlas refresh --offline`
re-runs everything downstream of harvesting against `data/raw/` as it
already stands, which is the fast way to test a normalizer change.

`make help` lists every target. The `atlas` CLI (`uv run atlas <cmd>`) has
the same stages individually — `harvest`, `normalize`, `enrich`, `graph`,
`diff`, `validate`, `refresh`, `schema`, `check-urls`, `dod` — plus
`--help` on each.

## Data model

Every record, from every source, is the same shape: a pydantic v2 model in
`atlas/schema.py`, which is the single source of truth. The human-readable
[`docs/schema.md`](docs/schema.md) and machine-readable
[`docs/schema.json`](docs/schema.json) are generated from it with
`uv run atlas schema --export` and must never be hand-edited — CI runs
`atlas schema --check` and fails the build if they drift.

The catalog itself is `data/catalog/catalog.jsonl`: one record per line,
keys sorted, deterministic. Records the pipeline deliberately dropped
(software packages, unreleased projects) are listed with their reason in
`data/catalog/excluded.jsonl` rather than silently disappearing.

## How the monthly refresh works

A [scheduled GitHub Actions workflow](.github/workflows/monthly-refresh.yml)
runs on the 1st of each month (and on demand) and executes
`python -m atlas.refresh` — the same module the `atlas refresh` CLI wraps,
so it is the same pipeline `make refresh` runs locally. It probes every
source first as a positive control, re-harvests, re-normalizes,
re-enriches, rebuilds the graph and search index, and writes a
human-readable diff into `data/changelog/`.

It never pushes to `main`. It opens a pull request from branch
`bot/monthly-refresh` with that diff as the PR body; a human reviews and
merges it, and merging to `main` is what
[deploys](.github/workflows/deploy-pages.yml) the updated site. A source
that is down is reported in the diff instead of failing the run, and a
listing that shrinks by more than 20 % trips a guard that refuses to delete
anything.

## Propose a dataset, or correct one

Missing datasets and wrong fields are both bugs, and both are fixed by
opening an issue. The two forms are schema-shaped, so a submission arrives
in roughly the right shape already:

- [**Propose a dataset**](https://github.com/nielspac177/clinical-data-atlas/issues/new?template=propose-dataset.yml)
  — a resource that isn't in the catalog yet.
- [**Edit or flag a dataset**](https://github.com/nielspac177/clinical-data-atlas/issues/new?template=dataset-edit.yml)
  — a stale link, a wrong access tier, a field that no longer matches the
  source.

Corrections that belong upstream get fixed in the harvester or the
normalization rules rather than by hand-editing the catalog, so the fix
survives the next refresh.

## Contributing

- **Conventional commits:** `<area>: <imperative>`, where area is one of
  `harvest normalize enrich graph site data ci docs test chore quality`
  (e.g. `harvest: add TCIA collections`). Small commits.
- **Tests are required**, and the bar is specific: schema validation on
  every catalog record, plus a smoke test per harvester against a cached
  fixture under `tests/fixtures/`.
- **No network in unit tests.** `tests/conftest.py` blocks sockets whenever
  `ATLAS_OFFLINE=1`, which is the default under pytest. Tests that need the
  real network are marked `live` or `e2e` and run only via `make test-live`
  / `make e2e`.
- **Never hand-edit generated data** under `data/catalog/` or
  `data/graph/`, and never hand-edit `docs/schema.*`. Fix the pipeline or
  the model, then regenerate.
- **Never fabricate a record or a field.** If a source doesn't state
  something, the field stays null. If a source can't be verified, it gets
  documented, not guessed.
- Before opening a PR: `uv run ruff check . && uv run ruff format --check .`,
  `make test`, `uv run atlas schema --check`, and `make site` (which also
  greps the build for unresolved placeholders) — that is what CI runs.

Adding a whole new source has its own five-step procedure in
[`CLAUDE.md`](CLAUDE.md) — probe the live API by hand first, then write the
harvester, the normalizer, fixtures and docs.

## Citation

`CITATION.cff` is the authoritative, machine-readable version — GitHub
renders a "Cite this repository" button from it. In BibTeX:

```bibtex
@software{pacheco_barrios_clinical_data_atlas_2026,
  author  = {Pacheco-Barrios, Niels},
  title   = {Clinical Data Atlas},
  year    = {2026},
  version = {0.1.0},
  url     = {https://nielspac177.github.io/clinical-data-atlas/},
  note    = {Code MIT; catalog metadata CC BY 4.0}
}
```

Please also cite each dataset you actually use the way its own source asks —
a record's `dataset_doi` and `papers[]` fields are there for exactly that.

## Licenses

| What | License |
|---|---|
| Code — the pipeline and the site | [MIT](LICENSE) |
| Catalog metadata under `data/` | [CC BY 4.0](data/LICENSE) |
| The datasets themselves | Not the atlas's to license. A record's `license` field reports what its source states; the source's own terms always govern. |

## Acknowledgements

The catalog exists because these projects publish open, unauthenticated
metadata APIs and let tools like this one read them politely:
[OpenNeuro](https://openneuro.org/), [PhysioNet](https://physionet.org/),
the [NCI Genomic Data Commons](https://portal.gdc.cancer.gov/), and
[The Cancer Imaging Archive](https://www.cancerimagingarchive.net/), with
DOI metadata from [DataCite](https://datacite.org/), condition codes from
[MeSH](https://www.nlm.nih.gov/mesh/) and organization ids from
[ROR](https://ror.org/). The concept is a widening of
[datasets.neuro2.ai](https://datasets.neuro2.ai/) from neuroimaging to the
whole clinical landscape.

Maintained by Niels Pacheco-Barrios. This is an independent project, not
affiliated with or endorsed by any of the repositories or institutions it
catalogs.
