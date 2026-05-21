"""Tests for ``signalforge prune-existing`` (US-002 parser + US-003 orchestrator).

US-002 shipped the **contract surface**: :func:`add_parser` (the full flag
set) and a stub handler. US-003 replaces the stub with the real
ingest -> prune -> diff orchestrator (no draft, no grade, **no LLM call**).

Parser-surface coverage (US-002):

* the subcommand is registered in ``_build_parser`` and dispatches to the
  handler;
* ``--schema`` is required (argparse exits 2 if omitted);
* ``--scope`` / ``--sample-strategy`` / ``--format`` reject typos via
  ``choices`` (exit 2);
* the dropped flags (``--mode`` / ``--write`` / ``--min-score`` /
  ``--select`` / ``--estimate``) are NOT present;
* ``--help`` exits 0 and lists every flag.

Orchestrator coverage (US-003) — in-process ``main(argv)`` against an
Austin-aligned fixture project + a :class:`FakeBigQueryClient`-backed
adapter (patched onto ``signalforge.cli.prune_existing._make_warehouse_adapter``,
DEC-009):

* happy path -> exit 0, stdout carries the rendered diff;
* skipped (unsupported) tests land in the stderr summary; ``--verbose``
  adds per-item detail;
* each ingest error path -> the correct exit-code tier + a no-traceback
  floor on stderr (every path asserts ``"Traceback" not in stderr``);
* ``--dry-run`` writes no ``.signalforge/diff.json`` sidecar;
* ``--format json`` emits JSON to stdout;
* bare-name and unique_id ``<model>`` both resolve.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from signalforge.cli import main
from signalforge.cli import prune_existing as prune_existing_cmd
from signalforge.cli.prune_existing import cmd_prune_existing
from signalforge.warehouse.adapters.bigquery import BigQueryAdapter
from tests.warehouse._fake import FakeBigQueryClient

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_FIXTURE_PROJECT = _FIXTURES / "dbt_project_austin"
_FIXTURE_SCHEMA = _FIXTURES / "ingest" / "schema_austin_bikeshare.yml"
_MODEL_UNIQUE_ID = "model.signalforge_test_austin.stg_bikeshare_trips"
_MODEL_BARE_NAME = "stg_bikeshare_trips"

# The Austin schema.yml issues four warehouse COUNT(*) queries (not_null +
# unique on trip_id, accepted_values on subscriber_type, not_null on
# duration_minutes); the relationships test on start_station_id points at a
# model absent from the manifest, so prune routes it to
# ``requires-future-data`` with NO warehouse query. Returning 0 / 5 / 0 / 2
# failures yields a kept/dropped mix.
_FAILURE_COUNTS = (0, 5, 0, 2)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the Austin fixture project + the external schema.yml into
    ``tmp_path`` so audit JSONLs / sidecars land in temp (mirrors the e2e
    helper's tmp-isolation). Returns ``(project_dir, schema_path)``.
    """
    project_dir = tmp_path / "project"
    shutil.copytree(_FIXTURE_PROJECT, project_dir)
    # Plant the external schema.yml INSIDE the project tree so the
    # ``canonicalise_user_path`` containment gate accepts it.
    schema_path = project_dir / "models" / "staging" / "external_schema.yml"
    shutil.copy(_FIXTURE_SCHEMA, schema_path)
    return project_dir, schema_path


def _make_fake_adapter_factory(failure_counts: tuple[int, ...] = _FAILURE_COUNTS):
    """Return a ``_make_warehouse_adapter`` replacement returning a
    :class:`FakeBigQueryClient`-backed :class:`BigQueryAdapter` whose
    COUNT(*) queries yield the supplied failure counts in order.
    """

    def factory(profile: object) -> BigQueryAdapter:
        fake = FakeBigQueryClient(project="bigquery-public-data")
        for fails in failure_counts:
            fake.expect_query(
                matching=re.compile("COUNT", re.IGNORECASE),
                returns=[{"failures": fails}],
            )
        return BigQueryAdapter(
            project="bigquery-public-data",
            location="US",
            max_bytes_billed=100_000_000,
            client=fake,
        )

    return factory


def _run(
    argv: list[str],
    *,
    adapter_factory=None,
) -> int:
    """Invoke ``main(argv)`` with ``_make_warehouse_adapter`` patched.

    When ``adapter_factory`` is ``None`` a default failure-count factory is
    used. Error-path tests that fail BEFORE the prune stage (bad schema,
    unknown model, anchor-contract violation) can pass a factory that is
    never reached.
    """
    factory = adapter_factory if adapter_factory is not None else _make_fake_adapter_factory()
    with patch("signalforge.cli.prune_existing._make_warehouse_adapter", factory):
        return main(argv)


def _base_argv(project_dir: Path, schema_path: Path, model: str = _MODEL_UNIQUE_ID) -> list[str]:
    """Build the canonical argv for a successful run: full scope + oneshot
    so the prune path is plain ``SELECT COUNT(*)`` per test (no materialise).
    """
    return [
        "prune-existing",
        model,
        "--schema",
        str(schema_path),
        "--project-dir",
        str(project_dir),
        "--scope",
        "full",
        "--sample-strategy",
        "oneshot",
    ]


# ---------------------------------------------------------------------------
# Registration + dispatch
# ---------------------------------------------------------------------------


def test_subcommand_registered_in_build_parser() -> None:
    """``prune-existing`` is a registered subcommand of the top-level parser."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    choices: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 — argparse introspection in test
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            choices.update(action.choices)
    assert "prune-existing" in choices


def test_parser_sets_func_to_handler() -> None:
    """Parsing a ``prune-existing`` invocation wires ``args.func`` to the handler."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["prune-existing", "customers", "--schema", "schema.yml"])
    assert args.func is cmd_prune_existing


def test_module_all_exports() -> None:
    """``__all__`` exports exactly ``add_parser`` and ``cmd_prune_existing``."""
    assert set(prune_existing_cmd.__all__) == {"add_parser", "cmd_prune_existing"}


# ---------------------------------------------------------------------------
# Required --schema
# ---------------------------------------------------------------------------


def test_schema_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    """Omitting ``--schema`` is an argparse usage error -> exit 2."""
    exit_code = main(["prune-existing", "customers"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--schema" in err


def test_schema_value_accepted() -> None:
    """``--schema <path>`` parses cleanly when supplied."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["prune-existing", "customers", "--schema", "my_schema.yml"])
    assert args.schema == "my_schema.yml"


# ---------------------------------------------------------------------------
# choices rejection on the three choice flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "bad"),
    [
        ("--scope", "bogus"),
        ("--sample-strategy", "bogus"),
        ("--format", "bogus"),
    ],
)
def test_choice_flags_reject_invalid(
    flag: str,
    bad: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each choice flag rejects a typo via exit 2 (argparse usage error)."""
    base = ["prune-existing", "customers", "--schema", "schema.yml"]
    assert main([*base, flag, bad]) == 2


@pytest.mark.parametrize(
    ("dest", "default"),
    [
        ("scope", None),
        ("sample_strategy", None),
        ("format", "ansi"),
        ("dry_run", False),
        ("quiet", False),
        ("verbose", False),
        ("no_color", False),
    ],
)
def test_flag_defaults(dest: str, default: object) -> None:
    """Sentinel / boolean defaults match the documented contract."""
    from signalforge.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["prune-existing", "customers", "--schema", "schema.yml"])
    assert getattr(args, dest) == default


# ---------------------------------------------------------------------------
# Dropped flags must NOT be present (DEC-002 / DEC-003)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", ["--mode", "--write", "--min-score", "--select", "--estimate"])
def test_dropped_flags_absent(dropped: str) -> None:
    """The flags dropped vs. ``generate`` are rejected as unknown -> exit 2."""
    exit_code = main(["prune-existing", "customers", "--schema", "schema.yml", dropped])
    assert exit_code == 2


def test_quiet_verbose_mutually_exclusive() -> None:
    """Supplying both ``--quiet`` and ``--verbose`` is a usage error -> exit 2."""
    base = ["prune-existing", "customers", "--schema", "schema.yml"]
    assert main([*base, "--quiet", "--verbose"]) == 2


def test_help_lists_every_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``prune-existing --help`` exits 0 and names every flag in the set."""
    exit_code = main(["prune-existing", "--help"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Traceback" not in out
    for token in (
        "<model>",
        "--schema",
        "--project-dir",
        "--manifest",
        "--profiles-dir",
        "--scope",
        "--sample-strategy",
        "--format",
        "--dry-run",
        "--quiet",
        "--verbose",
        "--no-color",
    ):
        assert token in out, f"{token!r} missing from --help output"


# ---------------------------------------------------------------------------
# US-003 — orchestrator happy path
# ---------------------------------------------------------------------------


def test_happy_path_exit_0_and_diff_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean run exits 0 and writes the rendered diff to stdout."""
    project_dir, schema_path = _setup_project(tmp_path)
    code = _run(_base_argv(project_dir, schema_path))
    captured = capsys.readouterr()
    assert code == 0
    # The diff renders a unified diff against the operator's schema.yml; the
    # YAML diff fence carries the model's columns.
    assert "stg_bikeshare_trips" in captured.out
    assert "Traceback" not in captured.err


def test_bare_name_model_resolves(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A bare model name resolves the same as the unique_id form (DEC-008)."""
    project_dir, schema_path = _setup_project(tmp_path)
    code = _run(_base_argv(project_dir, schema_path, model=_MODEL_BARE_NAME))
    assert code == 0
    assert "Traceback" not in capsys.readouterr().err


def test_format_json_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--format json`` emits a parseable JSON document to stdout."""
    import json

    project_dir, schema_path = _setup_project(tmp_path)
    argv = [*_base_argv(project_dir, schema_path), "--format", "json"]
    code = _run(argv)
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["model_unique_id"] == _MODEL_UNIQUE_ID
    # grading_report=None -> never flagged (DEC-004 / #104 DEC-011).
    assert payload["flagged_count"] == 0


# ---------------------------------------------------------------------------
# US-003 — skipped-test report (DEC-007)
# ---------------------------------------------------------------------------


def test_skipped_summary_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Unsupported tests land in one stderr summary line grouped by reason."""
    project_dir, schema_path = _setup_project(tmp_path)
    code = _run(_base_argv(project_dir, schema_path))
    err = capsys.readouterr().err
    assert code == 0
    # The fixture carries two unsupported tests: one custom/generic
    # (dbt_utils.*) and one unsupported bare string (positive).
    assert "Skipped 2 unsupported tests:" in err
    assert "custom-or-generic-test×1" in err
    assert "unsupported-test-type×1" in err
    # Without --verbose, no per-item detail lines.
    assert "reason=" not in err


def test_skipped_verbose_adds_detail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--verbose`` follows the summary with one indented line per skip."""
    project_dir, schema_path = _setup_project(tmp_path)
    argv = [*_base_argv(project_dir, schema_path), "--verbose"]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 0
    assert "Skipped 2 unsupported tests:" in err
    assert "dbt_utils.unique_combination_of_columns" in err
    assert "positive" in err
    assert "reason=custom-or-generic-test" in err
    assert "reason=unsupported-test-type" in err


def test_skipped_suppressed_by_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--quiet`` suppresses the skipped-test report entirely."""
    project_dir, schema_path = _setup_project(tmp_path)
    argv = [*_base_argv(project_dir, schema_path), "--quiet"]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 0
    assert "Skipped" not in err


# ---------------------------------------------------------------------------
# US-003 — --dry-run writes no sidecar
# ---------------------------------------------------------------------------


def test_dry_run_writes_no_sidecar(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--dry-run`` runs the pipeline but leaves no ``.signalforge/diff.json``."""
    project_dir, schema_path = _setup_project(tmp_path)
    argv = [*_base_argv(project_dir, schema_path), "--dry-run"]
    code = _run(argv)
    assert code == 0
    assert not (project_dir / ".signalforge" / "diff.json").exists()
    assert "Traceback" not in capsys.readouterr().err


def test_default_writes_sidecar(tmp_path: Path) -> None:
    """Without ``--dry-run`` the sidecar lands at ``.signalforge/diff.json``."""
    project_dir, schema_path = _setup_project(tmp_path)
    code = _run(_base_argv(project_dir, schema_path))
    assert code == 0
    assert (project_dir / ".signalforge" / "diff.json").is_file()


# ---------------------------------------------------------------------------
# US-003 — error paths (exit-code tiers + no-traceback floor)
# ---------------------------------------------------------------------------


def test_schema_not_found_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing ``--schema`` file -> IngestSchemaNotFoundError -> exit 1."""
    project_dir, _ = _setup_project(tmp_path)
    missing = project_dir / "models" / "staging" / "does_not_exist.yml"
    argv = [
        "prune-existing",
        _MODEL_UNIQUE_ID,
        "--schema",
        str(missing),
        "--project-dir",
        str(project_dir),
    ]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 1
    assert "Traceback" not in err
    assert "ERROR:" in err


def test_schema_parse_error_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A malformed YAML schema -> IngestSchemaParseError -> exit 1."""
    project_dir, schema_path = _setup_project(tmp_path)
    schema_path.write_text("version: 2\nmodels: [unterminated\n", encoding="utf-8")
    argv = [
        "prune-existing",
        _MODEL_UNIQUE_ID,
        "--schema",
        str(schema_path),
        "--project-dir",
        str(project_dir),
    ]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 1
    assert "Traceback" not in err


def test_ingest_model_not_found_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A schema that doesn't declare the model -> IngestModelNotFoundError -> exit 2."""
    project_dir, schema_path = _setup_project(tmp_path)
    # The model exists in the manifest (resolution succeeds) but the schema
    # declares a DIFFERENT model name -> read_schema raises
    # IngestModelNotFoundError.
    schema_path.write_text(
        "version: 2\nmodels:\n  - name: some_other_model\n    columns:\n      - name: x\n",
        encoding="utf-8",
    )
    argv = [
        "prune-existing",
        _MODEL_UNIQUE_ID,
        "--schema",
        str(schema_path),
        "--project-dir",
        str(project_dir),
    ]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err


def test_ingest_anchor_contract_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A test referencing a column absent from the model -> IngestAnchorContractError -> exit 2."""
    project_dir, schema_path = _setup_project(tmp_path)
    schema_path.write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_bikeshare_trips\n"
        "    columns:\n"
        "      - name: phantom_column\n"
        "        tests:\n"
        "          - not_null\n",
        encoding="utf-8",
    )
    argv = [
        "prune-existing",
        _MODEL_UNIQUE_ID,
        "--schema",
        str(schema_path),
        "--project-dir",
        str(project_dir),
    ]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err


def test_unknown_model_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A model absent from the manifest -> ModelNotFoundError -> exit 2."""
    project_dir, schema_path = _setup_project(tmp_path)
    argv = [
        "prune-existing",
        "no_such_model",
        "--schema",
        str(schema_path),
        "--project-dir",
        str(project_dir),
    ]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err


def test_bad_project_dir_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A ``--project-dir`` without dbt_project.yml -> CliPathError -> exit 1."""
    not_a_project = tmp_path / "empty"
    not_a_project.mkdir()
    argv = [
        "prune-existing",
        _MODEL_UNIQUE_ID,
        "--schema",
        "schema.yml",
        "--project-dir",
        str(not_a_project),
    ]
    code = _run(argv)
    err = capsys.readouterr().err
    assert code == 1
    assert "Traceback" not in err
