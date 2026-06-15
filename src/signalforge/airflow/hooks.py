"""Placeholder SignalForge Airflow hook — skeleton only (#230 US-002).

This module defines the stub :class:`SignalForgeHook`. Like the operator stub
(:mod:`signalforge.airflow.operators`), it is a plain class that deliberately
does NOT subclass Apache Airflow's ``BaseHook`` at module scope — a module-scope
subclass would force an eager ``from airflow ...`` import and break the
no-eager-import gate (DEC-004 / DEC-006). Keeping the stub Airflow-free lets
``import signalforge.airflow.hooks`` (and the lazy
``signalforge.airflow.SignalForgeHook`` re-export) stay free of ``airflow`` in
``sys.modules``.

The real hook — which DOES subclass ``BaseHook`` — lands with an implementing
child of epic #228, built at runtime via the lazy factory
:func:`signalforge.airflow._airflow_compat.make_base_hook`.
"""

from __future__ import annotations

from typing import Any


class SignalForgeHook:
    """Stub for the future SignalForge Airflow hook.

    Skeleton placeholder (#230). Constructing it raises
    :class:`NotImplementedError` — the hook behaviour (a real ``BaseHook``
    subclass built via
    :func:`signalforge.airflow._airflow_compat.make_base_hook`) lands with a
    later epic-#228 child.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "SignalForgeHook is a skeleton placeholder; the hook lands in a later "
            "epic #228 child. #230 ships the package skeleton only."
        )


__all__ = ["SignalForgeHook"]
