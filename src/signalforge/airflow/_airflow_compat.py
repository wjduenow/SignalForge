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

Two responsibilities:

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

Observability discipline (mirroring the warehouse/LLM shims): no logger calls in
this shim. Logging lives in the implementing operator/hook where the task
context is known. The shim itself is structural plumbing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


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


# Only the factory functions are public API. The ``_Base*Protocol`` types stay
# ``_``-prefixed internals (still directly importable for tests / implementing
# children) and are deliberately NOT listed in ``__all__`` — per the convention
# that a subpackage's public contract is its ``__all__`` and ``_``-prefixed names
# are internal.
__all__ = [
    "make_base_hook",
    "make_base_operator",
]
