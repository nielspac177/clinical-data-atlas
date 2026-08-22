# Sources

Index of every source the atlas harvests from or curates. Each row is
verified against its live endpoint before a harvester ships — see
`PROJECT_BRIEF.md` for the "APIs first, polite scraping second" policy and
`docs/superpowers/specs/2026-08-22-clinical-data-atlas-design.md` for the
endpoints verified so far. Per-source notes live in `docs/sources/<source>.md`.

| Source | Endpoint | Auth | Politeness | Verified | Count | Notes |
|---|---|---|---|---|---|---|
| [TCIA](sources/tcia.md) | NBIA v1 `getCollectionValues` + per-collection `getModalityValues`/`getBodyPartValues`/`getPatient`; DataCite `dois?prefix=10.7937` (cursor-paged) | none | 350 ms/host | 2026-08-22 | 242 (156 public collections + 86 gated DataCite DOIs) | Two APIs joined on title/url-slug rules — no shared id. `getCollectionDescriptions` (500) and `*ValuesAndCounts` (401) unusable, so all descriptive metadata comes from DataCite. |
