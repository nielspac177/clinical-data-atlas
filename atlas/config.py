"""Central configuration: paths, identity, and environment-driven flags.

Nothing in this module talks to the network or touches the filesystem
beyond resolving paths, so it is safe to import from anywhere, including
tests.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo-relative filesystem layout: every path the pipeline reads or writes
# lives under DATA, one subdirectory per pipeline stage.
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
CATALOG = DATA / "catalog"
GRAPH = DATA / "graph"
CHANGELOG = DATA / "changelog"

# Package version, used in the HTTP User-Agent; falls back to the
# pyproject.toml value when package metadata isn't installed (e.g. running
# straight from a checkout without `uv sync`).
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    VERSION = _pkg_version("clinical-data-atlas")
except PackageNotFoundError:
    VERSION = "0.1.0"

# Project identity used on every outbound request and in citations.
MAILTO = os.environ.get("ATLAS_MAILTO", "87744596+nielspac177@users.noreply.github.com")
UA = f"clinical-data-atlas/{VERSION} (+https://github.com/nielspac177/clinical-data-atlas; mailto:{MAILTO})"
MAINTAINER = os.environ.get("ATLAS_MAINTAINER", "Niels Pacheco-Barrios")
REPO_URL = "https://github.com/nielspac177/clinical-data-atlas"
SITE_URL = "https://nielspac177.github.io/clinical-data-atlas/"

# Minimum seconds between requests to the same host — the "polite" half of
# "APIs first, polite scraping second".
POLITE_DELAY = 0.35

# Environment-driven behavior flags, read once at import time.
OFFLINE = os.environ.get("ATLAS_OFFLINE") == "1"  # unit tests: block real sockets
LIVE = os.environ.get("ATLAS_LIVE") == "1"  # opt in to live-network tests/harvests
LLM = os.environ.get("ATLAS_LLM")  # LLM backend override, e.g. "anthropic" | "cli"
HTTP_CACHE = os.environ.get("ATLAS_HTTP_CACHE") == "1"  # cache HTTP responses to disk
