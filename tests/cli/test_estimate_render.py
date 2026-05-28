"""Tests for ``signalforge.cli._estimate.render`` (US-004 of issue #36).

These tests pin the load-bearing invariants of the text renderer:

1. Happy-path output matches the snapshot fixture byte-for-byte.
2. Partial-failure (DEC-005) shape matches its own snapshot.
3. Output ends with exactly one trailing newline.
4. The footer carries the price-table version + tests/column
   heuristic verbatim.
5. The per-criterion section lists every active rubric criterion.

The fixtures at ``tests/fixtures/estimate/output_*.txt`` ARE the
contract; the renderer pins to them. A drift in either direction
breaks the test loudly — update the fixture (after a deliberate
contract change) OR fix the renderer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.cli._estimate import CriterionEstimate, EstimateReport, render

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "estimate"


def _make_happy_report() -> EstimateReport:
    """Build the canonical Austin-shaped report used by the happy
    fixture. The values are NOT derived from a live run — they are
    the locked snapshot inputs so the renderer's output is fully
    determined by this constructor + the renderer code.
    """
    criteria = (
        CriterionEstimate(
            criterion_id="clarity",
            criterion_text_truncated="clarity check",
            calls=62,
            input_tokens_per_call=619,
            total_input_tokens=38_400,
            estimated_output_tokens_per_call=50,
            usd=0.0288,
        ),
        CriterionEstimate(
            criterion_id="consistency",
            criterion_text_truncated="consistency check",
            calls=62,
            input_tokens_per_call=619,
            total_input_tokens=38_400,
            estimated_output_tokens_per_call=50,
            usd=0.0288,
        ),
        CriterionEstimate(
            criterion_id="rationale",
            criterion_text_truncated="rationale check",
            calls=62,
            input_tokens_per_call=619,
            total_input_tokens=38_400,
            estimated_output_tokens_per_call=50,
            usd=0.0288,
        ),
        CriterionEstimate(
            criterion_id="no-redundant",
            criterion_text_truncated="no-redundant check",
            calls=62,
            input_tokens_per_call=619,
            total_input_tokens=38_400,
            estimated_output_tokens_per_call=50,
            usd=0.0288,
        ),
    )
    return EstimateReport(
        model_unique_id="model.austin.stg_bikeshare_trips",
        drafter_model="claude-sonnet-4-6",
        grader_model="claude-sonnet-4-6",
        draft_input_tokens=3_124,
        draft_output_tokens_estimate=1_200,
        draft_usd=0.0091,
        grade_artifacts_count=62,
        grade_criteria_count=4,
        grade_per_criterion=criteria,
        grade_usd=0.1152,
        total_llm_usd=0.1243,
        warehouse_bytes_per_row=84,
        warehouse_total_bytes=35_300_000,
        warehouse_unavailable_reason=None,
        warehouse_estimate_source="BigQuery dryRun",
        tests_per_column_heuristic=3.5,
        sample_size=10_000,
        price_table_version="2026-05-11",
        duration_seconds=0.5,
    )


def _make_partial_failure_report() -> EstimateReport:
    """Build the partial-failure variant: identical to the happy
    report except the warehouse fields are ``None`` and
    ``warehouse_unavailable_reason`` carries the engine's
    ``f"{type(exc).__name__}: ..."`` shape.
    """
    happy = _make_happy_report()
    return EstimateReport(
        **{
            **happy.model_dump(),
            "warehouse_bytes_per_row": None,
            "warehouse_total_bytes": None,
            "warehouse_unavailable_reason": (
                "WarehouseAuthError: Application Default Credentials were not found"
            ),
        }
    )


# ---------------------------------------------------------------------------
# AC tests
# ---------------------------------------------------------------------------


def test_render_matches_happy_snapshot_byte_for_byte() -> None:
    """AC-1. ``render(report)`` matches ``output_happy.txt`` exactly."""
    report = _make_happy_report()
    expected = (_FIXTURE_DIR / "output_happy.txt").read_text(encoding="utf-8")
    actual = render(report)
    assert actual == expected


def test_render_partial_failure_warehouse_unavailable_shape() -> None:
    """AC-2. Partial-failure rendering matches its dedicated snapshot.

    The warehouse section carries ``<unavailable: <ErrorClass>>`` and
    the totals line shows ``<unknown>`` (DEC-005 mirror of
    ``prune-engine.md`` DEC-009 conservative-bias verbatim).
    """
    report = _make_partial_failure_report()
    expected = (_FIXTURE_DIR / "output_warehouse_unavailable.txt").read_text(encoding="utf-8")
    actual = render(report)
    assert actual == expected


def test_render_ends_with_single_trailing_newline() -> None:
    """AC-3. Output ends with exactly one ``\\n``."""
    report = _make_happy_report()
    out = render(report)
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_render_footer_carries_price_table_version_and_heuristic() -> None:
    """AC-4. Footer line includes the price-table version + heuristic.

    The footer is the bottom-most line of the output (after the
    trailing newline strip). Asserts both the version string and the
    tests/column phrasing land verbatim.
    """
    report = _make_happy_report()
    out = render(report)
    lines = out.rstrip("\n").splitlines()
    footer = lines[-1]
    assert "Price table: 2026-05-11" in footer
    assert "Heuristic: ~3.5 tests/column (canonical fixture average)" in footer


def test_render_per_criterion_section_lists_each_active_criterion() -> None:
    """AC-5. Every criterion in ``report.grade_per_criterion`` appears
    in the rendered output. Pinning by id is sufficient — the id is
    the stable join key the grade engine uses (``grade-layer.md``
    DEC-009) and the renderer must surface it for operator review.
    """
    report = _make_happy_report()
    out = render(report)
    for criterion in report.grade_per_criterion:
        assert criterion.criterion_id in out, (
            f"criterion {criterion.criterion_id!r} missing from rendered output"
        )


# ---------------------------------------------------------------------------
# Determinism + format guards (defence-in-depth)
# ---------------------------------------------------------------------------


def test_render_is_deterministic_across_calls() -> None:
    """Pure-function contract: same report -> same bytes every call.

    Calls ``render`` three times on the same report and asserts the
    outputs are byte-equal. Catches accidental introduction of
    timestamps / UUIDs / dict-order non-determinism.
    """
    report = _make_happy_report()
    first = render(report)
    second = render(report)
    third = render(report)
    assert first == second == third


@pytest.mark.parametrize(
    ("dollar_field", "expected_substring"),
    [
        ("draft", "$0.0091"),
        ("grade", "$0.1152"),
        ("total", "$0.1243"),
    ],
)
def test_render_usd_fields_format_to_four_decimals(
    dollar_field: str,
    expected_substring: str,
) -> None:
    """USD values render at 4-decimal precision (``$x.xxxx``).

    Four decimals matches the engine's hand-calculation precision
    (``test_estimate_total_llm_usd_matches_hand_calculation`` in the
    engine test suite). Three decimals would round dust costs into
    invisibility; five decimals reads as noise.
    """
    del dollar_field  # parameterised purely for human-readable IDs
    report = _make_happy_report()
    out = render(report)
    assert expected_substring in out
