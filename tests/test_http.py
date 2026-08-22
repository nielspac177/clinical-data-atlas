"""Tests for atlas.http: retries, backoff, politeness, offline guard, cache.

Every test here runs fully offline: ``urllib.request.urlopen`` and
``time.sleep``/``time.time`` are monkeypatched, so nothing ever touches a
real socket or clock (on top of the ``tests/conftest.py`` socket guard).
"""

import hashlib
import json
import time
import urllib.error
import urllib.request

import pytest

from atlas import config, http


@pytest.fixture(autouse=True)
def _reset_politeness_state():
    """Give every test a clean per-host politeness clock."""
    http._last_call.clear()
    yield
    http._last_call.clear()


@pytest.fixture
def online(monkeypatch):
    """Bypass the offline guard and per-host delay for tests that exercise
    the (monkeypatched, still socket-free) request path.
    """
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "POLITE_DELAY", 0.0)


class _FakeResponse:
    """Minimal stand-in for the context-manager object urlopen() returns."""

    def __init__(self, status=200, url="http://host-a.test/", body=b""):
        self.status = status
        self.url = url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _json_response(obj, status=200, url="http://host-a.test/"):
    return _FakeResponse(status=status, url=url, body=json.dumps(obj).encode("utf-8"))


def _http_error(code, url="http://host-a.test/"):
    return urllib.error.HTTPError(
        url=url, code=code, msg=f"error {code}", hdrs=None, fp=None
    )


def _install_sequence(monkeypatch, outcomes):
    """Patch urlopen to return/raise each of `outcomes` in call order."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _install_sleep_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(time, "sleep", calls.append)
    return calls


# ---------------------------------------------------------------------------
# OfflineError: checked at call time, not import time
# ---------------------------------------------------------------------------


def test_get_json_raises_offline_error_by_default():
    """ATLAS_OFFLINE=1 is the unit-test default (tests/conftest.py) -- no
    monkeypatching needed to prove the guard is armed."""
    assert config.OFFLINE is True
    with pytest.raises(http.OfflineError):
        http.get_json("http://host-a.test/data.json")


def test_post_json_raises_offline_error_by_default():
    with pytest.raises(http.OfflineError):
        http.post_json("http://host-a.test/data.json", {"x": 1})


def test_head_status_raises_offline_error_by_default():
    with pytest.raises(http.OfflineError):
        http.head_status("http://host-a.test/")


def test_offline_error_is_checked_live_via_config_attribute(monkeypatch):
    """Toggling atlas.config.OFFLINE after atlas.http is imported must take
    effect on the next call -- proves the check reads config.OFFLINE live
    rather than a value captured at import time.
    """
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "POLITE_DELAY", 0.0)
    _install_sequence(monkeypatch, [_json_response({"ok": True})])
    assert http.get_json("http://host-a.test/x") == {"ok": True}

    monkeypatch.setattr(config, "OFFLINE", True)
    with pytest.raises(http.OfflineError, match="host-a.test"):
        http.get_json("http://host-a.test/x")


# ---------------------------------------------------------------------------
# get_json: success / retries / backoff
# ---------------------------------------------------------------------------


def test_get_json_success_first_try(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_json_response({"a": 1})])
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.get_json("http://host-a.test/x") == {"a": 1}
    assert len(calls) == 1
    assert sleeps == []


def test_get_json_404_returns_none_without_retry(online, monkeypatch, capsys):
    calls = _install_sequence(monkeypatch, [_http_error(404)])
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.get_json("http://host-a.test/missing") is None
    assert len(calls) == 1
    assert sleeps == []
    assert "[http] 404" in capsys.readouterr().err


def test_get_json_other_4xx_returns_none_without_retry(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_http_error(401)])
    assert http.get_json("http://host-a.test/x") is None
    assert len(calls) == 1


def test_get_json_quiet_suppresses_diagnostics(online, monkeypatch, capsys):
    _install_sequence(monkeypatch, [_http_error(404)])
    assert http.get_json("http://host-a.test/x", quiet=True) is None
    assert capsys.readouterr().err == ""


def test_get_json_429_is_retried_then_succeeds(online, monkeypatch):
    calls = _install_sequence(
        monkeypatch, [_http_error(429), _http_error(429), _json_response({"ok": 1})]
    )
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.get_json("http://host-a.test/x") == {"ok": 1}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_get_json_exhausts_retries_returns_none(online, monkeypatch, capsys):
    calls = _install_sequence(
        monkeypatch, [_http_error(500), _http_error(500), _http_error(500)]
    )
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.get_json("http://host-a.test/x") is None
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # no sleep spent after the final, exhausted attempt
    assert "[http] failed" in capsys.readouterr().err


def test_get_json_backoff_sequence_is_1_2_4(online, monkeypatch):
    calls = _install_sequence(
        monkeypatch,
        [_http_error(500), _http_error(500), _http_error(500), _http_error(500)],
    )
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.get_json("http://host-a.test/x", retries=4) is None
    assert len(calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_get_json_generic_exception_is_retried(online, monkeypatch):
    calls = _install_sequence(
        monkeypatch, [TimeoutError("slow"), _json_response({"ok": 1})]
    )
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.get_json("http://host-a.test/x") == {"ok": 1}
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_get_json_retries_default_is_3(online, monkeypatch):
    calls = _install_sequence(
        monkeypatch, [_http_error(500), _http_error(500), _http_error(500)]
    )
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.get_json("http://host-a.test/x") is None
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


# ---------------------------------------------------------------------------
# get_json: params -> qs merge
# ---------------------------------------------------------------------------


def test_get_json_params_merged_via_qs(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_json_response({"ok": 1})])
    http.get_json("http://host-a.test/x", params={"a": 1, "b": None, "c": "z"})
    assert len(calls) == 1
    url = calls[0].full_url
    assert url.startswith("http://host-a.test/x?")
    assert "a=1" in url
    assert "c=z" in url
    assert "b=" not in url


def test_get_json_params_appended_to_existing_query(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_json_response({"ok": 1})])
    http.get_json("http://host-a.test/x?existing=1", params={"a": 2})
    assert calls[0].full_url == "http://host-a.test/x?existing=1&a=2"


def test_get_json_no_params_leaves_url_untouched(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_json_response({"ok": 1})])
    http.get_json("http://host-a.test/x")
    assert calls[0].full_url == "http://host-a.test/x"


# ---------------------------------------------------------------------------
# get_json: per-host politeness
# ---------------------------------------------------------------------------


def test_get_json_politeness_sleeps_per_host(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "POLITE_DELAY", 5.0)

    clock = {"now": 1000.0}
    sleeps = []

    def fake_time():
        return clock["now"]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    _install_sequence(
        monkeypatch,
        [_json_response({"n": 1}), _json_response({"n": 2}), _json_response({"n": 3})],
    )

    http.get_json("http://host-a.test/1")  # first call ever to host-a: no debt yet
    assert sleeps == []

    clock["now"] += 1.0  # only 1s elapsed since the call above
    http.get_json("http://host-a.test/2")  # same host: must wait out the remainder
    assert sleeps == [4.0]

    http.get_json("http://host-b.test/1")  # different host: no debt of its own
    assert sleeps == [4.0]


# ---------------------------------------------------------------------------
# get_json: on-disk cache
# ---------------------------------------------------------------------------


def test_get_json_cache_write_then_hit_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "HTTP_CACHE", True)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "POLITE_DELAY", 0.0)
    calls = _install_sequence(monkeypatch, [_json_response({"cached": True})])

    url = "http://host-a.test/cacheme"
    assert http.get_json(url) == {"cached": True}
    assert len(calls) == 1

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_file = tmp_path / ".cache" / "http" / f"{digest}.json"
    assert cache_file.exists()
    envelope = json.loads(cache_file.read_text(encoding="utf-8"))
    assert envelope["url"] == url
    assert envelope["body"] == {"cached": True}
    assert isinstance(envelope["fetched_at"], (int, float))

    # Go fully offline and make a second urlopen call blow up if it is ever
    # attempted: a cache hit must short-circuit before both the network call
    # and the offline guard.
    monkeypatch.setattr(config, "OFFLINE", True)

    def _boom(*_a, **_kw):
        raise AssertionError("urlopen should not be called on a cache hit")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    assert http.get_json(url) == {"cached": True}


def test_get_json_cache_expired_ttl_refetches(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "HTTP_CACHE", True)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "POLITE_DELAY", 0.0)
    monkeypatch.setenv("ATLAS_HTTP_CACHE_TTL", "60")

    url = "http://host-a.test/stale"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_file = tmp_path / ".cache" / "http" / f"{digest}.json"
    cache_file.parent.mkdir(parents=True)
    stale = {"url": url, "fetched_at": time.time() - 3600, "body": {"old": True}}
    cache_file.write_text(json.dumps(stale), encoding="utf-8")

    calls = _install_sequence(monkeypatch, [_json_response({"fresh": True})])
    assert http.get_json(url) == {"fresh": True}
    assert len(calls) == 1


def test_get_json_cache_fresh_within_ttl_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "HTTP_CACHE", True)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setenv("ATLAS_HTTP_CACHE_TTL", "3600")

    url = "http://host-a.test/fresh"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_file = tmp_path / ".cache" / "http" / f"{digest}.json"
    cache_file.parent.mkdir(parents=True)
    fresh = {"url": url, "fetched_at": time.time() - 10, "body": {"still": "good"}}
    cache_file.write_text(json.dumps(fresh), encoding="utf-8")

    def _boom(*_a, **_kw):
        raise AssertionError("a fresh cache entry must not trigger a request")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert http.get_json(url) == {"still": "good"}


def test_get_json_cache_disabled_by_default_hits_network_each_time(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "POLITE_DELAY", 0.0)
    assert config.HTTP_CACHE is False
    calls = _install_sequence(
        monkeypatch, [_json_response({"n": 1}), _json_response({"n": 2})]
    )
    assert http.get_json("http://host-a.test/x") == {"n": 1}
    assert http.get_json("http://host-a.test/x") == {"n": 2}
    assert len(calls) == 2


def test_post_json_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "HTTP_CACHE", True)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "POLITE_DELAY", 0.0)
    _install_sequence(monkeypatch, [_json_response({"ok": 1})])
    http.post_json("http://host-a.test/x", {"payload": 1})
    cache_dir = tmp_path / ".cache" / "http"
    assert not cache_dir.exists() or list(cache_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# head_status
# ---------------------------------------------------------------------------


def test_head_status_success_first_try(online, monkeypatch):
    calls = _install_sequence(
        monkeypatch, [_FakeResponse(status=200, url="http://host-a.test/final")]
    )
    assert http.head_status("http://host-a.test/x") == (200, "http://host-a.test/final")
    assert len(calls) == 1
    assert calls[0].get_method() == "HEAD"


def test_head_status_head_405_falls_back_to_get(online, monkeypatch):
    calls = _install_sequence(
        monkeypatch,
        [_http_error(405), _FakeResponse(status=200, url="http://host-a.test/x")],
    )
    assert http.head_status("http://host-a.test/x") == (200, "http://host-a.test/x")
    assert len(calls) == 2
    assert calls[0].get_method() == "HEAD"
    assert calls[1].get_method() == "GET"


@pytest.mark.parametrize("code", [403, 405, 501])
def test_head_status_fallback_codes_retry_as_get(online, monkeypatch, code):
    calls = _install_sequence(
        monkeypatch,
        [_http_error(code), _FakeResponse(status=200, url="http://host-a.test/x")],
    )
    assert http.head_status("http://host-a.test/x") == (200, "http://host-a.test/x")
    assert len(calls) == 2


def test_head_status_non_fallback_4xx_returns_immediately(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_http_error(404)])
    assert http.head_status("http://host-a.test/x") == (404, "http://host-a.test/x")
    assert len(calls) == 1


def test_head_status_total_failure_returns_zero(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [OSError("down"), OSError("still down")])
    assert http.head_status("http://host-a.test/x") == (0, "http://host-a.test/x")
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# qs
# ---------------------------------------------------------------------------


def test_qs_drops_none_values():
    assert http.qs(a=1, b=None, c="z") == "a=1&c=z"


def test_qs_empty_when_no_params_or_all_none():
    assert http.qs() == ""
    assert http.qs(a=None) == ""


def test_qs_url_encodes_special_characters():
    assert http.qs(q="a b/c") == "q=a+b%2Fc"


# ---------------------------------------------------------------------------
# post_json
# ---------------------------------------------------------------------------


def test_post_json_success(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_json_response({"created": True})])
    result = http.post_json("http://host-a.test/x", {"a": 1})
    assert result == {"created": True}
    assert len(calls) == 1
    assert calls[0].get_method() == "POST"
    assert json.loads(calls[0].data.decode("utf-8")) == {"a": 1}


def test_post_json_4xx_returns_none_without_retry(online, monkeypatch):
    calls = _install_sequence(monkeypatch, [_http_error(400)])
    assert http.post_json("http://host-a.test/x", {"a": 1}) is None
    assert len(calls) == 1


def test_post_json_429_is_retried(online, monkeypatch):
    calls = _install_sequence(
        monkeypatch, [_http_error(429), _json_response({"ok": 1})]
    )
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.post_json("http://host-a.test/x", {}, retries=2) == {"ok": 1}
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_post_json_shares_backoff_with_get_json(online, monkeypatch):
    """post_json and get_json both delegate to atlas.http._request_json, so
    a 429->429->200 sequence must produce the exact same [1.0, 2.0] backoff
    get_json's own 429 test produces (see test_get_json_429_is_retried_then_succeeds)."""
    calls = _install_sequence(
        monkeypatch, [_http_error(429), _http_error(429), _json_response({"ok": 1})]
    )
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.post_json("http://host-a.test/x", {}, retries=3) == {"ok": 1}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_post_json_exhausts_retries_returns_none(online, monkeypatch, capsys):
    calls = _install_sequence(monkeypatch, [_http_error(500), _http_error(500)])
    sleeps = _install_sleep_recorder(monkeypatch)
    assert http.post_json("http://host-a.test/x", {}, retries=2) is None
    assert len(calls) == 2
    assert sleeps == [1.0]
    assert "[http] failed" in capsys.readouterr().err
