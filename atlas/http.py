"""Polite stdlib-only HTTP client: retries, backoff, cache, offline guard.

Ported from the prior ``neurodatahub`` project's ``scripts/lib/http.py``
(same design, comments translated from Spanish to English), with three
additions this project needs: an ``OfflineError`` guard so unit tests can
never slip through to a real socket, a ``params`` kwarg on :func:`get_json`,
and an optional on-disk response cache.

Design carried over unchanged:

- Stdlib ``urllib`` only -- no ``requests``, no extra dependency.
- :func:`get_json` returns ``None`` instead of raising on failure. This is
  deliberate: a monthly refresh that touches eight sources must not abort
  because one of them is down -- see ``atlas.harvest``/``atlas.refresh``.
- Per-host politeness: at most one request every :data:`atlas.config.POLITE_DELAY`
  seconds to the same ``netloc``, tracked in :data:`_last_call`.
- Retries use exponential backoff starting at 1s and doubling (1, 2, 4, ...).
  A ``429`` is treated as transient and retried like a 5xx or a network
  error; any other ``4xx`` means retrying won't help, so it returns/aborts
  immediately without spending a retry.
- :func:`head_status` falls back from HEAD to GET when a server rejects HEAD
  outright (403/405/501) -- some servers do, and that is not evidence the
  linked resource itself is broken.

:func:`get_json` and :func:`post_json` share one private attempt loop,
:func:`_request_json` -- politeness, retries, backoff, and diagnostics live
in exactly one place, so the retry contract can't drift between the two
(e.g. one honoring ``quiet`` and the other always printing). Each public
function stays a thin wrapper: `get_json` adds params/cache handling around
a GET request, `post_json` builds a POST request and never caches.

Both the offline guard and the cache read :data:`atlas.config.OFFLINE`,
:data:`atlas.config.HTTP_CACHE`, and :data:`atlas.config.ROOT` through the
``config`` module object at call time rather than importing the values
directly, so tests can monkeypatch ``atlas.config.OFFLINE`` (etc.) after
this module is imported and have it take effect immediately.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from atlas import config, io

_DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h; override via ATLAS_HTTP_CACHE_TTL


class OfflineError(RuntimeError):
    """Raised by a network call while :data:`atlas.config.OFFLINE` is true."""


# ---------------------------------------------------------------------------
# Internal helpers: offline guard, politeness, cache, diagnostics
# ---------------------------------------------------------------------------


def _ensure_online(url: str) -> None:
    if config.OFFLINE:
        raise OfflineError(f"network disabled (ATLAS_OFFLINE=1): {url}")


_last_call: dict[str, float] = {}


def _polite(host: str) -> None:
    """Sleep just long enough to keep `host` at or under POLITE_DELAY."""
    prev = _last_call.get(host, 0.0)
    wait = config.POLITE_DELAY - (time.time() - prev)
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def _log(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, file=sys.stderr)


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return config.ROOT / ".cache" / "http" / f"{digest}.json"


def _cache_ttl_seconds() -> float:
    raw = os.environ.get("ATLAS_HTTP_CACHE_TTL")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(_DEFAULT_CACHE_TTL_SECONDS)


def _read_cache(path: Path) -> Any:
    """The cached body at `path` if present and fresh, else None (a plain
    cache miss covers "absent", "corrupt", and "expired" alike)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        envelope = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(envelope, dict):
        return None
    fetched_at = envelope.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - fetched_at > _cache_ttl_seconds():
        return None
    return envelope.get("body")


def _write_cache(path: Path, url: str, body: Any) -> None:
    envelope = {"url": url, "fetched_at": time.time(), "body": body}
    io.write_atomic(path, io.canonical_json(envelope))


_FAILED = object()  # sentinel: "every attempt failed", distinct from a
# legitimate successful JSON `null` response body (which is plain `None`)


def _request_json(
    req: urllib.request.Request,
    *,
    host: str,
    timeout: int,
    retries: int,
    quiet: bool,
) -> Any:
    """Shared GET/POST attempt loop: politeness, retries, backoff, diagnostics.

    Returns the parsed JSON body on success (which may legitimately be
    ``None`` for a JSON ``null`` response), or the :data:`_FAILED` sentinel
    once every attempt has failed -- callers translate that sentinel to
    whatever their own public "failure" return value is. A ``429`` or any
    non-``HTTPError`` exception is treated as transient and retried with
    exponential backoff (1, 2, 4, ... seconds); any other ``4xx`` is not
    retried.
    """
    url = req.full_url
    backoff = 1.0
    last_reason = "unknown"
    for attempt in range(retries):
        _polite(host)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 429 and 400 <= e.code < 500:
                _log(quiet, f"[http] {e.code} {url}")
                return _FAILED
            last_reason = str(e.code)
        except Exception as e:  # noqa: BLE001 -- one dead source must not abort a run
            last_reason = type(e).__name__
        else:
            return body

        if attempt < retries - 1:
            time.sleep(backoff)
            backoff *= 2

    _log(quiet, f"[http] failed ({last_reason}) {url}")
    return _FAILED


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def qs(**params: Any) -> str:
    """URL-encoded query string, silently dropping ``None``-valued kwargs
    (so callers can pass optional filters without building a dict by hand)."""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
    retries: int = 3,
    quiet: bool = False,
) -> Any:
    """GET `url` (with `params` merged in via :func:`qs`) and return the
    parsed JSON body, or ``None`` if every attempt fails.

    Returning ``None`` rather than raising is deliberate -- see the module
    docstring. Raises :class:`OfflineError` instead of ever touching the
    network when :data:`atlas.config.OFFLINE` is true, unless a fresh cache
    entry answers the request first.
    """
    if params:
        query = qs(**params)
        if query:
            url = f"{url}{'&' if '?' in url else '?'}{query}"

    cache_path: Path | None = None
    if config.HTTP_CACHE:
        cache_path = _cache_path(url)
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached

    _ensure_online(url)

    host = urllib.parse.urlparse(url).netloc
    req = urllib.request.Request(
        url, headers={"User-Agent": config.UA, "Accept": "application/json"}
    )
    result = _request_json(
        req, host=host, timeout=timeout, retries=retries, quiet=quiet
    )
    if result is _FAILED:
        return None
    if cache_path is not None:
        _write_cache(cache_path, url, result)
    return result


def post_json(url: str, payload: Any, *, timeout: int = 30, retries: int = 2) -> Any:
    """POST `payload` as JSON and return the parsed JSON response, or
    ``None`` if every attempt fails. Never cached (POST is not idempotent).

    Retry semantics are exactly :func:`get_json`'s, via the same
    :func:`_request_json` helper: a 429 or a network error is retried with
    exponential backoff, any other 4xx returns ``None`` immediately.
    """
    _ensure_online(url)

    host = urllib.parse.urlparse(url).netloc
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": config.UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    result = _request_json(
        req, host=host, timeout=timeout, retries=retries, quiet=False
    )
    return None if result is _FAILED else result


def head_status(url: str, timeout: int = 20) -> tuple[int, str]:
    """``(status_code, final_url)`` for `url`, for dead-link checking.

    Tries HEAD first; falls back to GET if a server rejects HEAD outright
    (403/405/501) rather than treating that as a broken link. Returns
    ``(0, url)`` if both attempts fail outright (DNS/timeout/connection
    errors, not HTTP error responses). Kept separate from
    :func:`_request_json`: it has no backoff/retry-count contract to share,
    just a HEAD-then-GET fallback on specific status codes.
    """
    _ensure_online(url)

    host = urllib.parse.urlparse(url).netloc
    for method in ("HEAD", "GET"):
        _polite(host)
        req = urllib.request.Request(
            url, method=method, headers={"User-Agent": config.UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.url
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue
            return e.code, url
        except Exception:  # noqa: BLE001 -- any transport failure means "unreachable"
            if method == "GET":
                return 0, url
    return 0, url
