# Sources

Index of every source the atlas harvests from or curates. Each row is
verified against its live endpoint before a harvester ships — see
`PROJECT_BRIEF.md` for the "APIs first, polite scraping second" policy and
`docs/superpowers/specs/2026-08-22-clinical-data-atlas-design.md` for the
endpoints verified so far. Per-source notes live in `docs/sources/<source>.md`.

| Source | Endpoint | Auth | Politeness | Verified | Count | Notes |
|---|---|---|---|---|---|---|
| PhysioNet | `GET /api/v1/project/published/` | none | 350 ms/host | 2026-08-22 | 532 latest-version entries (716 total incl. older versions) | Single-request flat listing, all resource types kept in raw; brief's `/rest/database-list/`-style endpoints are 404 — see `docs/sources/physionet.md` |
| OpenNeuro | POST `https://openneuro.org/crn/graphql` (GraphQL, paged 100/req) | none | 350 ms/host | 2026-08-22 | 1,864 | SPA site -- GraphQL only; robots.txt fully permissive; ~0.6% of nodes/page fail server-side and come back as a `null` edge (skipped, not fatal); see `docs/sources/openneuro.md` |
