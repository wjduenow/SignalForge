"""Typed shapes for the prune layer.

Hosts `PruneResult`, `PruneDecision`, the `DropReason` and `Scope` literals,
and the user-facing `PruneConfig`. `PruneResult` carries the kept/dropped
split plus the full per-test decision list and the schema version for
forward-compat. `PruneDecision` carries every field the diff renderer (#8)
and grader (#7) need to explain a verdict: anchor, type, args, decision,
reason, failure count, scope, elapsed time, compiled-SQL hash, the literal
SQL, and a one-line "why". Read-back models use `extra="ignore"` paired with
a one-off `extra="forbid"` drift detector; `PruneConfig` uses `extra="forbid"`
so config typos fail loud.

See plans/super/6-prune-engine.md for the full design.
"""

from __future__ import annotations
