"""The SignalForge ``generate`` Apache Airflow operator (#232 US-003).

This module ships :class:`SignalForgeGenerateOperator` — the operator a DAG
author wires to run ``signalforge generate`` (single model or a ``--select``
batch) as one Airflow task. The four pure helpers below
(:func:`_build_generate_argv`, :func:`_validate_operator_config`,
:func:`_resolve_select_models`, :func:`_aggregate_batch_result`) are the
Airflow-free machinery the operator drives.

**Deferred class construction (the load-bearing structural constraint).** The
real operator must subclass Apache Airflow's ``BaseOperator``, which requires
``airflow`` at runtime — but importing THIS module must stay Airflow-free, so the
ungated helper tests and the no-eager-import / import-confinement gates keep
passing with Airflow absent (DEC-004 / DEC-006 / DEC-007). So there is NO
module-scope ``class X(BaseOperator)``. Instead a PEP 562 module-level
:func:`__getattr__` resolves the ``SignalForgeGenerateOperator`` name lazily via
:func:`_get_generate_operator_class`:

* When Apache Airflow is installed, the cached factory
  :func:`_make_generate_operator_class` builds the real ``BaseOperator``
  subclass at access time — the ``from airflow ...`` import stays confined to the
  one shim (:func:`signalforge.airflow._airflow_compat.make_base_operator`), out
  of module scope.
* When Airflow is NOT installed, the name resolves (without importing airflow —
  via :func:`importlib.util.find_spec`, which does not execute the module) to the
  airflow-free placeholder :class:`_GenerateOperatorAirflowMissing`, whose
  construction raises :class:`ModuleNotFoundError`. This keeps the no-eager
  gate's ``from signalforge.airflow import SignalForgeGenerateOperator;
  assert ... is not None`` (with no airflow in ``sys.modules``) green: attribute
  ACCESS is airflow-free; only CONSTRUCTION of the real operator needs airflow.

The ``find_spec`` guard is a deliberate, documented extension of the bare
"factory builds the real class" shape: it is what lets attribute access stay
airflow-free so the no-eager-import gate keeps passing without the ``[airflow]``
extra installed.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from signalforge.airflow._airflow_compat import make_base_operator, raise_for_outcome
from signalforge.airflow.errors import AirflowConfigError
from signalforge.airflow.result import (
    OnFlagged,
    SignalForgeRunResult,
    decide_task_outcome,
)
from signalforge.airflow.runner import run_signalforge

if TYPE_CHECKING:
    # Type-checker-only declaration of the operator name. At runtime the class is
    # built by the find_spec-guarded factory below (it subclasses Apache
    # Airflow's ``BaseOperator``, which is NOT a typecheck dependency), and the
    # public name is resolved through the module-level :func:`__getattr__`. This
    # block exists so ``signalforge.airflow.__init__``'s ``TYPE_CHECKING``
    # re-export of the name resolves and ``__all__`` is satisfied under pyright.
    class SignalForgeGenerateOperator:  # noqa: D401 - type stub only
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        def execute(self, context: Any) -> Any: ...


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


class _GenerateOperatorAirflowMissing:
    """Stand-in for :class:`SignalForgeGenerateOperator` when Airflow is absent.

    Resolving ``signalforge.airflow.SignalForgeGenerateOperator`` without the
    ``[airflow]`` optional extra installed returns THIS class (attribute access
    stays airflow-free, keeping the no-eager-import gate green). Constructing it
    raises :class:`ModuleNotFoundError` (an :class:`ImportError` subclass) naming
    the remediation — the real operator subclasses ``BaseOperator`` and so genuinely
    requires Airflow at construction time.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise ModuleNotFoundError(
            "SignalForgeGenerateOperator requires Apache Airflow, which is not "
            "installed. Install the optional extra: "
            "pip install 'signalforge-dbt[airflow]'."
        )


# Cache for the resolved operator class — the real ``BaseOperator`` subclass when
# Airflow is installed, else the airflow-free placeholder above. Built once on
# first attribute access and reused (a process either has Airflow or it does not,
# so the cached choice is stable for the process lifetime).
_GENERATE_OPERATOR_CLASS: type | None = None


def _make_generate_operator_class() -> type:  # pragma: no cover - requires the [airflow] extra
    """Build and return the real ``BaseOperator``-subclassing operator class.

    Reached only when Apache Airflow is installed (guarded by
    :func:`_get_generate_operator_class`'s ``find_spec`` check). The base class is
    obtained via the one shim
    :func:`signalforge.airflow._airflow_compat.make_base_operator`, so the
    ``from airflow ...`` import stays confined there and out of this module's
    scope (DEC-004 / DEC-007). Marked ``# pragma: no cover`` because its body —
    the ``BaseOperator`` subclass and its ``execute`` — runs only under the gated
    ``[airflow]`` extra (the gated ``tests/airflow/test_operators.py`` exercise
    it); the default coverage env never installs Airflow.
    """
    _Base = make_base_operator()

    class SignalForgeGenerateOperator(_Base):  # type: ignore[valid-type, misc]
        """Run ``signalforge generate`` as one Apache Airflow task.

        Wraps the Airflow-free :func:`signalforge.airflow.runner.run_signalforge`
        seam: maps operator params to a ``signalforge generate`` argv, runs it,
        maps the result to a :class:`~signalforge.airflow.result.TaskOutcome` via
        the pure :func:`~signalforge.airflow.result.decide_task_outcome`, and
        translates that outcome into the matching Airflow signal via
        :func:`~signalforge.airflow._airflow_compat.raise_for_outcome`. Returns
        the run's XCom payload (counts + sidecar paths).

        Single model vs. ``--select`` batch:

        * Exactly one of ``model`` (a positional model arg) or ``select`` (a
          ``--select`` selector expression) is set (mutex, validated).
        * For a batch, the operator resolves the selector to its model ids itself
          and loops ``run_signalforge`` once per model (DEC-001), then aggregates
          the per-model results into one task outcome (DEC-008) and returns a
          ``{"models": [...], "aggregate": ...}`` XCom (DEC-010). When the
          operator's ``cache_scope`` is unset and the batch has ≥2 models, it is
          forced to ``"project"`` (DEC-007) so the Anthropic cached prefix
          amortises across the siblings.
        """

        # Airflow renders these fields from the task context before ``execute``
        # (DEC-005), so a DAG author can template e.g. ``as_of="{{ ds }}"``.
        template_fields = ("project_dir", "select", "model", "profiles_dir", "as_of")

        def __init__(
            self,
            *,
            task_id: str,
            project_dir: str,
            model: str | None = None,
            select: str | None = None,
            profiles_dir: str | None = None,
            write: bool = False,
            no_grade: bool = False,
            cache_scope: str | None = None,
            as_of: str | None = None,
            on_flagged: OnFlagged = "fail",
            invocation: Literal["in_process", "subprocess"] = "in_process",
            **kwargs: Any,
        ) -> None:
            # BaseOperator owns ``task_id`` + the standard Airflow kwargs
            # (``retries`` / ``retry_delay`` / ``depends_on_past`` / ...).
            super().__init__(task_id=task_id, **kwargs)
            self.project_dir = project_dir
            self.model = model
            self.select = select
            self.profiles_dir = profiles_dir
            self.write = write
            self.no_grade = no_grade
            self.cache_scope = cache_scope
            self.as_of = as_of
            # Annotate the Literal-typed attrs explicitly so pyright does NOT
            # widen them to ``str`` on assignment (which would break the typed
            # ``decide_task_outcome`` / ``run_signalforge`` calls below).
            self.on_flagged: OnFlagged = on_flagged
            self.invocation: Literal["in_process", "subprocess"] = invocation
            # Fail fast at DAG-parse / instantiation time. The leading-dash
            # argv-injection guard is harmless here: an un-rendered Jinja
            # template (e.g. ``"{{ ds }}"``) never begins with ``-``, so it
            # cannot false-trip. ``execute`` re-validates the RENDERED values
            # (where the dash guard is meaningful) before building any argv.
            _validate_operator_config(
                project_dir=project_dir,
                model=model,
                select=select,
                on_flagged=on_flagged,
            )

        def execute(self, context: Any) -> dict[str, object]:
            # Re-validate the now-rendered template_fields (DEC-005): the
            # leading-dash argv-injection guard is meaningful only once Jinja has
            # rendered ``model`` / ``select`` / ``project_dir`` to their final
            # values.
            _validate_operator_config(
                project_dir=self.project_dir,
                model=self.model,
                select=self.select,
                on_flagged=self.on_flagged,
            )

            if self.select is None:
                return self._execute_single()
            return self._execute_batch()

        def _execute_single(self) -> dict[str, object]:
            argv = _build_generate_argv(
                model=self.model,
                select=None,
                project_dir=self.project_dir,
                profiles_dir=self.profiles_dir,
                write=self.write,
                no_grade=self.no_grade,
                cache_scope=self.cache_scope,
                as_of=self.as_of,
            )
            result = run_signalforge(argv, project_dir=self.project_dir, invocation=self.invocation)
            outcome = decide_task_outcome(result, on_flagged=self.on_flagged)
            raise_for_outcome(
                outcome,
                message=(
                    f"signalforge generate for {self.model!r} produced "
                    f"task outcome {outcome.value} "
                    f"(exit_code={result.exit_code}, flagged={result.flagged})."
                ),
            )
            return result.to_xcom()

        def _execute_batch(self) -> dict[str, object]:
            assert self.select is not None  # narrowed by execute()
            model_ids = _resolve_select_models(self.project_dir, self.select)
            # DEC-007: force project-scope caching for a ≥2-model batch when the
            # operator did not pin a scope, so the Anthropic cached prefix
            # amortises across the siblings instead of paying creation per model.
            resolved_cache_scope = self.cache_scope
            if resolved_cache_scope is None and len(model_ids) >= 2:
                resolved_cache_scope = "project"

            results: list[SignalForgeRunResult] = []
            for model_id in model_ids:
                argv = _build_generate_argv(
                    model=model_id,
                    select=None,
                    project_dir=self.project_dir,
                    profiles_dir=self.profiles_dir,
                    write=self.write,
                    no_grade=self.no_grade,
                    cache_scope=resolved_cache_scope,
                    as_of=self.as_of,
                )
                results.append(
                    run_signalforge(argv, project_dir=self.project_dir, invocation=self.invocation)
                )

            aggregate = _aggregate_batch_result(results)
            outcome = decide_task_outcome(aggregate, on_flagged=self.on_flagged)
            raise_for_outcome(
                outcome,
                message=(
                    f"signalforge generate --select {self.select!r} over "
                    f"{len(model_ids)} model(s) produced task outcome "
                    f"{outcome.value} (exit_code={aggregate.exit_code}, "
                    f"flagged={aggregate.flagged})."
                ),
            )
            return {
                "models": [r.to_xcom() for r in results],
                "aggregate": aggregate.to_xcom(),
            }

    return SignalForgeGenerateOperator


def _get_generate_operator_class() -> type:
    """Resolve (and cache) the operator class without importing airflow eagerly.

    Attribute ACCESS must stay airflow-free so the no-eager-import gate passes
    with the ``[airflow]`` extra absent. :func:`importlib.util.find_spec` checks
    Airflow availability WITHOUT executing/importing it (it does not add
    ``airflow`` to ``sys.modules``). When Airflow is present, build the real
    ``BaseOperator`` subclass; when absent, return the airflow-free placeholder
    whose construction raises :class:`ModuleNotFoundError`.
    """
    global _GENERATE_OPERATOR_CLASS
    if _GENERATE_OPERATOR_CLASS is None:
        if importlib.util.find_spec("airflow") is None:
            _GENERATE_OPERATOR_CLASS = _GenerateOperatorAirflowMissing
        else:  # pragma: no cover - requires the [airflow] extra
            _GENERATE_OPERATOR_CLASS = _make_generate_operator_class()
    return _GENERATE_OPERATOR_CLASS


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolution of the public ``SignalForgeGenerateOperator`` name.

    Building the real ``BaseOperator`` subclass at module scope would force an
    eager ``from airflow ...`` import; resolving the name here (via the
    find_spec-guarded :func:`_get_generate_operator_class`) keeps attribute access
    airflow-free while still yielding the real operator when Airflow is installed.
    """
    if name == "SignalForgeGenerateOperator":
        return _get_generate_operator_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["SignalForgeGenerateOperator"]
