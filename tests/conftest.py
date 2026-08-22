"""Shared pytest fixtures: default to offline mode and block real sockets.

Any test not marked ``live`` or ``e2e`` gets a ``socket.socket`` whose
``connect``/``connect_ex`` raise instead of touching the network. This
keeps the unit suite fast, deterministic, and safe to run anywhere.
"""

from __future__ import annotations

import os
import socket

import pytest

# Default to offline unless the environment already set this explicitly
# (e.g. a live/e2e run that unsets or overrides it before pytest starts).
os.environ.setdefault("ATLAS_OFFLINE", "1")


def _blocked_connect(*_args, **_kwargs):
    raise RuntimeError("network disabled in unit tests")


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    """Block real socket connections unless the test opts in via markers."""
    offline = os.environ.get("ATLAS_OFFLINE") == "1"
    live = request.node.get_closest_marker("live")
    e2e = request.node.get_closest_marker("e2e")
    if offline and not (live or e2e):
        monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)
    yield
