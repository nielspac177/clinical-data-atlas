# Clinical Data Atlas

Clinical Data Atlas is a public, living, explorable catalog of open and
gated clinical & neuroscience datasets — presented as an interactive 3D
knowledge graph plus a filterable table, refreshed monthly. It widens the
concept behind [datasets.neuro2.ai](https://datasets.neuro2.ai/) from
neuroimaging to the whole clinical landscape. The atlas catalogs datasets
and links out to them; it never downloads, rehosts, or redistributes any
dataset's actual data — see `PROJECT_BRIEF.md` for the full brief and
non-negotiables.

**Site:** https://nielspac177.github.io/clinical-data-atlas/

## Quickstart

```bash
uv sync
make refresh   # harvest -> normalize -> enrich -> graph -> diff, one command
make serve     # build the static site into _site/ and serve it locally
```

## Data model

Every catalog record follows one canonical schema, documented in
[`docs/schema.md`](docs/schema.md) and generated from the single source of
truth at `atlas/schema.py` via `atlas schema --export`.

## Sources

[`docs/sources.md`](docs/sources.md) indexes every harvested and curated
source — endpoint, auth, politeness, and last-verified date. Per-source
detail lives under [`docs/sources/`](docs/sources/).

## How refresh works

A monthly GitHub Actions workflow runs `make refresh`, which re-harvests
every source, re-normalizes and re-enriches records, rebuilds the graph,
and writes a human-readable diff into `data/changelog/`. The workflow opens
a pull request with the result rather than pushing to `main` directly;
merging that PR is what deploys the updated site.

## Contributing

Propose a new dataset or flag a problem with an existing record via a
GitHub issue — the issue forms mirror the catalog schema, so a submission
already arrives in roughly the right shape.

## Licenses

- Code: [MIT](LICENSE)
- Catalog metadata (`data/`): [CC BY 4.0](data/LICENSE)

## Citation

Citation metadata will be published in `CITATION.cff` once the project
reaches its first release.
