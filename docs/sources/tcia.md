# The Cancer Imaging Archive (TCIA)

- **Source id:** `tcia` · **Harvester:** `atlas/harvest/tcia.py` (+ the reusable
  DataCite client `atlas/harvest/datacite.py`) · **Normalizer:**
  `atlas/normalize/tcia.py`
- **Endpoint(s):**
  - NBIA v1 — `https://services.cancerimagingarchive.net/nbia-api/services/v1/`
    · `getCollectionValues`, `getModalityValues?Collection=…`,
    `getBodyPartValues?Collection=…`, `getPatient?Collection=…`
  - DataCite — `https://api.datacite.org/dois?prefix=10.7937&page[size]=100&page[cursor]=1`,
    then `links.next` until exhausted
- **Auth:** none on either API.
- **Politeness:** 350 ms/host (`POLITE_DELAY`), UA `clinical-data-atlas/<ver>
  (+<repo>; mailto:<mailto>)`. A full harvest is ~470 requests (1 listing +
  3 × 156 collections + 4 DataCite pages) across two hosts, so ~3 minutes;
  `--fast` collapses it to 5 requests once `data/raw/tcia/` is populated.
- **robots.txt:** `services.cancerimagingarchive.net` → 404 (none served);
  `api.datacite.org` → 404 (none served); `www.cancerimagingarchive.net`
  (link target only, never crawled) → `User-agent: * / Disallow:` — allow all.
- **Verified:** 2026-08-22, re-probed 2026-08-23 · **Live counts:** 156
  public NBIA collections and 317 DataCite DOIs under prefix `10.7937`,
  identical on both dates.
- **Committed snapshot:** `data/raw/tcia/manifest.json` `harvested_at` =
  2026-08-22, **241** records, all 241 in `data/catalog/catalog.jsonl`:
  155 with an NBIA half (151 joined to a DataCite DOI, 4 NBIA-only) plus 86
  gated DataCite `/collection/` DOIs that NBIA does not list publicly. The
  156th public collection, `PSMA-PET-CT-Lesions`, is in the snapshot as a
  DataCite-only record — see "A mixed-case url slug defeats join rule (c)"
  under Quirks.
- **Deviation from brief:** `getCollectionDescriptions` and every
  `*ValuesAndCounts` endpoint are unusable and are never called (see Quirks);
  the brief's example gated collection `prostate-mri-us-biopsy` is in fact a
  public NBIA collection today. Two rules the brief left open are decided in
  Quirks below (final match tie-break, `limit` semantics).

## Why two APIs

Neither API alone produces a record:

| Need | NBIA v1 | DataCite |
|---|---|---|
| Which collections are public | yes (this is its whole job) | no |
| Modalities, body parts, patients | yes | no |
| Title, abstract, license, DOI, authors, version | no | yes |
| Gated/limited-access collections | no (they are simply absent) | yes |

So the harvest lists collections from NBIA, pulls per-collection detail,
lists every `10.7937` DOI from DataCite, and joins them.

## Quirks

**Unusable NBIA endpoints.** `getCollectionDescriptions` returns 500 and the
`*ValuesAndCounts` family returns 401 — with or without a guest token. They
are never requested. Consequences: no `size_bytes`, no per-modality image
counts, and every description/license/DOI has to come from DataCite.

**`getCollectionValues` is slow** (~6 s) and is also the harvester's
`probe()`. `harvest()` calls `probe()` for the listing rather than issuing a
second identical request.

**`getBodyPartValues` returns bare `{}` entries** for series with no
`BodyPartExamined`. They carry no information and are dropped; the stored
payload keeps the remaining values as plain strings rather than the
`{"BodyPartExamined": …}` wrappers (same for `getModalityValues`).
`getPatient` objects are stored whole.

**DataCite cursor paging is mandatory.** `page[number]` paging is unstable —
records shift between pages mid-walk, silently dropping and duplicating DOIs.
`list_by_prefix` starts at `page[cursor]=1`, follows `links.next` verbatim,
and refuses to return a listing it can't vouch for: it raises `AssertionError`
on a repeated record id, when the record count disagrees with the first page's
`meta.total`, or when the walk runs past `MAX_PAGES` (200). It also stops on
an empty page that still carries a `links.next` — the cursor walking past the
end — which would otherwise spin to the cap. Duplicate detection keys on the
record's `id` rather than `attributes.doi`, so two records that merely *lack* a
DOI can't look like a duplicate pair. The bracketed parameter names can't go
through `urlencode`, so the URL is assembled by hand and passed to `get_json`
whole.

**The join has no shared identifier.** Three signals, any one sufficient,
jointly covering 151 of 156 collections on 2026-08-22 (49 by url slug, 59 by
parenthesised suffix, 48 by alternative title — with overlap):

| Rule | Example |
|---|---|
| (a) an alternative title equal to the collection name | `C-NMC 2019` |
| (b) a parenthesised suffix in the main title equal to the name | `… Aggressive Fibromatosis (A091105)` |
| (c) the url slug equal to `io.slugify(name)` | `…/collection/4d-lung/` → `4D-Lung` |

(a) and (b) are exact and case-sensitive on purpose: TCIA's short names are
the collection names verbatim, and loosening the comparison starts matching
sibling collections that differ only in punctuation
(`Vestibular-Schwannoma-MC-RC` vs `Vestibular-Schwannoma-MC-RC 2`).

**Tie-break.** Five collections matched two DOIs each. The brief's tie-break
(prefer a `/collection/` url, then the latest `publicationYear`) resolves
three of them; `Mouse-Mammary` and `Soft-tissue-Sarcoma` are still tied on
both. A third term — the lowest DOI — was added so a refresh always resolves
them the same way instead of flipping a record's DOI between runs.

**A tie-break loser is not a gated collection.** It describes a collection
that *is* in NBIA, so it is excluded from `gated_records` along with the
winner; treating it as gated would list the same collection twice.

**Five collections match no DOI** (`MIDI-B-*` ×4, `PSMA-PET-CT-Lesions`).
They still produce records — from NBIA alone, `record_status="needs_review"`,
name = the collection name, summary = a template built from NBIA facts. Four
of them (the `MIDI-B-*` set) are in the committed snapshot in exactly that
shape; the fifth is the case below.

**A mixed-case url slug defeats join rule (c).** `by_slug` in
`_candidates()` is keyed on the DataCite landing-page slug exactly as the
url spells it, while the lookup asks for `io.slugify(name)`, which is
lowercased. A slug that is already lowercase (`4d-lung`) matches; one that
is not does not. The live case is `PSMA-PET-CT-Lesions`: NBIA lists the
collection, DataCite has `10.7937/r7ep-3x37` at
`…/collection/PSMA-PET-CT-Lesions/`, and rules (a) and (b) do not fire for
it either, so the DOI stays "unmatched" and is written as a *gated* record.
Because a gated record's `native_id` is that same url slug, its envelope
lands on the identical raw-store key as the NBIA-only record and is the one
that survives — hence 241 records for 156 collections, and
`tcia:psma-pet-ct-lesions` carrying `access="registration"` and
`record_status="needs_review"` for what is really a public collection. The
`needs_review` flag is doing its job; the fix belongs in the join rule
(compare slugs case-insensitively) and is deliberately not papered over by
hand-editing the catalog. Not yet fixed — recorded here so the next TCIA
change starts from the real behavior.

**`limit` caps records, not collections.** A truncated run spends what is
left of `limit` on gated records, and gating is always decided against the
*full* collection listing — otherwise a `--limit 5` run would invent 234
phantom "not in NBIA" records for the collections it merely skipped.

**DataCite is fetched before the NBIA detail loop.** Four requests versus
~470, so a DataCite outage costs seconds rather than the whole NBIA crawl
before failing anyway.

**`--fast` self-heals.** A cached `nbia` half carrying `errors` is re-fetched
rather than reused, so one flaky request doesn't become permanent.

**One DOI, two collections is a hard error.** `match_collections` raises
`AssertionError` naming both collections and the DOI. It has never fired on
the live data (151 matches over 156 × 317), and if it does it means the join
rules have gone ambiguous — two catalog records would otherwise claim the same
`dataset_doi`.

**Volatile DataCite fields.** `updated`, `viewCount`, `downloadCount`,
`citationCount` and the three `*OverTime` arrays change without the metadata
changing. They stay in the stored payload (they are real API output) but are
listed in `VOLATILE`, so `RawStore.write` ignores them when deciding whether
a record changed. `relationships` is dropped entirely — it only names the
DataCite client account.

**DataCite affiliations are plain strings** in all 317 TCIA records, though
the schema also allows `{"name": …}` objects. Both shapes are accepted.

**`identifiers[]` carries a `TCIA Short Name`** on some records, which would
be a cleaner join key than any of the three rules above. It is not used —
the brief fixes the matching rules, and the field's coverage across all 317
DOIs has not been verified.

**`publicationYear` is the DOI's minting year**, not when the data was
collected, so `published` and `years` are left null rather than fabricated.

**Known validation warnings.** The 86 gated records have no NBIA half, so
they warn `no modalities assigned` and `access=… has no access_notes`
(`access_notes` is not part of this source's mapping). Both are warnings, not
errors.

**`provenance.raw_hash` is the volatile-stripped hash** — the same value
`manifest.json` records, via the public `harvest.tcia.strip_volatile()`
(controller ruling R12). Hashing the payload as stored would make every TCIA
record's `raw_hash` change on every refresh, purely because someone viewed a
DataCite landing page, and bury the real changes in the changelog.

## Field mapping

`nbia` = the NBIA half of the payload, `dc` = the DataCite `attributes` half.
Either may be `null`.

| raw | canonical | transform |
|---|---|---|
| envelope `native_id` | `id` | `tcia:` + `io.slugify(native_id)` |
| envelope `native_id` | `source_native_id` | verbatim (collection name, or the DataCite url slug for gated records) |
| `dc.titles[]` with no `titleType` | `name` | else `nbia.collection`, else `native_id` |
| `dc.url` | `url` | else `https://www.cancerimagingarchive.net/collections/` |
| `dc.descriptions[0].description` | `summary` | `io.strip_html` then first 40 words; when absent, the template `"<name>: imaging collection on The Cancer Imaging Archive (<modalities>; <n> subjects)."`, dropping whichever clause the source doesn't support |
| `nbia.modalities[]` | `modalities` | `MR`→MRI, `CT`→CT, `PT`→PET, `NM`→SPECT, `US`→ultrasound, `MG`→mammography, `CR`/`DX`/`RF`/`XA`→xray, `RTSTRUCT`/`RTDOSE`/`RTPLAN`→radiotherapy, `SM`→pathology; deduped, in `vocab.MODALITIES` order |
| `nbia.modalities[]` | `keywords` | `SEG`/`REG`/`FUSION`/`KO`/`PR`/`SR`/`RWV`/`OT` → lowercased keyword (they describe derived objects, not a way of imaging a patient) |
| `nbia.body_parts[]` | `keywords` | lowercased, appended after the modality keywords |
| `nbia.patients[]` | `sample_size`, `sample_unit` | count of patients with `Phantom != "YES"`, unit `participants`; both null when NBIA reported no patient list |
| `nbia.patients[].SpeciesDescription` / `.Phantom` | `species` | no patients → `unknown`; all `Phantom=="YES"` → `phantom`; else over the non-phantom patients: all `Homo sapiens` → `human`, none human → `animal`, both → `mixed`, none described → `unknown` |
| `dc.rightsList[].rightsIdentifier` | `license` | SPDX (`cc-by-4.0`→`CC-BY-4.0`, `cc-by-3.0`, `cc-by-nc-4.0`, `cc-by-nc-3.0`, `cc-by-nc-nd-3.0`, `cc0-1.0`→`CC0-1.0`) |
| `dc.rightsList[].rights` | `license` | verbatim when no identifier maps (e.g. `NCTN Data Archive License`); null when `rightsList` is empty |
| presence of `nbia` / `dc.rightsList[].rights` | `access`, `access_tiers` | NBIA-listed → `open`; gated → `application` when the rights text matches `Limited Access\|Controlled\|NCTN\|Data Usage Policy`, else `registration`. `access_tiers = [access]` |
| presence of both halves + `nbia.errors` | `record_status` | `active` only when NBIA *and* DataCite both described the record and no per-collection request failed; `needs_review` otherwise |
| `dc.creators[].name` | `authors[].name` | verbatim, source order |
| `dc.creators[].nameIdentifiers[]` where scheme `ORCID` | `authors[].orcid` | verbatim (bare id or url, as given) |
| `dc.creators[].affiliation[]` | `institutions[].name` | string or `{"name"}`; deduped by name, first mention wins |
| `dc.relatedIdentifiers[]` of type `DOI` | `papers[]` | `IsCitedBy`→`is_cited_by`, `IsDescribedBy`/`IsSupplementTo`→`describes`, anything else→`other`; DOIs via `common.clean_doi`, deduped, capped at 20 |
| `dc.doi` | `dataset_doi` | `common.clean_doi` then `common.collapse_version_doi` (a no-op on every TCIA DOI seen, applied for cross-source consistency) |
| `dc.version` | `version` | as a string |
| `name` | `domains` | `["oncology"]`, then (word-anchored, so `impedance`/`Covidien` don't fire): `/\bcovid\b/i` → `["infectious_disease","pulmonology"]`, `/\bhealthy\b\|\bnormative\b/i` → drop `oncology`, `/\bpedi\w*/i` → add `pediatrics`; emitted in `vocab.DOMAINS` order |
| `dc` main title | `conditions[]` | only the four unambiguous multi-word cancer phrases present in `vocab.CONDITION_ALIASES` (`glioblastoma multiforme`, `lung adenocarcinoma`, `breast cancer`, `lung cancer`); everything else is left to the enrich stage |
| — | `published`, `years`, `countries`, `population`, `size_bytes`, `access_notes` | always null/empty — no source field supports them |
| whole payload | `provenance` | `common.make_provenance(via="api:nbia+datacite", …, raw_hash=io.content_hash(strip_volatile(payload)))` — the volatile-stripped hash, identical to the one in `manifest.json` |

`enrichment_text(envelope)` = name + DataCite main title + description +
body parts + modality codes, HTML-stripped, deduplicated (`name` *is* the title
whenever there is one), capped at 1,500 characters.

## Fixtures

`tests/fixtures/tcia/` holds one live capture (2026-08-22), trimmed:

- `collections.json` — the first 5 of the 156 collection names.
- `modalities_<slug>.json`, `bodyparts_<slug>.json`, `patients_<slug>.json` —
  the three detail responses for each of those 5; patient lists truncated to
  5 entries.
- `datacite_page.json` — one page of 8 real DOIs: the 5 that match those
  collections plus 3 gated `/collection/` DOIs. `creators` and
  `relatedIdentifiers` are truncated to 5 entries each, `meta.total` is set
  to 8 and `links.next` removed so the page is self-consistent as a
  single-page listing.
- `records/*.json` — the 8 envelopes the harvester produces from the above.
