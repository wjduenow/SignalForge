"""SignalForge prune layer.

The signal-vs-volume gate. Runs every candidate test from the drafter against
real warehouse data via the warehouse adapter and drops the ones with no
signal: tests that always pass on warehouse samples, tests that fail on
known-clean data, and tests whose required parent data does not yet exist.
Conservative defaults keep tests we cannot evaluate (`kept-without-evidence`)
rather than silently dropping them, and every kept/dropped test ships with a
structured `PruneDecision` carrying the drop reason, failure count, scope, and
the literal compiled SQL so downstream stages (#7 grader, #8 diff renderer) can
explain every artifact.

Public API lands in US-013; this module currently re-exports nothing.

See plans/super/6-prune-engine.md for the full design.
"""
