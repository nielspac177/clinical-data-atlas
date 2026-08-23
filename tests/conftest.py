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
"""

from __future__ import annotations

import os
import socket

import pytest

# Default to offline unless the environment already set this explicitly
# (e.g. a live/e2e run that unsets or overrides it before pytest starts).
os.environ.setdefault("ATLAS_OFFLINE", "1")

# Imported after the default above so `config.OFFLINE` reads the right value.
from atlas import config


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
