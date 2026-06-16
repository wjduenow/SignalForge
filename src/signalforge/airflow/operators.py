"""Placeholder SignalForge Airflow operator(s) — skeleton only (#230 US-002).

This module defines the stub :class:`SignalForgeGenerateOperator`. It is a plain
class that deliberately does NOT subclass Apache Airflow's ``BaseOperator`` at
module scope: a literal ``class X(BaseOperator)`` would force an eager
``from airflow ...`` import at import time and break the no-eager-import gate
(DEC-004 / DEC-006). Keeping the stub Airflow-free is what lets
``import signalforge.airflow.operators`` (and the lazy
``signalforge.airflow.SignalForgeGenerateOperator`` re-export) stay free of
``airflow`` in ``sys.modules``.

The real operator — which DOES subclass ``BaseOperator`` — lands with an
implementing child of epic #228. That child builds the subclass at runtime via
the lazy factory :func:`signalforge.airflow._airflow_compat.make_base_operator`,
so the airflow import stays confined to the one shim and out of module scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from signalforge.airflow.errors import AirflowConfigError
from signalforge.airflow.result import SignalForgeRunResult

_VALID_ON_FLAGGED: frozenset[str] = frozenset({"fail", "skip", "succeed"})


def _build_generate_argv(
    *,
    model: str | None,
    select: str | None,
    project_dir: str,
    profiles_dir: str | None,
    write: bool,
    no_grade: bool,
    cache_scope: str | None,
    as_of: str | None,
) -> list[str]:
    """Map operator params to a ``signalforge generate`` CLI argv list (pure).

    Builds the ``["generate", ...]`` argv consumed by
    :func:`signalforge.airflow.runner.run_signalforge`. Airflow-free, no I/O.

    Exactly one of ``model`` (positional) or ``select`` (``--select <expr>``) is
    expected — the caller guarantees the mutex via
    :func:`_validate_operator_config`; this builder simply prefers ``model`` when
    present and otherwise emits the ``--select`` form.

    Flag contract:

    * ``--format json`` is ALWAYS present — the runner parses the diff JSON off
      stdout.
    * ``--project-dir <project_dir>`` is ALWAYS present.
    * ``write=True`` → ``--write``; ``write=False`` → ``--dry-run`` (DEC-003 —
      the safe scheduled default writes nothing).
    * ``no_grade=True`` → ``--no-grade``.
    * ``as_of`` (non-empty) → ``--as-of <as_of>``.
    * ``cache_scope`` (non-``None``) → ``--cache-scope <cache_scope>``.
    * ``profiles_dir`` (non-empty) → ``--profiles-dir <profiles_dir>``.
    """
    argv: list[str] = ["generate"]
    if model is not None:
        argv.append(model)
    else:
        argv += ["--select", select if select is not None else ""]

    argv += ["--project-dir", project_dir, "--format", "json"]

    if write:
        argv.append("--write")
    else:
        argv.append("--dry-run")

    if no_grade:
        argv.append("--no-grade")
    if as_of:
        argv += ["--as-of", as_of]
    if cache_scope is not None:
        argv += ["--cache-scope", cache_scope]
    if profiles_dir:
        argv += ["--profiles-dir", profiles_dir]

    return argv


def _validate_operator_config(
    *,
    project_dir: str | None,
    model: str | None,
    select: str | None,
    on_flagged: str,
) -> None:
    """Validate operator params, raising :class:`AirflowConfigError` (DEC-009).

    Pure, airflow-free, no I/O. Runs BEFORE any ``run_signalforge`` call. Raises
    on:

    * empty / ``None`` ``project_dir``;
    * ``model`` and ``select`` both set OR both unset (mutex — exactly one);
    * a ``model`` / ``select`` value beginning with ``-`` (argv-injection guard);
    * ``on_flagged`` outside ``{"fail", "skip", "succeed"}``.
    """
    if not project_dir:
        raise AirflowConfigError("`project_dir` must be set (non-empty).")

    if (model is None) == (select is None):
        raise AirflowConfigError(
            "Exactly one of `model` or `select` must be set "
            f"(got model={model!r}, select={select!r})."
        )

    if model is not None and model.startswith("-"):
        raise AirflowConfigError(
            f"`model` must not begin with '-' (got {model!r}); refusing as an argv-injection guard."
        )
    if select is not None and select.startswith("-"):
        raise AirflowConfigError(
            f"`select` must not begin with '-' (got {select!r}); refusing as an "
            "argv-injection guard."
        )

    if on_flagged not in _VALID_ON_FLAGGED:
        raise AirflowConfigError(
            f"`on_flagged` must be one of {{fail, skip, succeed}} (got {on_flagged!r})."
        )


def _resolve_select_models(project_dir: str, select: str) -> tuple[str, ...]:
    """Resolve a ``--select`` expression to a sorted tuple of model unique_ids.

    Airflow-free (it does manifest I/O, but imports no ``airflow``). Loads the
    dbt manifest under ``project_dir`` via :func:`signalforge.manifest.load` and
    resolves ``select`` via :func:`signalforge.manifest.select_models`, returning
    the matched models' ``unique_id`` values as a sorted tuple (DEC-001 — the
    operator resolves the selector itself so it can loop ``run_signalforge`` once
    per model and aggregate accurate per-model XCom).

    Every failure maps to :class:`AirflowConfigError` (CLI tier 2, DEC-006),
    carrying the source exception's ``remediation`` when present and chaining via
    ``from exc``:

    * a manifest load failure (``ManifestNotFoundError`` /
      ``UnsupportedManifestVersionError`` / any other ``ManifestError`` — e.g. a
      symlink-escape ``ModelPathOutsideProjectError``);
    * a selector parse failure (``SelectorParseError``, itself a
      ``ManifestError`` subclass);
    * a zero-match selector (``select_models`` returns an empty tuple — the
      manifest layer does not raise on empty; the message names the selector).
    """
    # Lazy import: keeps `import signalforge.airflow.operators` (and the argv
    # builders above) free of the manifest package's import cost until a caller
    # actually resolves a selector. Airflow-free either way.
    from signalforge.manifest import load, select_models
    from signalforge.manifest.errors import ManifestError

    try:
        manifest = load(project_dir)
    except ManifestError as exc:
        raise AirflowConfigError(
            f"Failed to load the dbt manifest for project_dir "
            f"{project_dir!r}: {getattr(exc, 'message', exc)}",
            remediation=getattr(exc, "remediation", None),
        ) from exc

    try:
        matched = select_models(manifest, select)
    except ManifestError as exc:  # SelectorParseError (a ManifestError subclass)
        raise AirflowConfigError(
            f"Failed to parse the --select expression {select!r}: {getattr(exc, 'message', exc)}",
            remediation=getattr(exc, "remediation", None),
        ) from exc

    if not matched:
        raise AirflowConfigError(
            f"The --select expression {select!r} matched no models in the dbt "
            "project under project_dir "
            f"{project_dir!r}."
        )

    return tuple(sorted(model.unique_id for model in matched))


def _aggregate_batch_result(
    results: Sequence[SignalForgeRunResult],
) -> SignalForgeRunResult:
    """Roll up per-model batch results into ONE aggregate (pure, DEC-008).

    Airflow-free, no I/O. Given a non-empty sequence of
    :class:`SignalForgeRunResult` (one per model in a ``--select`` batch),
    returns a single aggregate that drives the one Airflow task state via
    :func:`signalforge.airflow.result.decide_task_outcome`:

    * ``exit_code`` = ``max`` over the per-model exit codes (the 4-tier severity
      ordering is just integer ``max`` — mirrors the CLI's
      ``_run_batch.total_exit_code``);
    * ``kept`` / ``kept_uncertain`` / ``dropped`` / ``flagged`` = element-wise
      sums;
    * ``model_unique_ids`` = concatenation, in input order, of each result's
      ``model_unique_ids`` (de-dup not required);
    * ``mean_grade`` = mean of the non-``None`` per-model means, or ``None`` when
      every per-model mean is ``None``;
    * ``diff_sidecar_path`` / ``grade_sidecar_path`` = ``None`` (a batch
      aggregate has no single sidecar);
    * ``duration_seconds`` = sum of the non-``None`` per-model durations, or
      ``None`` when every duration is ``None``;
    * ``stdout`` / ``stderr`` = empty strings (the aggregate carries no bulk
      text).

    Raises:
        ValueError: if ``results`` is empty (caller invariant: a batch always
            resolves at least one model before aggregation).
    """
    if not results:
        raise ValueError("_aggregate_batch_result requires a non-empty sequence of results.")

    model_unique_ids: tuple[str, ...] = tuple(
        uid for result in results for uid in result.model_unique_ids
    )

    grades = [r.mean_grade for r in results if r.mean_grade is not None]
    mean_grade = (sum(grades) / len(grades)) if grades else None

    durations = [r.duration_seconds for r in results if r.duration_seconds is not None]
    duration_seconds = sum(durations) if durations else None

    return SignalForgeRunResult(
        exit_code=max(r.exit_code for r in results),
        model_unique_ids=model_unique_ids,
        kept=sum(r.kept for r in results),
        kept_uncertain=sum(r.kept_uncertain for r in results),
        dropped=sum(r.dropped for r in results),
        flagged=sum(r.flagged for r in results),
        mean_grade=mean_grade,
        diff_sidecar_path=None,
        grade_sidecar_path=None,
        duration_seconds=duration_seconds,
        stdout="",
        stderr="",
    )


class SignalForgeGenerateOperator:
    """Stub for the future SignalForge ``generate`` Airflow operator.

    Skeleton placeholder (#230). Constructing it raises
    :class:`NotImplementedError` — the operator behaviour (a real
    ``BaseOperator`` subclass built via
    :func:`signalforge.airflow._airflow_compat.make_base_operator`) lands with a
    later epic-#228 child.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "SignalForgeGenerateOperator is a skeleton placeholder; the operator "
            "lands in a later epic #228 child. #230 ships the package skeleton only."
        )


__all__ = ["SignalForgeGenerateOperator"]
