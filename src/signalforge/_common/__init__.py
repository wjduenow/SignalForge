"""Layer-neutral helpers shared across pipeline stages.

Modules under :mod:`signalforge._common` never depend on a layer-specific
error hierarchy. Programming-error guards raise plain :class:`ValueError`;
recurrent typed conditions (e.g. path-containment failure) raise a
project-neutral typed exception defined alongside the helper (see
:class:`signalforge._common.path_safety.PathContainmentError`).

Cross-layer consumers import from here directly; the public-API surface
lives on the consuming layer, which catches the layer-neutral typed
exception at its orchestrator boundary and re-raises as its own typed
error so the layer's catch surface stays homogeneous.
"""
