"""Small maintenance tools that are run by hand, not by the pipeline.

Nothing in `atlas/` imports this package: each module here is a
`python -m atlas.tools.<name>` entry point whose output is committed
(see `atlas/tools/og_png.py`). They may depend on the optional `e2e`
dependency group, which the library itself never does.
"""
