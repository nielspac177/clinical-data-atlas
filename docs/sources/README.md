# docs/sources/

One markdown file per data source, named `<source>.md` where `<source>`
matches the `source` enum value in `atlas/schema.py` and the module names
`atlas/harvest/<source>.py` / `atlas/normalize/<source>.py` (e.g.
`openneuro.md`, `physionet.md`, `gdc.md`, `tcia.md`).

Each file covers, in whatever shape fits the source:
- the endpoint(s) actually used, and how they were verified live
- auth requirements (none, API key, application, ...)
- politeness constraints (rate limits, robots.txt, ToS notes)
- the date it was last verified live
- known quirks, endpoint corrections, or caveats vs. the original brief
- for Tier B curated sources, the `access_howto` a researcher needs

`docs/sources.md` is the index — one row per source summarizing the fields
above. Keep it in sync whenever a per-source file changes.
