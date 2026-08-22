"""PhysioNet normalizer: raw published-project entries -> canonical `Record`.

Reads the envelopes `atlas.harvest.physionet.PhysioNetHarvester` writes
(one per published project's *latest* version, every resource type kept)
and narrows to dataset-shaped resources: a `Database` or `Challenge`
becomes a `Record`; `Software`/`Model` -- present in raw for provenance,
never meant to be catalog entries -- come back as `Excluded`.

Only fields this source states explicitly are mapped here. `domains`,
`modalities`, and `conditions` are left empty: the enrich stage (Tasks
2.1-2.2) fills them in later from `enrichment_text`, a short pile of free
text built here (title + short_description + stripped abstract + topics).
"""

from __future__ import annotations

from atlas import io
from atlas.normalize import common
from atlas.normalize.common import Excluded
from atlas.schema import Record, Years, word_count

SOURCE = "physionet"

# resource_type values that are catalog-worthy datasets; the other two
# resource types the API reports (Software, Model) are tooling/code, not
# data, and are excluded rather than normalized.
_DATASET_RESOURCE_TYPES = {"Database", "Challenge"}

# PhysioNet's four access_policy values -> the canonical access vocabulary
# (`atlas.vocab.ACCESS_ORDER`), per docs/schema.md's "Access & provenance
# semantics" section.
_ACCESS_MAP: dict[str, str] = {
    "Open": "open",
    "Restricted": "registration",
    "Credentialed": "credentialed",
    "Contributor Review": "application",
}

_ENRICHMENT_TEXT_MAX_CHARS = 1500
_SUMMARY_MAX_WORDS = 40


def _access_notes(payload: dict) -> str | None:
    dua = payload.get("dua")
    if isinstance(dua, dict):
        return dua.get("name")
    return None


def _license(payload: dict) -> str | None:
    license_obj = payload.get("license")
    name = license_obj.get("name") if isinstance(license_obj, dict) else None
    if not name:
        return None
    return common.license_to_spdx(name) or name


def _summary(payload: dict, abstract_text: str) -> str:
    """`short_description` verbatim when it's at most 40 words; otherwise
    the first 40 words of the (already HTML-stripped) abstract.

    A short_description that's present but happens to be empty is
    treated the same as "too long to use as-is": both fall through to
    the abstract. If even the abstract is empty, the record's own title
    is the last resort -- never an empty string, which `Record.summary`
    (min_length=1) would reject outright.
    """
    short = (payload.get("short_description") or "").strip()
    if short and word_count(short) <= _SUMMARY_MAX_WORDS:
        return short
    from_abstract = io.first_words(abstract_text, _SUMMARY_MAX_WORDS)
    return from_abstract or (payload.get("title") or "").strip()


def normalize(
    envelope: dict, *, harvested_at: str, first_seen: str
) -> Record | Excluded:
    payload = envelope["payload"]
    native_id = envelope["native_id"]

    resource_type = payload.get("resource_type")
    if resource_type not in _DATASET_RESOURCE_TYPES:
        return Excluded(native_id=native_id, reason="not_a_dataset")

    slug = payload["slug"]
    abstract_text = io.strip_html(payload.get("abstract") or "")
    short_description = payload.get("short_description") or ""

    access = _ACCESS_MAP[payload["access_policy"]]

    keywords = list(payload.get("topics") or [])
    if resource_type == "Challenge" and "challenge" not in keywords:
        keywords.append("challenge")

    sample = common.regex_sample_size(f"{short_description} {abstract_text}")
    sample_size, sample_unit = sample if sample else (None, None)

    return Record(
        id=f"{SOURCE}:{slug}",
        source=SOURCE,
        source_native_id=native_id,
        name=payload["title"],
        summary=_summary(payload, abstract_text),
        url=payload["source_url"],
        dataset_doi=common.clean_doi(
            payload.get("core_doi") or payload.get("version_doi")
        ),
        version=payload.get("version"),
        published=payload.get("publish_date"),
        domains=[],
        modalities=[],
        conditions=[],
        keywords=keywords,
        population=None,
        species="human",
        sample_size=sample_size,
        sample_unit=sample_unit,
        size_bytes=payload.get("main_storage_size"),
        countries=[],
        years=Years(),
        access=access,
        access_tiers=[access],
        access_notes=_access_notes(payload),
        access_howto=None,
        license=_license(payload),
        institutions=[],
        authors=[],
        papers=[],
        related=[],
        record_status="active",
        provenance=common.make_provenance(
            via="api:physionet-published",
            harvested_at=harvested_at,
            first_seen=first_seen,
            raw_hash=io.content_hash(payload),
        ),
    )


def enrichment_text(envelope: dict) -> str:
    """Free text fed to the enrich stage's rules/LLM classification:
    title + short_description + stripped abstract + topics, capped at
    1,500 characters (a plain truncation, not a "preview")."""
    payload = envelope["payload"]
    parts = [
        payload.get("title") or "",
        payload.get("short_description") or "",
        io.strip_html(payload.get("abstract") or ""),
        " ".join(payload.get("topics") or []),
    ]
    text = " ".join(part for part in parts if part)
    return text[:_ENRICHMENT_TEXT_MAX_CHARS]
