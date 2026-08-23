"""Pluggable LLM backends and the per-record answer cache.

Three backends implement one :class:`Backend` protocol -- ask for JSON,
get parsed JSON back, or an :class:`LLMError`:

- :class:`AnthropicBackend` -- the Messages API via the optional
  `anthropic` package (`pip install 'clinical-data-atlas[llm]'`),
  imported *lazily inside the class* so that neither this module nor the
  unit suite needs the package present.
- :class:`ClaudeCLIBackend` -- the local ``claude`` CLI, one
  non-interactive, non-persisted run per call.
- :class:`NullBackend` -- always fails. Selected when nothing else is
  available, which is what makes "no credentials anywhere" a *rules-only*
  run rather than a crash.

:func:`select_backend` picks one; an explicit override (``atlas enrich
--llm``) or `ATLAS_LLM` always wins over auto-detection.

**The cache is keyed per record, never per batch.** The key hashes the
prompt version, the output schema, and one record's
:func:`atlas.enrich.prompts.record_input` -- so regrouping the same
records into different batches (a different `--source` selection, a
changed `batch_size`) cannot invalidate a single cached answer. Files
land in ``data/raw/enrich/llm/<hex>.json`` (the bare digest -- a colon
in a committed filename is illegal on Windows) and are committed like
the rest of ``data/raw/``: they *are* the provenance for every
LLM-derived field, and they make a re-run reproducible with no model
access at all. Each entry records the model that produced it, so a later
run over the committed cache stamps *that* model into the record rather
than whichever backend happens to be configured.

:func:`cache_lookup`, :func:`complete`, and :func:`cache_put` are the
three cache primitives; :func:`cached_complete` composes them for one
record and `classify.enrich_records` composes the same three for a
batch, so neither the envelope nor the failure policy exists twice.

:func:`cached_complete` never raises: a failing backend yields `None` and
its message is appended to :data:`LAST_ERRORS`, because one unavailable
model must not take down a refresh (the affected records simply stay
rules-only).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from atlas import config, io
from atlas.enrich import prompts


class LLMError(RuntimeError):
    """Any failure to obtain a parsed JSON answer from a backend."""


# Read once at import time, like `atlas.config`'s flags.
DEFAULT_MODEL = os.environ.get("ATLAS_MODEL", "claude-opus-5")
DEFAULT_CLI_MODEL = os.environ.get("ATLAS_CLI_MODEL", "opus")

# Where cached answers live -- under `data/raw/`, because they are raw
# provenance for the enrich stage exactly as harvested payloads are for
# the harvest stage.
CACHE_DIR = config.RAW / "enrich" / "llm"

# Messages from recent backend failures, for the enrich stage's
# stats/report. Callers may clear it; nothing here reads it back.
LAST_ERRORS: list[str] = []
MAX_RECORDED_ERRORS = 50

# Non-streaming, so keep `max_tokens` well under the SDK's HTTP timeout.
MAX_TOKENS = 8000
# One batch of ~8 records is a couple of seconds of work; 5 minutes is a
# hang, not a slow answer.
CLI_TIMEOUT = 300


class Backend(Protocol):
    """What the enrich stage needs from a model: a name, a model id (or
    `None` when there is no model), and one JSON-in/JSON-out call."""

    name: str
    model: str | None

    def complete_json(self, prompt: str, schema: dict) -> dict | list:
        """Return `prompt`'s answer, parsed, constrained to `schema`.

        Raises :class:`LLMError` for every failure mode -- transport,
        refusal, unparsable output -- so callers handle one exception
        type regardless of which backend is in play.
        """


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class NullBackend:
    """The no-model backend: selected when no credentials and no CLI are
    available, so a run degrades to rules-only instead of failing."""

    name = "none"
    model: str | None = None

    def complete_json(self, prompt: str, schema: dict) -> dict | list:
        raise LLMError("no LLM backend")


class AnthropicBackend:
    """The Messages API, with structured outputs.

    The `anthropic` import happens inside :meth:`_module`, not at module
    scope: the package is an optional extra, and importing this module
    (or running the unit suite) must not require it.
    """

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model: str | None = model
        self._client = None

    def _module(self):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError(
                "the 'anthropic' package is not installed "
                "(pip install 'clinical-data-atlas[llm]')"
            ) from exc
        return anthropic

    def _get_client(self):
        """The lazily-built client. Constructed with no arguments on
        purpose: the SDK resolves `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_
        TOKEN`, or an `ant auth login` profile itself, and hard-coding a
        key here would be one more place a secret could leak."""
        if self._client is None:
            anthropic = self._module()
            try:
                self._client = anthropic.Anthropic()
            except Exception as exc:  # e.g. no credentials resolvable at all
                raise LLMError(f"cannot construct Anthropic client: {exc}") from exc
        return self._client

    def complete_json(self, prompt: str, schema: dict) -> dict | list:
        return _guarded(self.name, self._call, prompt, schema)

    def _call(self, prompt: str, schema: dict) -> dict | list:
        anthropic = self._module()
        client = self._get_client()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=prompts.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        # Most specific first: RateLimitError is an APIStatusError, and
        # the SDK has already retried 429/5xx twice by the time either
        # reaches us -- so there is nothing left to do but report.
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"api error {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMError(f"refused: {category}")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("response carried no text block")
        return _parse_json(text, "anthropic response")


class ClaudeCLIBackend:
    """The local ``claude`` CLI, one non-interactive run per call.

    ``--no-session-persistence`` keeps the pipeline from leaving session
    state behind, and ``--json-schema`` gives the CLI path the same
    structured-output guarantee the API path gets. The CLI takes no
    separate system prompt, so it is prepended to the user prompt.
    """

    name = "claude_cli"

    def __init__(self, model: str = DEFAULT_CLI_MODEL) -> None:
        self.model: str | None = model

    def complete_json(self, prompt: str, schema: dict) -> dict | list:
        return _guarded(self.name, self._call, prompt, schema)

    def _call(self, prompt: str, schema: dict) -> dict | list:
        command = [
            "claude",
            "-p",
            "--bare",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--model",
            self.model,
        ]
        try:
            # Fixed argv, never a shell; a non-zero exit is handled below.
            proc = subprocess.run(
                command,
                input=f"{prompts.SYSTEM_PROMPT}\n\n{prompt}",
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LLMError("the 'claude' CLI is not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"the 'claude' CLI timed out after {CLI_TIMEOUT}s") from exc

        if proc.returncode != 0:
            raise LLMError(
                f"the 'claude' CLI exited {proc.returncode}: {_tail(proc.stderr)}"
            )

        envelope = _parse_json(
            proc.stdout, f"claude CLI stdout (stderr: {_tail(proc.stderr)})"
        )
        if isinstance(envelope, dict):
            if "structured_output" in envelope:
                return envelope["structured_output"]
            if isinstance(envelope.get("result"), str):
                return _parse_json(envelope["result"], "claude CLI result field")
        return envelope


_BACKENDS: dict[str, type] = {
    "anthropic": AnthropicBackend,
    "claude_cli": ClaudeCLIBackend,
    "none": NullBackend,
}


def select_backend(override: str | None = None) -> Backend:
    """Pick a backend: `override`, else `ATLAS_LLM`, else auto-detect.

    Auto-detection prefers the API (an `ANTHROPIC_API_KEY` in the
    environment) over the CLI, and falls back to :class:`NullBackend` --
    "no model available" is a supported, rules-only run, never an error.
    An unknown name raises `ValueError`: a typo in `--llm` should fail
    loudly rather than silently disable enrichment.
    """
    choice = override or config.LLM
    if choice:
        if choice not in _BACKENDS:
            raise ValueError(
                f"unknown LLM backend {choice!r} "
                f"(choose from: {', '.join(sorted(_BACKENDS))})"
            )
        return _BACKENDS[choice]()

    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicBackend()
    if shutil.which("claude"):
        return ClaudeCLIBackend()
    return NullBackend()


# ---------------------------------------------------------------------------
# Per-record cache
# ---------------------------------------------------------------------------


def cache_key(record_input: dict, schema: dict) -> str:
    """``"sha256:<hex>"`` over the prompt version, the schema, and one
    record's canonical input -- see the module docstring for why all
    three, and why it is per record rather than per batch."""
    payload = (
        prompts.PROMPT_VERSION
        + io.content_hash(schema)
        + io.canonical_json(record_input)
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_path(key: str, *, cache_dir: Path = CACHE_DIR) -> Path:
    """The file `key` is stored in: the bare hex digest, *without* the
    ``sha256:`` prefix (Ruling R15). A colon is legal on POSIX but is the
    alternate-data-stream separator on Windows, and `data/raw/` is
    committed -- a checkout must not depend on the OS that produced it.
    The prefix survives inside the file, in its ``key`` field."""
    return Path(cache_dir) / f"{key.removeprefix('sha256:')}.json"


def cache_lookup(key: str, *, cache_dir: Path = CACHE_DIR) -> dict | None:
    """The whole cached entry for `key` -- ``{key, backend, model,
    prompt_version, output}`` -- or `None` on a miss.

    Callers need more than the answer: the entry records *which model*
    produced it, and that is what a later run must stamp into
    `provenance.enrichment`, not whichever backend happens to be
    configured today. This is the one place the cache is read; everything
    else (`cache_get`, `cached_complete`, `classify.enrich_records`) goes
    through it.

    A file that exists but doesn't parse (a hand-edit, a write from
    before `write_atomic`) is treated as a miss rather than an error: the
    worst case is one extra model call, which then rewrites it correctly.
    """
    path = cache_path(key, cache_dir=cache_dir)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict) or entry.get("output") is None:
        return None
    return entry


def cache_get(key: str, *, cache_dir: Path = CACHE_DIR) -> dict | list | None:
    """Just the cached `output` for `key` (see :func:`cache_lookup`)."""
    entry = cache_lookup(key, cache_dir=cache_dir)
    return None if entry is None else entry.get("output")


def cache_put(
    key: str,
    backend: Backend,
    output: dict | list,
    *,
    cache_dir: Path = CACHE_DIR,
) -> Path:
    """Store `output` for `key` and return the file written.

    The stored answer is the model's *raw* item, before any guard runs --
    so re-running with tightened guards re-filters the same answer
    instead of re-asking, and the file stays an honest record of what the
    model actually said.
    """
    path = cache_path(key, cache_dir=cache_dir)
    io.write_atomic(
        path,
        io.pretty_json(
            {
                "key": key,
                "backend": backend.name,
                "model": backend.model,
                "prompt_version": prompts.PROMPT_VERSION,
                "output": output,
            }
        ),
    )
    return path


def cached_complete(
    backend: Backend,
    key: str,
    prompt: str,
    schema: dict,
    *,
    cache_dir: Path = CACHE_DIR,
) -> dict | list | None:
    """`key`'s cached answer, or one fresh call, or `None` on failure.

    The single-record composition of the three cache primitives --
    :func:`cache_lookup`, :func:`complete`, :func:`cache_put`.
    `classify.enrich_records` composes those same three for the batch
    case (N keys, one call), so there is one implementation of each step
    and no second copy of the envelope or error handling to drift.

    Never raises: a backend failure is recorded in :data:`LAST_ERRORS`
    and reported as `None`, so a broken or missing model degrades that
    record to rules-only rather than killing the run.
    """
    entry = cache_lookup(key, cache_dir=cache_dir)
    if entry is not None:
        return entry.get("output")
    output = complete(backend, prompt, schema)
    if output is None:
        return None
    cache_put(key, backend, output, cache_dir=cache_dir)
    return output


def complete(backend: Backend, prompt: str, schema: dict) -> dict | list | None:
    """One backend call, with the failure policy applied once: an
    :class:`LLMError` becomes `None` plus an entry in :data:`LAST_ERRORS`,
    never an exception the pipeline has to handle a second way."""
    try:
        return backend.complete_json(prompt, schema)
    except LLMError as exc:
        record_error(str(exc))
        return None


def record_error(message: str) -> None:
    """Note a backend failure for the enrich stage's stats, keeping only
    the most recent :data:`MAX_RECORDED_ERRORS` -- a refresh over a broken
    source could otherwise accumulate one string per record. Mutates the
    list in place so an early `from atlas.enrich.llm import LAST_ERRORS`
    stays live."""
    LAST_ERRORS.append(message)
    del LAST_ERRORS[:-MAX_RECORDED_ERRORS]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _guarded(name: str, call, prompt: str, schema: dict) -> dict | list:
    """Run `call`, guaranteeing the :class:`Backend` contract: every
    failure leaves as an `LLMError`.

    The per-SDK `except` chains inside each backend name the failures we
    understand; this catches the ones we don't (a `TypeError` from an SDK
    signature change, a `KeyError` from an unexpected envelope) so a
    surprise upstream can never escape the enrich stage as an unhandled
    exception and kill the whole refresh.
    """
    try:
        return call(prompt, schema)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(
            f"{name} backend failed: {exc.__class__.__name__}: {exc}"
        ) from exc


def _parse_json(text: str, what: str) -> dict | list:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"unparsable JSON from {what}: {exc}") from exc


def _tail(text: str | None, limit: int = 400) -> str:
    """The last `limit` characters of `text`, whitespace-trimmed -- enough
    of a stderr tail to diagnose a failure without dumping a log into the
    error message."""
    if not text:
        return ""
    return text.strip()[-limit:]
