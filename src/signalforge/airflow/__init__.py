"""``signalforge.airflow`` — the Apache Airflow integration subpackage skeleton.

Second child of epic #228 (Airflow operator, v0.7 roadmap). This package is the
seam every later epic-#228 child plugs into; #230 ships the *skeleton* only — no
operator behaviour yet.

Two load-bearing design rules govern this subpackage:

1. **Zero eager Airflow import (DEC-006).** ``import signalforge`` and
   ``import signalforge.airflow`` must NOT pull ``airflow`` into
   ``sys.modules``. Apache Airflow is a heavy, version-pinned dependency that
   ships only behind the ``[airflow]`` optional extra (installed into the #229
   isolated, constraints-pinned ``.venv-airflow`` — see
   ``docs/research/airflow-test-environment.md``); the base install stays
   Airflow-free. So the public operator/hook names are resolved **lazily** via a
   PEP 562 module-level :func:`__getattr__` that imports the implementing module
   only on attribute access. The :mod:`signalforge.airflow.operators` /
   :mod:`signalforge.airflow.hooks` modules are themselves Airflow-free (the
   stub classes do NOT subclass the real ``BaseOperator`` at module scope —
   DEC-004), and the sole place any ``from airflow ...`` import lives is the
   lazy shim :mod:`signalforge.airflow._airflow_compat` (DEC-007), whose imports
   only fire inside its ``make_*`` factory functions when an implementing child
   actually needs the real Airflow base class.

2. **One shim per vendor (DEC-007).** Mirrors the
   ``.claude/rules/llm-drafter.md`` "One SDK seam" rule and the warehouse
   ``_snowflake_client`` / LLM ``_openai_client`` precedents:
   :mod:`signalforge.airflow._airflow_compat` is the SOLE home for every
   ``from airflow ...`` import and every airflow ``# type: ignore`` /
   ``# pyright: ignore``. A standalone line-scan confinement test (US-004)
   enforces this.

Error classes (``AirflowIntegrationError`` / ``AirflowConfigError``), the result
core (``SignalForgeRunResult`` / ``TaskOutcome`` / ``decide_task_outcome`` /
``OnFlagged``), the runner (``run_signalforge``), and the drift core
(``compute_drift`` / ``DriftReport`` / ``DriftArtifact`` / ``GradeRegression`` /
``SchemaShapeDelta`` — issue #235) are pure-Python and Airflow-free (none of
``signalforge.airflow.errors`` / ``.result`` / ``.runner`` / ``.drift`` carries
a ``from airflow ...`` import — the runner's only heavy import,
:func:`signalforge.cli.main`, is done lazily inside the function body), so they
are **eager** re-exports here (only the operator/hook names stay lazy —
DEC-003 / DEC-006). Importing them does not violate the no-eager-import gate.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from signalforge.airflow.drift import (
    DriftArtifact,
    DriftReport,
    GradeRegression,
    SchemaShapeDelta,
    compute_drift,
)
from signalforge.airflow.errors import AirflowConfigError, AirflowIntegrationError
from signalforge.airflow.result import (
    OnFlagged,
    SignalForgeRunResult,
    TaskOutcome,
    decide_task_outcome,
)
from signalforge.airflow.runner import run_signalforge

if TYPE_CHECKING:
    # Type-checker-only imports: pyright resolves the public names for callers
    # without forcing an eager runtime import (the ``if TYPE_CHECKING`` block is
    # never executed at runtime, so it does not violate the no-eager-import
    # gate). The modules themselves are Airflow-free per DEC-004.
    from signalforge.airflow.hooks import SignalForgeHook
    from signalforge.airflow.operators import (
        SignalForgeGenerateOperator,
        SignalForgePruneExistingOperator,
    )

# Public name -> submodule that defines it. The PEP 562 ``__getattr__`` below
# imports the submodule lazily on first attribute access, so ``import
# signalforge.airflow`` itself imports neither the submodule nor airflow.
_LAZY_NAMES: dict[str, str] = {
    "SignalForgeGenerateOperator": "operators",
    "SignalForgePruneExistingOperator": "operators",
    "SignalForgeHook": "hooks",
}

__all__ = [
    "AirflowConfigError",
    "AirflowIntegrationError",
    "DriftArtifact",
    "DriftReport",
    "GradeRegression",
    "OnFlagged",
    "SchemaShapeDelta",
    "SignalForgeGenerateOperator",
    "SignalForgeHook",
    "SignalForgePruneExistingOperator",
    "SignalForgeRunResult",
    "TaskOutcome",
    "compute_drift",
    "decide_task_outcome",
    "run_signalforge",
]


def __getattr__(name: str) -> object:
    """PEP 562 lazy attribute resolution for the public operator/hook names.

    Resolving ``signalforge.airflow.SignalForgeGenerateOperator`` (etc.)
    imports :mod:`signalforge.airflow.operators` only at that moment, keeping
    ``import signalforge.airflow`` free of any submodule (and therefore airflow)
    import. See DEC-006.
    """
    module_name = _LAZY_NAMES.get(name)
    if module_name is not None:
        module = import_module(f"{__name__}.{module_name}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Surface the lazy public names in ``dir(signalforge.airflow)``."""
    return sorted(__all__)
