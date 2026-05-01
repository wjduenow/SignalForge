"""Prune-layer config loader.

Loads the `prune:` top-level block from `signalforge.yml` into a typed
`PruneConfig` (scope, sample_size, test_timeout_seconds,
total_budget_seconds, capture_failure_rows). Outer file wrapper uses
`extra="ignore"` so sibling stage namespaces (`safety:`, `llm:`, future
`grade:`) don't break the loader; the inner `PruneConfig` block uses
`extra="forbid"` so a typo like `scop:` fails loud rather than silently
no-op'ing.

See plans/super/6-prune-engine.md for the full design.
"""

from __future__ import annotations
