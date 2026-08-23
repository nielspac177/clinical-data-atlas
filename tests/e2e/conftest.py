"""Fixtures for the browser suite: build the site, serve it, watch the console.

The site is a *static* site that only behaves correctly under its real
base path (`/clinical-data-atlas/`), because `app-table.js` fetches
`${BASE_URL}data/search-index.json`. So the session fixture below builds
`_site` into a temp directory, drops a `clinical-data-atlas -> _site`
symlink into a temp `www/`, and serves `www/` -- exactly the shape CI
uses, and the only shape in which a base-path bug is visible.

Where the data comes from:

- by default, `tests/fixtures/site/` -- six datasets, so the suite is a
  second or two rather than a minute, and never depends on what the last
  refresh happened to harvest;
- with `ATLAS_E2E_REAL=1`, `data/` (the real pipeline output), for a
  local sanity check against the actual catalog;
- `test_real_data_smoke` always uses the real build when `data/graph/`
  exists, and skips when it doesn't.

`--base-url` (passed by CI, which serves its own build) wins over all of
that: given one, nothing is built and no server is started. Which is
also why no test hard-codes a dataset or a count -- see the `catalog`
fixture below.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, NoReturn

import pytest

try:
    from playwright.sync_api import ConsoleMessage, Error, Page, expect
except ModuleNotFoundError as error:  # pragma: no cover - depends on the env
    # `tests/conftest.py` keeps this directory out of collection entirely
    # when playwright is absent, so this only fires for someone who named
    # `tests/e2e` on the command line -- pytest loads an argument's
    # conftest before any hook can object. Say what to install rather
    # than leaving them with a bare ModuleNotFoundError. (The wording is
    # duplicated in `tests/conftest.py`'s E2E_HINT, which cannot be
    # imported from here: both files are called `conftest`.)
    raise ModuleNotFoundError(
        "the browser suite needs the e2e dependency group: run `make e2e`, "
        "or `uv sync --group e2e && uv run playwright install chromium`"
    ) from error

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
FIXTURE_DATA = REPO_ROOT / "tests" / "fixtures" / "site"
REAL_GRAPH = REPO_ROOT / "data" / "graph" / "graph.json"
REAL_CATALOG = REPO_ROOT / "data" / "catalog" / "catalog.jsonl"

# The default 5 s is measured on a warm laptop. A two-core CI runner
# booting ForceGraph3D under swiftshader is several times slower, and a
# timeout this generous costs a passing run nothing.
expect.set_options(timeout=15_000)

#: The project-site path prefix the pages are built for and served under.
BASE_PATH = "/clinical-data-atlas/"

#: Served explicitly, so a machine with an odd `mimetypes` registry can't
#: hand the browser a module script it refuses to execute.
MIME_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".mjs": "text/javascript",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".xml": "application/xml",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything in `tests/e2e/` as `e2e`.

    The marker is what lifts the socket guard in `tests/conftest.py` and
    what `make test` / CI filter on, so it must never be possible to add
    a browser test here and forget it.
    """
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.e2e)


# --------------------------------------------------------------- building


def _build_site(out: Path, *, real: bool) -> Path:
    """Run `python -m atlas.sitebuild` into `out`; return the site root."""
    site = out / "_site"
    command = [
        sys.executable,
        "-m",
        "atlas.sitebuild",
        "--base-url",
        BASE_PATH,
        "--build",
        "e2e",
        "--out",
        str(site),
    ]
    if not real:
        command += [
            "--data-dir",
            str(FIXTURE_DATA),
            "--catalog",
            str(FIXTURE_DATA / "catalog.jsonl"),
            "--changelog-dir",
            str(FIXTURE_DATA / "changelog"),
        ]
    done = subprocess.run(
        command, cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        pytest.fail(
            f"sitebuild failed ({done.returncode}):\n{done.stdout}\n{done.stderr}"
        )
    return site


# ---------------------------------------------------------------- serving


class _QuietHandler(SimpleHTTPRequestHandler):
    """`SimpleHTTPRequestHandler` that keeps the test output pristine."""

    extensions_map: ClassVar[dict[str, str]] = {
        **SimpleHTTPRequestHandler.extensions_map,
        **MIME_TYPES,
    }

    def log_message(self, format: str, *args: object) -> None:
        pass


def _serve(site: Path, www: Path) -> Iterator[str]:
    """Serve `site` as `www/clinical-data-atlas`; yield its base URL."""
    www.mkdir(parents=True, exist_ok=True)
    link = www / BASE_PATH.strip("/")
    if not (link.exists() or link.is_symlink()):
        link.symlink_to(site, target_is_directory=True)

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(www))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}{BASE_PATH}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _external_base_url(config: pytest.Config) -> str | None:
    """`--base-url` / `PYTEST_BASE_URL`, normalised to end in a slash."""
    given = config.getoption("base_url", default=None)
    if not given:
        return None
    return given if given.endswith("/") else f"{given}/"


def _real_data_wanted() -> bool:
    return os.environ.get("ATLAS_E2E_REAL") == "1" and REAL_GRAPH.is_file()


@pytest.fixture(scope="session")
def base_url(request: pytest.FixtureRequest, tmp_path_factory) -> Iterator[str]:
    """Where the site under test lives (overrides pytest-base-url's)."""
    external = _external_base_url(request.config)
    if external:
        yield external
        return
    root = tmp_path_factory.mktemp("site")
    site = _build_site(root, real=_real_data_wanted())
    yield from _serve(site, root / "www")


@pytest.fixture(scope="session")
def real_base_url(request: pytest.FixtureRequest, tmp_path_factory) -> Iterator[str]:
    """A site built from `data/` -- skipped when this checkout has none.

    `base_url` is resolved lazily, and only on the branch that reuses it:
    asking for it up front would build and serve the fixture site for a
    `-k real_data_smoke` run that never looks at it.
    """
    if not REAL_GRAPH.is_file():
        pytest.skip("no data/graph/graph.json in this checkout")
    if _external_base_url(request.config) or _real_data_wanted():
        # Already the real thing; don't build and serve it twice.
        yield request.getfixturevalue("base_url")
        return
    root = tmp_path_factory.mktemp("site-real")
    site = _build_site(root, real=True)
    yield from _serve(site, root / "www")


# ------------------------------------------------------- the served data


def _fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def catalog_is_committed(root: Path = REPO_ROOT) -> bool:
    """True when `root` ships a catalog, so a data-less site is a bug.

    The same sentinel CI's `validate` step uses. Kept as a plain function
    (and exposed through `missing_data_guard`) because everything about
    whether this suite skips or fails hangs off it.
    """
    return (root / "data" / "catalog" / "catalog.jsonl").is_file()


@pytest.fixture(scope="session")
def missing_data_guard():
    """`catalog_is_committed`, for the test that guards the guard."""
    return catalog_is_committed


def _no_usable_data(base_url: str, reason: str) -> NoReturn:
    """Skip a data-less site, or fail it -- see `catalog_is_committed`."""
    where = f"the site at {base_url} serves no usable data/ ({reason})"
    if catalog_is_committed():
        pytest.fail(
            f"{where}, but this checkout has "
            f"{REAL_CATALOG.relative_to(REPO_ROOT)} -- the build dropped the "
            f"data rather than the data not existing"
        )
    pytest.skip(f"{where}; this checkout has no catalog either")


@pytest.fixture(scope="session", autouse=True)
def catalog(base_url: str) -> dict:
    """What the served build contains: `{rows, stats, graph}`.

    Tests assert against *this*, never against hard-coded counts, so the
    same suite means the same thing whether it is pointed at the six-row
    fixture catalog or at the real one under CI's `--base-url`.

    A site built before the first refresh has no `data/` at all; every
    page then fails its fetch, so there is nothing worth asserting and
    the suite skips with one reason rather than twenty identical
    failures. Once the checkout *has* a catalog the same symptom means
    something else entirely -- the build lost the data -- so it fails
    instead. Nothing upstream catches that: `atlas/sitebuild.py` only
    warns about missing graph JSON and still exits 0.
    """
    try:
        rows = _fetch_json(f"{base_url}data/search-index.json")
        stats = _fetch_json(f"{base_url}data/stats.json")
        graph = _fetch_json(f"{base_url}data/graph.json")
    except urllib.error.HTTPError as error:
        _no_usable_data(base_url, str(error))
    if not rows:
        _no_usable_data(base_url, "search-index.json is empty")
    return {"rows": rows, "stats": stats, "graph": graph}


# ---------------------------------------------------------------- browser


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    """Software WebGL, so the 3D scene renders on a headless CI box."""
    args = [
        *browser_type_launch_args.get("args", []),
        "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader",
    ]
    return {**browser_type_launch_args, "args": args}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """One fixed desktop viewport, so layout assertions mean something."""
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 800},
    }


@pytest.fixture(autouse=True)
def console_guard(request: pytest.FixtureRequest) -> Iterator[list[str]]:
    """Fail any test whose page logged an error or threw.

    Deliberately allow-lists nothing: a `console.error` on a static site
    is either a broken fetch, a broken module, or an exception the page
    swallowed, and all three are bugs a reader would meet. Chromium also
    logs failed requests as console errors, so a 404 on any asset or
    data file lands here too.

    The check runs at teardown, so a violation is reported as an ERROR
    on the test rather than a FAILED — the message names every message
    collected, and the run still exits non-zero.
    """
    if "page" not in request.fixturenames:
        yield []
        return

    page: Page = request.getfixturevalue("page")
    problems: list[str] = []

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            problems.append(f"console.error: {message.text}")

    def on_page_error(error: Error) -> None:
        problems.append(f"pageerror: {error.message}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    yield problems
    assert not problems, "browser console reported errors:\n" + "\n".join(problems)


@pytest.fixture(scope="session")
def artifacts_dir() -> Path:
    """`tests/e2e/artifacts/` (gitignored), created on demand."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS
