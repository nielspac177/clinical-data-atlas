# CLAUDE.md — Clinical Data Atlas

## 1. Mission & non-negotiables
A public, living catalog of open & gated clinical/neuroscience datasets: a
3D knowledge graph + filterable table, refreshed monthly. Full brief:
`PROJECT_BRIEF.md`. Frozen decisions & schema:
`docs/superpowers/specs/2026-08-22-clinical-data-atlas-design.md`.

- **Metadata only, never data.** Catalog and link out; never download,
  rehost, or redistribute a dataset's actual contents. Restricted sources
  (ADNI, PPMI, NIS, NSQIP, SEER, ...) are cataloged from public docs only.
- **APIs first, polite scraping second, never against ToS.** Every request
  sends `atlas.config.UA` = `clinical-data-atlas/<version> (+<REPO_URL>;
  mailto:<MAILTO>)`, rate-limited (`POLITE_DELAY`, 0.35s/host default),
  retried with exponential backoff (429 yes, other 4xx no). robots.txt is
  checked by hand per host when a source is onboarded and recorded in
  `docs/sources.md`; the client does not re-fetch it per request, so a new
  or moved endpoint needs that check redone.
- **One canonical schema** (`atlas/schema.py`) for every record, whatever
  the source.
- **Provenance on every record**: source, harvest method, harvested-at,
  last-verified.
- **Reproducible refresh**: `make refresh` reruns the whole pipeline with
  one command and writes a diff. One broken source must not kill the run.
- **Never fabricate a record.** If a source can't be verified, mark it and
  move on.

## 2. Repo map
| Path | Contents |
|---|---|
| `atlas/` | Package: config, http, io, vocab, schema, cli, refresh, diff, sitebuild |
| `atlas/harvest/` | One module per source: raw API -> raw JSON |
| `atlas/normalize/` | Raw source records -> canonical schema |
| `atlas/enrich/` | LLM classification, summaries, MeSH/ROR resolution, dedupe |
| `atlas/graph/` | Build graph nodes+edges JSON and the search index |
| `atlas/tools/` | Hand-run maintenance tools (og.png rasteriser); never imported by the pipeline |
| `data/raw/<source>/` | Raw harvested metadata, committed — it IS the provenance |
| `data/catalog/` | Merged canonical catalog (JSONL) — the product |
| `data/graph/` | `graph.json` + `search-index.json` consumed by the site |
| `data/changelog/` | One markdown diff per monthly refresh |
| `site/` | Static site (no backend), deployed to GitHub Pages |
| `docs/` | `sources.md` + `sources/`, `schema.md`, `schema.json`, `superpowers/` (spec + plan); `quality/` once the review loop has run |
| `tests/` | `conftest.py`, fixtures, unit/e2e tests |
| `.github/` | CI, deploy, monthly-refresh workflows; issue forms |

## 3. Commands
Makefile: `setup vendor harvest normalize enrich graph diff validate refresh
site serve test test-js test-live e2e og dod clean` — run `make help` for one-line
descriptions.

`atlas` CLI subcommands (`atlas/cli.py`): `schema` (export docs/schema.json
+ docs/schema.md), `harvest`, `normalize`, `enrich`, `graph`, `diff`,
`validate`, `refresh` (orchestrates all of the above), `check-urls`, `dod`
(definition-of-done gate for a phase). Run python commands via `uv run`.

`atlas refresh` == `python -m atlas.refresh` (same flags, same code):
`--sources a,b --skip-enrich --offline --fast --strict --dry-run --llm X
--max-llm-calls N --report PATH --summary-json PATH`. Exit codes: 0 ok, 1
validation errors (nothing written), 2 a source failed under `--strict` or
the run was misconfigured, 3 every source failed (nothing written).
`--offline` skips harvesting and normalizes `data/raw/` as it stands, with
the LLM/MeSH/ROR caches serving reads only. `enrich`/`graph`/`diff` run
single stages of that same pipeline for debugging; only `refresh` writes
`data/catalog/catalog.jsonl`, and `atlas enrich` writes its records to the
gitignored `.cache/enriched.jsonl` (its real product is the warmed
LLM/MeSH/ROR caches under `data/raw/enrich/`).

## 4. Schema
`atlas/schema.py` (pydantic v2) is the single source of truth. `docs/schema.json`
and `docs/schema.md` are generated from it via `atlas schema --export` —
never hand-edit them. CI fails the build if the checked-in docs drift from
the model.

## 5. Sources
`docs/sources.md` is the index (endpoint, auth, politeness, verified date,
count); one detail file per source lives at `docs/sources/<source>.md`.
Each source is a pair of modules: `atlas/harvest/<src>.py` (talks to the
API, writes `data/raw/`) and `atlas/normalize/<src>.py` (raw -> canonical
schema).

## 6. Working agreements
- Commit style: `<area>: <imperative>`, area one of `harvest|normalize|
  enrich|graph|site|data|ci|docs|test|chore|quality` (e.g. `harvest: add
  TCIA collections`).
- Tests required: schema validation on every record, plus a smoke test per
  harvester against a cached fixture.
- No network in unit tests — `tests/conftest.py` blocks sockets whenever
  `ATLAS_OFFLINE=1` (the default under pytest). Tests needing the real
  network are marked `live` or `e2e` and only run via `make test-live` /
  `make e2e` (`ATLAS_LIVE=1`, `-m live`).
- Never hand-edit generated data under `data/catalog/` or `data/graph/` —
  it's produced by `make refresh` / `make graph`. Fix the pipeline, then
  regenerate.
- The monthly refresh bot opens a PR with the diff; a human merges it;
  merging to `main` is what deploys the site.
- Secrets (e.g. `ANTHROPIC_API_KEY`) only ever come from GitHub Actions
  secrets or a local, gitignored `.env` — never committed, never logged.

## 7. How to add a source
1. Probe the live API by hand first; record the verified endpoint, auth,
   and quirks in `docs/sources/<src>.md` (and update `docs/sources.md`'s
   index row).
2. Write `atlas/harvest/<src>.py` defining a `Harvester` subclass (class
   attrs `name`, `harvest_method`) that fetches and caches raw records
   under `data/raw/<src>/` — it is auto-discovered by
   `atlas.harvest.get_registry()`, no registration call.
3. Write `atlas/normalize/<src>.py` defining `SOURCE`, `normalize()` (raw
   record -> canonical schema instance), and `enrichment_text()` (text fed
   to LLM classification) — auto-discovered by
   `atlas.normalize.get_normalizers()`.
4. Add fixtures under `tests/fixtures/<src>/` and unit tests against them;
   add one `live`-marked smoke test against the real API.
5. Run `make refresh SOURCE=<src>` to harvest just that source end to end.

## 8. How to run the quality loop
Adversarial review rounds are logged under `docs/quality/` (`README.md`,
`scorecard.json`, `findings.jsonl`, `rounds/`). Check current status against
a phase's definition of done with `make dod PHASE=<n>`; keep running rounds
until `dod` is green.

## 9. Release checklist
- [ ] CI green on `main`.
- [ ] `make dod PHASE=<n>` green.
- [ ] `CITATION.cff` version and release date bumped.
- [ ] `data/changelog/` has an entry for this release.
- [ ] Tag the release (`git tag vX.Y.Z`).

## 10. Do not
- Do not hand-edit generated data under `data/catalog/` or `data/graph/`.
- Do not commit `_site/` (build output) or anything under `site/data/`.
- Do not pin `astral-sh/setup-uv` (or any Action) to a floating tag — pin
  by SHA.
- Do not print secrets or API keys to logs, or commit them anywhere.
- Do not download or rehost a dataset's actual contents — metadata only,
  ever.
