"""Project-wide error base class.

:class:`SignalForgeError` is the conceptual root of every typed exception
SignalForge raises. Each layer (manifest, warehouse, safety, llm, draft,
prune, ...) defines its own intermediate base — :class:`SafetyError`,
:class:`WarehouseError`, :class:`PruneError`, etc. — so callers can
``except <Layer>Error`` to catch every failure that originated inside that
layer without sniffing message text.

A caller that wants to catch "any SignalForge-typed failure" (e.g. a top-level
CLI handler that wants to render a remediation line and exit non-zero) can
catch :class:`SignalForgeError`. Anything that isn't a subclass is, by
definition, an unexpected programming error and should propagate.

This module deliberately stays minimal: no message formatting, no
``default_remediation``, no ``__str__`` override. Each layer's intermediate
base owns its own rendering convention (the ``message + ↳ Remediation:``
shape established by :mod:`signalforge.manifest.errors` and mirrored across
the other layers). Centralising the rendering here would force every layer
to inherit it whether they want to or not.
"""

from __future__ import annotations


class SignalForgeError(Exception):
    """Project-wide root of every typed SignalForge exception.

    Each subpackage's intermediate base (e.g. :class:`PruneError`,
    :class:`SafetyError`) inherits from this class. Callers that want to
    catch any SignalForge-typed failure may catch this directly; callers
    that want layer-specific catches should catch the intermediate base.
    """


__all__ = ["SignalForgeError"]
