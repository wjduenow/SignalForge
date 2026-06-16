"""Airflow-free runner: drive ``signalforge`` and parse its output.

US-002 of issue #231 (epic #228, v0.7 Airflow operator roadmap). This module
turns one ``signalforge generate`` (or ``prune-existing``) invocation into a
:class:`~signalforge.airflow.result.SignalForgeRunResult` — the pure, frozen
value the operator (US-003) consumes via
:func:`~signalforge.airflow.result.decide_task_outcome`.

**This module imports NO airflow.** That is load-bearing: it lets
``signalforge.airflow.__init__`` re-export :func:`run_signalforge` **eagerly**
(alongside the result core / error classes — only the operator/hook names stay
lazy, DEC-006). The sole heavy import — :func:`signalforge.cli.main` — is done
**lazily inside the function body** so that merely importing
``signalforge.airflow`` / ``signalforge.airflow.runner`` does not eagerly drag
the whole CLI (and its dbt / pydantic / warehouse imports) into the process.

Two invocation modes:

* ``in_process`` (default) — call :func:`signalforge.cli.main` directly,
  capturing stdout/stderr via :func:`contextlib.redirect_stdout` /
  ``redirect_stderr``. Faster (no fresh interpreter), but mutates some
  process-global state, so this mode snapshots and restores it in a ``finally``
  block (DEC-003) to keep a long-lived Airflow worker clean across tasks:
  ``sys.excepthook``, the ``NO_COLOR`` / ``FORCE_COLOR`` / ``DBT_PROFILES_DIR``
  env vars, and — best-effort — the root logger's handlers + level (the CLI's
  ``setup_logging`` calls ``logging.basicConfig(force=True)``, which removes and
  may close the root logger's handlers; re-attaching them is best-effort, so for
  full process isolation in a long-lived worker prefer ``subprocess``).
* ``subprocess`` — shell out to ``python -m signalforge`` (LIST form, never
  ``shell=True``) for full process isolation. A ``timeout_seconds`` overrun
  surfaces as :class:`subprocess.TimeoutExpired` propagating unchanged (a
  runtime/external failure, NOT an operator misconfiguration — it is
  deliberately not wrapped in :class:`AirflowConfigError`).

The result is parsed from the JSON diff render on stdout (the same shape as
:meth:`signalforge.diff.models.DiffReport.model_dump_json`) plus the on-disk
``grade.json`` sidecar (for ``mean_grade``). Sidecars are located under the
EFFECTIVE ``--project-dir`` — the value actually passed to the CLI (a
caller-supplied ``--project-dir`` in ``argv`` if present, else the injected
``project_dir`` param) — so the runner reads them from the same tree the run
wrote them under. Both
parses are **best-effort**: a non-zero exit that produced no diff, a non-JSON
``--format``, or a missing/odd sidecar degrades to empty counts / ``None``
rather than crashing — the operator maps the captured ``exit_code`` to a task
outcome regardless of whether the JSON parsed (DEC-002).
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Literal

from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.airflow.result import SignalForgeRunResult

# The ONLY process-global env keys ``cmd_generate`` / ``cmd_prune_existing``
# mutate (verified against ``src/signalforge/cli/{generate,prune_existing}.py``:
# ``NO_COLOR`` and ``DBT_PROFILES_DIR`` are set there; ``FORCE_COLOR`` is read by
# the colour-precedence chain and snapshotted here defensively so a future flag
# that sets it cannot leak across in-process runs). Restored in a ``finally``
# block by :func:`_run_in_process` (DEC-003).
_ISOLATED_ENV_KEYS: tuple[str, ...] = ("NO_COLOR", "FORCE_COLOR", "DBT_PROFILES_DIR")

# Conventional sidecar directory + filenames (mirrors the diff / grade
# orchestrators' default ``<project_dir>/.signalforge/<name>.json`` paths).
_SIGNALFORGE_DIR = ".signalforge"
_DIFF_SIDECAR_NAME = "diff.json"
_GRADE_SIDECAR_NAME = "grade.json"


def _find_flag(argv: list[str], flag: str) -> tuple[str | None, bool]:
    """Return ``(value, present)`` for ``flag`` in ``argv``.

    Handles both the space-separated (``--format json``) and the
    ``=``-joined (``--format=json``) forms. ``value`` is ``None`` when the
    flag is present but carries no following token; ``present`` is ``False``
    when the flag does not appear at all.
    """
    eq_prefix = f"{flag}="
    for i, token in enumerate(argv):
        if token == flag:
            value = argv[i + 1] if i + 1 < len(argv) else None
            return value, True
        if token.startswith(eq_prefix):
            return token[len(eq_prefix) :], True
    return None, False


def normalise_argv(argv: list[str], project_dir: str | Path) -> tuple[list[str], bool]:
    """Return ``(normalised_argv, stdout_is_json)``.

    * ``--format``: if absent, inject ``--format json`` (so the diff render on
      stdout is JSON-parseable). If the caller already passed an explicit
      ``--format <x>``, it is left ALONE — the caller chose, and a non-json
      format means the stdout parse is skipped/best-effort.
    * ``--project-dir``: if absent, append ``--project-dir <project_dir>``. If
      the caller already passed one, it is left as-is — and ``run_signalforge``
      locates the sidecars under the EFFECTIVE ``--project-dir`` (the value
      actually present in the returned argv), so a caller-supplied
      ``--project-dir`` that differs from ``project_dir`` is honoured for the
      sidecar reads too.

    The second element of the tuple is ``True`` iff the effective ``--format``
    is ``json`` — the signal :func:`run_signalforge` uses to decide whether to
    attempt the stdout JSON parse at all.
    """
    out = list(argv)
    fmt_value, fmt_present = _find_flag(out, "--format")
    if not fmt_present:
        out += ["--format", "json"]
        fmt_value = "json"
    stdout_is_json = fmt_value == "json"

    _, project_dir_present = _find_flag(out, "--project-dir")
    if not project_dir_present:
        out += ["--project-dir", str(project_dir)]

    return out, stdout_is_json


def _exit_code_from_systemexit(exc: SystemExit, stderr_buf: io.StringIO) -> int:
    """Convert a ``SystemExit`` raised by ``main`` into an exit code.

    Subprocess parity: in ``subprocess`` mode a ``sys.exit`` / argparse-usage /
    ``--version`` path becomes a process returncode; in-process it would instead
    raise :class:`SystemExit`, escape, and bypass :class:`SignalForgeRunResult`
    creation. This mirrors Python's own ``sys.exit`` semantics (and
    :func:`signalforge.cli.main`'s own ``SystemExit`` handling): ``None`` → 0, an
    ``int`` → that code, anything else (a str message) is written to the captured
    stderr buffer and mapped to exit 1.
    """
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    stderr_buf.write(str(code))
    return 1


def _run_in_process(normalised_argv: list[str]) -> tuple[int, str, str]:
    """Call :func:`signalforge.cli.main` in-process, capturing output.

    A :class:`SystemExit` raised by ``main`` (argparse usage / ``--version`` /
    any ``sys.exit``) is converted to an exit code via
    :func:`_exit_code_from_systemexit` (subprocess parity) — it does NOT escape.

    Best-effort process-global isolation across tasks in a long-lived Airflow
    worker (DEC-003). Snapshots — before running ``main`` — and restores in a
    ``finally`` block (even when ``main`` raises; the exception propagates AFTER
    restoration):

    * ``sys.excepthook``;
    * the :data:`_ISOLATED_ENV_KEYS` env vars
      (``NO_COLOR`` / ``FORCE_COLOR`` / ``DBT_PROFILES_DIR``);
    * the root logger's handler LIST, level, and ``disabled`` flag.

    The root-logger restore is needed because the CLI's
    :func:`signalforge.cli._helpers.setup_logging` calls
    ``logging.basicConfig(..., force=True)``, which REMOVES (and ``close()``-s)
    the root logger's existing handlers and installs a single stderr handler —
    tearing down Airflow's per-task log handlers. Re-attaching the original
    handler list + level routes subsequent worker logging back through Airflow.

    Residual limitation: ``basicConfig(force=True)`` may have already CLOSED the
    original handler objects, so re-attaching them is best-effort (still
    strictly better than leaving ``basicConfig``'s lone stderr handler in
    place). For full process isolation in a long-lived worker, prefer
    ``invocation="subprocess"``.
    """
    # Lazy import: keeps ``import signalforge.airflow.runner`` from eagerly
    # pulling the whole CLI (and its dbt / warehouse / pydantic imports).
    from signalforge.cli import main

    saved_excepthook = sys.excepthook
    saved_env: dict[str, str | None] = {k: os.environ.get(k) for k in _ISOLATED_ENV_KEYS}
    _root = logging.getLogger()
    saved_handlers = _root.handlers[:]
    saved_level = _root.level
    saved_disabled = _root.disabled

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            try:
                exit_code = main(normalised_argv)
            except SystemExit as exc:
                exit_code = _exit_code_from_systemexit(exc, stderr_buf)
    finally:
        sys.excepthook = saved_excepthook
        for key, prior in saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        # Best-effort: re-attach the original handler list + level so worker
        # logging routes back through Airflow's handlers (which the CLI's
        # ``setup_logging`` torn down via ``basicConfig(force=True)``). The
        # original handlers may have been closed; see the docstring.
        _root.handlers[:] = saved_handlers
        _root.setLevel(saved_level)
        _root.disabled = saved_disabled

    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _run_subprocess(
    normalised_argv: list[str], timeout_seconds: float | None
) -> tuple[int, str, str]:
    """Run ``python -m signalforge`` in a fresh interpreter (full isolation).

    LIST form, NEVER ``shell=True`` — ``normalised_argv`` may contain operator
    / model strings, so a shell would be an injection seam. On a
    ``timeout_seconds`` overrun, :func:`subprocess.run` raises
    :class:`subprocess.TimeoutExpired`, which propagates unchanged (a runtime /
    external failure, not an operator misconfiguration — deliberately not
    wrapped in :class:`AirflowConfigError`).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "signalforge", *normalised_argv],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_diff_stdout(stdout: str, stdout_is_json: bool) -> dict[str, Any] | None:
    """Parse captured stdout as the ``DiffReport`` JSON object, best-effort.

    Returns ``None`` (parse skipped / failed) when the caller chose a non-json
    ``--format``, when stdout is empty (a non-zero exit may produce no diff),
    when the text is not valid JSON, or when the top-level value is not a JSON
    object. Never raises — a defensive parse so a failed run still yields a
    :class:`SignalForgeRunResult` carrying the real ``exit_code``.
    """
    if not stdout_is_json:
        return None
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _coerce_int(value: object) -> int:
    """Return ``value`` if it is a real (non-bool) int, else ``0``."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _coerce_float_or_none(value: object) -> float | None:
    """Return ``value`` coerced to ``float`` if numeric (non-bool), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _sidecar_path_if_exists(project_dir: Path, filename: str) -> str | None:
    """Return the canonicalised string path to a sidecar iff it exists on disk.

    Routes ``<project_dir>/.signalforge/<filename>`` through the
    symlink-hardened :func:`canonicalise_path` (mirrors how the diff / grade
    orchestrators read sidecars). Returns ``None`` on containment failure, on a
    bare ``OSError`` while resolving (e.g. ``PermissionError`` or a non-ELOOP
    resolve error — :func:`canonicalise_path` can raise these unwrapped), or
    when the file is absent (so ``--dry-run`` — which suppresses the sidecars —
    yields ``None``). Both sidecar-path lookups go through this helper, so the
    diff- and grade-sidecar checks degrade symmetrically (the grade read path
    already swallows ``OSError`` on the file read).
    """
    raw = project_dir / _SIGNALFORGE_DIR / filename
    try:
        canonical = canonicalise_path(raw, project_dir)
    except (PathContainmentError, OSError):
        return None
    return str(canonical) if canonical.exists() else None


def _read_grade_sidecar(project_dir: Path) -> tuple[str | None, float | None]:
    """Return ``(grade_sidecar_path, mean_grade)`` from the grade.json sidecar.

    * ``grade_sidecar_path`` is the canonicalised string path when grade.json
      exists, else ``None`` (so ``--no-grade`` / ``--dry-run`` → ``None``).
    * ``mean_grade`` is the sidecar's ``mean_score`` (a float) when present and
      parseable, else ``None``.

    Best-effort: a missing, unreadable, or unparseable sidecar degrades to
    ``None`` for ``mean_grade`` rather than crashing the run.
    """
    path_str = _sidecar_path_if_exists(project_dir, _GRADE_SIDECAR_NAME)
    if path_str is None:
        return None, None
    try:
        with open(path_str, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return path_str, None
    mean_grade: float | None = None
    if isinstance(data, dict):
        mean_grade = _coerce_float_or_none(data.get("mean_score"))
    return path_str, mean_grade


def run_signalforge(
    argv: list[str],
    *,
    project_dir: str | Path,
    invocation: Literal["in_process", "subprocess"] = "in_process",
    timeout_seconds: float | None = None,
) -> SignalForgeRunResult:
    """Run ``signalforge`` and parse its output into a :class:`SignalForgeRunResult`.

    Args:
        argv: The ``signalforge`` argument vector WITHOUT the program name —
            e.g. ``["generate", "models/.../stg_x.sql"]``. ``--format json``
            and ``--project-dir`` are injected by :func:`normalise_argv` when
            absent; an explicit ``--format <x>`` is honoured (a non-json format
            means the stdout parse is skipped).
        project_dir: The dbt project directory. Used to inject ``--project-dir``
            (when absent from ``argv``) and as the fallback root for the
            ``.signalforge/{diff,grade}.json`` sidecars. NOTE: sidecars are read
            from the EFFECTIVE ``--project-dir`` — if the caller passes an
            explicit ``--project-dir`` in ``argv`` that differs from this param,
            the sidecars are located under THAT path (the one the run used).
        invocation: ``"in_process"`` (default — call :func:`signalforge.cli.main`
            directly, with snapshot/restore of process-global state) or
            ``"subprocess"`` (shell out to ``python -m signalforge``, full
            isolation).
        timeout_seconds: Subprocess wall-clock timeout. Applies only to
            ``invocation="subprocess"``; an overrun raises
            :class:`subprocess.TimeoutExpired` (propagated unchanged).

    Returns:
        A frozen :class:`SignalForgeRunResult`. The :class:`TaskOutcome` is NOT
        computed here — that is the operator's call via
        :func:`~signalforge.airflow.result.decide_task_outcome` (US-003).

    Note:
        Single-model only (DEC-002): ``model_unique_ids`` carries at most the
        one model named in the diff JSON. A ``--select`` batch's stdout diff is
        the LAST model's render; aggregating a batch is a documented limitation.
    """
    # Reject an unknown ``invocation`` explicitly BEFORE running anything — a typo
    # must fail loud, not silently fall back to subprocess.
    if invocation not in ("in_process", "subprocess"):
        raise ValueError(
            f"Unsupported invocation={invocation!r}; expected 'in_process' or 'subprocess'."
        )

    normalised, stdout_is_json = normalise_argv(argv, project_dir)

    # Locate sidecars under the EFFECTIVE ``--project-dir`` actually passed to the
    # CLI (the caller's explicit value if present in ``argv``, else the injected
    # ``project_dir``). ``normalise_argv`` guarantees the flag is present, so a
    # caller who passes a different ``--project-dir`` than ``project_dir`` still
    # has its sidecars read from the path the run actually wrote them under.
    effective_project_dir_value, _ = _find_flag(normalised, "--project-dir")
    effective_project_dir = (
        Path(effective_project_dir_value) if effective_project_dir_value else Path(project_dir)
    )

    if invocation == "in_process":
        exit_code, stdout, stderr = _run_in_process(normalised)
    else:
        exit_code, stdout, stderr = _run_subprocess(normalised, timeout_seconds)

    report = _parse_diff_stdout(stdout, stdout_is_json)
    model_unique_ids: tuple[str, ...] = ()
    kept = kept_uncertain = dropped = flagged = 0
    duration_seconds: float | None = None
    if report is not None:
        model_uid = report.get("model_unique_id")
        if isinstance(model_uid, str) and model_uid:
            model_unique_ids = (model_uid,)
        kept = _coerce_int(report.get("kept_count"))
        kept_uncertain = _coerce_int(report.get("kept_uncertain_count"))
        dropped = _coerce_int(report.get("dropped_count"))
        flagged = _coerce_int(report.get("flagged_count"))
        duration_seconds = _coerce_float_or_none(report.get("duration_seconds"))

    diff_sidecar_path = _sidecar_path_if_exists(effective_project_dir, _DIFF_SIDECAR_NAME)
    grade_sidecar_path, mean_grade = _read_grade_sidecar(effective_project_dir)

    return SignalForgeRunResult(
        exit_code=exit_code,
        model_unique_ids=model_unique_ids,
        kept=kept,
        kept_uncertain=kept_uncertain,
        dropped=dropped,
        flagged=flagged,
        mean_grade=mean_grade,
        diff_sidecar_path=diff_sidecar_path,
        grade_sidecar_path=grade_sidecar_path,
        duration_seconds=duration_seconds,
        stdout=stdout,
        stderr=stderr,
    )


__all__ = ["normalise_argv", "run_signalforge"]
