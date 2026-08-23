"""Shared pytest fixtures: default to offline mode and block real sockets.

Any test not marked ``live`` or ``e2e`` gets a ``socket.socket`` whose
``connect``/``connect_ex`` raise instead of touching the network. This
keeps the unit suite fast, deterministic, and safe to run anywhere.

A test that *is* marked ``live`` or ``e2e`` gets the opposite treatment:
sockets are left alone and ``atlas.config.OFFLINE`` is forced to ``False``
for the duration of the test. ``config.OFFLINE`` is read once, at import
time, from ``ATLAS_OFFLINE`` -- and this module defaults that variable to
``"1"`` before anything imports ``atlas.config`` -- so without the
override every network call inside a live test would raise
``atlas.http.OfflineError`` and ``ATLAS_LIVE=1 pytest -m live`` would only
pass by accident. (``make test-live`` sets ``ATLAS_OFFLINE=0`` too; this
makes the marker sufficient on its own.)

This module also decides whether ``tests/e2e/`` is collectable at all --
see ``collect_ignore_glob`` below.
"""

from __future__ import annotations

import importlib.util
import os
import re
import socket
from pathlib import Path

import pytest

# Default to offline unless the environment already set this explicitly
# (e.g. a live/e2e run that unsets or overrides it before pytest starts).
os.environ.setdefault("ATLAS_OFFLINE", "1")

# Imported after the default above so `config.OFFLINE` reads the right value.
from atlas import config

E2E_DIR = Path(__file__).parent / "e2e"
HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
E2E_HINT = (
    "the browser suite needs the e2e dependency group: run `make e2e`, or "
    "`uv sync --group e2e && uv run playwright install chromium`"
)

# `tests/e2e/conftest.py` imports playwright at module scope, and `-m`
# deselection happens *after* collection -- so in an environment with only
# the `dev` group (CI's `test` job, and `make test` for anyone who has never
# run `make setup`), collecting a directory that was never going to run
# would abort the entire suite. The glob names the directory itself;
# `["e2e/*"]` does not work.
collect_ignore_glob = [] if HAVE_PLAYWRIGHT else ["e2e"]


def _selects_e2e(markexpr: str) -> bool:
    """True when `-m <expr>` asks *for* e2e rather than against it."""
    return "e2e" in re.sub(r"not\s+e2e", "", markexpr or "")


def _targets_e2e(args) -> bool:
    """True when a path argument points inside `tests/e2e/`."""
    for arg in args:
        try:
            path = Path(str(arg).split("::")[0]).resolve()
        except OSError:  # pragma: no cover - unparseable argument
            continue
        if path == E2E_DIR or E2E_DIR in path.parents:
            return True
    return False


def pytest_configure(config: pytest.Config) -> None:
    """Explain a missing browser suite instead of "no tests ran".

    `collect_ignore_glob` above makes the e2e directory invisible without
    playwright, which is right for a run that filtered it out anyway --
    but someone who explicitly asked for it deserves the reason rather
    than an empty selection.
    """
    if HAVE_PLAYWRIGHT:
        return
    if _selects_e2e(config.getoption("markexpr", default="")) or _targets_e2e(
        config.args
    ):
        raise pytest.UsageError(E2E_HINT)


def _blocked_connect(*_args, **_kwargs):
    raise RuntimeError("network disabled in unit tests")


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    """Block real socket connections unless the test opts in via markers."""
    live = request.node.get_closest_marker("live")
    e2e = request.node.get_closest_marker("e2e")
    if live or e2e:
        monkeypatch.setattr(config, "OFFLINE", False)
        yield
        return

    if os.environ.get("ATLAS_OFFLINE") == "1":
        monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)
    yield
