"""Pure helpers shared by every source's normalizer.

Nothing here touches the network or the filesystem -- every function is a
plain string/text transform, a schema-object builder, or (`Excluded`) a
tiny data carrier, so this module is safe to import and unit-test in
isolation, exactly like `atlas.io`.

`atlas/normalize/<source>.py` modules (Task 1.x, "M1 — Sources") are the
callers: they use `clean_title` on every `name` (and on any other text
field a source hands over unmassaged), `clean_doi`/`collapse_version_doi`
on whatever DOI a source reports, `year_of` on date-ish fields,
`regex_sample_size` to pull a participant/record count out of free text
when a source doesn't report one as a structured field, `license_to_spdx`
on a source's license string, and `make_provenance` to build every
`Record.provenance`.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from atlas import vocab
from atlas.schema import EnrichmentProv, Provenance

# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

# Tab/LF/VT/FF/CR (0x09-0x0d) are excluded from this class: they are
# whitespace, and the collapse below turns them into a space instead. The
# information-separator controls 0x1c-0x1f are `\s` too but are deleted
# anyway -- they are corruption in a title, not word separators.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")

# HTML tags a source has actually been seen to leak into a text field.
# An allowlist, because a dataset title is full of angle brackets that are
# not markup -- "children <18 years", "SpO2 < 90%", "Cohort <n=40>".
_HTML_TAG_NAMES = (
    "a|b|br|code|div|em|h[1-6]|hr|i|img|li|ol|p|pre|small|span|strong|sub|sup"
    "|table|tbody|td|th|thead|tr|u|ul"
    # Never a legitimate title, and stripped rather than passed through so
    # that this stays the single hygiene chokepoint for names.
    "|script|style|noscript|iframe|object|embed|svg"
)
# Either a bare tag (`<p>`, `</em>`, `<br/>`) or one carrying something that
# looks like an attribute (`<a href="...">`). The `=` in that second form is
# what keeps prose such as "Compare <A and B> groups" from reading as an
# anchor tag purely because it starts with an "a".
_HTML_FRAGMENT_RE = re.compile(
    rf"(?i)</?({_HTML_TAG_NAMES})\s*/?>|<({_HTML_TAG_NAMES})\s[^<>]*=[^<>]*>"
)
# Elements whose *contents* are markup too, never title text: dropped whole
# rather than leaving a script body behind as visible words.
_HTML_DROP_ELEMENT_RE = re.compile(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1\s*>")


def clean_title(s: str | None) -> str:
    """A source's title text as a single clean line.

    Every run of whitespace -- including the newlines that wrap a long
    title mid-sentence in 8 live OpenNeuro records, e.g.
    `openneuro:ds003126` -- collapses to one space, the ends are trimmed,
    and non-whitespace control characters are dropped outright. A `name`
    is rendered inline everywhere it appears (graph label, table cell,
    CSV column, changelog table row), so it must never carry a newline or
    a control character.

    Markup is removed span by span: only the substrings that *are* a
    recognizable tag (`_HTML_FRAGMENT_RE`) go, and entities are unescaped
    only when at least one such tag was found. Everything else is left
    exactly as the source wrote it, so a title that mixes the two --
    `"<p>Children <18 years</p>"` -- keeps its comparison. Handing the
    whole string to a general tag-stripper instead would swallow
    `"<18 years</p>"` as if it were one tag and silently shorten the name.

    `None`, empty and whitespace-only input all return `""`, so a caller
    can keep its own fallback chain with a plain `or`.
    """
    text = str(s or "")
    text = _HTML_DROP_ELEMENT_RE.sub(" ", text)
    if _HTML_FRAGMENT_RE.search(text):
        text = html.unescape(_HTML_FRAGMENT_RE.sub(" ", text))
    text = _CONTROL_CHARS_RE.sub("", text)
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# DOIs
# ---------------------------------------------------------------------------

# Case-insensitive `doi:` prefix, with or without a following space.
_DOI_PREFIX_RE = re.compile(r"(?i)^doi:\s*")
# The two doi.org URL forms the brief calls out, checked case-insensitively.
_DOI_URL_PREFIXES = ("https://doi.org/", "http://dx.doi.org/")
# A DOI's shape: `10.` + a 4-9 digit registrant code + `/` + a non-empty,
# whitespace-free suffix.
_DOI_PATTERN_RE = re.compile(r"10\.\d{4,9}/\S+")


def clean_doi(s: str | None) -> str | None:
    """Normalize a free-text DOI: strip a `doi:` or doi.org URL prefix,
    lowercase, strip surrounding whitespace.

    Returns `None` when `s` is falsy or the remainder doesn't match the
    DOI shape `10.NNNN/suffix` -- i.e. this also doubles as a validator,
    so a normalizer can pass through whatever a source reports and treat
    `None` as "not a real DOI".
    """
    if not s:
        return None
    text = s.strip()

    prefix_match = _DOI_PREFIX_RE.match(text)
    if prefix_match:
        text = text[prefix_match.end() :]
    else:
        # Only a doi.org URL when it wasn't already a bare `doi:` form --
        # the two prefixes are mutually exclusive in practice.
        for prefix in _DOI_URL_PREFIXES:
            if text.lower().startswith(prefix):
                text = text[len(prefix) :]
                break

    text = text.strip().lower()
    if not _DOI_PATTERN_RE.fullmatch(text):
        return None
    return text


# An OpenNeuro-style trailing version suffix: `.v1`, `.v1.0`, `.v10.2.3`, ...
_VERSION_SUFFIX_RE = re.compile(r"\.v\d+(\.\d+)*$")


def collapse_version_doi(doi: str) -> str:
    """Strip a trailing OpenNeuro-style `.vX`/`.vX.Y`/`.vX.Y.Z` version
    suffix from `doi`. A DOI with no such suffix is returned unchanged."""
    return _VERSION_SUFFIX_RE.sub("", doi)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_LEADING_YEAR_RE = re.compile(r"^(\d{4})")


def year_of(date_str: str | None) -> int | None:
    """The leading 4-digit year of an ISO-ish date string (`"2020"`,
    `"2020-05-01"`, `"2020-05-01T00:00:00Z"`), or `None` if `date_str` is
    falsy or doesn't start with 4 digits."""
    if not date_str:
        return None
    match = _LEADING_YEAR_RE.match(date_str.strip())
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Sample size extraction from free text
# ---------------------------------------------------------------------------

# Unit words a source's prose might use, grouped into the `sample_unit`
# family each belongs to (`atlas.vocab.SAMPLE_UNITS`).
_UNIT_FAMILY: dict[str, str] = {
    "subjects": "participants",
    "participants": "participants",
    "patients": "participants",
    "volunteers": "participants",
    "individuals": "participants",
    "admissions": "admissions",
    "records": "records",
    "recordings": "records",
    "images": "images",
}

_SAMPLE_SIZE_RE = re.compile(
    r"(\d[\d,]*)\s+(" + "|".join(_UNIT_FAMILY) + r")\b", re.IGNORECASE
)


def regex_sample_size(text: str | None) -> tuple[int, str] | None:
    """Pull a `(count, sample_unit)` pair out of free text such as a
    dataset description, e.g. `"recordings from 18 subjects"` ->
    `(18, "participants")`.

    Every `<number> <unit word>` occurrence in `text` is matched and each
    unit word mapped to its family (see `_UNIT_FAMILY`); thousands
    separators in the number are removed before parsing. The result is
    returned only when the text is unambiguous: exactly one unit family
    is mentioned, and every mention of that family agrees on the number
    (a family mentioned twice with two different counts, or two
    different families both mentioned, each return `None` rather than
    guessing).
    """
    if not text:
        return None

    matches = [
        (int(number.replace(",", "")), _UNIT_FAMILY[unit.lower()])
        for number, unit in _SAMPLE_SIZE_RE.findall(text)
    ]
    if not matches:
        return None

    families = {family for _, family in matches}
    if len(families) > 1:
        return None  # e.g. "18 subjects ... 200 recordings" -- ambiguous

    (family,) = families
    numbers = {number for number, _ in matches}
    if len(numbers) > 1:
        return None  # same family, conflicting counts -- ambiguous

    (number,) = numbers
    return number, family


# ---------------------------------------------------------------------------
# Licenses
# ---------------------------------------------------------------------------


def _normalize_license_key(name: str) -> str:
    return " ".join(name.split()).lower()


# Built once from vocab.LICENSE_MAP for the case-insensitive/whitespace-
# normalized fallback lookup.
_LICENSE_MAP_NORMALIZED: dict[str, str] = {
    _normalize_license_key(name): spdx for name, spdx in vocab.LICENSE_MAP.items()
}


def license_to_spdx(name: str | None) -> str | None:
    """SPDX identifier for a source's free-text license `name`: an exact
    `vocab.LICENSE_MAP` lookup first, then a case-insensitive,
    whitespace-normalized one, else `None` (the caller keeps the
    source's verbatim string in that case -- see `Record.license`)."""
    if not name:
        return None
    if name in vocab.LICENSE_MAP:
        return vocab.LICENSE_MAP[name]
    return _LICENSE_MAP_NORMALIZED.get(_normalize_license_key(name))


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def make_provenance(
    *, via: str, harvested_at: str, first_seen: str, raw_hash: str | None
) -> Provenance:
    """Build the `Provenance` for a record produced by a normalizer.

    Mind the swap: `harvested_at` (the parameter -- this *run's* date)
    becomes the `last_verified` *field*, while `first_seen` becomes the
    `harvested_at` *field*. That's not a bug -- it's `Provenance.
    harvested_at`'s documented meaning, "date this record was first
    captured, a first-seen date that never changes", versus `
    last_verified`, "the date the source listing last confirmed this
    record" (i.e. today, for a record a run just re-saw).

    `enrichment` is a not-yet-enriched placeholder: normalizers only map
    explicit source fields (keyword-based domain/modality/condition
    inference happens in the enrich stage, Tasks 2.1-2.2), so no
    enrichment field has a value yet. `method="rules"` + empty `fields`
    here are wholesale-overwritten by `merge.apply_enrichment` (Task 2.4)
    once the enrich stage actually runs on this record.
    """
    return Provenance(
        harvested_via=via,
        harvested_at=first_seen,
        last_verified=harvested_at,
        raw_hash=raw_hash,
        enrichment=EnrichmentProv(method="rules", fields={}),
    )


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


@dataclass
class Excluded:
    """A raw record a normalizer deliberately left out of the catalog
    (e.g. wrong species, a broken or placeholder source record) --
    tracked so exclusions are visible and auditable rather than silently
    dropped. Written to `data/catalog/normalized/<source>.excluded.jsonl`
    by `atlas.cli`'s `normalize` subcommand."""

    native_id: str
    reason: str
