"""Base-env behavioural tests for the ``signalforge.airflow`` skeleton (#230).

These run in the DEFAULT pytest suite — they carry NO ``pytest.mark.airflow``
marker and never import the real ``apache-airflow`` package. They cover the
skeleton surface that the import-confinement / no-eager-import gates do not:
the shim module's protocol definitions, the stub operators/hooks raising
``NotImplementedError`` on construction, and the error module's repr-safe
``_format_value`` helper.

The lazy factories ``make_base_operator`` / ``make_base_hook`` are deliberately
NOT exercised here: their bodies require the optional ``[airflow]`` extra (absent
in the default coverage env) and are marked ``# pragma: no cover``, mirroring
``signalforge.warehouse.adapters._snowflake_client.make_real_client``.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from signalforge.airflow import _airflow_compat
from signalforge.airflow.errors import _format_value


def test_importing_shim_does_not_import_airflow() -> None:
    """Importing the shim defines its protocols/factories WITHOUT pulling the
    real ``airflow`` package into ``sys.modules`` (the import is lazy, confined
    to the factory bodies).

    Scrub any pre-existing ``airflow`` entries and reload the shim first, so the
    assertion measures what importing ``_airflow_compat`` *itself* does rather
    than depending on test order / a prior import (mirrors the snowflake-shim
    precedent ``tests/warehouse/test_snowflake_client.py``)."""
    for name in list(sys.modules):
        if name == "airflow" or name.startswith("airflow."):
            del sys.modules[name]
        if name == "signalforge.airflow._airflow_compat":
            del sys.modules[name]
    # Fresh import (not reload — a sibling test may have scrubbed the module from
    # sys.modules, and reload requires it to still be present). This measures
    # what importing _airflow_compat itself does, order-independently.
    mod = importlib.import_module("signalforge.airflow._airflow_compat")

    assert not any(m == "airflow" or m.startswith("airflow.") for m in sys.modules), (
        "importing _airflow_compat must not import the real airflow package"
    )
    # Factories are present and callable (their bodies stay lazy / SDK-gated).
    assert callable(mod.make_base_operator)
    assert callable(mod.make_base_hook)


def test_operator_protocol_is_runtime_checkable() -> None:
    """``_BaseOperatorProtocol`` structurally matches an object exposing
    ``execute`` and rejects one that does not."""

    class _HasExecute:
        def execute(self, context: object) -> None: ...

    class _Missing:
        pass

    assert isinstance(_HasExecute(), _airflow_compat._BaseOperatorProtocol)
    assert not isinstance(_Missing(), _airflow_compat._BaseOperatorProtocol)


def test_hook_protocol_is_runtime_checkable() -> None:
    """``_BaseHookProtocol`` structurally matches an object exposing
    ``get_conn`` and rejects one that does not."""

    class _HasGetConn:
        def get_conn(self) -> None: ...

    class _Missing:
        pass

    assert isinstance(_HasGetConn(), _airflow_compat._BaseHookProtocol)
    assert not isinstance(_Missing(), _airflow_compat._BaseHookProtocol)


def test_generate_operator_stub_raises_not_implemented() -> None:
    """The skeleton operator is construction-inert until an epic-#228 child
    implements it."""
    from signalforge.airflow import SignalForgeGenerateOperator

    with pytest.raises(NotImplementedError, match="skeleton placeholder"):
        SignalForgeGenerateOperator()


def test_hook_stub_raises_not_implemented() -> None:
    """The skeleton hook is construction-inert until an epic-#228 child
    implements it."""
    from signalforge.airflow import SignalForgeHook

    with pytest.raises(NotImplementedError):
        SignalForgeHook()


def test_package_dir_and_unknown_attr() -> None:
    """``__dir__`` surfaces the lazy public names; an unknown attribute raises
    ``AttributeError`` (PEP 562 ``__getattr__`` fall-through)."""
    import signalforge.airflow as pkg

    listed = dir(pkg)
    assert {"SignalForgeGenerateOperator", "SignalForgeHook"} <= set(listed)
    with pytest.raises(AttributeError):
        _ = pkg.does_not_exist  # type: ignore[attr-defined]


def test_format_value_quotes_and_escapes() -> None:
    """``_format_value`` routes user-supplied values through ``repr()`` so a
    crafted value cannot inject control characters / spoofed log lines."""
    assert _format_value("plain") == "'plain'"
    # A newline-bearing value is escaped (repr keeps it on one visible line).
    assert "\n" not in _format_value("foo\nINFO: spoofed")
    assert _format_value(None) == "None"
