"""Tests for ``signalforge.diff.engine`` (US-010 of issue #8).

Pins the load-bearing properties of the diff orchestrator:

1. **Boundary checks (DEC-002)** — three negative tests, one per
   mismatch error.
2. **existing_schema size cap (DEC-006)** — oversize raises BEFORE any
   ``yaml.safe_load`` call.
3. **existing_schema soft warn (DEC-014)** — a payload between the
   warn-at threshold and the hard cap emits a single ``WARNING``.
4. **Renderer dispatch (DEC-004)** — ``config.render_kind`` selects the
   :class:`AnsiRenderer` / :class:`MarkdownRenderer` /
   :class:`JsonRenderer` concrete.
5. **JsonRenderer round-trip** — output parses back to a structured
   dict whose top-level fields match the report's metadata.
6. **Sidecar write (US-007 integration)** — when ``sidecar_path`` is
   supplied, :func:`signalforge.diff._sidecar.write_sidecar` produces a
   readable JSON document at the canonicalised path.
7. **Symlink containment (DEC-002 of US-007)** — a sidecar path
   pointing outside ``project_dir`` raises
   :class:`DiffSidecarWriteError`.
8. **INFO log (DEC-015)** — single ``logging.INFO`` event with
   lazy-format JSON payload at end-of-run.
9. **End-to-end happy path** — the assembled :class:`DiffReport`
   carries kept / dropped / flagged entries derived from the prune
   result and the optional grading report.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from signalforge.diff.config import DiffConfig
from signalforge.diff.engine import render_diff
from signalforge.diff.errors import (
    DiffCandidateModelMismatchError,
    DiffGradingReportModelMismatchError,
    DiffInputTooLargeError,
    DiffPruneResultModelMismatchError,
    DiffSidecarWriteError,
)
from signalforge.diff.models import DiffReport
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestNotNull,
)
from signalforge.grade.models import GradingReport, GradingResult
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneDecision, PruneResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model(name: str = "orders", unique_id: str = "model.shop.orders") -> Model:
    return Model(
        unique_id=unique_id,
        name=name,
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={
            "order_id": Column(name="order_id"),
            "customer_id": Column(name="customer_id"),
        },
        raw_code="select 1",
    )


def _make_candidate(name: str = "orders") -> CandidateSchema:
    """A small candidate carrying one column-scoped not_null test."""
    return CandidateSchema(
        name=name,
        description="orders fact table",
        rationale="grain: one row per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale="primary identifier",
                tests=(CandidateTestNotNull(column="order_id"),),
            ),
            CandidateColumn(
                name="customer_id",
                description="FK to customers",
                rationale="links to dim_customer",
            ),
        ),
    )


def _make_prune_result(
    *,
    model_unique_id: str = "model.shop.orders",
    decisions: tuple[PruneDecision, ...] = (),
) -> PruneResult:
    return PruneResult(
        model_unique_id=model_unique_id,
        decisions=decisions,
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )


def _kept_decision_for(test_anchor: str = "column.order_id") -> PruneDecision:
    return PruneDecision(
        test_anchor=test_anchor,
        test=CandidateTestNotNull(column="order_id"),
        decision="kept",
        reason="kept",
        failures=42,
        sampled_rows=1000,
        scope="sample",
        elapsed_ms=10,
        compiled_sql_hash="0" * 16,
        compiled_sql="select 1",
        why="ran on 1k sample, 42 failing rows",
    )


def _dropped_decision_for(test_anchor: str = "column.customer_id") -> PruneDecision:
    return PruneDecision(
        test_anchor=test_anchor,
        test=CandidateTestNotNull(column="customer_id"),
        decision="dropped",
        reason="always-passes",
        failures=0,
        sampled_rows=1000,
        scope="sample",
        elapsed_ms=10,
        compiled_sql_hash="0" * 16,
        compiled_sql="select 1",
        why="ran on 1k sample, 0 failing rows",
    )


def _grading_report_for(
    model_unique_id: str = "model.shop.orders",
    *,
    results: tuple[GradingResult, ...] = (),
) -> GradingReport:
    return GradingReport(
        signalforge_version="0.0.0-test",
        model_unique_id=model_unique_id,
        run_id="r" * 32,
        timestamp=datetime(2026, 5, 2, tzinfo=timezone.utc),
        rubric_hash="0" * 16,
        thresholds=(0.7, 0.5),
        results=results,
        duration_seconds=0.0,
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".signalforge").mkdir()
    return project


# ---------------------------------------------------------------------------
# 1. Boundary checks (DEC-002)
# ---------------------------------------------------------------------------


def test_candidate_model_mismatch_raises(project_dir: Path) -> None:
    """``candidate.name != model.name`` raises ``DiffCandidateModelMismatchError``."""
    model = _make_model(name="orders")
    candidate = _make_candidate(name="customers")  # mismatch
    prune_result = _make_prune_result()
    with pytest.raises(DiffCandidateModelMismatchError):
        render_diff(model, candidate, prune_result, project_dir=project_dir)


def test_prune_result_model_mismatch_raises(project_dir: Path) -> None:
    """``prune_result.model_unique_id != model.unique_id`` raises."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(model_unique_id="model.shop.other")  # mismatch
    with pytest.raises(DiffPruneResultModelMismatchError):
        render_diff(model, candidate, prune_result, project_dir=project_dir)


def test_grading_report_model_mismatch_raises(project_dir: Path) -> None:
    """``grading_report.model_unique_id != model.unique_id`` raises."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    grading_report = _grading_report_for(model_unique_id="model.shop.other")  # mismatch
    with pytest.raises(DiffGradingReportModelMismatchError):
        render_diff(
            model,
            candidate,
            prune_result,
            grading_report=grading_report,
            project_dir=project_dir,
        )


def test_grading_report_none_skips_check(project_dir: Path) -> None:
    """``grading_report=None`` skips the DEC-002 check (it's optional)."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    report = render_diff(
        model, candidate, prune_result, grading_report=None, project_dir=project_dir
    )
    assert report.grading_report_hash is None


# ---------------------------------------------------------------------------
# 2. existing_schema size cap (DEC-006) + soft warn (DEC-014)
# ---------------------------------------------------------------------------


def test_existing_schema_oversize_raises_before_safe_load(project_dir: Path) -> None:
    """An ``existing_schema`` exceeding the byte cap raises BEFORE
    ``yaml.safe_load`` runs.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    config = DiffConfig(existing_schema_size_limit_bytes=100)
    # 1000 bytes of YAML — well above the configured 100-byte cap.
    big_payload = "version: 2\n" + "# pad\n" * 200
    assert len(big_payload.encode("utf-8")) > 100
    with pytest.raises(DiffInputTooLargeError):
        render_diff(
            model,
            candidate,
            prune_result,
            existing_schema=big_payload,
            config=config,
            project_dir=project_dir,
        )


def test_existing_schema_soft_warn_emitted(
    project_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A payload above the warn-at threshold but below the hard cap
    emits a single ``WARNING`` log line.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    config = DiffConfig(
        existing_schema_size_limit_bytes=10_000,
        existing_schema_warn_at_bytes=100,
    )
    payload = "version: 2\n" + "# x: y\n" * 50
    encoded_size = len(payload.encode("utf-8"))
    assert encoded_size > 100
    assert encoded_size < 10_000

    with caplog.at_level(logging.WARNING, logger="signalforge.diff.engine"):
        render_diff(
            model,
            candidate,
            prune_result,
            existing_schema=payload,
            config=config,
            project_dir=project_dir,
        )

    warns = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warns) == 1
    # Lazy-format payload: the second positional arg is the JSON body.
    msg = warns[0].getMessage()
    assert "large existing schema.yml" in msg
    # Find the JSON segment in the formatted message and confirm it
    # carries the expected fields.
    payload_json = msg.split("large existing schema.yml: ", 1)[1]
    parsed = json.loads(payload_json)
    assert parsed["bytes"] == encoded_size
    assert parsed["model_unique_id"] == model.unique_id
    assert parsed["warn_at"] == 100


# ---------------------------------------------------------------------------
# 3. Renderer dispatch (DEC-004)
# ---------------------------------------------------------------------------


def test_render_kind_json_writes_json_to_output_path(project_dir: Path) -> None:
    """``render_kind="json"`` makes the orchestrator write the JSON body
    to ``output_path``.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    config = DiffConfig(render_kind="json")
    out = project_dir / "diff.json"

    render_diff(
        model,
        candidate,
        prune_result,
        config=config,
        output_path=out,
        project_dir=project_dir,
    )

    body = out.read_text(encoding="utf-8")
    parsed = json.loads(body)
    # JsonRenderer output mirrors DiffReport.model_dump_json shape.
    assert parsed["model_unique_id"] == model.unique_id
    assert parsed["schema_version"] == 1
    assert parsed["audit_schema_version"] == 1


def test_render_kind_ansi_writes_ansi_to_output_path(project_dir: Path) -> None:
    """``render_kind="ansi"`` (default) writes the ANSI text shape."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    config = DiffConfig()  # render_kind defaults to "ansi"
    out = project_dir / "diff.txt"

    render_diff(
        model,
        candidate,
        prune_result,
        config=config,
        output_path=out,
        project_dir=project_dir,
    )

    body = out.read_text(encoding="utf-8")
    # ANSI text contains the model_unique_id header line; not JSON.
    assert "diff: " in body
    assert "kept=" in body
    # Confirm it isn't JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)


def test_render_kind_markdown_writes_markdown_to_output_path(project_dir: Path) -> None:
    """``render_kind="markdown"`` writes Markdown shape."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    config = DiffConfig(render_kind="markdown")
    out = project_dir / "diff.md"

    render_diff(
        model,
        candidate,
        prune_result,
        config=config,
        output_path=out,
        project_dir=project_dir,
    )

    body = out.read_text(encoding="utf-8")
    assert body.startswith("# Diff:")


# ---------------------------------------------------------------------------
# 4. JsonRenderer round-trip
# ---------------------------------------------------------------------------


def test_json_renderer_round_trips_through_json_loads(project_dir: Path) -> None:
    """``model_dump_json(indent=2)`` parses back to a dict whose
    top-level fields match the report's metadata.
    """
    from signalforge.diff._renderers import JsonRenderer

    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(decisions=(_kept_decision_for(),))
    report = render_diff(model, candidate, prune_result, project_dir=project_dir)
    text = JsonRenderer().render(report)
    parsed = json.loads(text)
    assert parsed["model_unique_id"] == model.unique_id
    assert parsed["kept_count"] == report.kept_count
    assert parsed["dropped_count"] == report.dropped_count


# ---------------------------------------------------------------------------
# 5. Sidecar write (US-007 integration)
# ---------------------------------------------------------------------------


def test_sidecar_write_happens_when_path_provided(project_dir: Path) -> None:
    """Supplying ``sidecar_path`` causes the sidecar JSON to land on disk."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    sidecar = project_dir / ".signalforge" / "diff.json"

    render_diff(
        model,
        candidate,
        prune_result,
        sidecar_path=sidecar,
        project_dir=project_dir,
    )

    assert sidecar.exists()
    parsed = json.loads(sidecar.read_text(encoding="utf-8"))
    assert parsed["model_unique_id"] == model.unique_id


def test_sidecar_not_written_when_path_omitted(project_dir: Path) -> None:
    """No ``sidecar_path`` arg means no sidecar lands on disk."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    render_diff(model, candidate, prune_result, project_dir=project_dir)

    # Default sidecar path the writer would land at.
    sidecar = project_dir / ".signalforge" / "diff.json"
    assert not sidecar.exists()


# ---------------------------------------------------------------------------
# 6. Symlink containment (DiffSidecarWriteError)
# ---------------------------------------------------------------------------


def test_sidecar_path_outside_project_raises(project_dir: Path, tmp_path: Path) -> None:
    """A ``sidecar_path`` that escapes the project tree raises
    :class:`DiffSidecarWriteError`.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    # Absolute path outside the project tree.
    outside = tmp_path / "outside" / "diff.json"

    with pytest.raises(DiffSidecarWriteError):
        render_diff(
            model,
            candidate,
            prune_result,
            sidecar_path=outside,
            project_dir=project_dir,
        )

    # No on-disk artefact left behind by the failed-loud path.
    assert not outside.exists()


def test_output_path_outside_project_raises(project_dir: Path, tmp_path: Path) -> None:
    """An ``output_path`` that escapes the project tree raises
    :class:`DiffSidecarWriteError`.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    outside = tmp_path / "outside" / "diff.txt"
    with pytest.raises(DiffSidecarWriteError):
        render_diff(
            model,
            candidate,
            prune_result,
            output_path=outside,
            project_dir=project_dir,
        )


# ---------------------------------------------------------------------------
# 7. INFO log (DEC-015)
# ---------------------------------------------------------------------------


def test_info_log_emitted_at_happy_path_end(
    project_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A single ``logging.INFO`` event lands at the end of a happy path
    with a lazy-format JSON payload carrying every documented field.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(decisions=(_kept_decision_for(),))

    with caplog.at_level(logging.INFO, logger="signalforge.diff.engine"):
        render_diff(model, candidate, prune_result, project_dir=project_dir)

    infos = [rec for rec in caplog.records if rec.levelname == "INFO"]
    assert len(infos) == 1
    msg = infos[0].getMessage()
    assert "rendered diff: " in msg
    payload_json = msg.split("rendered diff: ", 1)[1]
    parsed = json.loads(payload_json)
    for key in (
        "run_id",
        "model_unique_id",
        "render_kind",
        "kept",
        "dropped",
        "flagged",
        "has_existing_schema",
        "duration_seconds",
        "candidate_hash",
        "prune_result_hash",
        "grading_report_hash",
    ):
        assert key in parsed
    assert parsed["model_unique_id"] == model.unique_id
    assert parsed["kept"] == 1
    assert parsed["dropped"] == 0
    assert parsed["has_existing_schema"] is False


# ---------------------------------------------------------------------------
# 8. End-to-end happy path
# ---------------------------------------------------------------------------


def test_end_to_end_kept_dropped_flagged_entries(project_dir: Path) -> None:
    """The assembled :class:`DiffReport` carries kept + dropped entries
    derived from the prune result and a ``flagged`` entry derived from a
    failing grading aggregate.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(
        decisions=(
            _kept_decision_for(),  # column.order_id, kept
            _dropped_decision_for(),  # column.customer_id, dropped
        )
    )
    # Provide a failing grading result against the kept test → flagged.
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="test.column.order_id.not_null",
                criterion_id="clarity",
                score=0.4,
                passed=False,
                evidence="",
                reasoning="below threshold",
            ),
        )
    )

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=grading_report,
        project_dir=project_dir,
    )

    assert isinstance(report, DiffReport)
    assert report.model_unique_id == model.unique_id
    assert report.has_existing_schema is False
    assert report.unified_diff.startswith("--- /dev/null")
    # Entries: optionally model-level doc rows aren't graded here so
    # they don't surface; we expect the 2 prune decisions plus
    # potentially per-column doc rows depending on graded results.
    artifact_ids = {e.artifact_id for e in report.entries}
    assert "test.column.order_id.not_null" in artifact_ids
    assert "test.column.customer_id.not_null" in artifact_ids

    # Tier counts: the kept order_id test is flagged because the
    # aggregate score is < 1.0 with passed=False; the dropped
    # customer_id test stays "dropped".
    flagged_entries = [e for e in report.entries if e.tier == "flagged"]
    assert len(flagged_entries) == 1
    assert flagged_entries[0].artifact_id == "test.column.order_id.not_null"
    assert report.flagged_count == 1

    dropped_entries = [e for e in report.entries if e.tier == "dropped"]
    assert len(dropped_entries) == 1
    assert dropped_entries[0].drop_reason == "always-passes"

    # Hashes (DEC-016).
    assert len(report.candidate_hash) == 16
    assert len(report.prune_result_hash) == 16
    assert report.grading_report_hash is not None
    assert len(report.grading_report_hash) == 16


def test_existing_schema_none_produces_dev_null_unified_diff(project_dir: Path) -> None:
    """``existing_schema=None`` sources the unified diff from ``/dev/null``."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    report = render_diff(
        model, candidate, prune_result, existing_schema=None, project_dir=project_dir
    )
    assert report.has_existing_schema is False
    assert report.existing_yaml is None
    assert "/dev/null" in report.unified_diff


def test_existing_schema_supplied_appears_in_report(project_dir: Path) -> None:
    """An ``existing_schema`` payload round-trips into the report's
    ``existing_yaml`` field and the unified diff.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    existing = "version: 2\nmodels:\n  - name: orders\n"
    report = render_diff(
        model,
        candidate,
        prune_result,
        existing_schema=existing,
        project_dir=project_dir,
    )
    assert report.has_existing_schema is True
    assert report.existing_yaml == existing
    # Header tofile reflects the model name.
    assert "b/models/orders.yml" in report.unified_diff


def test_unused_helpers_smoke() -> None:
    """Sanity check: the engine module exposes only ``render_diff``
    publicly per DEC-001 of #8.
    """
    from signalforge.diff import engine

    assert engine.__all__ == ("render_diff",)
    # The os module import isn't part of the public surface; this test
    # only confirms the public surface stayed minimal.
    assert "os" not in engine.__all__
    # Defence against accidental re-exports of internals.
    _ = os
