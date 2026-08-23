"""Graph + search index + stats build (`data/graph/*.json`).

`atlas.graph.build` turns a list of canonical `Record`s into the three
frozen data contracts the static site consumes: `graph.json` (nodes +
links for the 3D force graph), `search-index.json` (flat rows for the
filterable table), and `stats.json` (small aggregate counts). See
`atlas.graph.build` for the implementation and
`docs/superpowers/specs/2026-08-22-clinical-data-atlas-design.md`'s "Data
contracts" section for the frozen shapes.
"""

from __future__ import annotations
