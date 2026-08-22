"""Trivial smoke test so the suite has at least one passing test."""

import atlas


def test_version():
    assert atlas.__version__ == "0.1.0"
