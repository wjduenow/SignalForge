"""US-001 (#232) — airflow-free pure operator helpers.

Covers :func:`signalforge.airflow.operators._build_generate_argv` (params →
``signalforge generate`` argv) and
:func:`signalforge.airflow.operators._validate_operator_config` (DEC-009 param
guards). Both helpers are pure — no airflow import, no I/O — so this file is
**UNGATED**: it carries NO ``@pytest.mark.airflow`` marker and never imports
``apache-airflow``. It runs in the default ``uv run pytest`` suite and exercises
every branch of both helpers (the airflow-free core's 100%-ungated codecov patch
gate).
"""

from __future__ import annotations

import pytest

from signalforge.airflow.errors import AirflowConfigError
from signalforge.airflow.operators import (
    _build_generate_argv,
    _validate_operator_config,
)


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
