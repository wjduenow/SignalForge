"""US-001 + US-002 (#232) — airflow-free pure operator helpers.

Covers the airflow-free operator helpers in
``signalforge.airflow.operators``:

* :func:`_build_generate_argv` (params → ``signalforge generate`` argv) and
  :func:`_validate_operator_config` (DEC-009 param guards) — US-001;
* :func:`_resolve_select_models` (``--select`` → sorted unique_ids, mapping
  ``ManifestError`` / ``SelectorParseError`` / zero-match → ``AirflowConfigError``,
  DEC-001/006) and :func:`_aggregate_batch_result` (per-model rollup, DEC-008) —
  US-002;
* :func:`_build_prune_existing_argv` (params → ``signalforge prune-existing`` argv,
  always ``--dry-run`` + ``--format json``; #233 DEC-005) and
  :func:`_validate_prune_existing_config` (required ``schema`` / ``model``,
  leading-dash + ``on_flagged`` guards; #233 DEC-001/002/004) — #233 US-001.

None of these import ``airflow`` (``_resolve_select_models`` does manifest I/O
but is still airflow-free), so this file is **UNGATED**: it carries NO
``@pytest.mark.airflow`` marker and never imports ``apache-airflow``. It runs in
the default ``uv run pytest`` suite and exercises every branch (the airflow-free
core's 100%-ungated codecov patch gate).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from signalforge.airflow.drift import DriftArtifact, DriftReport
from signalforge.airflow.errors import AirflowConfigError
from signalforge.airflow.operators import (
    _aggregate_batch_result,
    _build_drift_inputs,
    _build_generate_argv,
    _build_prune_existing_argv,
    _drift_task_outcome,
    _resolve_select_models,
    _validate_drift_config,
    _validate_operator_config,
    _validate_prune_existing_config,
    _without_sidecar_paths,
    build_drift_report,
)
from signalforge.airflow.result import SignalForgeRunResult, TaskOutcome
from signalforge.diff.models import DiffReport as SfDiffReport
from signalforge.grade.models import GradingReport

# Engineered drift sidecar PAIR committed by #235 US-001: the
# ``test.column.amount.not_null`` artifact goes kept → dropped/always-passes
# between the prev and curr diff (signal rot), and the grade mean falls
# 0.9 → 0.8 (a regression beyond the 0.05 default threshold).
_DRIFT_PAIRS = Path(__file__).resolve().parents[1] / "fixtures" / "airflow" / "drift_pairs"


def _load_diff(name: str) -> SfDiffReport:
    return SfDiffReport.model_validate_json((_DRIFT_PAIRS / f"{name}.json").read_text())


def _load_grade(name: str) -> GradingReport:
    return GradingReport.model_validate_json((_DRIFT_PAIRS / f"{name}.json").read_text())


# A committed multi-model dbt fixture: tags `staging` (stg_a, stg_b) + `marts`
# (fct_x). Resolves through the real `signalforge.manifest.load` + selector.
_MULTI_PROJECT = Path(__file__).resolve().parents[1] / "fixtures" / "dbt_project_multi"
_STG_A = "model.dbt_project_multi.stg_a"
_STG_B = "model.dbt_project_multi.stg_b"
_FCT_X = "model.dbt_project_multi.fct_x"


def _argv(**overrides: object) -> list[str]:
    """Build argv with sensible defaults; ``overrides`` tune individual params."""
    params: dict[str, object] = {
        "model": "models/staging/stg_x.sql",
        "select": None,
        "project_dir": "/proj",
        "profiles_dir": None,
        "write": False,
        "no_grade": False,
        "cache_scope": None,
        "as_of": None,
    }
    params.update(overrides)
    return _build_generate_argv(**params)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _build_generate_argv                                                        #
# --------------------------------------------------------------------------- #


def test_build_argv_single_model_dry_run() -> None:
    """Single positional model + ``write=False`` → ``--dry-run`` (DEC-003)."""
    argv = _argv(model="models/staging/stg_x.sql", write=False)
    assert argv[0] == "generate"
    assert argv[1] == "models/staging/stg_x.sql"
    assert "--dry-run" in argv
    assert "--write" not in argv
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    assert "--project-dir" in argv and argv[argv.index("--project-dir") + 1] == "/proj"
    assert "--select" not in argv


def test_build_argv_select_and_write() -> None:
    """``--select <expr>`` form + ``write=True`` → ``--write``."""
    argv = _argv(model=None, select="tag:staging", write=True)
    assert argv[0] == "generate"
    assert "--select" in argv and argv[argv.index("--select") + 1] == "tag:staging"
    assert "--write" in argv
    assert "--dry-run" not in argv
    # No positional model token when --select is used.
    assert "models/staging/stg_x.sql" not in argv


def test_build_argv_format_json_always_present() -> None:
    """``--format json`` is present on both the model and select shapes."""
    model_argv = _argv(model="m.sql", select=None)
    select_argv = _argv(model=None, select="tag:x")
    for argv in (model_argv, select_argv):
        assert argv[argv.index("--format") + 1] == "json"


def test_build_argv_no_grade_injected_when_set() -> None:
    assert "--no-grade" in _argv(no_grade=True)
    assert "--no-grade" not in _argv(no_grade=False)


def test_build_argv_as_of_injected_when_set() -> None:
    argv = _argv(as_of="2026-06-15")
    assert "--as-of" in argv and argv[argv.index("--as-of") + 1] == "2026-06-15"
    assert "--as-of" not in _argv(as_of=None)
    # An empty string is treated as absent.
    assert "--as-of" not in _argv(as_of="")


def test_build_argv_cache_scope_injected_when_set() -> None:
    argv = _argv(cache_scope="project")
    assert "--cache-scope" in argv and argv[argv.index("--cache-scope") + 1] == "project"
    assert "--cache-scope" not in _argv(cache_scope=None)
    # An empty string is treated as absent — never emit `--cache-scope ""`
    # (argparse `choices` would reject it).
    assert "--cache-scope" not in _argv(cache_scope="")


def test_without_sidecar_paths_nulls_only_path_fields() -> None:
    """Per-model batch XCom nulls the unstable sidecar paths, keeping counts."""
    xcom = {
        "kept": 3,
        "flagged": 1,
        "diff_sidecar_path": ".signalforge/diff.json",
        "grade_sidecar_path": ".signalforge/grade.json",
    }
    stripped = _without_sidecar_paths(xcom)
    assert stripped["diff_sidecar_path"] is None
    assert stripped["grade_sidecar_path"] is None
    assert stripped["kept"] == 3 and stripped["flagged"] == 1
    # Original is not mutated (copy semantics).
    assert xcom["diff_sidecar_path"] == ".signalforge/diff.json"


def test_build_argv_profiles_dir_injected_when_set() -> None:
    argv = _argv(profiles_dir="/home/u/.dbt")
    assert "--profiles-dir" in argv
    assert argv[argv.index("--profiles-dir") + 1] == "/home/u/.dbt"
    assert "--profiles-dir" not in _argv(profiles_dir=None)
    # An empty string is treated as absent.
    assert "--profiles-dir" not in _argv(profiles_dir="")


def test_build_argv_select_none_emits_empty_token() -> None:
    """Defensive: model and select both ``None`` still emits a (blank) --select.

    The caller guarantees the mutex via ``_validate_operator_config``; this pins
    the builder's else-branch fallback so the token shape is deterministic.
    """
    argv = _build_generate_argv(
        model=None,
        select=None,
        project_dir="/proj",
        profiles_dir=None,
        write=False,
        no_grade=False,
        cache_scope=None,
        as_of=None,
    )
    assert argv[1] == "--select"
    assert argv[2] == ""


# --------------------------------------------------------------------------- #
# _validate_operator_config                                                   #
# --------------------------------------------------------------------------- #


def test_validate_accepts_valid_model_config() -> None:
    """A valid single-model config does not raise."""
    _validate_operator_config(project_dir="/proj", model="m.sql", select=None, on_flagged="fail")


def test_validate_accepts_valid_select_config() -> None:
    """A valid --select config does not raise."""
    _validate_operator_config(project_dir="/proj", model=None, select="tag:x", on_flagged="skip")


def test_validate_rejects_empty_project_dir() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_operator_config(project_dir="", model="m.sql", select=None, on_flagged="fail")


def test_validate_rejects_none_project_dir() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_operator_config(project_dir=None, model="m.sql", select=None, on_flagged="fail")


def test_validate_rejects_model_and_select_both_set() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_operator_config(
            project_dir="/proj", model="m.sql", select="tag:x", on_flagged="fail"
        )


def test_validate_rejects_model_and_select_both_unset() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_operator_config(project_dir="/proj", model=None, select=None, on_flagged="fail")


def test_validate_rejects_leading_dash_model() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_operator_config(
            project_dir="/proj", model="--evil", select=None, on_flagged="fail"
        )


def test_validate_rejects_leading_dash_select() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_operator_config(project_dir="/proj", model=None, select="-x", on_flagged="fail")


def test_validate_rejects_bogus_on_flagged() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_operator_config(
            project_dir="/proj", model="m.sql", select=None, on_flagged="bogus"
        )


@pytest.mark.parametrize("on_flagged", ["fail", "skip", "succeed"])
def test_validate_accepts_each_valid_on_flagged(on_flagged: str) -> None:
    _validate_operator_config(
        project_dir="/proj", model="m.sql", select=None, on_flagged=on_flagged
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_validate_rejects_blank_model(blank: str) -> None:
    """A blank ``model`` must NOT satisfy the mutex (regression: ``model=""``
    previously passed `is not None` and emitted an empty positional)."""
    with pytest.raises(AirflowConfigError, match="non-empty"):
        _validate_operator_config(project_dir="/proj", model=blank, select=None, on_flagged="fail")


@pytest.mark.parametrize("blank", ["", "   "])
def test_validate_rejects_blank_select(blank: str) -> None:
    with pytest.raises(AirflowConfigError, match="non-empty"):
        _validate_operator_config(project_dir="/proj", model=None, select=blank, on_flagged="fail")


def test_validate_rejects_non_str_model() -> None:
    """A non-str (e.g. a templated value that rendered to a list/int) fails loud."""
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_operator_config(
            project_dir="/proj",
            model=123,  # type: ignore[arg-type]
            select=None,
            on_flagged="fail",
        )


# --------------------------------------------------------------------------- #
# _resolve_select_models                                                      #
# --------------------------------------------------------------------------- #


def test_resolve_select_returns_sorted_unique_ids() -> None:
    """A valid `tag:` selector resolves to the matched unique_ids, sorted."""
    matched = _resolve_select_models(str(_MULTI_PROJECT), "tag:staging")
    assert matched == (_STG_A, _STG_B)
    # Explicitly sorted (the helper sorts; pin it independently of selector order).
    assert list(matched) == sorted(matched)


def test_resolve_select_unions_multiple_atoms() -> None:
    """A multi-atom selector unions matches across tags, sorted by unique_id."""
    matched = _resolve_select_models(str(_MULTI_PROJECT), "tag:staging,tag:marts")
    assert matched == (_FCT_X, _STG_A, _STG_B)


def test_resolve_select_parse_error_maps_to_config_error() -> None:
    """A malformed selector (`SelectorParseError`) → `AirflowConfigError`."""
    with pytest.raises(AirflowConfigError) as excinfo:
        # An empty atom is a parse error in the selector grammar.
        _resolve_select_models(str(_MULTI_PROJECT), "tag:staging,,tag:marts")
    # The selector that failed is named, and a remediation line is rendered.
    assert "tag:staging,,tag:marts" in excinfo.value.message
    assert "↳ Remediation:" in str(excinfo.value)


def test_resolve_select_zero_match_maps_to_config_error() -> None:
    """A selector that matches nothing → `AirflowConfigError` naming the selector."""
    with pytest.raises(AirflowConfigError) as excinfo:
        _resolve_select_models(str(_MULTI_PROJECT), "tag:does_not_exist")
    assert "tag:does_not_exist" in excinfo.value.message
    assert "matched no models" in excinfo.value.message


def test_resolve_select_missing_project_dir_maps_to_config_error(tmp_path: Path) -> None:
    """A non-existent project_dir (`ManifestNotFoundError`) → `AirflowConfigError`."""
    missing = tmp_path / "no_such_project"
    with pytest.raises(AirflowConfigError) as excinfo:
        _resolve_select_models(str(missing), "tag:staging")
    # The source ManifestError's remediation is carried onto the AirflowConfigError.
    assert "↳ Remediation:" in str(excinfo.value)


def test_resolve_select_invalid_manifest_maps_to_config_error(tmp_path: Path) -> None:
    """A project dir lacking a manifest → `ManifestError` → `AirflowConfigError`."""
    # An existing directory but with no target/manifest.json under it.
    project = tmp_path / "empty_project"
    project.mkdir()
    with pytest.raises(AirflowConfigError):
        _resolve_select_models(str(project), "tag:staging")


# --------------------------------------------------------------------------- #
# _aggregate_batch_result                                                     #
# --------------------------------------------------------------------------- #


def _result(
    *,
    exit_code: int = 0,
    model_unique_ids: tuple[str, ...] = ("m",),
    kept: int = 0,
    kept_uncertain: int = 0,
    dropped: int = 0,
    flagged: int = 0,
    mean_grade: float | None = None,
    duration_seconds: float | None = None,
) -> SignalForgeRunResult:
    """Build a SignalForgeRunResult with batch-relevant fields tunable."""
    return SignalForgeRunResult(
        exit_code=exit_code,
        model_unique_ids=model_unique_ids,
        kept=kept,
        kept_uncertain=kept_uncertain,
        dropped=dropped,
        flagged=flagged,
        mean_grade=mean_grade,
        diff_sidecar_path="/some/diff.json",
        grade_sidecar_path="/some/grade.json",
        duration_seconds=duration_seconds,
        stdout="bulk stdout",
        stderr="bulk stderr",
    )


def test_aggregate_max_exit_code() -> None:
    """`exit_code` is the max over per-model codes (4-tier severity = int max)."""
    agg = _aggregate_batch_result(
        [_result(exit_code=0), _result(exit_code=3), _result(exit_code=2)]
    )
    assert agg.exit_code == 3


def test_aggregate_sums_counts_and_unions_ids() -> None:
    """Counts sum element-wise; model_unique_ids concatenate in input order."""
    agg = _aggregate_batch_result(
        [
            _result(model_unique_ids=("m.a",), kept=2, kept_uncertain=1, dropped=3, flagged=1),
            _result(
                model_unique_ids=("m.b", "m.c"),
                kept=5,
                kept_uncertain=0,
                dropped=1,
                flagged=4,
            ),
        ]
    )
    assert agg.kept == 7
    assert agg.kept_uncertain == 1
    assert agg.dropped == 4
    assert agg.flagged == 5
    assert agg.model_unique_ids == ("m.a", "m.b", "m.c")
    # flagged > 0 → below_threshold derived property holds on the aggregate.
    assert agg.below_threshold is True


def test_aggregate_mean_grade_with_one_none() -> None:
    """`mean_grade` averages only the non-None per-model means."""
    agg = _aggregate_batch_result(
        [
            _result(mean_grade=0.8),
            _result(mean_grade=None),
            _result(mean_grade=0.6),
        ]
    )
    assert agg.mean_grade == pytest.approx(0.7)


def test_aggregate_mean_grade_all_none() -> None:
    """When every per-model mean is None, the aggregate mean is None."""
    agg = _aggregate_batch_result([_result(mean_grade=None), _result(mean_grade=None)])
    assert agg.mean_grade is None


def test_aggregate_duration_sums_non_none() -> None:
    """`duration_seconds` sums the non-None per-model durations."""
    agg = _aggregate_batch_result(
        [
            _result(duration_seconds=1.5),
            _result(duration_seconds=None),
            _result(duration_seconds=2.0),
        ]
    )
    assert agg.duration_seconds == pytest.approx(3.5)


def test_aggregate_duration_all_none() -> None:
    """When every per-model duration is None, the aggregate duration is None."""
    agg = _aggregate_batch_result([_result(duration_seconds=None)])
    assert agg.duration_seconds is None


def test_aggregate_single_result() -> None:
    """A single-result input aggregates to its own counts; ids pass through."""
    agg = _aggregate_batch_result([_result(exit_code=2, model_unique_ids=("m.only",), kept=9)])
    assert agg.exit_code == 2
    assert agg.model_unique_ids == ("m.only",)
    assert agg.kept == 9


def test_aggregate_clears_sidecar_paths_and_bulk_text() -> None:
    """Aggregate carries no single sidecar path and no bulk stdout/stderr."""
    agg = _aggregate_batch_result([_result(), _result()])
    assert agg.diff_sidecar_path is None
    assert agg.grade_sidecar_path is None
    assert agg.stdout == ""
    assert agg.stderr == ""


def test_aggregate_empty_sequence_raises() -> None:
    """An empty batch violates the caller invariant → ValueError."""
    with pytest.raises(ValueError):
        _aggregate_batch_result([])


# --------------------------------------------------------------------------- #
# _build_prune_existing_argv (#233 US-001)                                    #
# --------------------------------------------------------------------------- #


def _prune_argv(**overrides: object) -> list[str]:
    """Build a prune-existing argv with sensible defaults; ``overrides`` tune params."""
    params: dict[str, object] = {
        "model": "customers",
        "schema": "models/marts/schema.yml",
        "project_dir": "/proj",
        "profiles_dir": None,
        "manifest": None,
        "scope": None,
        "sample_strategy": None,
        "as_of": None,
        "tests_dir": None,
    }
    params.update(overrides)
    return _build_prune_existing_argv(**params)  # type: ignore[arg-type]


def test_build_prune_argv_base_shape() -> None:
    """Base argv: prune-existing <model> --schema <p> --project-dir <d> --format json --dry-run."""
    argv = _prune_argv()
    assert argv[0] == "prune-existing"
    assert argv[1] == "customers"
    assert "--schema" in argv and argv[argv.index("--schema") + 1] == "models/marts/schema.yml"
    assert "--project-dir" in argv and argv[argv.index("--project-dir") + 1] == "/proj"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    # --dry-run is ALWAYS present (read-only monitor; no write branch).
    assert "--dry-run" in argv
    assert "--write" not in argv
    # Single-model only — there is no --select / --mode on this surface.
    assert "--select" not in argv
    assert "--mode" not in argv


def test_build_prune_argv_no_optionals_when_none() -> None:
    """No optional passthrough flags appear when every optional is None."""
    argv = _prune_argv()
    for flag in (
        "--profiles-dir",
        "--manifest",
        "--scope",
        "--sample-strategy",
        "--as-of",
        "--tests-dir",
    ):
        assert flag not in argv


def test_build_prune_argv_profiles_dir_injected_when_set() -> None:
    argv = _prune_argv(profiles_dir="/home/u/.dbt")
    assert argv[argv.index("--profiles-dir") + 1] == "/home/u/.dbt"
    assert "--profiles-dir" not in _prune_argv(profiles_dir=None)
    # An empty string is treated as absent.
    assert "--profiles-dir" not in _prune_argv(profiles_dir="")


def test_build_prune_argv_manifest_injected_when_set() -> None:
    argv = _prune_argv(manifest="target/manifest.json")
    assert argv[argv.index("--manifest") + 1] == "target/manifest.json"
    assert "--manifest" not in _prune_argv(manifest=None)
    assert "--manifest" not in _prune_argv(manifest="")


def test_build_prune_argv_scope_injected_when_set() -> None:
    argv = _prune_argv(scope="full")
    assert argv[argv.index("--scope") + 1] == "full"
    assert "--scope" not in _prune_argv(scope=None)
    assert "--scope" not in _prune_argv(scope="")


def test_build_prune_argv_sample_strategy_injected_when_set() -> None:
    argv = _prune_argv(sample_strategy="oneshot")
    assert argv[argv.index("--sample-strategy") + 1] == "oneshot"
    assert "--sample-strategy" not in _prune_argv(sample_strategy=None)
    assert "--sample-strategy" not in _prune_argv(sample_strategy="")


def test_build_prune_argv_as_of_injected_when_set() -> None:
    argv = _prune_argv(as_of="2026-06-15")
    assert argv[argv.index("--as-of") + 1] == "2026-06-15"
    assert "--as-of" not in _prune_argv(as_of=None)
    assert "--as-of" not in _prune_argv(as_of="")


def test_build_prune_argv_tests_dir_injected_when_set() -> None:
    argv = _prune_argv(tests_dir="tests")
    assert argv[argv.index("--tests-dir") + 1] == "tests"
    assert "--tests-dir" not in _prune_argv(tests_dir=None)
    assert "--tests-dir" not in _prune_argv(tests_dir="")


def test_build_prune_argv_all_optionals_together() -> None:
    """Every optional flag injects in order when all are set."""
    argv = _prune_argv(
        profiles_dir="/home/u/.dbt",
        manifest="target/manifest.json",
        scope="sample",
        sample_strategy="materialised",
        as_of="2026-06-15",
        tests_dir="tests",
    )
    # Optional flags follow the fixed base block, in declared order.
    assert argv.index("--profiles-dir") < argv.index("--manifest")
    assert argv.index("--manifest") < argv.index("--scope")
    assert argv.index("--scope") < argv.index("--sample-strategy")
    assert argv.index("--sample-strategy") < argv.index("--as-of")
    assert argv.index("--as-of") < argv.index("--tests-dir")


# --------------------------------------------------------------------------- #
# _validate_prune_existing_config (#233 US-001)                               #
# --------------------------------------------------------------------------- #


def test_validate_prune_accepts_valid_config() -> None:
    """A valid prune-existing config does not raise."""
    _validate_prune_existing_config(
        project_dir="/proj", model="customers", schema="schema.yml", on_flagged="fail"
    )


def test_validate_prune_rejects_empty_project_dir() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_prune_existing_config(
            project_dir="", model="customers", schema="schema.yml", on_flagged="fail"
        )


def test_validate_prune_rejects_none_project_dir() -> None:
    with pytest.raises(AirflowConfigError):
        _validate_prune_existing_config(
            project_dir=None, model="customers", schema="schema.yml", on_flagged="fail"
        )


def test_validate_prune_rejects_none_schema() -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_prune_existing_config(
            project_dir="/proj", model="customers", schema=None, on_flagged="fail"
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_validate_prune_rejects_blank_schema(blank: str) -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_prune_existing_config(
            project_dir="/proj", model="customers", schema=blank, on_flagged="fail"
        )


def test_validate_prune_rejects_non_str_schema() -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_prune_existing_config(
            project_dir="/proj",
            model="customers",
            schema=123,  # type: ignore[arg-type]
            on_flagged="fail",
        )


def test_validate_prune_rejects_none_model() -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_prune_existing_config(
            project_dir="/proj", model=None, schema="schema.yml", on_flagged="fail"
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_validate_prune_rejects_blank_model(blank: str) -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_prune_existing_config(
            project_dir="/proj", model=blank, schema="schema.yml", on_flagged="fail"
        )


def test_validate_prune_rejects_non_str_model() -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_prune_existing_config(
            project_dir="/proj",
            model=123,  # type: ignore[arg-type]
            schema="schema.yml",
            on_flagged="fail",
        )


def test_validate_prune_rejects_leading_dash_model() -> None:
    with pytest.raises(AirflowConfigError, match="argv-injection guard"):
        _validate_prune_existing_config(
            project_dir="/proj", model="--evil", schema="schema.yml", on_flagged="fail"
        )


def test_validate_prune_rejects_leading_dash_schema() -> None:
    with pytest.raises(AirflowConfigError, match="argv-injection guard"):
        _validate_prune_existing_config(
            project_dir="/proj", model="customers", schema="--evil", on_flagged="fail"
        )


def test_validate_prune_rejects_bogus_on_flagged() -> None:
    with pytest.raises(AirflowConfigError, match="on_flagged"):
        _validate_prune_existing_config(
            project_dir="/proj", model="customers", schema="schema.yml", on_flagged="bogus"
        )


@pytest.mark.parametrize("on_flagged", ["fail", "skip", "succeed"])
def test_validate_prune_accepts_each_valid_on_flagged(on_flagged: str) -> None:
    _validate_prune_existing_config(
        project_dir="/proj", model="customers", schema="schema.yml", on_flagged=on_flagged
    )


# --------------------------------------------------------------------------- #
# _validate_operator_config — drift params (#235 US-004)                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("on_drift", ["fail", "skip", "succeed"])
def test_validate_accepts_each_valid_on_drift(on_drift: str) -> None:
    _validate_operator_config(
        project_dir="/proj", model="m.sql", select=None, on_flagged="fail", on_drift=on_drift
    )


def test_validate_rejects_bogus_on_drift() -> None:
    with pytest.raises(AirflowConfigError, match="on_drift"):
        _validate_operator_config(
            project_dir="/proj", model="m.sql", select=None, on_flagged="fail", on_drift="bogus"
        )


def test_validate_accepts_set_drift_paths() -> None:
    """A valid prior-diff path + history dir do not raise."""
    _validate_operator_config(
        project_dir="/proj",
        model="m.sql",
        select=None,
        on_flagged="fail",
        detect_drift_against="/history/2026-06-14/diff.json",
        drift_history_dir="/history/2026-06-15",
    )


def test_validate_accepts_templated_drift_paths() -> None:
    """An un-rendered Jinja template (begins with '{') is not a leading-dash hit."""
    _validate_operator_config(
        project_dir="/proj",
        model="m.sql",
        select=None,
        on_flagged="fail",
        detect_drift_against="/history/{{ prev_ds }}/diff.json",
        drift_history_dir="/history/{{ ds }}",
    )


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_validate_treats_blank_drift_paths_as_off(blank: str | None) -> None:
    """``None`` / blank drift paths mean 'feature off' — not an error (opt-in)."""
    _validate_operator_config(
        project_dir="/proj",
        model="m.sql",
        select=None,
        on_flagged="fail",
        detect_drift_against=blank,
        drift_history_dir=blank,
    )


def test_validate_rejects_leading_dash_detect_drift_against() -> None:
    with pytest.raises(AirflowConfigError, match="argv-injection guard"):
        _validate_operator_config(
            project_dir="/proj",
            model="m.sql",
            select=None,
            on_flagged="fail",
            detect_drift_against="--evil",
        )


def test_validate_rejects_leading_dash_drift_history_dir() -> None:
    with pytest.raises(AirflowConfigError, match="argv-injection guard"):
        _validate_operator_config(
            project_dir="/proj",
            model="m.sql",
            select=None,
            on_flagged="fail",
            drift_history_dir="-x",
        )


def test_validate_rejects_non_str_drift_path() -> None:
    with pytest.raises(AirflowConfigError, match="must be a string"):
        _validate_operator_config(
            project_dir="/proj",
            model="m.sql",
            select=None,
            on_flagged="fail",
            detect_drift_against=123,  # type: ignore[arg-type]
        )


def test_drift_params_do_not_leak_into_generate_argv() -> None:
    """Drift is operator-internal: it never becomes a ``signalforge generate`` flag.

    Guards against a future change wiring drift into the argv builder — there is
    no ``--detect-drift-against`` / ``--drift-history-dir`` / ``--on-drift`` CLI
    flag, so none must appear in the generate argv.
    """
    argv = _argv()
    for tok in ("--detect-drift-against", "--drift-history-dir", "--on-drift"):
        assert tok not in argv


# --------------------------------------------------------------------------- #
# build_drift_report (#235 US-004) — pure drift orchestration                  #
# --------------------------------------------------------------------------- #


def test_build_drift_report_baseline_when_no_prior() -> None:
    """``prior_diff=None`` → a non-alarming baseline report; compute_drift NOT called."""
    curr = _load_diff("signal_rot_curr_diff")
    report = build_drift_report(
        current_diff=curr,
        prior_diff=None,
        current_grade=None,
        prior_grade=None,
        as_of=None,
        grade_regression_threshold=0.05,
    )
    assert report.baseline is True
    assert report.alarming is False
    assert report.model_unique_id == "model.shop.fct_orders"
    assert report.newly_always_passes == ()
    assert report.newly_dropped == ()
    assert report.newly_kept == ()
    assert report.grade_regressions == ()
    assert report.degrade_reason is None
    # No comparison performed → no prior hash; as_of threads through.
    assert report.previous_diff_hash == ""


def test_build_drift_report_with_prior_delegates_to_compute_drift() -> None:
    """``prior_diff`` set → delegates to compute_drift; signal rot ⇒ alarming."""
    prev = _load_diff("signal_rot_prev_diff")
    curr = _load_diff("signal_rot_curr_diff")
    report = build_drift_report(
        current_diff=curr,
        prior_diff=prev,
        current_grade=None,
        prior_grade=None,
        as_of=date(2026, 6, 15),
        grade_regression_threshold=0.05,
    )
    assert report.baseline is False
    assert report.alarming is True
    assert len(report.newly_always_passes) == 1
    assert report.newly_always_passes[0].artifact_id == "test.column.amount.not_null"
    assert report.as_of == date(2026, 6, 15)
    # Real comparison → real input hashes (not the baseline empty-string sentinel).
    assert report.previous_diff_hash != ""
    assert report.current_diff_hash != ""


def test_build_drift_report_threads_grades_to_compute_drift() -> None:
    """Both grades present → grade-regression axis reaches compute_drift.

    The fixture grades fall 0.9 → 0.8 (delta 0.1 ≥ the 0.05 threshold), so a
    regression is emitted — proving the helper passes the grades through rather
    than dropping them.
    """
    prev = _load_diff("signal_rot_prev_diff")
    curr = _load_diff("signal_rot_curr_diff")
    report = build_drift_report(
        current_diff=curr,
        prior_diff=prev,
        current_grade=_load_grade("signal_rot_curr_grade"),
        prior_grade=_load_grade("signal_rot_prev_grade"),
        as_of=None,
        grade_regression_threshold=0.05,
    )
    assert len(report.grade_regressions) == 1
    regression = report.grade_regressions[0]
    assert regression.previous_mean == pytest.approx(0.9)
    assert regression.current_mean == pytest.approx(0.8)
    assert report.alarming is True


def test_build_drift_report_no_grades_leaves_regressions_empty() -> None:
    """A missing grade (``--no-grade``) leaves ``grade_regressions`` empty (degrade)."""
    prev = _load_diff("signal_rot_prev_diff")
    curr = _load_diff("signal_rot_curr_diff")
    report = build_drift_report(
        current_diff=curr,
        prior_diff=prev,
        current_grade=None,
        prior_grade=None,
        as_of=None,
        grade_regression_threshold=0.05,
    )
    assert report.grade_regressions == ()


# --------------------------------------------------------------------------- #
# _validate_drift_config (#235 US-005)                                         #
# --------------------------------------------------------------------------- #


def test_validate_drift_accepts_valid_config() -> None:
    """A valid dedicated-drift config does not raise."""
    _validate_drift_config(
        previous_diff_path="/history/2026-06-14/diff.json",
        current_diff_path="/history/2026-06-15/diff.json",
        on_drift="fail",
    )


def test_validate_drift_accepts_templated_paths() -> None:
    """An un-rendered Jinja template (begins with '{') is not a leading-dash hit."""
    _validate_drift_config(
        previous_diff_path="/history/{{ prev_ds }}/diff.json",
        current_diff_path="/history/{{ ds }}/diff.json",
        on_drift="skip",
    )


@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_validate_drift_rejects_blank_previous_diff_path(blank: str | None) -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_drift_config(
            previous_diff_path=blank,
            current_diff_path="/c/diff.json",
            on_drift="fail",
        )


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_validate_drift_rejects_blank_current_diff_path(blank: str | None) -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_drift_config(
            previous_diff_path="/p/diff.json",
            current_diff_path=blank,
            on_drift="fail",
        )


def test_validate_drift_rejects_non_str_path() -> None:
    with pytest.raises(AirflowConfigError, match="non-empty string"):
        _validate_drift_config(
            previous_diff_path=123,  # type: ignore[arg-type]
            current_diff_path="/c/diff.json",
            on_drift="fail",
        )


def test_validate_drift_rejects_leading_dash_previous() -> None:
    with pytest.raises(AirflowConfigError, match="must not begin with"):
        _validate_drift_config(
            previous_diff_path="-evil",
            current_diff_path="/c/diff.json",
            on_drift="fail",
        )


def test_validate_drift_rejects_leading_dash_current() -> None:
    with pytest.raises(AirflowConfigError, match="must not begin with"):
        _validate_drift_config(
            previous_diff_path="/p/diff.json",
            current_diff_path="-evil",
            on_drift="fail",
        )


def test_validate_drift_rejects_bogus_on_drift() -> None:
    with pytest.raises(AirflowConfigError, match="on_drift"):
        _validate_drift_config(
            previous_diff_path="/p/diff.json",
            current_diff_path="/c/diff.json",
            on_drift="bogus",
        )


@pytest.mark.parametrize("on_drift", ["fail", "skip", "succeed"])
def test_validate_drift_accepts_each_valid_on_drift(on_drift: str) -> None:
    _validate_drift_config(
        previous_diff_path="/p/diff.json",
        current_diff_path="/c/diff.json",
        on_drift=on_drift,
    )


# --------------------------------------------------------------------------- #
# _build_drift_inputs (#235 US-005)                                            #
# --------------------------------------------------------------------------- #


def test_build_drift_inputs_auto_siblings_grades_when_none() -> None:
    """Unset grade paths resolve to the ``grade.json`` sibling of each diff path."""
    prev_diff, curr_diff, prev_grade, curr_grade = _build_drift_inputs(
        previous_diff_path="/history/2026-06-14/diff.json",
        current_diff_path="/history/2026-06-15/diff.json",
        previous_grade_path=None,
        current_grade_path=None,
    )
    # Diff paths pass through unchanged.
    assert prev_diff == "/history/2026-06-14/diff.json"
    assert curr_diff == "/history/2026-06-15/diff.json"
    # Grades auto-sibling next to each diff.
    assert prev_grade == str(Path("/history/2026-06-14/grade.json"))
    assert curr_grade == str(Path("/history/2026-06-15/grade.json"))


def test_build_drift_inputs_uses_explicit_grade_paths_when_set() -> None:
    """Explicit grade paths win over the auto-sibling resolution."""
    _, _, prev_grade, curr_grade = _build_drift_inputs(
        previous_diff_path="/history/prev/diff.json",
        current_diff_path="/history/curr/diff.json",
        previous_grade_path="/custom/prev_grade.json",
        current_grade_path="/custom/curr_grade.json",
    )
    assert prev_grade == "/custom/prev_grade.json"
    assert curr_grade == "/custom/curr_grade.json"


def test_build_drift_inputs_mixes_explicit_and_auto() -> None:
    """A set previous grade + unset current grade resolve independently."""
    _, _, prev_grade, curr_grade = _build_drift_inputs(
        previous_diff_path="/history/prev/diff.json",
        current_diff_path="/history/curr/diff.json",
        previous_grade_path="/custom/prev_grade.json",
        current_grade_path=None,
    )
    assert prev_grade == "/custom/prev_grade.json"
    assert curr_grade == str(Path("/history/curr/grade.json"))


# --------------------------------------------------------------------------- #
# _drift_task_outcome (#235 US-005)                                            #
# --------------------------------------------------------------------------- #


def _non_alarming_drift() -> DriftReport:
    """A baseline (no comparison) → non-alarming DriftReport."""
    return DriftReport(
        signalforge_version="0",
        model_unique_id="model.shop.fct_orders",
        as_of=None,
        grade_regression_threshold=0.05,
        baseline=True,
        previous_diff_hash="",
        current_diff_hash="",
    )


def _alarming_drift() -> DriftReport:
    """A DriftReport carrying a signal-rot transition → alarming."""
    return DriftReport(
        signalforge_version="0",
        model_unique_id="model.shop.fct_orders",
        as_of=None,
        grade_regression_threshold=0.05,
        previous_diff_hash="aa",
        current_diff_hash="bb",
        newly_always_passes=(
            DriftArtifact(
                artifact_id="test.column.amount.not_null",
                previous_tier="kept",
                current_tier="dropped",
                previous_drop_reason=None,
                current_drop_reason="always-passes",
            ),
        ),
    )


def test_drift_outcome_non_alarming_is_success_regardless_of_policy() -> None:
    """A non-alarming report → SUCCESS for every ``on_drift`` policy."""
    drift = _non_alarming_drift()
    assert drift.alarming is False
    assert _drift_task_outcome(drift, "fail") == TaskOutcome.SUCCESS
    assert _drift_task_outcome(drift, "skip") == TaskOutcome.SUCCESS
    assert _drift_task_outcome(drift, "succeed") == TaskOutcome.SUCCESS


def test_drift_outcome_alarming_fail_is_fail_no_retry() -> None:
    """An alarming report under the default ``on_drift="fail"`` → FAIL_NO_RETRY."""
    assert _drift_task_outcome(_alarming_drift(), "fail") == TaskOutcome.FAIL_NO_RETRY


def test_drift_outcome_alarming_skip_is_skip() -> None:
    assert _drift_task_outcome(_alarming_drift(), "skip") == TaskOutcome.SKIP


def test_drift_outcome_alarming_succeed_is_success() -> None:
    assert _drift_task_outcome(_alarming_drift(), "succeed") == TaskOutcome.SUCCESS
