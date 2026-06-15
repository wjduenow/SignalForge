"""US-004 (#230) DEC-006 / DEC-009 — no eager Apache Airflow import.

``import signalforge`` and ``import signalforge.airflow`` must NOT pull the real
``airflow`` top-level package into ``sys.modules``: Airflow ships only behind the
``[airflow]`` optional extra and must stay out of the base install's import path.
Resolving the lazy public names (``SignalForgeGenerateOperator`` /
``SignalForgeHook``) through the PEP 562 ``__getattr__`` (DEC-006) imports the
airflow-free stub modules only — still no airflow.

**This test is UNGATED (DEC-009).** NO ``airflow`` pytest marker, and it never
imports the real ``apache-airflow`` package. It runs in the default
``uv run pytest`` suite, where Airflow is deliberately not installed — that is
exactly the environment the "core stays lean" gate must hold in. A regression
that adds a module-scope ``from airflow ...`` import would make
``import signalforge.airflow`` raise ``ModuleNotFoundError`` here (Airflow
absent) and ERROR the suite; the ``sys.modules`` assertion additionally catches
the case where a maintainer runs with Airflow installed.

Mirrors ``tests/warehouse/test_snowflake_client.py``
``::test_importing_shim_does_not_import_snowflake_connector`` (the in-process
``sys.modules``-scrub form) and adds a subprocess form in a truly clean
interpreter for robustness.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

_SUBPROCESS_SCRIPT = """
import importlib
import sys

importlib.import_module("signalforge")
importlib.import_module("signalforge.airflow")

# Resolving the lazy public names imports the airflow-free stub modules only.
from signalforge.airflow import SignalForgeGenerateOperator, SignalForgeHook

assert SignalForgeGenerateOperator is not None
assert SignalForgeHook is not None

leaked = sorted(
    name for name in sys.modules if name == "airflow" or name.startswith("airflow.")
)
assert not leaked, f"airflow leaked into sys.modules: {leaked}"
print("OK")
"""


def _airflow_modules_in_sys_modules() -> list[str]:
    return sorted(name for name in sys.modules if name == "airflow" or name.startswith("airflow."))


def test_importing_signalforge_airflow_does_not_import_airflow_in_process() -> None:
    """In-process mirror of the snowflake precedent: drop any stale
    ``airflow`` / ``signalforge.airflow`` entries, re-import, then assert the
    top-level ``airflow`` package was not pulled in.
    """
    for name in list(sys.modules):
        if name == "airflow" or name.startswith("airflow."):
            del sys.modules[name]
        if name == "signalforge.airflow" or name.startswith("signalforge.airflow."):
            del sys.modules[name]

    importlib.import_module("signalforge.airflow")

    assert not _airflow_modules_in_sys_modules(), (
        "importing signalforge.airflow must not pull the real `airflow` package "
        "into sys.modules — every airflow import is lazy in _airflow_compat.py"
    )


def test_resolving_lazy_names_does_not_import_airflow_in_process() -> None:
    """Accessing the lazy public names goes through PEP 562 ``__getattr__`` and
    imports only the airflow-free stub modules — still no airflow.
    """
    for name in list(sys.modules):
        if name == "airflow" or name.startswith("airflow."):
            del sys.modules[name]

    from signalforge.airflow import SignalForgeGenerateOperator, SignalForgeHook

    assert SignalForgeGenerateOperator is not None
    assert SignalForgeHook is not None
    assert not _airflow_modules_in_sys_modules(), (
        "resolving the lazy operator/hook names must not import airflow — the "
        "stubs deliberately do not subclass BaseOperator/BaseHook at module scope"
    )


def test_importing_signalforge_airflow_does_not_import_airflow_subprocess() -> None:
    """Truly-clean-interpreter form: a fresh ``python -c`` process imports
    ``signalforge`` + ``signalforge.airflow``, resolves the lazy names, and
    asserts no ``airflow`` entry in ``sys.modules`` — returncode 0.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "clean-interpreter import of signalforge.airflow failed or leaked "
        f"airflow into sys.modules.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip().endswith("OK")
