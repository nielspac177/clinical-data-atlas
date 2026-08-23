# PhysioNet

- **Source id:** `physionet` · **Harvester:** `atlas/harvest/physionet.py` · **Normalizer:** `atlas/normalize/physionet.py`
- **Endpoint(s):** `GET https://physionet.org/api/v1/project/published/` (the only endpoint; used for both `probe()` and `harvest()`) · **Auth:** none · **Politeness:** 350 ms/host (`atlas.http` default), UA `clinical-data-atlas/<version> (+https://github.com/nielspac177/clinical-data-atlas; mailto:<MAILTO>)` · **robots.txt:** allows (`https://physionet.org/robots.txt` is `User-Agent: *` / `Allow: /`, no `Disallow` of any kind)
- **Verified:** 2026-08-22 · **Count:** 716 listing entries total; 532 are `is_latest_version=true` (the harvester writes only those); 382 Open / 240 Credentialed / 74 Restricted / 20 Contributor Review by `access_policy`, 586 Database / 62 Software / 53 Challenge / 15 Model by `resource_type` (whole-listing counts, all versions) · **Deviation from brief:** the brief's `/rest/database-list/`-style endpoints return 404. The real, verified endpoint is `/api/v1/project/published/` — a single flat JSON array covering every published project version, all resource types, with no pagination.

## Quirks

- **One request, no pagination.** The endpoint returns the entire 716-entry listing (~1.3 MB) in one response. `probe()` and `harvest()` are both exactly one `GET`.
- **The listing includes every version of every project, not just the current one.** A popular project (e.g. `mimic-iv`) shows up multiple times, once per published version, with only the current one flagged `is_latest_version: true`. The harvester writes one raw envelope per **slug** (native id) only for the entry where `is_latest_version` is true; older versions of the same slug are skipped at harvest time, not merely excluded later — they are never written to `data/raw/physionet/records/`.
- **`limit` caps written records, not scanned ones.** `harvest(limit=N)` still walks the whole listing but stops once it has *written* `N` latest-version entries (post-filter) — it does not simply slice the first `N` raw array entries, which could otherwise yield fewer than `N` records (or zero) if older-version entries happen to sort first.
- **The raw cache still keeps every resource type**, including `Software` and `Model` (tooling/code, not data) alongside `Database`/`Challenge` — the harvester doesn't filter on `resource_type` at all, so `data/raw/physionet/` stays the honest, complete record of what PhysioNet published. It's the *normalizer* that narrows to dataset-shaped resources, returning `Excluded(reason="not_a_dataset")` for `Software`/`Model`.
- **`core_doi` is frequently `null`** even when the project has a DOI — PhysioNet only stamps a version-independent `core_doi` on some projects; the rest carry only a `version_doi`. `dataset_doi` falls back to `version_doi` in that case (both point at the same DOI-issuing prefix `10.13026/`).
- **`dua` is `null` for `Open`-access projects** (no data use agreement needed) and an object with a `name` for the other three `access_policy` values. `access_notes` mirrors that directly: `null` for Open, the DUA's name otherwise.
- **An unrecognized `access_policy` value is excluded, never guessed.** Only the four documented values (`Open`, `Restricted`, `Credentialed`, `Contributor Review`) map to a canonical `access` tier. A fifth value the API might introduce later (or a missing/`null` `access_policy`) is not silently defaulted to some tier — the record comes back as `Excluded(reason="unmapped_access_policy:<value>")` instead, so a schema change upstream shows up as a visible exclusion rather than a mis-tagged access level (or a crash that would take the whole `atlas normalize` run down with it).
- **License names are source-specific, not just Creative Commons.** Beyond a handful of CC/ODC licenses that map to SPDX via `vocab.LICENSE_MAP` (e.g. `"Open Data Commons Attribution License v1.0"` → `"ODC-By-1.0"`, confirmed live on `slpdb`/`wfdb-swig-matlab`), most gated projects carry a PhysioNet-specific license name (`"PhysioNet Restricted Health Data License 1.5.0"`, `"...Credentialed..."`, `"...Contributor Review..."`) that has no SPDX identifier and passes through verbatim (confirmed live on `ct-ich`/`eicu-crd`/`hirid`).
- **No structured sample-size, country, year-range, author, or institution fields on this endpoint.** `sample_size`/`sample_unit` are recovered heuristically via `common.regex_sample_size` over `short_description` + the stripped `abstract`, and are `None`/`None` when that text is ambiguous or silent (e.g. `ct-ich`'s abstract mentions counts without any of the recognized unit words). `countries`, `years`, `institutions`, and `authors` are left empty — the API simply doesn't state them.
- **`short_description` is sometimes empty** (e.g. most `Software` entries, including `wfdb-swig-matlab`) — `summary` falls back to the first 40 words of the stripped `abstract` in that case, same as when `short_description` runs longer than 40 words.

## Field mapping

| raw | canonical | transform |
|---|---|---|
| `slug` | `id`, `source_native_id` | `id = "physionet:" + slug`; native id used verbatim as `source_native_id` |
| `source_url` | `url` | passthrough (e.g. `https://physionet.org/content/<slug>/<version>/`) |
| `title` | `name` | passthrough |
| `short_description`, `abstract` | `summary` | `short_description` verbatim if ≤40 words; else first 40 words of `strip_html(abstract)`; else (both empty) `title`, as a last resort so `summary` is never empty |
| `access_policy` | `access`, `access_tiers` | `Open`→`open`, `Restricted`→`registration`, `Credentialed`→`credentialed`, `Contributor Review`→`application`; `access_tiers = [access]` (this endpoint reports exactly one access path per entry); any other value → `Excluded(reason="unmapped_access_policy:<value>")` |
| `dua.name` | `access_notes` | `dua.name` when `dua` is an object, else `None` |
| `license.name` | `license` | `common.license_to_spdx(name)`, falling back to the verbatim `name` when unmapped |
| `core_doi`, `version_doi` | `dataset_doi` | `common.clean_doi(core_doi or version_doi)` |
| `version` | `version` | passthrough |
| `publish_date` | `published` | passthrough (`YYYY-MM-DD`) |
| `main_storage_size` | `size_bytes` | passthrough |
| `topics`, `resource_type` | `keywords` | `topics` verbatim, plus `"challenge"` appended (deduped) when `resource_type == "Challenge"` |
| `short_description` + `strip_html(abstract)` | `sample_size`, `sample_unit` | `common.regex_sample_size(...)`; `(None, None)` when ambiguous or no unit word matches |
| *(not on this endpoint)* | `domains`, `modalities`, `conditions` | left empty at normalize time; filled later by the enrich stage from `enrichment_text` |
| *(not on this endpoint)* | `species` | always `"human"` (PhysioNet's published-projects catalog is human clinical/physiological data) |
| *(not on this endpoint)* | `countries`, `years`, `institutions`, `authors` | left empty/null — the API does not state them |
| `resource_type` | *(filter)* | `Database`/`Challenge` → normalized to a `Record`; `Software`/`Model` → `Excluded(reason="not_a_dataset")` |
| — | `record_status` | always `"active"` for a normalized record |
| whole `payload` | `provenance.raw_hash` | `io.content_hash(payload)` |
| — | `provenance.harvested_via` | `"api:physionet-published"` |
