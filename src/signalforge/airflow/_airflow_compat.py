"""The ONE Apache Airflow shim — sole home for every ``from airflow ...`` import.

US-002 (#230, DEC-007) establishes the single seam where every airflow
``# type: ignore[...]`` / ``# pyright: ignore[...]`` is allowed to live, and the
only module in :mod:`signalforge.airflow` that may carry a ``from airflow ...``
import. Mirrors the one-shim-per-vendor precedent established by
:mod:`signalforge.warehouse.adapters._snowflake_client` (the
``snowflake-connector-python`` SDK) and :mod:`signalforge.llm._openai_client`
(the ``openai`` SDK) — see ``.claude/rules/llm-drafter.md`` § "One SDK seam". A
standalone line-scan confinement test (US-004) enforces that no sibling module
under :mod:`signalforge.airflow` imports airflow or carries an airflow type
ignore.

Three responsibilities:

* Duck-typed :class:`_BaseOperatorProtocol` / :class:`_BaseHookProtocol` —
  describe only the surface SignalForge's orchestration code will touch
  (operator: :meth:`execute`; hook: :meth:`get_conn`). They let pyright pass
  WITHOUT Airflow installed (Airflow is not a typecheck dependency — it ships
  behind the ``[airflow]`` extra), exactly as ``_BQClientProtocol`` /
  ``_SnowflakeClientProtocol`` do for their vendors.

* :func:`make_base_operator` / :func:`make_base_hook` — lazy factories that
  import the real Airflow base class **inside the function body** and return it.
  An implementing epic-#228 child calls these to do the real ``BaseOperator``
  subclassing at call time (NOT at module-import time, which would break the
  no-eager-import gate — DEC-004). Because the ``from airflow ...`` import is
  confined to these bodies, importing this shim never requires Airflow to be
  installed, and ``import signalforge.airflow`` stays Airflow-free.

* :func:`raise_for_outcome` — the Airflow-side translator (#231, DEC-006) that
  turns the NEUTRAL :class:`signalforge.airflow.result.TaskOutcome` produced by
  the pure :func:`signalforge.airflow.result.decide_task_outcome` into the
  matching airflow exception (``AirflowFailException`` no-retry /
  ``AirflowException`` retryable / ``AirflowSkipException`` skip / no-raise on
  success). Its ``from airflow.exceptions import ...`` is confined to the
  function body for the same reason as the factories' imports.

Observability discipline (mirroring the warehouse/LLM shims): no logger calls in
this shim. Logging lives in the implementing operator/hook where the task
context is known. The shim itself is structural plumbing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Airflow-free import — `result` carries NO `from airflow ...` (it is the pure
# decision core, US-001), so importing `TaskOutcome` here is safe and creates no
# circular import (`result` does not import this shim).
from signalforge.airflow.result import TaskOutcome


@runtime_checkable
class _BaseOperatorProtocol(Protocol):
    """Duck-typed surface of an Apache Airflow ``BaseOperator`` subclass.

    Narrow on purpose — only the method SignalForge's orchestration code drives:
    Airflow's task runner invokes ``operator.execute(context)`` when the task
    runs. Typing against this protocol lets pyright check the operator surface
    without importing the real ``airflow.models.BaseOperator`` (Airflow is not a
    typecheck dependency). Mirrors ``_BQClientProtocol`` /
    ``_SnowflakeClientProtocol``.
    """

    def execute(self, context: Any) -> Any: ...


@runtime_checkable
class _BaseHookProtocol(Protocol):
    """Duck-typed surface of an Apache Airflow ``BaseHook`` subclass.

    Narrow on purpose — only ``hook.get_conn()``, the DB-API-style connection
    accessor every Airflow hook exposes. Typing against this protocol lets
    pyright check the hook surface without importing the real
    ``airflow.hooks.base.BaseHook``.
    """

    def get_conn(self) -> Any: ...


def make_base_operator() -> type:  # pragma: no cover - requires the [airflow] extra
    """Lazily import and return Apache Airflow's ``BaseOperator`` class.

    The ``from airflow ...`` import is confined to this function body (DEC-007)
    so importing this shim — and therefore ``import signalforge.airflow`` — never
    requires Airflow to be installed (it ships only under the ``[airflow]``
    optional extra). An implementing epic-#228 child calls this at runtime to
    build a real ``BaseOperator`` subclass via the factory, keeping the
    subclassing out of module scope (DEC-004 — module-scope subclassing would
    eagerly import airflow and break the no-eager-import gate).

    The single airflow ``# type: ignore`` for the import lives here per the
    one-shim-per-vendor confinement rule.
    """
    from airflow.models import BaseOperator  # type: ignore[import-not-found]

    return BaseOperator  # type: ignore[no-any-return]


def make_base_hook() -> type:  # pragma: no cover - requires the [airflow] extra
    """Lazily import and return Apache Airflow's ``BaseHook`` class.

    Lazy sibling of :func:`make_base_operator` for hooks. The
    ``from airflow ...`` import is confined to this function body (DEC-007); an
    implementing child calls this to build a real ``BaseHook`` subclass at
    runtime rather than at module scope (DEC-004).
    """
    from airflow.hooks.base import BaseHook  # type: ignore[import-not-found]

    return BaseHook  # type: ignore[no-any-return]


def raise_for_outcome(  # pragma: no cover - requires the [airflow] extra
    outcome: TaskOutcome, *, message: str
) -> None:
    """Translate a :class:`TaskOutcome` into the matching Apache Airflow signal.

    This is the Airflow-side half of the result → task-state contract (#231,
    DEC-006). :func:`signalforge.airflow.result.decide_task_outcome` is the pure,
    Airflow-free decision table that maps a CLI exit code (the four-tier
    taxonomy, ``.claude/rules/cli-layer.md``) + the operator's ``on_flagged``
    policy to a NEUTRAL :class:`TaskOutcome`; this function turns that neutral
    outcome into the airflow exception the task runner understands. Keeping the
    decision pure and the airflow translation here is what lets the decision
    logic stay unit-testable without Airflow installed (one-shim-per-vendor,
    DEC-007).

    Mapping (the ``AirflowFailException`` vs ``AirflowException`` distinction is
    load-bearing — it is how Airflow's retry policy is engaged or suppressed):

    * :attr:`TaskOutcome.FAIL_NO_RETRY` → raise ``AirflowFailException`` — a
      *hard* failure that bypasses the task's retry policy (CLI exit tier 1/2:
      load/parse + input-validation failures are deterministic; retrying cannot
      help, and a below-threshold flagged run under ``on_flagged="fail"`` is a
      reviewer signal, not a transient).
    * :attr:`TaskOutcome.FAIL_RETRYABLE` → raise ``AirflowException`` — an
      ordinary task failure that DOES honour the task's ``retries`` /
      ``retry_delay`` (CLI exit tier 3: external-dependency failures — auth /
      rate-limit / warehouse / API blips — are worth retrying).
    * :attr:`TaskOutcome.SKIP` → raise ``AirflowSkipException`` — mark the task
      skipped (``on_flagged="skip"`` on a flagged exit-0 run).
    * :attr:`TaskOutcome.SUCCESS` → return ``None`` (no raise) — the task
      succeeds.

    The ``from airflow.exceptions import ...`` is confined to this function body
    (DEC-007) so importing the shim never requires Airflow; the single airflow
    ``# type: ignore`` for the import lives here per the one-shim-per-vendor rule.
    """
    from airflow.exceptions import (  # type: ignore[import-not-found]
        AirflowException,
        AirflowFailException,
        AirflowSkipException,
    )

    if outcome == TaskOutcome.SUCCESS:
        return None
    if outcome == TaskOutcome.SKIP:
        raise AirflowSkipException(message)
    if outcome == TaskOutcome.FAIL_NO_RETRY:
        raise AirflowFailException(message)
    # outcome == TaskOutcome.FAIL_RETRYABLE — retryable per the task's policy.
    raise AirflowException(message)


# Only the factory functions are public API. The ``_Base*Protocol`` types stay
# ``_``-prefixed internals (still directly importable for tests / implementing
# children) and are deliberately NOT listed in ``__all__`` — per the convention
# that a subpackage's public contract is its ``__all__`` and ``_``-prefixed names
# are internal.
__all__ = [
    "make_base_hook",
    "make_base_operator",
    "raise_for_outcome",
]
