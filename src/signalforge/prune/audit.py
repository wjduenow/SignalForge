"""Fail-closed prune-event audit writer.

Mirrors the safety layer's audit and the drafter's response audit at the
prune-decision boundary: opens with `O_APPEND | O_CREAT | 0o600`, writes one
JSONL line per `PruneEvent`, calls `os.fsync`, closes. Catches no exceptions
internally — `OSError`, `PermissionError`, encoding failures all propagate so
an unaudited prune decision can never silently land. Size cap is checked
before any file open. AST audit-completeness scan gates `PruneEvent`
construction to this module only.

See plans/super/6-prune-engine.md for the full design.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)
