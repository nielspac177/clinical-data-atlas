# Sources

Index of every source the atlas harvests from, plus the ancillary lookup
services enrichment uses and the candidates queued for Phases 1–2.

Every endpoint below was probed by hand against the live API **before** its
harvester was written, per `PROJECT_BRIEF.md`'s "verify before building"
rule; where the brief's endpoint turned out to be wrong, the correction and
its evidence are recorded here and in the per-source file. Detailed notes —
full quirk lists, complete raw→canonical field mappings, fixture
provenance — live in `docs/sources/<source>.md`; this file is the index and
does not duplicate them.

- Canonical record shape: [`docs/schema.md`](schema.md) (generated from
  `atlas/schema.py`).
- Frozen decisions, including the endpoint corrections:
  [`docs/superpowers/specs/2026-08-22-clinical-data-atlas-design.md`](superpowers/specs/2026-08-22-clinical-data-atlas-design.md).

## Tier A — automated harvesters (Phase 0, refreshed monthly)

| Source | Endpoint(s) used | Auth | Politeness | Endpoints verified | Last harvested | Raw records | Catalog records | Detail |
|---|---|---|---|---|---|---|---|---|
| OpenNeuro | POST `https://openneuro.org/crn/graphql` (`datasets(first:100, after, orderBy:{created:descending})`, cursor-paged) | none | 350 ms/host | 2026-08-22, re-probed 2026-08-23 | 2026-08-23 | 1,858 | 1,858 | [openneuro.md](sources/openneuro.md) |
| PhysioNet | GET `https://physionet.org/api/v1/project/published/` (single unpaginated array) | none | 350 ms/host | 2026-08-22, re-probed 2026-08-23 | 2026-08-22 | 532 | 464 | [physionet.md](sources/physionet.md) |
| GDC | GET `https://api.gdc.cancer.gov/projects?size=100&from=0&expand=summary,summary.data_categories,summary.experimental_strategies,program&format=json` | none | 350 ms/host | 2026-08-22, re-probed 2026-08-23 | 2026-08-23 | 93 | 92 | [gdc.md](sources/gdc.md) |
| TCIA | NBIA v1 `getCollectionValues` + per-collection `getModalityValues` / `getBodyPartValues` / `getPatient`; DataCite `dois?prefix=10.7937` (cursor-paged) | none | 350 ms/host | 2026-08-22, re-probed 2026-08-23 | 2026-08-22 | 241 | 241 | [tcia.md](sources/tcia.md) |

**Totals:** 2,724 raw records committed under `data/raw/`; 2,655 records in
`data/catalog/catalog.jsonl`; 69 in `data/catalog/excluded.jsonl` (68
PhysioNet `Software`/`Model` entries as `not_a_dataset`, 1 GDC project as
`not_released`).

*"Endpoints verified"* is the date the endpoint's shape, auth and paging
were confirmed by hand against the live API. *"Last harvested"* is the
`harvested_at` field of `data/raw/<source>/manifest.json` — the date the
committed raw snapshot was actually fetched. Both are re-stated per source
below with the live counts observed on each date.

## Shared harvest policy

Applies to every request the pipeline makes, from `atlas/http.py` and
`atlas/config.py`. No source has a per-source override.

- **User-Agent** (`atlas.config.UA`, sent on every GET, POST and HEAD):
  `clinical-data-atlas/<version> (+https://github.com/nielspac177/clinical-data-atlas; mailto:<MAILTO>)`
  — at version `0.1.0` and the default mailto that is
  `clinical-data-atlas/0.1.0 (+https://github.com/nielspac177/clinical-data-atlas; mailto:87744596+nielspac177@users.noreply.github.com)`.
  The address is overridable with `ATLAS_MAILTO`; the repo URL is fixed in
  `atlas.config.REPO_URL`.
- **Rate limit:** `atlas.config.POLITE_DELAY = 0.35` — at most one request
  every 350 ms *per host*, enforced in `atlas.http._polite()` by netloc, so
  a source split across two hosts (TCIA) is throttled on each
  independently.
- **Retries and backoff:** exponential backoff starting at 1 s and doubling
  (1, 2, 4 …); `get_json` makes up to 3 attempts, `post_json` up to 2.
  A `429` is treated as transient and retried; any other `4xx` is not
  retried at all (retrying will not help and only adds load). Default
  timeout 30 s. A source that fails every attempt yields `None` rather than
  raising, so one dead source is reported in the refresh diff instead of
  killing the run.
- **Auth:** none. Every Phase 0 endpoint is public and unauthenticated; the
  pipeline holds no source credentials and sends no `Authorization` header.
  The only secret the project ever uses is `ANTHROPIC_API_KEY`, for LLM
  enrichment, which never touches a source API.
- **robots.txt:** checked by hand for every host at source-onboarding time
  and recorded below and in the per-source file; the runtime client does
  not re-fetch `robots.txt` per request. Only whole APIs are onboarded, so
  the check is a one-time gate on the source rather than a per-URL
  decision, and a host that disallows the path we would need is not
  harvested (see DHS under Tier B). Re-check it when adding or moving an
  endpoint.
- **Terms of service:** every Phase 0 source publishes its API for
  programmatic metadata access and none requires registration for the
  endpoints used. The atlas reads *metadata only*: no dataset contents,
  images, waveforms, or records are ever downloaded, mirrored, or
  redistributed — the raw cache under `data/raw/` holds API responses
  describing datasets, nothing from inside them.
- **Provenance:** every catalog record carries
  `provenance.harvested_via` (the `harvest_method` string in the table
  below), `harvested_at`, `last_verified` and a `raw_hash` of the payload
  it was built from.
- **Raw cache and change detection:** one pretty-printed JSON envelope per
  record under `data/raw/<source>/records/<native_id>.json`, plus
  `manifest.json` (`source`, `harvested_at`, `endpoints`, `status`,
  `error`, `counts`, and a `hash`/`first_seen` per record). Volatile fields
  are stripped before hashing so cosmetic churn does not show up as change.
  A listing that comes back more than 20 % smaller than the previous run
  trips the shrink guard: nothing is deleted and the source is reported
  failed.
- **Offline and cache switches:** `ATLAS_OFFLINE=1` (default under pytest)
  makes any network call raise `OfflineError`; `ATLAS_HTTP_CACHE=1` adds a
  24 h on-disk response cache under `.cache/http/`. Unit tests never touch
  the network.

| Source | `provenance.harvested_via` | Harvester | Normalizer |
|---|---|---|---|
| OpenNeuro | `api:openneuro-graphql` | `atlas/harvest/openneuro.py` | `atlas/normalize/openneuro.py` |
| PhysioNet | `api:physionet-published` | `atlas/harvest/physionet.py` | `atlas/normalize/physionet.py` |
| GDC | `api:gdc-projects` | `atlas/harvest/gdc.py` | `atlas/normalize/gdc.py` |
| TCIA | `api:nbia+datacite` | `atlas/harvest/tcia.py` (+ `atlas/harvest/datacite.py`) | `atlas/normalize/tcia.py` |

---

## OpenNeuro

- **Endpoint:** POST `https://openneuro.org/crn/graphql`, a single GraphQL
  query paged 100 datasets per request via
  `datasets(first: 100, after: <cursor>, orderBy: {created: descending})`;
  the walk follows `pageInfo.hasNextPage`/`endCursor`. Record url is
  derived, not returned: `https://openneuro.org/datasets/<id>`.
- **Auth:** none.
- **Politeness / robots:** shared policy (350 ms, UA, backoff).
  `https://openneuro.org/robots.txt` verified live 2026-08-22:
  `User-agent: *` / `Allow: /`, fully permissive. openneuro.org is a
  single-page app, so the GraphQL API is not merely the polite route, it is
  the only route to the data — nothing is scraped.
- **Deviation from the brief:** none. The brief named the public GraphQL
  API and the live query shape matched it exactly. What the brief left
  underspecified is the per-field normalization of messy real values
  (free-text `species`, short-form licenses, hand-typed DOI fields); those
  rules are written out explicitly in the detail file rather than guessed
  silently.
- **Quirks that matter at the index level** — full list in
  [openneuro.md](sources/openneuro.md): roughly 0.6 % of dataset nodes fail
  to resolve server-side and come back as a JSON `null` edge with no id
  recoverable; the harvester skips them instead of failing the page. That
  is exactly the gap between the live catalog size and the committed
  snapshot below.
- **Verified 2026-08-22**, re-probed **2026-08-23**: `pageInfo.count` =
  **1,864** datasets on both dates.
- **Committed snapshot:** `data/raw/openneuro/manifest.json`
  `harvested_at` = **2026-08-23**, 1,858 records listed and written
  (1,864 − 6 unresolvable `null` edges); 1,858 in the catalog, none
  excluded.
- **Field mapping:** [openneuro.md § Field mapping](sources/openneuro.md#field-mapping).

## PhysioNet

- **Endpoint:** GET `https://physionet.org/api/v1/project/published/` —
  one request, no pagination, a flat ~1.3 MB JSON array covering every
  published version of every project. Used for both `probe()` and
  `harvest()`.
- **Auth:** none. (Individual *datasets* may be credentialed; the listing
  API that describes them is open.)
- **Politeness / robots:** shared policy. `https://physionet.org/robots.txt`
  verified live 2026-08-22: `User-Agent: *` / `Allow: /`, with no
  `Disallow` of any kind.
- **Deviation from the brief:** the brief's `/rest/database-list/`-style
  endpoints return **404**. The verified replacement is
  `/api/v1/project/published/`, which differs in three ways worth
  planning around: it is unpaginated, it returns *all* resource types
  (`Database`, `Challenge`, `Software`, `Model`), and it returns *every
  published version* of each project rather than only the current one.
  The harvester writes only entries flagged `is_latest_version: true`, one
  per slug, and keeps every resource type in the raw cache; it is the
  normalizer that narrows to dataset-shaped resources.
- **Verified 2026-08-22**, re-probed **2026-08-23**: **716** listing
  entries, **532** of them `is_latest_version` on both dates. By
  `access_policy` (all versions): 382 Open / 240 Credentialed / 74
  Restricted / 20 Contributor Review. By `resource_type`: 586 Database /
  62 Software / 53 Challenge / 15 Model.
- **Committed snapshot:** `data/raw/physionet/manifest.json`
  `harvested_at` = **2026-08-22**, 532 latest-version records; **464** in
  the catalog after 68 `Software`/`Model` entries are excluded as
  `not_a_dataset`.
- **Access mapping:** `Open`→`open`, `Restricted`→`registration`,
  `Credentialed`→`credentialed`, `Contributor Review`→`application`; an
  unrecognized fifth value is excluded rather than guessed.
- **Field mapping:** [physionet.md § Field mapping](sources/physionet.md#field-mapping).

## GDC (NCI Genomic Data Commons)

- **Endpoint:** GET `https://api.gdc.cancer.gov/projects` with
  `size=100&from=0&expand=summary,summary.data_categories,summary.experimental_strategies,program&format=json`,
  paged defensively via `from=` against `data.pagination.total`. Record url
  is derived: `https://portal.gdc.cancer.gov/projects/<project_id>`.
- **Auth:** none for the projects listing (the *controlled* data behind
  those projects needs dbGaP authorization, which the atlas never touches
  and only describes in `access_notes`).
- **Politeness / robots:** shared policy.
  `https://api.gdc.cancer.gov/robots.txt` returns **404** — no robots.txt
  is served for the API host, so there is no directive to honor; the polite
  delay and UA still apply.
- **Deviation from the brief:** none. Endpoint, parameters and response
  shape all matched. One later change is a post-review ruling, not an API
  correction: the study-title heuristic that decides when a project `name`
  may be used as a disease `Condition` was broadened past the brief's
  literal five-keyword list (ruling R13) after a live project shipped its
  full trial title as a bogus condition.
- **Verified 2026-08-22**, re-probed **2026-08-23**: `data.pagination`
  reports **93** projects on both dates — one `size=100` page in practice,
  though the `from=` walk is implemented and fixture-tested so a future
  94th–101st project cannot be silently dropped.
- **Committed snapshot:** `data/raw/gdc/manifest.json` `harvested_at` =
  **2026-08-23**, 93 records; **92** in the catalog (`CGCI-BLGSP` excluded
  as `not_released`: `released=true` but `state="submitted"`).
- **Field mapping:** [gdc.md § Field mapping](sources/gdc.md#field-mapping).

## TCIA (The Cancer Imaging Archive)

- **Endpoints — two unrelated APIs, joined:**
  - NBIA v1, base
    `https://services.cancerimagingarchive.net/nbia-api/services/v1/`:
    `getCollectionValues` (the public collection listing, also the
    harvester's `probe()`), then per collection
    `getModalityValues?Collection=…`, `getBodyPartValues?Collection=…`,
    `getPatient?Collection=…`.
  - DataCite:
    `https://api.datacite.org/dois?prefix=10.7937&page[size]=100&page[cursor]=1`,
    then `links.next` until exhausted — via the reusable client
    `atlas/harvest/datacite.py`.
- **Auth:** none on either API.
- **Politeness / robots:** shared policy, applied per host — the two APIs
  throttle independently. A full harvest is roughly 470 requests
  (1 listing + 3 × ~156 collections + 4 DataCite pages), about three
  minutes; `--fast` collapses that to 5 requests once `data/raw/tcia/` is
  populated. robots.txt verified live 2026-08-22:
  `services.cancerimagingarchive.net` → 404 (none served),
  `api.datacite.org` → 404 (none served), and
  `www.cancerimagingarchive.net` (a link target only, never crawled) →
  `User-agent: *` / `Disallow:` — allow all.
- **Deviations from the brief:**
  - `getCollectionDescriptions` returns **500** and the whole
    `*ValuesAndCounts` family returns **401** — with or without a guest
    token. They are never called. Consequence: NBIA supplies no
    description, license, DOI, author or title, so *all* descriptive
    metadata comes from DataCite prefix `10.7937`, and there is no
    `size_bytes` or per-modality image count for any TCIA record.
  - DataCite must be walked with `page[cursor]`, not `page[number]`:
    number-paging is unstable and silently drops and duplicates DOIs
    mid-walk. The client refuses to return a listing it cannot vouch for —
    it raises on a repeated record id, on a record count that disagrees
    with the first page's `meta.total`, and on running past 200 pages.
  - The two APIs share no identifier, so records are joined on three
    title/url rules (alternative title, parenthesised suffix, url slug);
    unmatched DataCite `/collection/` DOIs become gated records that NBIA
    does not list publicly.
  - The brief's example gated collection `prostate-mri-us-biopsy` is in
    fact a public NBIA collection today.
- **Verified 2026-08-22**, re-probed **2026-08-23**: NBIA
  `getCollectionValues` returns **156** public collections and DataCite
  reports `meta.total` = **317** DOIs under prefix `10.7937`, on both
  dates.
- **Committed snapshot:** `data/raw/tcia/manifest.json` `harvested_at` =
  **2026-08-22**, **241** records — 155 with an NBIA half (151 of them
  joined to a DataCite DOI, 4 NBIA-only `needs_review` records with no DOI
  match) plus 86 DataCite-only gated collections; all 241 reach the
  catalog. One live collection, `PSMA-PET-CT-Lesions`, is currently
  cataloged from its DataCite half alone and carries
  `record_status: needs_review` — its landing-page slug is mixed-case
  while the url-slug join rule compares against a lowercased slug, so the
  two halves did not meet. Flagged rather than hand-corrected: the fix
  belongs in the join rule, not in the catalog.
- **Field mapping:** [tcia.md § Field mapping](sources/tcia.md#field-mapping).

---

## Ancillary services

Not sources — no record ever originates here. These are lookup APIs the
enrichment stage calls to resolve values a source stated in free text, each
with an on-disk cache that is committed so a refresh re-resolves nothing it
already knows. All three follow the shared politeness policy and need no
auth.

| Service | Endpoint | Used by | Cache |
|---|---|---|---|
| DataCite | `https://api.datacite.org/dois?prefix=<prefix>&page[size]=100&page[cursor]=1` → follow `links.next` | `atlas/harvest/datacite.py`, currently only for TCIA's `10.7937` prefix | none — walked fresh each harvest into `data/raw/tcia/` |
| MeSH | GET `https://id.nlm.nih.gov/mesh/lookup/descriptor?label=<label>&match=exact&limit=10` (falls back to `match=startswith`) | `atlas/enrich/mesh.py`, condition label → descriptor id | `data/raw/enrich/mesh.json` — 535 labels cached, 142 resolved to a descriptor |
| ROR | GET `https://api.ror.org/v2/organizations?affiliation=<name>` | `atlas/enrich/ror.py`, institution name → ROR id, canonical name, country | `data/raw/enrich/ror.json` — 4 names cached, 4 resolved |

- **DataCite** is a general client, not a TCIA detail: it takes any DOI
  prefix. Its cursor-paging contract and assertions are described under
  TCIA above and in [tcia.md](sources/tcia.md).
- **MeSH** tries an exact match on the label, then an exact match on
  `vocab.CONDITION_ALIASES[label]`, then a `startswith` match accepted only
  when it returns exactly one hit — an ambiguous prefix is treated as "not
  found" rather than guessed. `match=contains` is never used.
- **ROR** trusts its own matcher's top hit only when ROR marked it
  `"chosen": true` *and* scored it ≥ 0.9; anything weaker resolves to
  nothing rather than to a fabricated affiliation.
- Both lookups **negative-cache**: a definitive "no such descriptor /
  organization" is stored as `null` so it is only ever asked once. A
  *network failure* is deliberately not cached — it leaves the cache
  untouched so a later connected run retries.
- Verified live 2026-08-22. Both are read-only under `ATLAS_OFFLINE=1`:
  cached answers are served, misses simply stay unresolved.

---

## Tier B / Phase 1–2 candidates

Verified endpoints, **not yet harvested** — no record from any of these is
in the catalog, and none has a harvester in `atlas/harvest/`. Listed here
so the verification work done in Phase 0 is not repeated, and so the
robots.txt findings are on the record before anyone writes the code.

| Candidate | Phase | Endpoint (verified 2026-08-22) | Live count then | Status |
|---|---|---|---|---|
| Crossref — *Scientific Data* (ISSN 2052-4463) | 1 | `https://api.crossref.org/journals/2052-4463/works?rows=100&cursor=*&filter=from-index-date:<date>&select=…` | 8,870 works | Not yet harvested |
| Crossref — *Data in Brief* (ISSN 2352-3409) | 1 | `https://api.crossref.org/journals/2352-3409/works?rows=100&cursor=*&filter=from-index-date:<date>&select=…` | 12,940 works | Not yet harvested |
| DHS Program | 2 | `https://api.dhsprogram.com/rest/dhs/surveys?f=json&perpage=100&page=N` | ~374 surveys | Not yet harvested — **opt-in only**, see below |
| World Bank Microdata (NADA) | 2 | `https://microdata.worldbank.org/index.php/api/catalog/search?format=json&ps=100&page=N` | thousands (filter to health-relevant) | Not yet harvested |
| INEI ENDES (Peru) | 2 | none — the portal exposes no working NADA API (404) | — | Not yet harvested; curated record planned, with the Peru surveys in the DHS family as the API-reachable alternative |

- **Crossref** is the Phase 1 journal track: both ISSNs are Data
  Descriptor / data-article journals, so the harvest is a `from-index-date`
  watermark walk with a 7-day overlap, an LLM clinical-relevance filter,
  and dedupe against the Phase 0 repository records (repository wins the
  primary `url`; the article moves to `papers[]`). Cursors are never
  persisted between runs. Abstracts arrive as JATS XML.
- **DHS Program — opt-in because of robots.txt.**
  `https://api.dhsprogram.com/robots.txt` (re-verified live 2026-08-23,
  HTTP 200) reads `User-agent: *` / `Allow: /` followed by
  `Disallow: /rest/dhs/`, alongside `Disallow:` entries for `/mcp`,
  `/mcp/`, `/oauth/`, `/.well-known/` and `/readyz`. `/rest/dhs/` is
  precisely the path the surveys endpoint lives on. Under the brief's
  "never against terms of service" rule the atlas therefore does **not**
  harvest DHS by default; if it is ever enabled it must be behind an
  explicit opt-in flag, documented as such, and ideally after asking the
  DHS Program directly. This is the one Phase 0 endpoint verification that
  ended in "do not build it".
- **World Bank NADA** serves a standard NADA catalog search API with DDI
  metadata; the work in Phase 2 is the health-relevance filter, not the
  transport.
- **INEI ENDES** has no reachable NADA API, so it becomes a hand-written
  curated record with an `access_howto` paragraph, per the brief's Tier B
  pattern.
- The rest of Tier B — NHANES, PPMI, ADNI, HCUP NIS, ACS NSQIP, SEER — has
  no API at all and is curated by hand from public documentation in
  Phase 2, then watched by a monthly monitor that flags changes rather
  than rewriting entries.

---

## Keeping this file honest

- Add a source by probing it live first, then writing
  `docs/sources/<src>.md` and an index row here — the procedure is in
  `CLAUDE.md § How to add a source`.
- Re-probe every Tier A endpoint with `uv run atlas harvest --probe`
  (also the monthly workflow's positive control) or the live smoke tests
  via `make test-live`.
- The "Last harvested" column is the `harvested_at` field of each
  `data/raw/<source>/manifest.json`; the monthly refresh workflow includes
  this file in its bot PR's `add-paths`, so counts and dates are meant to
  move with the data.
