# Sources

Index of every source the atlas harvests from or curates. Each row is
verified against its live endpoint before a harvester ships — see
`PROJECT_BRIEF.md` for the "APIs first, polite scraping second" policy and
`docs/superpowers/specs/2026-08-22-clinical-data-atlas-design.md` for the
endpoints verified so far. Per-source notes live in `docs/sources/<source>.md`.

| Source | Endpoint | Auth | Politeness | Verified | Count | Notes |
|---|---|---|---|---|---|---|
| GDC | `https://api.gdc.cancer.gov/projects` | none | 350 ms/host | 2026-08-22 | 93 projects | One page in practice (`size=100`), paged defensively via `from=`; no robots.txt. See `docs/sources/gdc.md`. |
