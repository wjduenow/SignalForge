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

from typing import Any

from signalforge.airflow.errors import AirflowConfigError

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
