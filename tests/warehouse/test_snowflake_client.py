"""US-002 (#119) — the Snowflake SDK shim seam.

The shim must import cleanly WITHOUT triggering a top-level
``snowflake.connector`` import (the SDK import is lazy inside
``make_real_client``), and a minimal fake satisfying the narrow
``cursor/execute/fetchall/close`` surface must structurally satisfy the
``@runtime_checkable`` ``_SnowflakeClientProtocol``.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest


def test_importing_shim_does_not_import_snowflake_connector() -> None:
    """The shim's ``import snowflake.connector`` is lazy (inside
    ``make_real_client``), so a fresh import of the shim module must NOT
    pull ``snowflake.connector`` into ``sys.modules``.

    Drop any pre-existing ``snowflake.connector`` / shim entries from
    ``sys.modules`` first so the assertion measures what THIS import does,
    not what a prior collector import already cached.
    """
    for name in list(sys.modules):
        if name == "snowflake.connector" or name.startswith("snowflake.connector."):
            del sys.modules[name]
        if name.endswith("_snowflake_client"):
            del sys.modules[name]

    importlib.import_module("signalforge.warehouse.adapters._snowflake_client")

    assert "snowflake.connector" not in sys.modules, (
        "importing the shim must not trigger a top-level "
        "snowflake.connector import — it is lazy inside make_real_client"
    )


def test_fake_satisfies_protocol() -> None:
    """A minimal fake exposing cursor/execute/fetchall/close structurally
    satisfies the ``@runtime_checkable`` ``_SnowflakeClientProtocol``.
    """
    from signalforge.warehouse.adapters._snowflake_client import (
        _SnowflakeClientProtocol,
    )

    class _FakeConn:
        def cursor(self) -> Any:
            return self

        def execute(self, command: str, *args: Any, **kwargs: Any) -> Any:
            return None

        def fetchall(self) -> Any:
            return []

        def close(self) -> None:
            return None

    assert isinstance(_FakeConn(), _SnowflakeClientProtocol)


def test_object_missing_method_does_not_satisfy_protocol() -> None:
    """A class missing one of the four methods must NOT satisfy the
    protocol — proves the runtime_checkable surface is load-bearing, not a
    rubber-stamp.
    """
    from signalforge.warehouse.adapters._snowflake_client import (
        _SnowflakeClientProtocol,
    )

    class _MissingClose:
        def cursor(self) -> Any:
            return self

        def execute(self, command: str, *args: Any, **kwargs: Any) -> Any:
            return None

        def fetchall(self) -> Any:
            return []

    assert not isinstance(_MissingClose(), _SnowflakeClientProtocol)


def test_public_names_are_exported() -> None:
    """``__all__`` lists the public-ish names the seam exposes."""
    from signalforge.warehouse.adapters import _snowflake_client

    assert set(_snowflake_client.__all__) == {
        "_SnowflakeClientProtocol",
        "make_real_client",
    }


def test_make_real_client_without_sdk_raises_import_error() -> None:
    """When ``snowflake.connector`` is absent, calling ``make_real_client``
    raises ``ImportError`` from the lazy import — NOT at module-import time.

    Simulate absence by inserting a ``None`` sentinel into ``sys.modules``
    for ``snowflake.connector`` so the in-function ``import`` fails even if
    the package is installed via the dev group.
    """
    from signalforge.warehouse.adapters._snowflake_client import make_real_client

    saved = {name: sys.modules.get(name) for name in ("snowflake", "snowflake.connector")}
    sys.modules["snowflake.connector"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(ImportError):
            make_real_client(account="a", user="u", password="p")
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
