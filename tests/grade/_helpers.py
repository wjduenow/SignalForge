"""Shared test helpers for the grade-layer test suite (US-011 of #186).

This module is **tests-only** — production code does NOT import from it.
The plural ``tests/grade/`` directory has no ``__init__.py`` (mirrors
``tests/cli/_e2e_helpers.py`` / ``tests/grade/_fake.py`` precedent); the
``_``-prefix marks the module as test infrastructure.

Public surface (US-011):

* :func:`_sort_grade_events` — restore deterministic ``(artifact_id,
  criterion_id)`` ordering to a list of grade-audit JSONL records after
  the asyncio refactor (US-009) made on-disk arrival order
  non-deterministic. Traces to DEC-015 of
  ``plans/super/186-grade-asyncio-parallel.md``: the orchestrator does
  NOT sort before writing (that would buffer and break per-decision
  fail-closed durability); tests sort after the fact when they need to
  snapshot-compare against an iteration-order baseline.
"""

from __future__ import annotations

from typing import Any


def _sort_grade_events(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort a list of grade-audit JSONL records by ``(artifact_id, criterion_id)``.

    Used by tests that snapshot the audit JSONL. Under the asyncio
    refactor (US-009 of #186), arrival order on disk is
    non-deterministic — coroutines complete in whatever order the event
    loop schedules them, throttled by the ``max_concurrent_calls``
    semaphore. This helper restores deterministic ordering for
    comparison without changing the on-disk shape.

    **Contract.** Two properties pinned by ``test_grade_helpers.py``:

    * **Idempotent.** ``_sort_grade_events(_sort_grade_events(x)) ==
      _sort_grade_events(x)``. Calling twice is byte-equal to calling
      once.
    * **Stable for ties.** Python's :func:`sorted` is stable, so two
      records sharing ``(artifact_id, criterion_id)`` (theoretically
      possible if a future revision adds per-attempt retry records to
      the JSONL — today the grader writes exactly one record per pair)
      preserve their input order.

    **Missing-key defensiveness.** A record missing ``artifact_id`` or
    ``criterion_id`` sorts as if the field were the empty string;
    accessing ``.get(key, "")`` never raises. This shields snapshot
    tests from a malformed fixture taking the whole test down via
    :class:`KeyError`.

    Traces to DEC-015 of ``plans/super/186-grade-asyncio-parallel.md``.
    """
    return sorted(
        lines,
        key=lambda r: (str(r.get("artifact_id", "")), str(r.get("criterion_id", ""))),
    )


__all__ = ["_sort_grade_events"]
