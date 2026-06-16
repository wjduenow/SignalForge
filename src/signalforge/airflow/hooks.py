"""The SignalForge Apache Airflow hook (#234 US-004).

:class:`SignalForgeHook` turns an Airflow Connection (plus an Airflow Variable
fallback) into a typed :class:`~signalforge.airflow._resolve.HookResolution` —
``profiles_dir`` (warehouse auth), ``provider`` (LLM SKU family), and
``api_key`` (the credential). It subclasses Apache Airflow's ``BaseHook`` so
``get_conn`` can call ``self.get_connection(conn_id)`` directly; all the actual
decision logic lives in the airflow-free pure resolver
:func:`signalforge.airflow._resolve.resolve_connection` (US-003), so it is
unit-tested 100% in the default suite while the airflow-touching wrapper here is
gated.

**Deferred class construction (the load-bearing structural constraint).** The
real hook must subclass ``BaseHook``, which requires ``airflow`` at runtime — but
importing THIS module must stay Airflow-free so the ungated no-eager-import /
import-confinement gates keep passing with Airflow absent (DEC-004 / DEC-006 /
DEC-007). So there is NO module-scope ``class X(BaseHook)``. Mirroring
:mod:`signalforge.airflow.operators` exactly, a PEP 562 module-level
:func:`__getattr__` resolves the ``SignalForgeHook`` name lazily via
:func:`_get_signalforge_hook_class`:

* When Apache Airflow is installed, the ``functools.cache``'d factory
  :func:`_make_signalforge_hook_class` builds the real ``BaseHook`` subclass at
  access time — the ``from airflow ...`` import stays confined to the one shim
  (:func:`signalforge.airflow._airflow_compat.make_base_hook`), out of module
  scope.
* When Airflow is NOT installed, the name resolves (without importing airflow —
  via :func:`importlib.util.find_spec`, which does not execute the module) to the
  airflow-free placeholder :class:`_SignalForgeHookAirflowMissing`, whose
  construction raises :class:`ModuleNotFoundError`. Attribute ACCESS is
  airflow-free; only CONSTRUCTION of the real hook needs Airflow.

Secrets hygiene: the hook never stores the resolved ``api_key`` on ``self`` and
never logs it; the redacting :meth:`__repr__` shows only ``signalforge_conn_id``
(one of the four leak-surface disciplines, DEC-007 — mirrors
:meth:`HookResolution.__repr__` and ``SnowflakeAdapter.__repr__``).
"""

from __future__ import annotations

import functools
import importlib.util
from typing import TYPE_CHECKING, Any

from signalforge.airflow import _airflow_compat
from signalforge.airflow._resolve import HookResolution, resolve_connection

if TYPE_CHECKING:
    # Type-checker-only declaration of the hook name. At runtime the class is
    # built by the find_spec-guarded factory below (it subclasses Apache
    # Airflow's ``BaseHook``, which is NOT a typecheck dependency), and the
    # public name is resolved through the module-level :func:`__getattr__`. This
    # block exists so ``signalforge.airflow.__init__``'s ``TYPE_CHECKING``
    # re-export of the name resolves under pyright.
    class SignalForgeHook:  # noqa: D401 - type stub only
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        def get_conn(self) -> HookResolution: ...


class _SignalForgeHookAirflowMissing:
    """Stand-in for :class:`SignalForgeHook` when Apache Airflow is absent.

    Resolving ``signalforge.airflow.SignalForgeHook`` without the ``[airflow]``
    optional extra installed returns THIS class (attribute access stays
    airflow-free, keeping the no-eager-import gate green). Constructing it raises
    :class:`ModuleNotFoundError` (an :class:`ImportError` subclass) naming the
    remediation — the real hook subclasses ``BaseHook`` and so genuinely requires
    Airflow at construction time.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise ModuleNotFoundError(
            "SignalForgeHook requires Apache Airflow, which is not installed. "
            "Install the optional extra: pip install 'signalforge-dbt[airflow]'."
        )


def _make_signalforge_hook_class() -> type:  # pragma: no cover - requires the [airflow] extra
    """Build and return the real ``BaseHook``-subclassing hook class.

    Reached only when Apache Airflow is installed (guarded by
    :func:`_get_signalforge_hook_class`'s ``find_spec`` check). The base class is
    obtained via the one shim
    :func:`signalforge.airflow._airflow_compat.make_base_hook`, so the
    ``from airflow ...`` import stays confined there and out of this module's
    scope (DEC-004 / DEC-007). Marked ``# pragma: no cover`` because its body —
    the ``BaseHook`` subclass and its ``get_conn`` — runs only under the gated
    ``[airflow]`` extra (the gated ``tests/airflow/test_hooks.py`` exercise it);
    the default coverage env never installs Airflow.
    """
    _Base = _airflow_compat.make_base_hook()

    class SignalForgeHook(_Base):  # type: ignore[valid-type, misc]
        """Resolve a SignalForge Airflow Connection to a :class:`HookResolution`.

        Subclasses Apache Airflow's ``BaseHook`` so :meth:`get_conn` can call
        ``self.get_connection(conn_id)`` directly. The resolution itself is done
        by the airflow-free pure
        :func:`signalforge.airflow._resolve.resolve_connection` (US-003), which
        reads the Connection's ``password`` (falling back to an Airflow Variable)
        for the API key, and ``provider`` / ``profiles_dir`` from the Connection
        ``extra`` JSON.
        """

        def __init__(self, signalforge_conn_id: str, **kwargs: Any) -> None:
            # ``BaseHook`` owns the standard hook kwargs (e.g. ``logger_name``);
            # pass them through.
            super().__init__(**kwargs)
            self.signalforge_conn_id = signalforge_conn_id

        def get_conn(self) -> HookResolution:
            """Resolve the configured Connection to a :class:`HookResolution`.

            ``project_dir`` is ``None`` at the hook layer: the hook has no
            project anchor to symlink-contain an ``extra.profiles_dir`` against
            (DEC-008's bounded-defence gap). The consuming operator — which DOES
            know its ``project_dir`` — supplies the containment anchor when it
            calls the resolver itself (US-005/US-006).
            """
            conn = self.get_connection(self.signalforge_conn_id)
            variable_lookup = _airflow_compat.airflow_variable_get
            return resolve_connection(conn, variable_lookup=variable_lookup, project_dir=None)

        def __repr__(self) -> str:
            # Leak-surface discipline (DEC-007): show only the conn id, never the
            # resolved key (which is not stored on ``self``) or any field-name
            # label that would reveal a credential is present.
            return f"SignalForgeHook(signalforge_conn_id={self.signalforge_conn_id!r})"

    return SignalForgeHook


@functools.cache
def _get_signalforge_hook_class() -> type:
    """Resolve (and cache) the hook class without importing airflow eagerly.

    Mirrors :func:`signalforge.airflow.operators._get_generate_operator_class`.
    Attribute ACCESS must stay airflow-free so the no-eager-import gate passes
    with the ``[airflow]`` extra absent. :func:`importlib.util.find_spec` checks
    Airflow availability WITHOUT executing/importing it (it does not add
    ``airflow`` to ``sys.modules``). When Airflow is present, build the real
    ``BaseHook`` subclass; when absent, return the airflow-free placeholder whose
    construction raises :class:`ModuleNotFoundError`.
    """
    if importlib.util.find_spec("airflow") is None:
        return _SignalForgeHookAirflowMissing
    return _make_signalforge_hook_class()  # pragma: no cover - requires [airflow]


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolution of the public ``SignalForgeHook`` name.

    Building a real ``BaseHook`` subclass at module scope would force an eager
    ``from airflow ...`` import; resolving the name here (via the find_spec-guarded
    getter) keeps attribute access airflow-free while still yielding the real hook
    when Airflow is installed.
    """
    if name == "SignalForgeHook":
        return _get_signalforge_hook_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["SignalForgeHook"]
