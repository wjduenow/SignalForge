"""The SignalForge Apache Airflow operators (#232 US-003, #233 US-002).

This module ships two operators a DAG author wires as one Airflow task each:

* :class:`SignalForgeGenerateOperator` — runs ``signalforge generate`` (single
  model or a ``--select`` batch). The four pure helpers below
  (:func:`_build_generate_argv`, :func:`_validate_operator_config`,
  :func:`_resolve_select_models`, :func:`_aggregate_batch_result`) are the
  Airflow-free machinery it drives.
* :class:`SignalForgePruneExistingOperator` — runs ``signalforge prune-existing``
  (ingest -> prune -> diff, **no LLM call**, read-only; #233). Its pure helpers
  are :func:`_build_prune_existing_argv` + :func:`_validate_prune_existing_config`.
  Single-model only — no batch apparatus (#233 DEC-003).

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

import contextlib
import functools
import importlib.util
import json
import logging
import os
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any, Literal

from signalforge.airflow._airflow_compat import (
    make_base_operator,
    raise_for_outcome,
    register_secret,
)
from signalforge.airflow._resolve import HookResolution
from signalforge.airflow.errors import AirflowConfigError
from signalforge.airflow.result import (
    OnFlagged,
    SignalForgeRunResult,
    decide_task_outcome,
)
from signalforge.airflow.runner import run_signalforge
from signalforge.llm.providers import PROVIDER_ENV_VAR_KEYS

_LOGGER = logging.getLogger("signalforge.airflow")

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

    class SignalForgePruneExistingOperator:  # noqa: D401 - type stub only
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        def execute(self, context: Any) -> Any: ...


_VALID_ON_FLAGGED: frozenset[str] = frozenset({"fail", "skip", "succeed"})


# --------------------------------------------------------------------------- #
# Shared `signalforge_conn_id` wiring helpers (#234 US-005)                    #
#                                                                             #
# Two of these are airflow-free + ungated-testable (the precedence merge and  #
# the env-injection context manager); the third constructs the gated hook and #
# is reached only on the conn-id path inside ``execute``. US-006 reuses the    #
# airflow-free merge for the prune-existing operator's profiles_dir-only       #
# precedence (it needs NO key injection), so the two concerns are kept         #
# deliberately decoupled (DEC-016).                                            #
# --------------------------------------------------------------------------- #


def _merge_with_resolution(param_value: str | None, extra_value: str | None) -> str | None:
    """Precedence merge for a value present on both the operator + Connection (DEC-012).

    Pure, airflow-free. Returns the explicit operator ``param_value`` when it is
    truthy (an explicitly-set, non-empty operator param), else the
    ``extra_value`` resolved from the Connection ``extra`` (itself possibly
    ``None``). Mirrors the CLI's ``flag > YAML > default`` precedence; the final
    fall-through to the tool default (when both are ``None``) is left to the
    downstream CLI. Used for ``profiles_dir`` (both operators) and ``cache_scope``
    (Generate only).
    """
    return param_value if param_value else extra_value


@contextlib.contextmanager
def _provider_key_env(env_var: str, api_key: str) -> Iterator[None]:
    """Inject ``os.environ[env_var] = api_key`` for the block, restoring on exit.

    Pure (touches only ``os.environ``), airflow-free, ungated-testable. Snapshots
    the prior value and, in a ``finally`` that fires on BOTH normal completion
    and an exception raised inside the block, restores it: if the var was absent
    before it is deleted; if it had a prior value (including an empty string)
    that value is restored. This keeps a resolved LLM credential from lingering
    in a long-lived Airflow worker's environment across tasks (DEC-015).

    ``os.environ`` values are always ``str``, so a ``None`` from ``.get`` means
    the var was genuinely absent — distinguishing it cleanly from a present
    empty-string value (which round-trips as ``""``).
    """
    prior = os.environ.get(env_var)
    os.environ[env_var] = api_key
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = prior


def _resolve_hook(  # pragma: no cover - needs [airflow]
    conn_id: str, project_dir: str | None = None
) -> HookResolution:
    """Construct the SignalForge hook and resolve its Connection (gated helper).

    Reused by both operators' ``execute`` (US-005 / US-006). Constructing
    :class:`signalforge.airflow.hooks.SignalForgeHook` requires Apache Airflow
    (it subclasses ``BaseHook``), so this helper is reached only on the
    ``signalforge_conn_id``-set path and is ``# pragma: no cover`` for the same
    reason as the operator factories. The hook import is lazy — its
    module-``__getattr__`` resolves the real class without eagerly importing
    airflow — so ``import signalforge.airflow.operators`` stays airflow-free.

    ``project_dir`` is the operator's project root: it is threaded into
    ``get_conn`` so a Connection-supplied ``extra.profiles_dir`` is
    symlink-contained against the project tree (DEC-008). The operator always
    knows its ``project_dir``, so the containment anchor is always supplied on
    the operator path.
    """
    from signalforge.airflow.hooks import SignalForgeHook

    return SignalForgeHook(conn_id).get_conn(project_dir=project_dir)


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
    * ``cache_scope`` (non-empty) → ``--cache-scope <cache_scope>``.
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
    if cache_scope:
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
    * a ``model`` / ``select`` that is set but blank or not a ``str`` (a blank
      value must NOT satisfy the mutex — ``model=""`` is "unset", not "set");
    * ``model`` and ``select`` both set OR both unset (mutex — exactly one);
    * a ``model`` / ``select`` value beginning with ``-`` (argv-injection guard);
    * ``on_flagged`` outside ``{"fail", "skip", "succeed"}``.
    """
    if not project_dir:
        raise AirflowConfigError("`project_dir` must be set (non-empty).")

    # A provided model/select must be a non-blank str BEFORE the mutex: a blank
    # string would otherwise pass `is not None` and bypass the exactly-one check,
    # then emit an empty positional / `--select ""` into the argv.
    for label, value in (("model", model), ("select", select)):
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise AirflowConfigError(
                f"`{label}` must be a non-empty string when set (got {value!r})."
            )
        if value.startswith("-"):
            raise AirflowConfigError(
                f"`{label}` must not begin with '-' (got {value!r}); refusing as an "
                "argv-injection guard."
            )

    if (model is None) == (select is None):
        raise AirflowConfigError(
            "Exactly one of `model` or `select` must be set "
            f"(got model={model!r}, select={select!r})."
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


def _build_prune_existing_argv(
    *,
    model: str,
    schema: str,
    project_dir: str,
    profiles_dir: str | None,
    manifest: str | None,
    scope: str | None,
    sample_strategy: str | None,
    as_of: str | None,
    tests_dir: str | None,
) -> list[str]:
    """Map operator params to a ``signalforge prune-existing`` argv list (pure).

    Builds the ``["prune-existing", ...]`` argv consumed by
    :func:`signalforge.airflow.runner.run_signalforge` for the no-LLM
    ingest -> prune -> diff path (#233 DEC-005). Airflow-free, no I/O.

    Flag contract:

    * The positional ``<model>`` is ALWAYS the second token (single-model
      only — ``prune-existing`` has no ``--select``; #233 DEC-003).
    * ``--schema <schema>`` is ALWAYS present (the hand-authored
      ``schema.yml`` to prune; #233 DEC-002).
    * ``--project-dir <project_dir>`` is ALWAYS present.
    * ``--format json`` is ALWAYS present — the runner parses the diff JSON
      off stdout.
    * ``--dry-run`` is ALWAYS present — the operator is a read-only monitor;
      the JSON transport is stdout (#231 DEC-005), not the suppressed
      sidecar. There is NO ``write=True`` branch (#233 DEC-001 — read-only).
    * ``profiles_dir`` (non-empty) → ``--profiles-dir <profiles_dir>``.
    * ``manifest`` (non-empty) → ``--manifest <manifest>``.
    * ``scope`` (non-empty) → ``--scope <scope>``.
    * ``sample_strategy`` (non-empty) → ``--sample-strategy <sample_strategy>``.
    * ``as_of`` (non-empty) → ``--as-of <as_of>``.
    * ``tests_dir`` (non-empty) → ``--tests-dir <tests_dir>`` (#233 DEC-006 —
      singular ``tests/*.sql`` ingestion).

    Each optional flag is omitted entirely when its value is ``None`` (or an
    empty string), so a falsy override never emits a bare flag.
    """
    argv: list[str] = [
        "prune-existing",
        model,
        "--schema",
        schema,
        "--project-dir",
        project_dir,
        "--format",
        "json",
        "--dry-run",
    ]
    if profiles_dir:
        argv += ["--profiles-dir", profiles_dir]
    if manifest:
        argv += ["--manifest", manifest]
    if scope:
        argv += ["--scope", scope]
    if sample_strategy:
        argv += ["--sample-strategy", sample_strategy]
    if as_of:
        argv += ["--as-of", as_of]
    if tests_dir:
        argv += ["--tests-dir", tests_dir]
    return argv


def _validate_prune_existing_config(
    *,
    project_dir: str | None,
    model: str | None,
    schema: str | None,
    on_flagged: str,
) -> None:
    """Validate prune-existing params, raising :class:`AirflowConfigError` (#233 DEC-001/002/004).

    Pure, airflow-free, no I/O. Runs BEFORE any ``run_signalforge`` call.
    There are deliberately NO ``write`` / ``mode`` params on this operator —
    ``prune-existing`` is read-only (no ``--write``) and makes no LLM call
    (``--mode`` is inert; #233 DEC-001). Raises on:

    * empty / ``None`` ``project_dir``;
    * empty / ``None`` (or non-``str`` / blank) ``schema`` — ``--schema`` is a
      required operator param (#233 DEC-002);
    * empty / ``None`` (or non-``str`` / blank) ``model`` — the single
      positional model is required (no ``--select``);
    * a ``model`` / ``schema`` value beginning with ``-`` (argv-injection
      guard);
    * ``on_flagged`` outside ``{"fail", "skip", "succeed"}`` (retained for
      symmetry with the sibling operators but documented inert without
      grading; #233 DEC-004).
    """
    if not project_dir:
        raise AirflowConfigError("`project_dir` must be set (non-empty).")

    # ``model`` and ``schema`` are BOTH required for the single-model,
    # required-schema prune-existing surface. A provided value must be a
    # non-blank str BEFORE the leading-dash check: a blank / non-str value
    # would otherwise emit an empty positional / ``--schema ""`` into the
    # argv (or crash the ``.startswith`` guard).
    for label, value in (("model", model), ("schema", schema)):
        if not isinstance(value, str) or not value.strip():
            raise AirflowConfigError(f"`{label}` must be a non-empty string (got {value!r}).")
        if value.startswith("-"):
            raise AirflowConfigError(
                f"`{label}` must not begin with '-' (got {value!r}); refusing as an "
                "argv-injection guard."
            )

    if on_flagged not in _VALID_ON_FLAGGED:
        raise AirflowConfigError(
            f"`on_flagged` must be one of {{fail, skip, succeed}} (got {on_flagged!r})."
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


class _PruneExistingOperatorAirflowMissing:
    """Stand-in for :class:`SignalForgePruneExistingOperator` when Airflow is absent.

    Mirrors :class:`_GenerateOperatorAirflowMissing` exactly. Resolving
    ``signalforge.airflow.SignalForgePruneExistingOperator`` without the
    ``[airflow]`` optional extra installed returns THIS class (attribute access
    stays airflow-free, keeping the no-eager-import gate green). Constructing it
    raises :class:`ModuleNotFoundError` (an :class:`ImportError` subclass) naming
    the remediation — the real operator subclasses ``BaseOperator`` and so
    genuinely requires Airflow at construction time.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise ModuleNotFoundError(
            "SignalForgePruneExistingOperator requires Apache Airflow, which is not "
            "installed. Install the optional extra: "
            "pip install 'signalforge-dbt[airflow]'."
        )


# Cache for the resolved operator class — the real ``BaseOperator`` subclass when
# Airflow is installed, else the airflow-free placeholder above. Built once on
# first attribute access and reused (a process either has Airflow or it does not,
# so the cached choice is stable for the process lifetime).


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

        Airflow-native credentials (#234, optional ``signalforge_conn_id``):

        * When ``signalforge_conn_id`` is ``None`` (the default), the operator is
          byte-identical to #232 — it relies on ambient env / inline config and
          never touches the hook (DEC-014).
        * When set, ``execute`` resolves the Connection via
          :class:`signalforge.airflow.hooks.SignalForgeHook`, REQUIRES both an LLM
          ``provider`` and an ``api_key`` (``generate`` calls the LLM; DEC-003),
          masks the key (DEC-006), precedence-merges ``profiles_dir`` /
          ``cache_scope`` (param > Connection ``extra``; DEC-012), and injects the
          provider's API-key env var only for the duration of the run — restoring
          it afterward so the secret never lingers across tasks (DEC-015). The
          conn id is NOT a ``template_fields`` entry and never enters XCom
          (DEC-007).
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
            signalforge_conn_id: str | None = None,
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
            # A conn id is NOT a templated value and is deliberately kept OUT of
            # ``template_fields`` (DEC-007): templating a credential reference is
            # an avoidable leak surface.
            self.signalforge_conn_id = signalforge_conn_id
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

            # No ``signalforge_conn_id`` → byte-identical to #232 (DEC-014): no
            # hook, no credential resolution, no env injection.
            effective_profiles_dir = self.profiles_dir
            effective_cache_scope = self.cache_scope
            env_cm: contextlib.AbstractContextManager[None] = contextlib.nullcontext()

            if self.signalforge_conn_id is not None:
                resolution = _resolve_hook(self.signalforge_conn_id, self.project_dir)
                # ``generate`` calls the LLM, so provider + key are REQUIRED here
                # (DEC-003 — the resolver is lenient; the consumer enforces
                # requiredness). PruneExisting, by contrast, needs neither.
                if resolution.provider is None or resolution.api_key is None:
                    raise AirflowConfigError(
                        f"The SignalForge Airflow Connection {self.signalforge_conn_id!r} "
                        "did not supply both an LLM `provider` and an API key, which "
                        "`signalforge generate` requires (it calls the LLM).",
                        remediation=(
                            "Set the Connection `extra.provider` to one of "
                            "anthropic / openai / gemini and put the API key in the "
                            "Connection `password` (or the `signalforge_api_key` Airflow "
                            "Variable). For a credential-free task, use "
                            "SignalForgePruneExistingOperator instead."
                        ),
                    )
                # Mask the resolved key in Airflow's logs BEFORE any logging or
                # run (DEC-006), belt-and-braces over Airflow's auto-masking.
                register_secret(resolution.api_key)
                # Precedence merge (DEC-012): explicit param > Connection extra.
                effective_profiles_dir = _merge_with_resolution(
                    self.profiles_dir, resolution.profiles_dir
                )
                effective_cache_scope = _merge_with_resolution(
                    self.cache_scope, resolution.cache_scope
                )
                # NEVER log the api_key (DEC-007). ``key_source`` is not exposed
                # by the resolution, so it is omitted.
                _LOGGER.info(
                    "signalforge generate resolved airflow connection: %s",
                    json.dumps(
                        {
                            "conn_id": self.signalforge_conn_id,
                            "provider": resolution.provider,
                            "profiles_dir_set": effective_profiles_dir is not None,
                        }
                    ),
                )
                env_cm = _provider_key_env(
                    PROVIDER_ENV_VAR_KEYS[resolution.provider], resolution.api_key
                )

            # The provider env var is injected only for the duration of the run
            # and restored in the context manager's ``finally`` (success OR
            # exception) so the secret never lingers across tasks (DEC-015).
            with env_cm:
                if self.select is None:
                    return self._execute_single(effective_profiles_dir, effective_cache_scope)
                return self._execute_batch(effective_profiles_dir, effective_cache_scope)

        def _execute_single(
            self, profiles_dir: str | None, cache_scope: str | None
        ) -> dict[str, object]:
            argv = _build_generate_argv(
                model=self.model,
                select=None,
                project_dir=self.project_dir,
                profiles_dir=profiles_dir,
                write=self.write,
                no_grade=self.no_grade,
                cache_scope=cache_scope,
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

        def _execute_batch(
            self, profiles_dir: str | None, cache_scope: str | None
        ) -> dict[str, object]:
            assert self.select is not None  # narrowed by execute()
            model_ids = _resolve_select_models(self.project_dir, self.select)
            # DEC-007: force project-scope caching for a ≥2-model batch when the
            # operator did not pin a scope (after the Connection-extra precedence
            # merge), so the Anthropic cached prefix amortises across the siblings
            # instead of paying creation per model.
            resolved_cache_scope = cache_scope
            if resolved_cache_scope is None and len(model_ids) >= 2:
                resolved_cache_scope = "project"

            results: list[SignalForgeRunResult] = []
            for model_id in model_ids:
                # model_id is a manifest-validated unique_id (dbt's
                # ``model.<pkg>.<name>`` grammar), not operator-supplied text, so
                # the leading-dash argv-injection guard from
                # _validate_operator_config does not apply here — it cannot begin
                # with ``-``.
                argv = _build_generate_argv(
                    model=model_id,
                    select=None,
                    project_dir=self.project_dir,
                    profiles_dir=profiles_dir,
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
            # Per-model sidecar paths are NOT stable across a batch: under
            # write=True every model overwrites the same `.signalforge/*.json`
            # (O_TRUNC last-writer-wins), so a per-model path would point at a
            # file holding a DIFFERENT model's diff. Null them in the per-model
            # XCom rather than ship a misleading path; operators needing stable
            # per-model sidecars run per-model with a per-run project dir
            # (see docs/airflow-ops.md). Counts/grades stay per-model and honest.
            return {
                "models": [_without_sidecar_paths(r.to_xcom()) for r in results],
                "aggregate": aggregate.to_xcom(),
            }

    return SignalForgeGenerateOperator


def _without_sidecar_paths(xcom: dict[str, object]) -> dict[str, object]:
    """Return a copy of a ``to_xcom()`` dict with the sidecar paths nulled.

    Used for per-model batch XCom: a batch shares one sidecar location, so the
    per-model paths are unstable (last-writer-wins) and would mislead.
    """
    return {**xcom, "diff_sidecar_path": None, "grade_sidecar_path": None}


@functools.cache
def _get_generate_operator_class() -> type:
    """Resolve (and cache) the operator class without importing airflow eagerly.

    Attribute ACCESS must stay airflow-free so the no-eager-import gate passes
    with the ``[airflow]`` extra absent. :func:`importlib.util.find_spec` checks
    Airflow availability WITHOUT executing/importing it (it does not add
    ``airflow`` to ``sys.modules``). When Airflow is present, build the real
    ``BaseOperator`` subclass; when absent, return the airflow-free placeholder
    whose construction raises :class:`ModuleNotFoundError`.
    """
    if importlib.util.find_spec("airflow") is None:
        return _GenerateOperatorAirflowMissing
    return _make_generate_operator_class()  # pragma: no cover - requires [airflow]


def _make_prune_existing_operator_class() -> type:  # pragma: no cover - requires [airflow]
    """Build and return the real ``BaseOperator``-subclassing prune-existing operator.

    Sibling of :func:`_make_generate_operator_class` (#233 DEC-008). Reached only
    when Apache Airflow is installed (guarded by
    :func:`_get_prune_existing_operator_class`'s ``find_spec`` check). The base
    class comes from the one shim
    :func:`signalforge.airflow._airflow_compat.make_base_operator`, so the
    ``from airflow ...`` import stays confined there and out of this module's
    scope (DEC-008). Marked ``# pragma: no cover`` because its body — the
    ``BaseOperator`` subclass and its ``execute`` — runs only under the gated
    ``[airflow]`` extra (the gated ``tests/airflow/test_operators.py`` exercise
    it); the default coverage env never installs Airflow.
    """
    _Base = make_base_operator()

    class SignalForgePruneExistingOperator(_Base):  # type: ignore[valid-type, misc]
        """Run ``signalforge prune-existing`` as one Apache Airflow task (no LLM).

        Wraps the Airflow-free :func:`signalforge.airflow.runner.run_signalforge`
        seam for the no-LLM ingest -> prune -> diff path (#233): maps operator
        params to a ``signalforge prune-existing`` argv, runs it, maps the result
        to a :class:`~signalforge.airflow.result.TaskOutcome` via the pure
        :func:`~signalforge.airflow.result.decide_task_outcome`, and translates
        that outcome into the matching Airflow signal via
        :func:`~signalforge.airflow._airflow_compat.raise_for_outcome`. Returns
        the run's XCom payload (counts + sidecar paths).

        Single-model only (#233 DEC-003): ``prune-existing`` takes one positional
        ``<model>`` and has no ``--select`` — so there is NO batch apparatus
        (no selector resolution, no per-model loop, no aggregation, no
        ``--cache-scope``). One ``run_signalforge`` call, one result, one XCom.

        Read-only by design (#233 DEC-001): no ``write`` / ``mode`` param, no
        Anthropic credential required. ``on_flagged`` is accepted for symmetry
        with the sibling operators but is inert today — ``prune-existing`` does
        no grading, so there is never a ``flagged`` tier and a clean (exit-0) run
        always yields ``SUCCESS`` regardless of ``on_flagged`` (#233 DEC-004).

        Airflow-native credentials (#234, optional ``signalforge_conn_id``):

        * When ``signalforge_conn_id`` is ``None`` (the default), the operator is
          byte-identical to #233 — it relies on ambient env / inline config and
          never touches the hook (DEC-014).
        * When set, ``execute`` resolves the Connection via
          :class:`signalforge.airflow.hooks.SignalForgeHook` and uses ONLY the
          resolved ``profiles_dir`` (warehouse auth), precedence-merged
          param > Connection ``extra`` (DEC-012). Because ``prune-existing`` makes
          NO LLM call (DEC-016), this path does NOT call ``register_secret``,
          injects NO provider env var, and ignores ``provider`` / ``api_key`` /
          ``cache_scope`` on the resolution — though an allowlist-invalid
          ``provider`` in the Connection ``extra`` still raises ``AirflowConfigError``
          (that check lives in the resolver, DEC-005). The conn id is NOT a
          ``template_fields`` entry and never enters XCom (DEC-007).
        """

        # Airflow renders these fields from the task context before ``execute``
        # (DEC-007), so a DAG author can template e.g. ``as_of="{{ ds }}"`` or
        # ``schema="{{ var.value.schema_path }}"``.
        template_fields = (
            "project_dir",
            "model",
            "schema",
            "profiles_dir",
            "as_of",
            "tests_dir",
        )

        def __init__(
            self,
            *,
            task_id: str,
            project_dir: str,
            model: str,
            schema: str,
            profiles_dir: str | None = None,
            manifest: str | None = None,
            scope: str | None = None,
            sample_strategy: str | None = None,
            as_of: str | None = None,
            tests_dir: str | None = None,
            signalforge_conn_id: str | None = None,
            on_flagged: OnFlagged = "fail",
            invocation: Literal["in_process", "subprocess"] = "in_process",
            **kwargs: Any,
        ) -> None:
            # BaseOperator owns ``task_id`` + the standard Airflow kwargs
            # (``retries`` / ``retry_delay`` / ``depends_on_past`` / ...).
            super().__init__(task_id=task_id, **kwargs)
            self.project_dir = project_dir
            self.model = model
            self.schema = schema
            self.profiles_dir = profiles_dir
            self.manifest = manifest
            self.scope = scope
            self.sample_strategy = sample_strategy
            self.as_of = as_of
            self.tests_dir = tests_dir
            # A conn id is NOT a templated value and is deliberately kept OUT of
            # ``template_fields`` (DEC-007): templating a credential reference is
            # an avoidable leak surface.
            self.signalforge_conn_id = signalforge_conn_id
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
            _validate_prune_existing_config(
                project_dir=project_dir,
                model=model,
                schema=schema,
                on_flagged=on_flagged,
            )

        def execute(self, context: Any) -> dict[str, object]:
            # Re-validate the now-rendered template_fields (DEC-007): the
            # leading-dash argv-injection guard is meaningful only once Jinja has
            # rendered ``model`` / ``schema`` / ``project_dir`` to final values.
            _validate_prune_existing_config(
                project_dir=self.project_dir,
                model=self.model,
                schema=self.schema,
                on_flagged=self.on_flagged,
            )

            # No ``signalforge_conn_id`` → byte-identical to #233 (DEC-014): no
            # hook, no credential resolution. When set, resolve ONLY the
            # warehouse-auth ``profiles_dir`` (DEC-016): prune-existing makes no
            # LLM call, so there is NO register_secret, NO provider env injection,
            # and ``provider`` / ``api_key`` / ``cache_scope`` on the resolution
            # are ignored (an allowlist-invalid ``provider``, if present, still
            # raises inside the resolver per DEC-005).
            effective_profiles_dir = self.profiles_dir
            if self.signalforge_conn_id is not None:
                resolution = _resolve_hook(self.signalforge_conn_id, self.project_dir)
                # Precedence merge (DEC-012): explicit param > Connection extra.
                effective_profiles_dir = _merge_with_resolution(
                    self.profiles_dir, resolution.profiles_dir
                )
                # NEVER log a secret (DEC-007). prune-existing resolves no key, so
                # there is none to leak; record only conn_id + whether a
                # profiles_dir was resolved.
                _LOGGER.info(
                    "signalforge prune-existing resolved airflow connection: %s",
                    json.dumps(
                        {
                            "conn_id": self.signalforge_conn_id,
                            "profiles_dir_set": effective_profiles_dir is not None,
                        }
                    ),
                )

            # Single call — no batch (#233 DEC-003).
            argv = _build_prune_existing_argv(
                model=self.model,
                schema=self.schema,
                project_dir=self.project_dir,
                profiles_dir=effective_profiles_dir,
                manifest=self.manifest,
                scope=self.scope,
                sample_strategy=self.sample_strategy,
                as_of=self.as_of,
                tests_dir=self.tests_dir,
            )
            result = run_signalforge(argv, project_dir=self.project_dir, invocation=self.invocation)
            outcome = decide_task_outcome(result, on_flagged=self.on_flagged)
            raise_for_outcome(
                outcome,
                message=(
                    f"signalforge prune-existing for {self.model!r} produced "
                    f"task outcome {outcome.value} "
                    f"(exit_code={result.exit_code}, flagged={result.flagged})."
                ),
            )
            return result.to_xcom()

    return SignalForgePruneExistingOperator


@functools.cache
def _get_prune_existing_operator_class() -> type:
    """Resolve (and cache) the prune-existing operator class without importing airflow eagerly.

    Sibling of :func:`_get_generate_operator_class` (#233 DEC-008). Attribute
    ACCESS must stay airflow-free so the no-eager-import gate passes with the
    ``[airflow]`` extra absent. :func:`importlib.util.find_spec` checks Airflow
    availability WITHOUT executing/importing it. When Airflow is present, build
    the real ``BaseOperator`` subclass; when absent, return the airflow-free
    placeholder whose construction raises :class:`ModuleNotFoundError`.
    """
    if importlib.util.find_spec("airflow") is None:
        return _PruneExistingOperatorAirflowMissing
    return _make_prune_existing_operator_class()  # pragma: no cover - requires [airflow]


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolution of the public operator names.

    Building a real ``BaseOperator`` subclass at module scope would force an
    eager ``from airflow ...`` import; resolving the names here (via the
    find_spec-guarded getters) keeps attribute access airflow-free while still
    yielding the real operator when Airflow is installed.
    """
    if name == "SignalForgeGenerateOperator":
        return _get_generate_operator_class()
    if name == "SignalForgePruneExistingOperator":
        return _get_prune_existing_operator_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["SignalForgeGenerateOperator", "SignalForgePruneExistingOperator"]
