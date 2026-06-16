"""Tests for ``signalforge.airflow.runner`` (issue #231 / US-002).

These tests import ONLY the Airflow-free runner — never the real
``apache-airflow`` package — so they run in the **default** pytest suite
(NO ``airflow`` marker). They pin:

1. The in-process happy path: a fake :func:`signalforge.cli.main` writes a
   diff-JSON fixture to stdout and returns exit 0; the parsed
   :class:`SignalForgeRunResult` carries the right counts / model id / duration.
2. Process-global isolation: ``sys.excepthook`` and the three env keys are
   restored exactly after a run — including when the fake ``main`` mutates them
   AND when it raises (the exception still propagates).
3. The ``normalise_argv`` helper: ``--format`` injection vs. honouring an
   explicit format; ``--project-dir`` injection.
4. ``grade.json`` presence → ``mean_grade`` populated; absence → ``None`` and
   ``grade_sidecar_path is None``; ``diff.json`` absence → ``diff_sidecar_path
   is None``.
5. Defensive parse: a non-JSON / empty / malformed-dict stdout does not crash.
6. Subprocess mode: the command list is exactly
   ``[sys.executable, "-m", "signalforge", ...]`` with NO ``shell=True``; a
   ``TimeoutExpired`` propagates unchanged; the normalised argv reaches the cmd.
7. Key-contract guard: the diff keys the runner reads + ``mean_score`` are real
   ``DiffReport`` / ``GradingReport`` surfaces (round-trip through the real
   serializer) — so a rename can't silently drift the parse to zero counts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from signalforge._common.path_safety import PathContainmentError
from signalforge.airflow.runner import _parse_diff_stdout, normalise_argv, run_signalforge

# A minimal ``DiffReport.model_dump_json`` shape — only the keys the runner
# parses. Mirrors ``src/signalforge/diff/models.py``'s field names.
_DIFF_JSON = {
    "model_unique_id": "model.demo.stg_trips",
    "run_id": "deadbeefcafe",
    "duration_seconds": 12.5,
    "kept_count": 4,
    "kept_uncertain_count": 2,
    "dropped_count": 7,
    "flagged_count": 1,
    "proposed_test_files": [],
}


def _fake_main_writing(stdout_text: str, *, exit_code: int = 0):
    """Build a fake ``cli.main`` that writes ``stdout_text`` and returns
    ``exit_code``. The ``redirect_stdout`` in the runner captures it."""

    def _fake_main(argv: list[str]) -> int:
        sys.stdout.write(stdout_text)
        return exit_code

    return _fake_main


# --------------------------------------------------------------------------- #
# normalise_argv helper
# --------------------------------------------------------------------------- #


def test_normalise_argv_injects_format_json_when_absent() -> None:
    out, stdout_is_json = normalise_argv(["generate", "models/x.sql"], "/proj")
    assert "--format" in out
    assert out[out.index("--format") + 1] == "json"
    assert stdout_is_json is True


def test_normalise_argv_leaves_explicit_format_alone() -> None:
    out, stdout_is_json = normalise_argv(
        ["generate", "models/x.sql", "--format", "markdown"], "/proj"
    )
    # The explicit choice is honoured; no second --format injected.
    assert out.count("--format") == 1
    assert out[out.index("--format") + 1] == "markdown"
    assert stdout_is_json is False


def test_normalise_argv_honours_equals_joined_format() -> None:
    out, stdout_is_json = normalise_argv(["generate", "--format=json"], "/proj")
    assert "--format=json" in out
    assert "--format" not in out  # the bare space-separated form was not injected
    assert stdout_is_json is True


def test_normalise_argv_injects_project_dir_when_absent() -> None:
    out, _ = normalise_argv(["generate", "models/x.sql"], "/proj/dir")
    assert "--project-dir" in out
    assert out[out.index("--project-dir") + 1] == "/proj/dir"


def test_normalise_argv_leaves_explicit_project_dir_alone() -> None:
    out, _ = normalise_argv(
        ["generate", "models/x.sql", "--project-dir", "/caller/choice"], "/proj/dir"
    )
    assert out.count("--project-dir") == 1
    assert out[out.index("--project-dir") + 1] == "/caller/choice"


# --------------------------------------------------------------------------- #
# in_process happy path + parsing
# --------------------------------------------------------------------------- #


def test_in_process_happy_path_parses_diff_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON), exit_code=0)
    )

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.exit_code == 0
    assert result.model_unique_ids == ("model.demo.stg_trips",)
    assert result.kept == 4
    assert result.kept_uncertain == 2
    assert result.dropped == 7
    assert result.flagged == 1
    assert result.duration_seconds == 12.5
    assert result.below_threshold is True  # flagged > 0
    assert result.stdout == json.dumps(_DIFF_JSON)


# --------------------------------------------------------------------------- #
# isolation restore (the DEC-003 contract)
# --------------------------------------------------------------------------- #


def test_in_process_restores_excepthook_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel_hook = sys.excepthook

    # NO_COLOR + DBT_PROFILES_DIR present beforehand; FORCE_COLOR absent.
    monkeypatch.setenv("NO_COLOR", "preexisting-no-color")
    monkeypatch.setenv("DBT_PROFILES_DIR", "/preexisting/profiles")
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    def _mutating_main(argv: list[str]) -> int:
        import os

        sys.excepthook = lambda *a: None  # type: ignore[assignment]
        os.environ["NO_COLOR"] = "1"
        os.environ["FORCE_COLOR"] = "1"  # was absent before
        os.environ["DBT_PROFILES_DIR"] = "/mutated/profiles"
        sys.stdout.write(json.dumps(_DIFF_JSON))
        return 0

    monkeypatch.setattr("signalforge.cli.main", _mutating_main)

    import os

    run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert sys.excepthook is sentinel_hook
    assert os.environ.get("NO_COLOR") == "preexisting-no-color"
    assert os.environ.get("DBT_PROFILES_DIR") == "/preexisting/profiles"
    # FORCE_COLOR was absent before → must be deleted again, not left at "1".
    assert "FORCE_COLOR" not in os.environ


def test_in_process_restores_state_even_when_main_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel_hook = sys.excepthook
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    def _raising_main(argv: list[str]) -> int:
        import os

        sys.excepthook = lambda *a: None  # type: ignore[assignment]
        os.environ["FORCE_COLOR"] = "1"
        raise RuntimeError("boom from main")

    monkeypatch.setattr("signalforge.cli.main", _raising_main)

    import os

    with pytest.raises(RuntimeError, match="boom from main"):
        run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    # Restoration in the finally block happened despite the exception.
    assert sys.excepthook is sentinel_hook
    assert "FORCE_COLOR" not in os.environ


# --------------------------------------------------------------------------- #
# grade.json / sidecar paths
# --------------------------------------------------------------------------- #


def _write_sidecar(project_dir: Path, name: str, payload: dict[str, Any] | str) -> None:
    sf_dir = project_dir / ".signalforge"
    sf_dir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (sf_dir / name).write_text(text, encoding="utf-8")


def test_grade_sidecar_present_populates_mean_grade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON)))
    _write_sidecar(tmp_path, "grade.json", {"mean_score": 0.83})
    _write_sidecar(tmp_path, "diff.json", {"anything": True})

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.mean_grade == 0.83
    assert result.grade_sidecar_path is not None
    assert result.grade_sidecar_path.endswith("grade.json")
    assert result.diff_sidecar_path is not None
    assert result.diff_sidecar_path.endswith("diff.json")


def test_grade_sidecar_absent_yields_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON)))
    # No .signalforge dir at all — both sidecars absent (mirrors --dry-run).
    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.mean_grade is None
    assert result.grade_sidecar_path is None
    assert result.diff_sidecar_path is None


def test_grade_sidecar_unparseable_keeps_path_but_none_mean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON)))
    _write_sidecar(tmp_path, "grade.json", "{not valid json")

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    # File exists → path is set; content unparseable → mean_grade None (no crash).
    assert result.grade_sidecar_path is not None
    assert result.mean_grade is None


def test_grade_sidecar_non_numeric_mean_score_yields_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``mean_score`` present but a bool / non-numeric → coerced to None (a bool is
    # NOT treated as a real float).
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON)))
    _write_sidecar(tmp_path, "grade.json", {"mean_score": True})

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.grade_sidecar_path is not None
    assert result.mean_grade is None


def test_grade_sidecar_non_dict_json_yields_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Valid JSON, but the top-level value is an array (not an object) — exercises
    # the ``isinstance(data, dict)`` false arm in ``_read_grade_sidecar``: the
    # path is still set, mean_grade degrades to None (no crash).
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON)))
    _write_sidecar(tmp_path, "grade.json", json.dumps([{"mean_score": 0.5}]))

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.grade_sidecar_path is not None
    assert result.mean_grade is None


def test_sidecar_path_containment_failure_yields_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A symlink-containment failure on the sidecar path degrades to None for both
    # sidecars and mean_grade — never crashes the run.
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON)))
    _write_sidecar(tmp_path, "grade.json", {"mean_score": 0.9})
    _write_sidecar(tmp_path, "diff.json", {"anything": True})

    def _boom(input_path: object, project_dir: object) -> Path:
        raise PathContainmentError("escapes project tree")

    # Patch the name in the function's OWN module globals (the namespace the
    # runner actually resolves ``canonicalise_path`` from) rather than by string
    # path. ``test_airflow_no_eager_import`` scrubs ``signalforge.airflow.runner``
    # from ``sys.modules``, so a string-path ``setattr`` could hit a freshly
    # re-imported module object while ``run_signalforge`` still lives in the
    # original one — ``__globals__`` is ordering-robust.
    monkeypatch.setitem(run_signalforge.__globals__, "canonicalise_path", _boom)

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.diff_sidecar_path is None
    assert result.grade_sidecar_path is None
    assert result.mean_grade is None


# --------------------------------------------------------------------------- #
# defensive parse
# --------------------------------------------------------------------------- #


def test_defensive_parse_non_json_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "signalforge.cli.main",
        _fake_main_writing("ERROR: something went wrong\n", exit_code=2),
    )

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.exit_code == 2
    assert result.model_unique_ids == ()
    assert result.kept == 0
    assert result.kept_uncertain == 0
    assert result.dropped == 0
    assert result.flagged == 0
    assert result.duration_seconds is None
    assert result.mean_grade is None


def test_defensive_parse_non_object_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Valid JSON, but the top-level value is an array (not an object) — the diff
    # render is always an object, so a non-object degrades to empty counts.
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps([1, 2, 3])))

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.model_unique_ids == ()
    assert result.kept == 0


def test_defensive_parse_empty_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing("", exit_code=3))

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.exit_code == 3
    assert result.model_unique_ids == ()
    assert result.kept == 0


def test_defensive_parse_malformed_dict_coerces_to_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Valid JSON object, but counts are the wrong type / model id is missing.
    bad = {
        "model_unique_id": None,
        "kept_count": "lots",
        "kept_uncertain_count": True,  # bool is not a real int
        "dropped_count": None,
        "flagged_count": 3.5,  # float, not int
        "duration_seconds": "soon",
    }
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(bad)))

    result = run_signalforge(["generate", "models/x.sql"], project_dir=tmp_path)

    assert result.model_unique_ids == ()
    assert result.kept == 0
    assert result.kept_uncertain == 0
    assert result.dropped == 0
    assert result.flagged == 0
    assert result.duration_seconds is None


def test_explicit_non_json_format_skips_stdout_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Even though stdout IS valid JSON, an explicit --format markdown means the
    # parse is skipped (best-effort) — counts stay zero.
    monkeypatch.setattr("signalforge.cli.main", _fake_main_writing(json.dumps(_DIFF_JSON)))
    result = run_signalforge(
        ["generate", "models/x.sql", "--format", "markdown"], project_dir=tmp_path
    )
    assert result.model_unique_ids == ()
    assert result.kept == 0


# --------------------------------------------------------------------------- #
# subprocess mode
# --------------------------------------------------------------------------- #


def test_subprocess_mode_builds_list_command_no_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _FakeProc:
        returncode = 0
        stdout = json.dumps(_DIFF_JSON)
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = run_signalforge(
        ["generate", "models/x.sql"], project_dir=tmp_path, invocation="subprocess"
    )

    cmd = captured["cmd"]
    assert cmd[:3] == [sys.executable, "-m", "signalforge"]
    assert "generate" in cmd
    # ``normalise_argv``'s injected flags reached the subprocess command (not just
    # the program-name prefix): both ``--format`` and ``--project-dir`` are threaded.
    assert "--format" in cmd
    assert "--project-dir" in cmd
    # LIST form invoked; shell=True must never be passed.
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"].get("capture_output") is True
    assert captured["kwargs"].get("text") is True

    # Parsed result still flows from the canned stdout.
    assert result.exit_code == 0
    assert result.model_unique_ids == ("model.demo.stg_trips",)
    assert result.kept == 4


def test_subprocess_mode_passes_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    run_signalforge(
        ["generate", "models/x.sql"],
        project_dir=tmp_path,
        invocation="subprocess",
        timeout_seconds=42.0,
    )

    assert captured["kwargs"].get("timeout") == 42.0


def test_subprocess_mode_propagates_timeout_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A subprocess wall-clock overrun surfaces as ``subprocess.TimeoutExpired``
    # propagating UNCHANGED — a runtime/external failure, deliberately NOT wrapped
    # in ``AirflowConfigError`` (the documented contract).
    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        run_signalforge(
            ["generate", "models/x.sql"],
            project_dir=tmp_path,
            invocation="subprocess",
            timeout_seconds=1.0,
        )


def test_top_level_main_module_importable() -> None:
    """``signalforge.__main__`` imports cleanly and re-exports the CLI ``main``
    (the module that makes ``python -m signalforge`` resolvable)."""
    import importlib

    module = importlib.import_module("signalforge.__main__")
    assert callable(module.main)


def test_python_dash_m_signalforge_resolves() -> None:
    """The ``python -m signalforge`` form the subprocess mode uses must actually
    resolve to the CLI (it depends on ``src/signalforge/__main__.py``).

    Real subprocess (cheap ``--version``, hermetic, no external deps) — mirrors
    the unmarked-subprocess precedent in ``test_airflow_no_eager_import.py``.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "signalforge", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.startswith("signalforge ")


# --------------------------------------------------------------------------- #
# key-contract guard (against the fake-driven-byte-identity blind spot)
# --------------------------------------------------------------------------- #
#
# Every runner test above feeds a hand-crafted ``_DIFF_JSON`` / ``{"mean_score":
# ...}`` that happens to match the real serialized shapes. A rename of
# ``DiffReport.kept_count`` -> ``kept`` (or dropping ``GradingReport.mean_score``)
# would keep ALL of those tests green while a real Airflow run parsed zero counts
# / ``None`` mean_grade. These ungated, airflow-free tests pin the parse contract
# against the REAL pydantic surfaces so it can't silently drift.


def test_runner_diff_keys_are_real_diffreport_fields() -> None:
    from signalforge.diff.models import DiffReport

    # ``DiffReport`` carries no field aliases, so ``model_json_schema().properties``
    # keys == the JSON keys ``model_dump_json(by_alias=True)`` emits == the keys the
    # runner reads. A rename of any of these fails this test loudly.
    properties = set(DiffReport.model_json_schema()["properties"])
    runner_diff_keys = {
        "model_unique_id",
        "kept_count",
        "kept_uncertain_count",
        "dropped_count",
        "flagged_count",
        "duration_seconds",
    }
    missing = runner_diff_keys - properties
    assert not missing, f"runner reads diff keys absent from DiffReport: {sorted(missing)}"


def test_runner_mean_score_is_real_gradingreport_computed_field() -> None:
    from signalforge.grade.models import GradingReport

    # ``mean_score`` is a computed field on ``GradingReport`` (serialized into the
    # grade.json sidecar). ``_read_grade_sidecar`` reads ``mean_score`` — drop or
    # rename it and this fails loudly.
    assert "mean_score" in GradingReport.model_computed_fields


def test_real_diffreport_roundtrips_through_parse_diff_stdout() -> None:
    # True round-trip against the REAL serializer: build a minimal ``DiffReport``,
    # render it via ``model_dump_json(by_alias=True)`` (the exact shape stdout
    # carries), and feed it through the runner's ``_parse_diff_stdout`` — the counts
    # must come back under the keys ``run_signalforge`` reads.
    from signalforge.diff.models import DiffReport

    report = DiffReport(
        signalforge_version="0.7.0.dev0",
        model_unique_id="model.demo.stg_trips",
        run_id="abc123",
        duration_seconds=9.5,
        proposed_yaml="",
        existing_yaml=None,
        unified_diff="",
        entries=(),
        kept_count=4,
        kept_uncertain_count=2,
        dropped_count=7,
        flagged_count=1,
        has_existing_schema=False,
        candidate_hash="h1",
        prune_result_hash="h2",
        grading_report_hash=None,
    )
    parsed = _parse_diff_stdout(report.model_dump_json(by_alias=True), True)
    assert parsed is not None
    assert parsed["model_unique_id"] == "model.demo.stg_trips"
    assert parsed["kept_count"] == 4
    assert parsed["kept_uncertain_count"] == 2
    assert parsed["dropped_count"] == 7
    assert parsed["flagged_count"] == 1
    assert parsed["duration_seconds"] == 9.5
