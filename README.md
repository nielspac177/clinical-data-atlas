# Clinical Data Atlas

[![CI](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/ci.yml/badge.svg)](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/ci.yml)
[![Deploy Pages](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/deploy-pages.yml/badge.svg)](https://github.com/nielspac177/clinical-data-atlas/actions/workflows/deploy-pages.yml)

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

A [monthly GitHub Actions workflow](.github/workflows/monthly-refresh.yml)
runs the same pipeline end to end via `python -m atlas.refresh` — the
module the `atlas refresh` CLI subcommand wraps, so it's the same pipeline
`make refresh` runs locally (`uv run atlas refresh`), just invoked
directly: it re-harvests every source, re-normalizes and re-enriches
records, rebuilds the graph, and writes a human-readable diff into
`data/changelog/`. The workflow never pushes to `main` directly — it opens
a pull request with the diff (branch `bot/monthly-refresh`); a human
reviews and merges it, and merging to `main` is what
[deploys](.github/workflows/deploy-pages.yml) the updated site.

## Contributing

Two issue forms mirror the catalog schema, so a submission already arrives
in roughly the right shape:

- [**Propose a dataset**](https://github.com/nielspac177/clinical-data-atlas/issues/new?template=propose-dataset.yml) —
  suggest a dataset or resource that isn't in the catalog yet.
- [**Edit or flag a dataset**](https://github.com/nielspac177/clinical-data-atlas/issues/new?template=dataset-edit.yml) —
  report a stale link, a wrong field, or anything else out of date on an
  existing record.

## Licenses

- Code: [MIT](LICENSE)
- Catalog metadata (`data/`): [CC BY 4.0](data/LICENSE)

## Citation

If you use this catalog, please cite it via [`CITATION.cff`](CITATION.cff)
(GitHub renders a "Cite this repository" button from it), or with the
BibTeX below:

```bibtex
@software{pacheco_barrios_clinical_data_atlas,
  author  = {Pacheco-Barrios, Niels},
  title   = {Clinical Data Atlas: a living catalog of open and gated clinical and neuroscience datasets},
  year    = {2026},
  url     = {https://nielspac177.github.io/clinical-data-atlas/},
  version = {0.1.0}
}
```
