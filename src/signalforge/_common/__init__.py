"""Layer-neutral helpers shared across pipeline stages.

Modules under :mod:`signalforge._common` raise plain :class:`ValueError`
on bad input and never depend on a layer-specific error hierarchy.
Consumers in :mod:`signalforge.grade` and :mod:`signalforge.diff` import
from here directly; the public-API surface lives on the consuming layer.
"""
