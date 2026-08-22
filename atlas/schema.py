"""Canonical pydantic schema for every catalog record (frozen v1).

`Record` is the single source of truth: every harvester's normalizer will
produce one, every validator checks against it, and `docs/schema.json` /
`docs/schema.md` are generated from it (`export_json_schema`,
`render_markdown`, wired up by ``atlas schema`` in `atlas/cli.py`). Never
hand-edit the generated docs — regenerate them from this module instead.
"""

from __future__ import annotations

import json
from datetime import date
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from atlas import config, vocab

# --------------------------------------------------------------------------
# Vocabulary-backed Literal types — built from atlas.vocab tuples so those
# tuples stay the single source of truth for every enumerated value.
# --------------------------------------------------------------------------
SourceLiteral = Literal[vocab.SOURCES]
DomainLiteral = Literal[vocab.DOMAINS]
ModalityLiteral = Literal[vocab.MODALITIES]
AccessLiteral = Literal[vocab.ACCESS_ORDER]
SampleUnitLiteral = Literal[vocab.SAMPLE_UNITS]
SpeciesLiteral = Literal[vocab.SPECIES]
RecordStatusLiteral = Literal[vocab.RECORD_STATUS]
PaperRelationLiteral = Literal[vocab.PAPER_RELATIONS]
RelatedRelationLiteral = Literal[vocab.RELATED_RELATIONS]
EnrichmentMethodLiteral = Literal[vocab.ENRICHMENT_METHODS]

CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]

_ID_PATTERN = r"^[a-z_]+:[A-Za-z0-9._-]+$"
_URL_PATTERN = r"^https?://"


def word_count(s: str) -> int:
    """Count words in `s` by splitting on whitespace."""
    return len(s.split())


class _StrictModel(BaseModel):
    """Shared base for every schema model: reject undeclared fields."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Nested sub-models
# --------------------------------------------------------------------------


class Years(_StrictModel):
    """Temporal coverage of the data collection, when known."""

    start: int | None = Field(
        default=None, description="First year of data collection."
    )
    end: int | None = Field(default=None, description="Last year of data collection.")


class Condition(_StrictModel):
    """A clinical condition studied by the dataset."""

    label: str = Field(description="Condition name, as reported or canonicalized.")
    mesh_id: str | None = Field(
        default=None, description="MeSH descriptor id for `label`, if resolved."
    )


class Institution(_StrictModel):
    """An institution associated with the dataset."""

    name: str = Field(description="Institution name.")
    ror_id: str | None = Field(
        default=None, description="Research Organization Registry id, if resolved."
    )
    country: CountryCode | None = Field(
        default=None, description="ISO-3166-1 alpha-2 country code, if known."
    )


class Author(_StrictModel):
    """An author or maintainer of the dataset."""

    name: str = Field(description="Author name.")
    orcid: str | None = Field(default=None, description="ORCID id, if known.")


class Paper(_StrictModel):
    """A paper linked to the dataset."""

    doi: str = Field(description="Paper DOI.")
    title: str | None = Field(default=None, description="Paper title, if known.")
    relation: PaperRelationLiteral = Field(
        description="How this paper relates to the dataset."
    )


class Related(_StrictModel):
    """A link to another atlas record."""

    id: str = Field(
        description="Id of the related atlas record (must exist among the records)."
    )
    relation: RelatedRelationLiteral = Field(description="How the two records relate.")


class EnrichmentProv(_StrictModel):
    """Provenance of the enrichment (classification/summarization) applied."""

    method: EnrichmentMethodLiteral = Field(
        description="How the enrichment fields were produced."
    )
    model: str | None = Field(
        default=None, description="LLM model id, if `method` used one."
    )
    prompt_version: str | None = Field(
        default=None, description="Prompt version, if `method` used an LLM."
    )
    at: date | None = Field(
        default=None, description="Date this enrichment was applied."
    )
    fields: dict[str, str] = Field(
        description='Per-field origin, e.g. `{"domains": "rules"}`.'
    )


class Provenance(_StrictModel):
    """Provenance of a record: how, when, and by what method it was produced."""

    harvested_via: str = Field(
        description="Harvest method (e.g. the API endpoint, or 'curated')."
    )
    harvested_at: date = Field(
        description="Date this record was first captured (first-seen date)."
    )
    last_verified: date = Field(
        description="Date the source listing last confirmed this record."
    )
    raw_hash: str | None = Field(
        default=None,
        description="Hash of the raw source payload this record was normalized from.",
    )
    enrichment: EnrichmentProv = Field(
        description="Provenance of enrichment applied to this record."
    )


# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


class Record(_StrictModel):
    """One catalog record: a single dataset or resource from one source."""

    id: str = Field(
        pattern=_ID_PATTERN,
        min_length=1,
        description="Stable identifier: `<source>:<source_native_id-derived slug>`.",
    )
    source: SourceLiteral = Field(description="Which harvester produced this record.")
    source_native_id: str = Field(
        min_length=1,
        description="This record's identifier in the source system, verbatim.",
    )
    name: str = Field(min_length=1, description="Dataset or resource title.")
    summary: str = Field(
        min_length=1,
        description="One- to two-sentence description, at most 40 words.",
    )
    url: str = Field(
        pattern=_URL_PATTERN,
        min_length=1,
        description="Canonical link to the source's own page for this record.",
    )

    dataset_doi: str | None = Field(
        default=None,
        description="DOI for the dataset, version-collapsed (no `.vX.Y.Z` suffix).",
    )
    version: str | None = Field(
        default=None,
        description="Source-reported version label, if the source versions its datasets.",
    )
    published: date | None = Field(
        default=None, description="Date the source first published this record."
    )

    domains: list[DomainLiteral] = Field(
        default_factory=list, description="Clinical domain(s) this dataset serves."
    )
    modalities: list[ModalityLiteral] = Field(
        default_factory=list, description="Data modality (or modalities) captured."
    )
    conditions: list[Condition] = Field(
        default_factory=list, description="Condition(s) studied."
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Free-text keywords from the source, unclassified.",
    )
    population: str | None = Field(
        default=None, description="Free-text description of the study population."
    )
    species: SpeciesLiteral = Field(description="Species studied.")

    sample_size: int | None = Field(
        description="Size of the dataset, in `sample_unit` units. Required but "
        "nullable: explicitly `null` when the source doesn't report a size."
    )
    sample_unit: SampleUnitLiteral | None = Field(
        description="Unit that `sample_size` counts. Required but nullable: "
        "explicitly `null` when `sample_size` is unknown."
    )
    size_bytes: int | None = Field(
        default=None, description="Total data size in bytes, if the source reports it."
    )

    countries: list[CountryCode] = Field(
        default_factory=list,
        description="ISO-3166-1 alpha-2 country codes for where the data was collected.",
    )
    years: Years = Field(
        description="Start/end year of data collection. Required but its own "
        "`start`/`end` are individually nullable — pass `{}` when both are unknown."
    )

    access: AccessLiteral = Field(
        description="Least-restrictive tier at which substantive data is usable."
    )
    access_tiers: list[AccessLiteral] = Field(
        default_factory=list,
        description="All access tiers present across the source's access paths.",
    )
    access_notes: str | None = Field(
        default=None,
        description=(
            "Prose nuance for `access`/`access_tiers` (e.g. mixed open + "
            "controlled access)."
        ),
    )
    access_howto: str | None = Field(
        default=None, description="Practical steps for obtaining access beyond `open`."
    )

    license: str | None = Field(
        default=None,
        description=(
            "License identifier: SPDX when mappable via `vocab.LICENSE_MAP`, "
            "else the source's verbatim string."
        ),
    )

    institutions: list[Institution] = Field(
        default_factory=list, description="Institution(s) associated with the dataset."
    )
    authors: list[Author] = Field(
        default_factory=list, description="Author(s) or maintainer(s) of the dataset."
    )
    papers: list[Paper] = Field(
        default_factory=list,
        description="Related paper(s) describing or citing the dataset.",
    )
    related: list[Related] = Field(
        default_factory=list, description="Other atlas records related to this one."
    )

    record_status: RecordStatusLiteral = Field(
        description="Lifecycle status of this record in the catalog."
    )
    provenance: Provenance = Field(
        description="How, when, and by what method this record was produced."
    )

    @field_validator("summary")
    @classmethod
    def _summary_word_limit(cls, v: str) -> str:
        n = word_count(v)
        if n > 40:
            raise ValueError(f"summary must be at most 40 words, got {n}")
        return v

    @model_validator(mode="after")
    def _id_matches_source(self) -> Record:
        prefix = f"{self.source}:"
        if not self.id.startswith(prefix):
            raise ValueError(
                f"id {self.id!r} must start with {prefix!r} (source={self.source!r})"
            )
        return self


# --------------------------------------------------------------------------
# Validation across a whole batch of records
# --------------------------------------------------------------------------


def validate_records(records: list[dict]) -> tuple[list[str], list[str]]:
    """Validate a batch of raw record dicts against `Record`.

    Returns `(errors, warnings)`.

    Errors: pydantic validation failures (one message per underlying issue,
    prefixed with the record's `id` or, when no usable id is available, its
    index in `records`), duplicate ids, and `related[].id` values that don't
    match the id of any *successfully validated* record — deliberately
    stricter than matching against all raw input ids, so that a record
    referencing another record which itself failed validation is still
    reported as dangling (its target doesn't exist as a valid record either).

    Warnings: empty `domains`, empty `modalities`, `access != "open"` with
    no `access_notes`, and `sample_size` set without `sample_unit`.
    """
    errors: list[str] = []
    warnings: list[str] = []

    seen_ids: set[str] = set()
    parsed: list[Record] = []

    for idx, raw in enumerate(records):
        has_id = isinstance(raw, dict) and isinstance(raw.get("id"), str)
        ref = raw["id"] if has_id else f"index {idx}"

        try:
            record = Record.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(part) for part in err["loc"])
                where = f"{ref} ({loc})" if loc else str(ref)
                errors.append(f"{where}: {err['msg']}")
            continue

        if record.id in seen_ids:
            errors.append(f"{record.id}: duplicate id")
        else:
            seen_ids.add(record.id)
        parsed.append(record)

    known_ids = {record.id for record in parsed}

    for record in parsed:
        for rel in record.related:
            if rel.id not in known_ids:
                errors.append(
                    f"{record.id}: related id {rel.id!r} does not match any record"
                )
        if not record.domains:
            warnings.append(f"{record.id}: no domains assigned")
        if not record.modalities:
            warnings.append(f"{record.id}: no modalities assigned")
        if record.access != "open" and not record.access_notes:
            warnings.append(
                f"{record.id}: access={record.access!r} has no access_notes"
            )
        if record.sample_size is not None and record.sample_unit is None:
            warnings.append(f"{record.id}: sample_size is set without sample_unit")

    return errors, warnings


# --------------------------------------------------------------------------
# Docs generation: docs/schema.json + docs/schema.md
# --------------------------------------------------------------------------

SCHEMA_JSON_PATH = config.ROOT / "docs" / "schema.json"
SCHEMA_MD_PATH = config.ROOT / "docs" / "schema.md"


def export_json_schema() -> dict:
    """Return `Record`'s JSON Schema as a plain dict (`title` == "Record")."""
    return Record.model_json_schema()


def export_json_schema_text() -> str:
    """Pretty-print `export_json_schema()`: sorted keys, trailing newline."""
    return json.dumps(export_json_schema(), indent=2, sort_keys=True) + "\n"


# (heading, values) for the "Vocabularies" section, in doc order.
_VOCAB_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Domains", vocab.DOMAINS),
    ("Modalities", vocab.MODALITIES),
    ("Access tiers", vocab.ACCESS_ORDER),
    ("Species", vocab.SPECIES),
    ("Sample units", vocab.SAMPLE_UNITS),
    ("Record status", vocab.RECORD_STATUS),
)

# Record field name -> vocab label, for fields whose values are enumerated
# in the "Vocabularies" section (so the field table can point there instead
# of spelling every value out inline).
_VOCAB_FIELD_LABELS: dict[str, str] = {
    "domains": "domain",
    "modalities": "modality",
    "species": "species",
    "sample_unit": "sample_unit",
    "access": "access",
    "access_tiers": "access",
    "record_status": "record_status",
}


def _unwrap_annotated(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


def _type_label(field_name: str, annotation: Any) -> str:
    """Human-readable type for the markdown field tables."""
    annotation = _unwrap_annotated(annotation)
    origin = get_origin(annotation)

    if origin in (UnionType, Union) and type(None) in get_args(annotation):
        (inner,) = [a for a in get_args(annotation) if a is not type(None)]
        return f"{_type_label(field_name, inner)} | null"

    if origin is list:
        (inner,) = get_args(annotation)
        return f"list[{_type_label(field_name, inner)}]"

    if origin is dict:
        key_t, val_t = get_args(annotation)
        return f"dict[{key_t.__name__}, {val_t.__name__}]"

    if origin is Literal:
        values = get_args(annotation)
        vocab_name = _VOCAB_FIELD_LABELS.get(field_name)
        if vocab_name:
            return f"enum: {vocab_name}"
        return "enum: " + ", ".join(values)

    if isinstance(annotation, type):
        return annotation.__name__

    return str(annotation)


def _field_rows(model: type[BaseModel]) -> list[tuple[str, str, str, str]]:
    rows = []
    for name, info in model.model_fields.items():
        type_label = _type_label(name, info.annotation)
        required = "Yes" if info.is_required() else "No"
        rows.append((name, type_label, required, info.description or ""))
    return rows


def _escape_cell(value: str) -> str:
    """Escape literal `|` so it can't be mistaken for a column separator."""
    return value.replace("|", "\\|")


def _render_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        cells = [_escape_cell(cell) for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# Nested models get their own field table, in this order.
_NESTED_MODELS: tuple[type[BaseModel], ...] = (
    Years,
    Condition,
    Institution,
    Author,
    Paper,
    Related,
    Provenance,
    EnrichmentProv,
)


def render_markdown() -> str:
    """Render `docs/schema.md`: field tables, vocab lists, semantic notes."""
    lines: list[str] = [
        "# Schema",
        "",
        (
            "Generated by `atlas schema --export` from the canonical pydantic "
            "model in `atlas/schema.py` — do not hand-edit; `atlas schema "
            "--check` (and CI) fails the build on drift. The machine-readable "
            "copy is `docs/schema.json` (`Record.model_json_schema()`)."
        ),
        "",
        "## Record fields",
        "",
        "One JSONL line per record (`data/catalog/catalog.jsonl`), keys sorted.",
        "",
        _render_table(
            ("Field", "Type", "Required", "Description"), _field_rows(Record)
        ),
        "",
        "## Nested objects",
        "",
    ]

    for model in _NESTED_MODELS:
        lines.append(f"### {model.__name__}")
        lines.append("")
        lines.append(
            _render_table(
                ("Field", "Type", "Required", "Description"), _field_rows(model)
            )
        )
        lines.append("")

    lines.append("## Vocabularies")
    lines.append("")
    for heading, values in _VOCAB_SECTIONS:
        lines.append(f"### {heading} ({len(values)})")
        lines.append("")
        if heading == "Access tiers":
            lines.append("Ordered least-restrictive to most-restrictive:")
            lines.append("")
        lines.append(", ".join(f"`{v}`" for v in values))
        lines.append("")

    lines.append("## Access & provenance semantics")
    lines.append("")
    lines.append(
        "**`access`.** `access` is the least-restrictive tier at which "
        "substantive data is usable. `access_tiers[]` lists every tier "
        "present across the source's own access paths, and `access_notes` "
        "carries the nuance in prose — for example, GDC clinical/biospecimen "
        "summaries are `open` but some genomic data is `application`-gated "
        'via dbGaP, so a GDC record can carry `access="open"`, '
        '`access_tiers=["open", "application"]`, and a note explaining the '
        "split. PhysioNet's access policies map directly: `Open` -> `open`, "
        "`Restricted` -> `registration`, `Credentialed` -> `credentialed`, "
        "`Contributor Review` -> `application`. When a source's own signals "
        "conflict, resolve to the more restrictive tier (the one that sorts "
        "later in `open < registration < credentialed < application < "
        "purchase`)."
    )
    lines.append("")
    lines.append(
        "**`provenance.last_verified` vs. `provenance.harvested_at`.** "
        "`provenance.harvested_at` is the date this record was first "
        "captured — a first-seen date that never changes. "
        "`provenance.last_verified` is the date the source listing was last "
        "confirmed to still describe this record, updated every time a "
        "refresh re-confirms it."
    )
    lines.append("")

    return "\n".join(lines)
