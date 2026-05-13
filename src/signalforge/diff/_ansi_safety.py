"""Backwards-compatible re-export of the shared ANSI CSI stripper.

The implementation lives in :mod:`signalforge._common.ansi_safety` —
promoted there in issue #60 so the CLI's ``print_stderr`` sink shares
the same regex without reaching into the diff layer's private surface.
This module preserves the original import path so every existing
``from signalforge.diff._ansi_safety import strip_ansi_escapes`` call
inside the diff layer keeps working unchanged.
"""

from __future__ import annotations

from signalforge._common.ansi_safety import _ANSI_CSI_RE, strip_ansi_escapes

__all__ = ["_ANSI_CSI_RE", "strip_ansi_escapes"]
