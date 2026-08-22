"""Regression tests for the network guard in tests/conftest.py.

These protect the project's global "no network in unit tests" constraint.
If the guard's marker check, monkeypatch target, or condition is ever
broken by a future edit, this file — not a human noticing slow CI — is what
should catch it.
"""

from __future__ import annotations

import _socket
import os
import socket

import pytest


def test_connect_is_blocked_by_default():
    """A default (unmarked) test must never be able to open a real socket."""
    with pytest.raises(RuntimeError, match="network disabled in unit tests"):
        socket.create_connection(("127.0.0.1", 9))


def test_connect_ex_is_blocked_by_default():
    """connect_ex must be blocked too, not just connect."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network disabled in unit tests"):
            s.connect_ex(("127.0.0.1", 9))
    finally:
        s.close()


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("ATLAS_LIVE") != "1",
    reason="live-marked tests only run with ATLAS_LIVE=1 (see `make test-live`)",
)
def test_live_marked_tests_bypass_the_guard():
    """live-marked tests must get real, unpatched sockets.

    Offline-safe: this only compares class attributes, it never actually
    opens a connection.
    """
    assert socket.socket.connect is _socket.socket.connect
    assert socket.socket.connect_ex is _socket.socket.connect_ex
