"""Sanity tests for :mod:`tests.grade._helpers` (US-011 of #186).

Pins the two load-bearing properties of :func:`_sort_grade_events`
(DEC-015 of ``plans/super/186-grade-asyncio-parallel.md``):

* **Idempotent** — calling twice equals calling once.
* **Stable for ties** — Python's :func:`sorted` preserves input order
  for records sharing the sort key.

Plus a missing-key defensiveness pin so a malformed fixture line cannot
take down every snapshot test in the grade suite via :class:`KeyError`.
"""

from __future__ import annotations

from typing import Any

from tests.grade._helpers import _sort_grade_events


def test_sort_grade_events_orders_by_artifact_id_then_criterion_id() -> None:
    """Records sort by ``artifact_id`` first, then ``criterion_id``."""
    unsorted: list[dict[str, Any]] = [
        {"artifact_id": "column.b.description", "criterion_id": "clarity"},
        {"artifact_id": "column.a.description", "criterion_id": "rationale"},
        {"artifact_id": "column.a.description", "criterion_id": "clarity"},
        {"artifact_id": "column.b.description", "criterion_id": "rationale"},
    ]

    result = _sort_grade_events(unsorted)

    assert [(r["artifact_id"], r["criterion_id"]) for r in result] == [
        ("column.a.description", "clarity"),
        ("column.a.description", "rationale"),
        ("column.b.description", "clarity"),
        ("column.b.description", "rationale"),
    ]


def test_sort_grade_events_is_idempotent() -> None:
    """``_sort_grade_events(_sort_grade_events(x)) == _sort_grade_events(x)``.

    Pins DEC-015 idempotency. Calling the helper twice produces the
    same list as calling it once — required so a snapshot test that
    happens to sort an already-sorted fixture stays byte-equal.
    """
    rows: list[dict[str, Any]] = [
        {"artifact_id": "column.b.description", "criterion_id": "clarity"},
        {"artifact_id": "column.a.description", "criterion_id": "rationale"},
        {"artifact_id": "column.a.description", "criterion_id": "clarity"},
    ]

    once = _sort_grade_events(rows)
    twice = _sort_grade_events(once)

    assert once == twice


def test_sort_grade_events_is_stable_for_ties() -> None:
    """Two records sharing ``(artifact_id, criterion_id)`` preserve
    their relative input order.

    Python's :func:`sorted` is stable; this test pins that we don't
    sneak in an unstable sort (e.g. via a second-pass shuffle). The
    grade audit JSONL writes exactly one record per pair today, but the
    helper must keep stability so a future per-attempt retry record
    shape (where two records legitimately share the sort key) doesn't
    break ordering expectations.
    """
    # Two records sharing the sort key but with distinguishable extra
    # data; ordering of the extra-data field is the stability witness.
    rows: list[dict[str, Any]] = [
        {"artifact_id": "column.a.description", "criterion_id": "clarity", "attempt": 0},
        {"artifact_id": "column.a.description", "criterion_id": "clarity", "attempt": 1},
        {"artifact_id": "column.a.description", "criterion_id": "clarity", "attempt": 2},
    ]

    result = _sort_grade_events(rows)

    # Identity-preserving — output rows are the same dict objects.
    assert [r["attempt"] for r in result] == [0, 1, 2]


def test_sort_grade_events_handles_missing_keys_gracefully() -> None:
    """A record missing ``artifact_id`` or ``criterion_id`` sorts as
    the empty string rather than raising :class:`KeyError`.

    Defence-in-depth: a malformed JSONL line should not take down every
    snapshot test in the grade suite. The empty-string fallback puts
    malformed rows at the start of the sorted output where a test
    asserting on specific rows will surface the corruption explicitly.
    """
    rows: list[dict[str, Any]] = [
        {"artifact_id": "column.b.description", "criterion_id": "clarity"},
        {"criterion_id": "rationale"},  # missing artifact_id
        {"artifact_id": "column.a.description"},  # missing criterion_id
    ]

    # No KeyError — the helper completes.
    result = _sort_grade_events(rows)

    # Malformed rows sort to the front (empty-string key < any concrete
    # column name); within them, the artifact_id-missing row sorts
    # first (its artifact_id is "" — strictly less than the other
    # malformed row's "column.a.description").
    assert len(result) == 3
    assert result[0].get("artifact_id", "") == ""
    assert result[1].get("artifact_id", "") == "column.a.description"
    assert result[2].get("artifact_id", "") == "column.b.description"


def test_sort_grade_events_on_empty_list_returns_empty_list() -> None:
    """Trivial edge — ``_sort_grade_events([]) == []``.

    Pins that the helper does not assume a non-empty input. The async
    core's ``cancelled_count + degraded_count == total_pairs`` budget
    case with zero pairs would produce an empty JSONL; the helper
    must round-trip it.
    """
    assert _sort_grade_events([]) == []


def test_sort_grade_events_returns_new_list_does_not_mutate_input() -> None:
    """The helper returns a freshly sorted list; the input list's
    original order is untouched.

    Pins that callers can safely pass a list they're still iterating
    over (e.g. ``_read_jsonl(audit_path)``); the helper does not sort
    in-place.
    """
    original: list[dict[str, Any]] = [
        {"artifact_id": "column.b.description", "criterion_id": "clarity"},
        {"artifact_id": "column.a.description", "criterion_id": "clarity"},
    ]
    snapshot = list(original)  # copy to compare against post-call

    _sort_grade_events(original)

    assert original == snapshot, "input list was mutated"
