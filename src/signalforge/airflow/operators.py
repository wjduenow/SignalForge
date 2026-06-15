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


class SignalForgeGenerateOperator:
    """Stub for the future SignalForge ``generate`` Airflow operator.

    Skeleton placeholder (#230). Constructing it raises
    :class:`NotImplementedError` — the operator behaviour (a real
    ``BaseOperator`` subclass built via
    :func:`signalforge.airflow._airflow_compat.make_base_operator`) lands with a
    later epic-#228 child.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "SignalForgeGenerateOperator is a skeleton placeholder; the operator "
            "lands in a later epic #228 child. #230 ships the package skeleton only."
        )


__all__ = ["SignalForgeGenerateOperator"]
