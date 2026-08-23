"""Tests for Task 4.3a: GitHub Actions workflows, issue forms, and
CITATION.cff.

These are structural checks only -- they don't execute the workflows (that
needs a real GitHub Actions runner), they verify: the YAML is well-formed;
every action is pinned defensibly (a bare major-version tag, or a full SHA
with a trailing ``# vX.Y.Z`` comment -- never a floating ``@main``/
``@master``); ``setup-uv`` specifically is always SHA-pinned; the issue
forms parse and their domains/modalities checkbox lists mirror
``atlas/vocab.py`` exactly, in order; and ``CITATION.cff`` parses and stays
in sync with ``atlas.__version__``.

Requires ``pyyaml`` (dev dependency group only -- see pyproject.toml; the
stdlib has no YAML parser and these are the only files in the repo that
need one).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import atlas
from atlas import vocab

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
ISSUE_TEMPLATE_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"

WORKFLOW_PATHS = sorted(WORKFLOWS_DIR.glob("*.yml"))

# Matches a `uses:` step line (with or without a leading `- `), capturing
# the `owner/repo[/path]@ref` value and an optional trailing `# comment`.
_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*(?:#\s*(.*))?\s*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAJOR_VERSION_RE = re.compile(r"^v[0-9]+$")
_VERSION_COMMENT_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _load_yaml(path: Path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _iter_uses(path: Path):
    """Yield ``(ref, comment)`` for every ``uses:`` line in `path`.

    Parsed from the raw text, not the loaded YAML tree: a YAML parser
    discards comments, and the ``# vX.Y.Z`` pin comment on a SHA-pinned
    action is exactly what needs checking here.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _USES_RE.match(line)
        if match:
            yield match.group(1), (match.group(2) or "").strip()


def _field_by_id(form: dict, field_id: str) -> dict:
    for field in form["body"]:
        if field.get("id") == field_id:
            return field
    raise AssertionError(f"no field with id={field_id!r} in this form")


# ---------------------------------------------------------------------------
# Workflow files: present, parse, `on`/`jobs`, every `uses:` pinned
# ---------------------------------------------------------------------------


def test_exactly_the_three_expected_workflows_exist():
    assert {p.name for p in WORKFLOW_PATHS} == {
        "ci.yml",
        "deploy-pages.yml",
        "monthly-refresh.yml",
    }


@pytest.mark.parametrize("path", WORKFLOW_PATHS, ids=lambda p: p.name)
def test_workflow_parses_and_has_on_and_jobs(path: Path):
    data = _load_yaml(path)
    assert isinstance(data, dict)
    # PyYAML resolves the bare key `on:` to the boolean `True` (YAML 1.1
    # implicit typing of the words on/off/yes/no/true/false) -- both
    # spellings below mean "the trigger key is present".
    assert True in data or "on" in data, f"{path.name} is missing `on:`"
    assert data.get("jobs"), f"{path.name} is missing `jobs:`"


@pytest.mark.parametrize("path", WORKFLOW_PATHS, ids=lambda p: p.name)
def test_every_uses_pins_a_major_version_or_a_commented_sha(path: Path):
    uses_lines = list(_iter_uses(path))
    assert uses_lines, f"{path.name} has no `uses:` steps to check"
    for ref, comment in uses_lines:
        assert "@" in ref, f"{path.name}: {ref!r} has no @ref"
        action, _, version_ref = ref.rpartition("@")
        assert version_ref not in ("main", "master"), (
            f"{path.name}: {action} is pinned to a floating branch {version_ref!r}"
        )
        if _SHA_RE.match(version_ref):
            assert _VERSION_COMMENT_RE.match(comment), (
                f"{path.name}: {action}@{version_ref} is SHA-pinned but its "
                f"trailing comment {comment!r} isn't `# vX.Y.Z`"
            )
        else:
            assert _MAJOR_VERSION_RE.match(version_ref), (
                f"{path.name}: {action}@{version_ref} is neither a full "
                "40-char SHA nor a bare major-version tag (e.g. v7)"
            )


@pytest.mark.parametrize("path", WORKFLOW_PATHS, ids=lambda p: p.name)
def test_setup_uv_is_pinned_by_sha_with_version_comment(path: Path):
    setup_uv_uses = [
        (ref, comment)
        for ref, comment in _iter_uses(path)
        if ref.startswith("astral-sh/setup-uv@")
    ]
    assert setup_uv_uses, f"{path.name} does not use astral-sh/setup-uv"
    for ref, comment in setup_uv_uses:
        _, _, version_ref = ref.rpartition("@")
        assert _SHA_RE.match(version_ref), (
            f"{path.name}: setup-uv must be pinned by full commit SHA, "
            f"got {version_ref!r}"
        )
        assert _VERSION_COMMENT_RE.match(comment), (
            f"{path.name}: setup-uv's SHA pin needs a `# vX.Y.Z` trailing "
            f"comment, got {comment!r}"
        )


def test_no_workflow_floats_on_main_or_master():
    offenders = [
        f"{path.name}: {ref}"
        for path in WORKFLOW_PATHS
        for ref, _comment in _iter_uses(path)
        if ref.rpartition("@")[2] in ("main", "master")
    ]
    assert not offenders, f"floating action pins found: {offenders}"


def test_ci_e2e_job_is_gated_until_task_3_4_adds_tests_e2e():
    text = (WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8")
    assert "if: hashFiles('tests/e2e/**') != ''" in text


def test_monthly_refresh_cron_is_first_of_month():
    data = _load_yaml(WORKFLOWS_DIR / "monthly-refresh.yml")
    triggers = data.get(True) or data.get("on")
    assert triggers["schedule"] == [{"cron": "17 5 1 * *"}]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ci.yml", {"contents": "read"}),
        (
            "deploy-pages.yml",
            {"contents": "read", "pages": "write", "id-token": "write"},
        ),
        (
            "monthly-refresh.yml",
            {"contents": "write", "pull-requests": "write"},
        ),
    ],
)
def test_workflow_permissions_match_spec(filename, expected):
    data = _load_yaml(WORKFLOWS_DIR / filename)
    assert data["permissions"] == expected


# ---------------------------------------------------------------------------
# Issue forms
# ---------------------------------------------------------------------------


def test_issue_template_config_disables_blank_issues_and_links_about_page():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "config.yml")
    assert data["blank_issues_enabled"] is False
    urls = [link["url"] for link in data["contact_links"]]
    assert "https://nielspac177.github.io/clinical-data-atlas/about.html" in urls


def test_propose_dataset_form_parses_with_expected_field_order():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "propose-dataset.yml")
    assert data["title"] == "[Dataset] "
    assert set(data["labels"]) == {"dataset-proposal", "triage"}
    ids = [field["id"] for field in data["body"] if "id" in field]
    assert ids == [
        "name",
        "url",
        "host",
        "access_tier",
        "license",
        "domains",
        "modalities",
        "conditions",
        "population",
        "sample_size",
        "sample_unit",
        "countries",
        "years",
        "institutions",
        "papers",
        "notes",
        "attestation",
    ]


def test_propose_dataset_access_tier_matches_vocab_access_order():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "propose-dataset.yml")
    field = _field_by_id(data, "access_tier")
    assert field["type"] == "dropdown"
    assert field["attributes"]["options"] == list(vocab.ACCESS_ORDER)
    assert field["validations"]["required"] is True


def test_propose_dataset_domains_checkboxes_equal_vocab_domains_in_order():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "propose-dataset.yml")
    field = _field_by_id(data, "domains")
    assert field["type"] == "checkboxes"
    labels = [opt["label"] for opt in field["attributes"]["options"]]
    assert labels == list(vocab.DOMAINS)
    assert len(labels) == 17


def test_propose_dataset_modalities_checkboxes_equal_vocab_modalities_in_order():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "propose-dataset.yml")
    field = _field_by_id(data, "modalities")
    assert field["type"] == "checkboxes"
    labels = [opt["label"] for opt in field["attributes"]["options"]]
    assert labels == list(vocab.MODALITIES)
    assert len(labels) == 33


def test_propose_dataset_sample_unit_matches_vocab_sample_units():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "propose-dataset.yml")
    field = _field_by_id(data, "sample_unit")
    assert field["type"] == "dropdown"
    assert field["attributes"]["options"] == list(vocab.SAMPLE_UNITS)


def test_propose_dataset_attestation_checkbox_is_required():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "propose-dataset.yml")
    field = _field_by_id(data, "attestation")
    assert field["type"] == "checkboxes"
    options = field["attributes"]["options"]
    assert len(options) == 1
    assert options[0]["required"] is True


def test_dataset_edit_form_parses_with_expected_fields():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "dataset-edit.yml")
    assert data["title"] == "[edit] "
    assert set(data["labels"]) == {"dataset-edit", "triage"}
    ids = [field["id"] for field in data["body"] if "id" in field]
    assert ids == ["id", "fields", "problem", "evidence_url"]


def test_dataset_edit_id_field_is_required_with_tcia_placeholder():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "dataset-edit.yml")
    field = _field_by_id(data, "id")
    assert field["type"] == "input"
    assert field["validations"]["required"] is True
    assert "tcia:" in field["attributes"]["placeholder"]


def test_dataset_edit_fields_dropdown_is_a_multiselect_of_schema_field_names():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "dataset-edit.yml")
    field = _field_by_id(data, "fields")
    assert field["type"] == "dropdown"
    assert field["attributes"].get("multiple") is True
    options = field["attributes"]["options"]
    # Spot-check against docs/schema.md's Record field table rather than
    # importing atlas.schema (pydantic may not be installed for a bare
    # `uv sync --group dev`-only environment running just this file).
    for name in ("id", "source", "access", "provenance", "record_status"):
        assert name in options


def test_dataset_edit_problem_required_evidence_url_optional():
    data = _load_yaml(ISSUE_TEMPLATE_DIR / "dataset-edit.yml")
    problem = _field_by_id(data, "problem")
    assert problem["type"] == "textarea"
    assert problem["validations"]["required"] is True
    evidence = _field_by_id(data, "evidence_url")
    assert evidence["validations"]["required"] is False


# ---------------------------------------------------------------------------
# CITATION.cff
# ---------------------------------------------------------------------------


def test_citation_cff_parses_and_matches_atlas_version():
    data = _load_yaml(ROOT / "CITATION.cff")
    assert data["cff-version"] == "1.2.0"
    assert str(data["version"]) == atlas.__version__
    assert data["license"] == "MIT"
    assert (
        data["repository-code"] == "https://github.com/nielspac177/clinical-data-atlas"
    )
    assert data["url"] == "https://nielspac177.github.io/clinical-data-atlas/"
    authors = data["authors"]
    assert len(authors) == 1
    assert authors[0]["family-names"] == "Pacheco-Barrios"
    assert authors[0]["given-names"] == "Niels"


def test_citation_cff_abstract_mentions_cc_by_licensed_metadata():
    data = _load_yaml(ROOT / "CITATION.cff")
    assert "CC BY 4.0" in data["abstract"]
