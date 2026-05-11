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
    DiffSidecarRecordTooLargeError,
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
    config = DiffConfig(
        existing_schema_size_limit_bytes=100,
        existing_schema_warn_at_bytes=50,
    )
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

    warns = [
        rec
        for rec in caplog.records
        if rec.name == "signalforge.diff.engine" and rec.levelno == logging.WARNING
    ]
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


def test_sidecar_oversize_raises_via_orchestrator(project_dir: Path) -> None:
    """End-to-end: ``render_diff(..., sidecar_path=...)`` with a config
    whose ``sidecar_size_limit_bytes`` is below the serialised report size
    surfaces :class:`DiffSidecarRecordTooLargeError` from the writer up
    through the orchestrator. Pins the wiring of
    ``DiffConfig.sidecar_size_limit_bytes`` (post-QG fix; the knob was
    silently ignored in the original implementation)."""
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    sidecar = project_dir / ".signalforge" / "diff.json"
    # 100-byte cap is well below any non-trivial report payload.
    config = DiffConfig(sidecar_size_limit_bytes=100)

    with pytest.raises(DiffSidecarRecordTooLargeError):
        render_diff(
            model,
            candidate,
            prune_result,
            config=config,
            sidecar_path=sidecar,
            project_dir=project_dir,
        )

    # Pre-write size check fires BEFORE any os.open — no on-disk artefact.
    assert not sidecar.exists()


def test_sidecar_default_path_used_when_path_omitted(project_dir: Path) -> None:
    """``sidecar_path is None`` and ``write_sidecar=True`` (default) lands
    the sidecar at ``<project_dir>/.signalforge/diff.json`` (post-QG fix
    #5, Q1=A — the diff sidecar is now an always-on durable record).
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    render_diff(model, candidate, prune_result, project_dir=project_dir)

    sidecar = project_dir / ".signalforge" / "diff.json"
    assert sidecar.exists()
    parsed = json.loads(sidecar.read_text(encoding="utf-8"))
    assert parsed["model_unique_id"] == model.unique_id


def test_sidecar_disabled_via_write_sidecar_false(project_dir: Path) -> None:
    """``write_sidecar=False`` skips the sidecar regardless of
    ``sidecar_path`` (post-QG fix #5).
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    render_diff(
        model,
        candidate,
        prune_result,
        write_sidecar=False,
        project_dir=project_dir,
    )

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

    infos = [
        rec
        for rec in caplog.records
        if rec.name == "signalforge.diff.engine" and rec.levelno == logging.INFO
    ]
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
    # The candidate carries 6 doc rows (model + 2 columns × 2 fields)
    # plus 1 kept prune decision; post-QG fix #2 surfaces every doc
    # row as ``tier=kept``.
    assert parsed["kept"] == 7
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
    """Sanity check: the engine module exposes ``render_diff`` plus
    the ``render_to_text`` in-process helper added in US-001 of #9.
    """
    from signalforge.diff import engine

    assert engine.__all__ == ("render_diff", "render_to_text")
    # The os module import isn't part of the public surface; this test
    # only confirms the public surface stayed minimal.
    assert "os" not in engine.__all__
    # Defence against accidental re-exports of internals.
    _ = os


# ---------------------------------------------------------------------------
# 9. Post-QG fix #1 — mkdir wraps as DiffSidecarWriteError
# ---------------------------------------------------------------------------


def test_output_path_mkdir_failure_raises_diff_sidecar_write_error(tmp_path: Path) -> None:
    """When ``project_dir``'s parent is an existing FILE (not a
    directory), ``mkdir(parents=True, exist_ok=True)`` raises
    ``FileExistsError``. Post-QG fix #1: that error is wrapped as
    :class:`DiffSidecarWriteError` so callers branch on the diff-layer
    error type rather than catching a raw ``OSError``.
    """
    # tmp_path/blocker is a regular file; project_dir = tmp_path/blocker/proj
    # — mkdir(parents=True) will fail because blocker is a file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    project_dir = blocker / "proj"

    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    output = project_dir / "diff.txt"

    with pytest.raises(DiffSidecarWriteError):
        render_diff(
            model,
            candidate,
            prune_result,
            output_path=output,
            project_dir=project_dir,
            write_sidecar=False,
        )


def test_sidecar_path_mkdir_failure_raises_diff_sidecar_write_error(tmp_path: Path) -> None:
    """Same as above for the sidecar-path branch (post-QG fix #1)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    project_dir = blocker / "proj"

    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()
    sidecar = project_dir / ".signalforge" / "diff.json"

    with pytest.raises(DiffSidecarWriteError):
        render_diff(
            model,
            candidate,
            prune_result,
            sidecar_path=sidecar,
            project_dir=project_dir,
        )


# ---------------------------------------------------------------------------
# 10. Post-QG fix #2 — doc artifacts always emit DiffEntry
# ---------------------------------------------------------------------------


def test_doc_artifacts_always_emit_entry_without_grading(project_dir: Path) -> None:
    """Per Architectural Commitment #5 (one-line "why" per artifact),
    every present description / rationale field on the candidate
    produces a :class:`DiffEntry` even when ``grading_report=None``
    (post-QG fix #2). The fallback ``why`` is ``"kept (no grading)"``.
    """
    model = _make_model()
    candidate = _make_candidate()  # has model + 2 column descriptions / rationales
    prune_result = _make_prune_result()

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=None,
        project_dir=project_dir,
    )

    artifact_ids = {e.artifact_id for e in report.entries}
    # Model-level doc rows.
    assert "model.description" in artifact_ids
    assert "model.rationale" in artifact_ids
    # Per-column doc rows for both columns the candidate carries.
    assert "column.order_id.description" in artifact_ids
    assert "column.order_id.rationale" in artifact_ids
    assert "column.customer_id.description" in artifact_ids
    assert "column.customer_id.rationale" in artifact_ids

    # Every doc row is tier=kept with score=None, passed=None,
    # why="kept (no grading)".
    doc_rows = [e for e in report.entries if e.test_type is None]
    assert len(doc_rows) == 6
    for row in doc_rows:
        assert row.tier == "kept"
        assert row.score is None
        assert row.passed is None
        assert row.why == "kept (no grading)"


# ---------------------------------------------------------------------------
# 11. Post-QG fix #3 — flagged-tier why reflects grading reason
# ---------------------------------------------------------------------------


def test_flagged_tier_why_uses_grading_reason_not_prune_reason(project_dir: Path) -> None:
    """When a kept artifact is flipped to ``tier="flagged"`` because of a
    failing :class:`GradingResult`, the row's ``why`` reflects the
    GRADING reason — never the prune ``decision.why`` (post-QG fix #3).
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(decisions=(_kept_decision_for(),))
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="test.column.order_id.not_null",
                criterion_id="clarity",
                score=0.4,
                passed=False,
                evidence="",
                reasoning="vague description; reword to add explicit FK semantics",
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

    flagged_entries = [e for e in report.entries if e.tier == "flagged"]
    assert len(flagged_entries) == 1
    flagged = flagged_entries[0]
    assert flagged.artifact_id == "test.column.order_id.not_null"
    # The flagged-tier why comes from grading, not from the prune
    # decision — the kept decision's ``why`` was "ran on 1k sample, 42
    # failing rows", which must NOT appear here.
    assert "ran on 1k sample" not in flagged.why
    assert "42 failing rows" not in flagged.why
    # The grading-derived why mentions the criterion + reasoning.
    assert "failed grading" in flagged.why
    assert "clarity" in flagged.why
    assert "vague description" in flagged.why


# ---------------------------------------------------------------------------
# 12. Post-QG fix #4 — structural args_hash join survives JSON rehydration
# ---------------------------------------------------------------------------


def test_structural_keying_survives_prune_result_json_roundtrip(project_dir: Path) -> None:
    """A :class:`PruneResult` round-tripped through JSON (so its
    inner :class:`CandidateTest` objects are NEW instances — different
    ``id()``) must produce the same artifact_ids in the rendered diff
    as the in-memory case (post-QG fix #4). Cross-stage parity with the
    grade engine is the load-bearing invariant.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(decisions=(_kept_decision_for(),))

    in_memory_report = render_diff(
        model,
        candidate,
        prune_result,
        project_dir=project_dir,
        write_sidecar=False,
    )
    in_memory_ids = {e.artifact_id for e in in_memory_report.entries}

    # Round-trip through JSON: dump, load, validate as a new PruneResult.
    rehydrated_prune = PruneResult.model_validate_json(prune_result.model_dump_json())
    # Sanity: the inner CandidateTest is a fresh object (different id()).
    assert id(rehydrated_prune.decisions[0].test) != id(prune_result.decisions[0].test)

    rehydrated_report = render_diff(
        model,
        candidate,
        rehydrated_prune,
        project_dir=project_dir,
        write_sidecar=False,
    )
    rehydrated_ids = {e.artifact_id for e in rehydrated_report.entries}

    assert in_memory_ids == rehydrated_ids
    assert "test.column.order_id.not_null" in rehydrated_ids


def test_distinct_accepted_values_get_distinct_artifact_ids(project_dir: Path) -> None:
    """Two ``accepted_values`` tests on the SAME column with different
    ``values`` lists collide on (scope, column, type); the
    args_hash suffix disambiguates them so the artifact_ids stay
    distinct (post-QG fix #4 collision rule).
    """
    from signalforge.draft.models import CandidateTestAcceptedValues

    # Candidate carries two accepted_values tests on order_id with
    # different values — same type, different args.
    test_a = CandidateTestAcceptedValues(column="order_id", values=("a", "b"))
    test_b = CandidateTestAcceptedValues(column="order_id", values=("c", "d"))
    candidate = CandidateSchema(
        name="orders",
        description="orders fact",
        rationale="grain: per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale="primary identifier",
                tests=(test_a, test_b),
            ),
            CandidateColumn(
                name="customer_id",
                description="FK",
                rationale="links",
            ),
        ),
    )
    decisions = (
        PruneDecision(
            test_anchor="column.order_id",
            test=test_a,
            decision="kept",
            reason="kept",
            failures=1,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 1 failing row",
        ),
        PruneDecision(
            test_anchor="column.order_id",
            test=test_b,
            decision="kept",
            reason="kept",
            failures=2,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 2 failing rows",
        ),
    )
    prune_result = _make_prune_result(decisions=decisions)

    model = _make_model()
    report = render_diff(
        model,
        candidate,
        prune_result,
        project_dir=project_dir,
        write_sidecar=False,
    )
    test_artifact_ids = [e.artifact_id for e in report.entries if e.test_type == "accepted_values"]
    # The two accepted_values tests must surface with distinct
    # artifact_ids — the args_hash suffix disambiguates.
    assert len(test_artifact_ids) == 2
    assert test_artifact_ids[0] != test_artifact_ids[1]
    # Both carry the args_hash suffix shape (5 dotted parts).
    for aid in test_artifact_ids:
        assert aid.startswith("test.column.order_id.accepted_values.")


# ---------------------------------------------------------------------------
# 13. Post-QG fix #6 — duration_seconds captured AFTER writes
# ---------------------------------------------------------------------------


def test_info_log_duration_includes_write_phase(
    project_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The INFO-log ``duration_seconds`` field is captured AFTER the
    sidecar write completes (post-QG fix #6), so it reflects the full
    wall-clock including renderer + writes. Sanity check: duration is
    a non-negative float.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    with caplog.at_level(logging.INFO, logger="signalforge.diff.engine"):
        report = render_diff(model, candidate, prune_result, project_dir=project_dir)

    infos = [
        rec
        for rec in caplog.records
        if rec.name == "signalforge.diff.engine" and rec.levelno == logging.INFO
    ]
    assert len(infos) == 1
    payload_json = infos[0].getMessage().split("rendered diff: ", 1)[1]
    parsed = json.loads(payload_json)
    assert isinstance(parsed["duration_seconds"], float)
    assert parsed["duration_seconds"] >= 0.0
    # Regression: the persisted report's duration matches the logged
    # value byte-for-byte; the sidecar JSON shares the same value
    # because the sidecar write happens after the duration refresh.
    assert report.duration_seconds == parsed["duration_seconds"]


# ---------------------------------------------------------------------------
# 11. Exact-duplicate test artifact_id parity (post-second-pass review fix)
# ---------------------------------------------------------------------------


def test_exact_duplicate_not_null_tests_get_distinct_artifact_ids(project_dir: Path) -> None:
    """Two byte-identical ``not_null`` tests on the same column must
    produce distinct artifact_ids — the first keeps the bare hash; the
    second gets a ``:1`` ordinal suffix matching grade engine's
    :func:`signalforge.grade.engine._test_args_hashes`.

    Without this, the JSONL ``(run_id, artifact_id, criterion_id)``
    triple would collide and the diff renderer / grade-sidecar join
    would silently drop duplicate rows.
    """
    nn1 = CandidateTestNotNull(column="order_id")
    nn2 = CandidateTestNotNull(column="order_id")  # exact duplicate
    candidate = CandidateSchema(
        name="orders",
        description="orders fact",
        rationale="grain: per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale="primary identifier",
                tests=(nn1, nn2),
            ),
        ),
    )
    decisions = (
        PruneDecision(
            test_anchor="column.order_id",
            test=nn1,
            decision="kept",
            reason="kept",
            failures=1,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 1 failing row",
        ),
        PruneDecision(
            test_anchor="column.order_id",
            test=nn2,
            decision="kept",
            reason="kept",
            failures=2,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 2 failing rows",
        ),
    )
    model = Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"order_id": Column(name="order_id")},
        raw_code="select 1",
    )
    prune_result = _make_prune_result(decisions=decisions)

    report = render_diff(
        model,
        candidate,
        prune_result,
        project_dir=project_dir,
        write_sidecar=False,
    )
    test_aids = [e.artifact_id for e in report.entries if e.test_type == "not_null"]
    # Two distinct artifact_ids — one for each duplicate.
    assert len(test_aids) == 2
    assert len(set(test_aids)) == 2
    # First keeps the bare hash; second gets ``:1`` ordinal.
    assert any(":1" in aid for aid in test_aids)


def test_exact_duplicate_accepted_values_get_distinct_artifact_ids(project_dir: Path) -> None:
    """Same as the not_null case but for accepted_values with identical
    values lists — exact duplicates must still get distinct
    artifact_ids via the ``:1`` ordinal suffix."""
    from signalforge.draft.models import CandidateTestAcceptedValues

    av1 = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    av2 = CandidateTestAcceptedValues(column="status", values=("a", "b"))  # exact duplicate
    candidate = CandidateSchema(
        name="orders",
        description="orders fact",
        rationale="grain: per order",
        columns=(
            CandidateColumn(
                name="status",
                description="status enum",
                rationale="state machine",
                tests=(av1, av2),
            ),
        ),
    )
    decisions = (
        PruneDecision(
            test_anchor="column.status",
            test=av1,
            decision="kept",
            reason="kept",
            failures=1,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 1 failing row",
        ),
        PruneDecision(
            test_anchor="column.status",
            test=av2,
            decision="dropped",
            reason="always-passes",
            failures=0,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 0 failing rows",
        ),
    )
    model = Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"status": Column(name="status")},
        raw_code="select 1",
    )
    prune_result = _make_prune_result(decisions=decisions)

    report = render_diff(
        model,
        candidate,
        prune_result,
        project_dir=project_dir,
        write_sidecar=False,
    )
    test_aids = [e.artifact_id for e in report.entries if e.test_type == "accepted_values"]
    assert len(test_aids) == 2
    assert len(set(test_aids)) == 2
    assert any(":1" in aid for aid in test_aids)


def test_exact_duplicate_cross_stage_parity_with_grade_engine(project_dir: Path) -> None:
    """The diff engine's exact-duplicate artifact_ids match what the
    grade engine produces byte-for-byte. This is the load-bearing
    invariant that guarantees the diff / grade JSONL join survives
    duplicate-test scenarios."""
    from signalforge.grade.engine import (
        _artifact_id_for as _grade_artifact_id_for,
    )
    from signalforge.grade.engine import (
        _test_args_hashes as _grade_test_args_hashes,
    )

    nn1 = CandidateTestNotNull(column="order_id")
    nn2 = CandidateTestNotNull(column="order_id")  # exact duplicate
    candidate = CandidateSchema(
        name="orders",
        description="orders fact",
        rationale="grain: per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale="primary identifier",
                tests=(nn1, nn2),
            ),
        ),
    )
    # Grade-side: id()-keyed; iterate column tests in declared order.
    grade_args = _grade_test_args_hashes(candidate)
    grade_aids = [
        _grade_artifact_id_for(
            scope="column",
            column_name="order_id",
            test=nn1,
            args_hash=grade_args[id(nn1)],
        ),
        _grade_artifact_id_for(
            scope="column",
            column_name="order_id",
            test=nn2,
            args_hash=grade_args[id(nn2)],
        ),
    ]

    # Diff-side: structural-key queue; build matching prune decisions.
    decisions = (
        PruneDecision(
            test_anchor="column.order_id",
            test=nn1,
            decision="kept",
            reason="kept",
            failures=1,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 1 failing row",
        ),
        PruneDecision(
            test_anchor="column.order_id",
            test=nn2,
            decision="kept",
            reason="kept",
            failures=2,
            sampled_rows=100,
            scope="sample",
            elapsed_ms=1,
            compiled_sql_hash="0" * 16,
            compiled_sql="select 1",
            why="ran on 100 sample, 2 failing rows",
        ),
    )
    model = Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"order_id": Column(name="order_id")},
        raw_code="select 1",
    )
    prune_result = _make_prune_result(decisions=decisions)

    report = render_diff(
        model,
        candidate,
        prune_result,
        project_dir=project_dir,
        write_sidecar=False,
    )
    diff_aids = [e.artifact_id for e in report.entries if e.test_type == "not_null"]
    # Set equality: both stages produce the same artifact_id pair.
    assert set(diff_aids) == set(grade_aids), (
        f"Cross-stage parity break: diff={diff_aids!r} grade={grade_aids!r}"
    )


# ---------------------------------------------------------------------------
# Issue #41 — rationale/evidence threaded into DiffEntry.why for kept rows
# ---------------------------------------------------------------------------


class TestTruncateWhy:
    """Unit coverage for :func:`signalforge.diff.engine._truncate_why`.

    The helper was extracted from :func:`_flagged_why`'s truncation step
    so the same one-line cap applies to the rationale/evidence cascade.
    Empty / whitespace-only input returns ``""`` so the caller's cascade
    can fall through to the next candidate.
    """

    def test_empty_string_returns_empty(self) -> None:
        from signalforge.diff.engine import _truncate_why

        assert _truncate_why("", 80) == ""

    def test_whitespace_only_returns_empty(self) -> None:
        from signalforge.diff.engine import _truncate_why

        # Caller's cascade depends on whitespace-only treated as empty.
        assert _truncate_why("   \t\n  ", 80) == ""

    def test_exact_max_chars_no_ellipsis(self) -> None:
        from signalforge.diff.engine import _truncate_why

        text = "a" * 10
        assert _truncate_why(text, 10) == text

    def test_one_below_max_chars_no_ellipsis(self) -> None:
        from signalforge.diff.engine import _truncate_why

        text = "a" * 9
        assert _truncate_why(text, 10) == text

    def test_one_above_max_chars_truncates_with_ellipsis(self) -> None:
        from signalforge.diff.engine import _truncate_why

        text = "a" * 11
        out = _truncate_why(text, 10)
        assert out.endswith("…")
        # max_chars - 1 chars of content + 1-char ellipsis = max_chars
        # display width.
        assert len(out) == 10

    def test_multibyte_emoji_truncates(self) -> None:
        from signalforge.diff.engine import _truncate_why

        # 30 crab emoji — each is one Python char but four UTF-8 bytes.
        # Helper measures Python char length, NOT bytes, so a 30-char
        # input over a 10-char budget truncates predictably.
        text = "🦀" * 30
        out = _truncate_why(text, 10)
        assert out.endswith("…")
        assert len(out) == 10
        # The truncated body should still be crab emoji (no broken
        # surrogate pairs since Python uses Unicode code points).
        assert out[:-1] == "🦀" * 9

    def test_non_positive_max_chars_returns_empty(self) -> None:
        from signalforge.diff.engine import _truncate_why

        assert _truncate_why("anything", 0) == ""
        assert _truncate_why("anything", -1) == ""


def test_kept_test_why_prefers_candidate_rationale(project_dir: Path) -> None:
    """Issue #41 cascade tier 1: a kept test with a non-empty rationale
    on the ``CandidateTest`` surfaces that rationale in
    ``DiffEntry.why`` instead of the prune boilerplate.
    """
    model = _make_model()
    rationale_text = "grain assertion: order_id must be non-null per Stripe webhook contract"
    candidate = CandidateSchema(
        name="orders",
        description="orders fact table",
        rationale="grain: one row per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale="primary identifier",
                tests=(CandidateTestNotNull(column="order_id", rationale=rationale_text),),
            ),
        ),
    )
    decision = PruneDecision(
        test_anchor="column.order_id",
        test=CandidateTestNotNull(column="order_id", rationale=rationale_text),
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
    prune_result = _make_prune_result(decisions=(decision,))

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=None,
        project_dir=project_dir,
    )

    kept_test_rows = [e for e in report.entries if e.test_type == "not_null" and e.tier == "kept"]
    assert len(kept_test_rows) == 1
    why = kept_test_rows[0].why
    # The rationale text is visible in the why; the boilerplate prune
    # decision.why is NOT.
    assert "grain assertion" in why
    assert "Stripe webhook" in why
    assert "1k sample" not in why
    assert "42 failing rows" not in why


def test_kept_test_why_cascades_to_grading_evidence(project_dir: Path) -> None:
    """Issue #41 cascade tier 2: a kept test with ``rationale=None``
    falls back to :attr:`GradingResult.evidence` for ``DiffEntry.why``.
    """
    model = _make_model()
    candidate = _make_candidate()  # rationale=None on the order_id not_null test
    # Use a passing grade so the row stays kept (not flagged).
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="test.column.order_id.not_null",
                criterion_id="clarity",
                score=0.95,
                passed=True,
                evidence="rubric saw 0 NULLs across 10k-row sample",
                reasoning="passed",
            ),
        )
    )
    prune_result = _make_prune_result(decisions=(_kept_decision_for(),))

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=grading_report,
        project_dir=project_dir,
    )

    kept_test_rows = [e for e in report.entries if e.test_type == "not_null" and e.tier == "kept"]
    assert len(kept_test_rows) == 1
    why = kept_test_rows[0].why
    assert "rubric saw 0 NULLs" in why
    # The prune boilerplate is NOT used when evidence is available.
    assert "1k sample" not in why


def test_kept_test_why_falls_back_to_decision_why(project_dir: Path) -> None:
    """Issue #41 cascade tier 3: with rationale=None AND evidence empty,
    the kept test's ``why`` falls back to ``decision.why`` (the existing
    pre-#41 source).
    """
    model = _make_model()
    candidate = _make_candidate()  # rationale=None
    # Passing grade with empty evidence → cascade falls through both
    # candidate.rationale and grading.evidence to decision.why.
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="test.column.order_id.not_null",
                criterion_id="clarity",
                score=0.95,
                passed=True,
                evidence="",
                reasoning="passed",
            ),
        )
    )
    prune_result = _make_prune_result(decisions=(_kept_decision_for(),))

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=grading_report,
        project_dir=project_dir,
    )

    kept_test_rows = [e for e in report.entries if e.test_type == "not_null" and e.tier == "kept"]
    assert len(kept_test_rows) == 1
    assert kept_test_rows[0].why == "ran on 1k sample, 42 failing rows"


def test_kept_test_why_skips_whitespace_only_rationale(project_dir: Path) -> None:
    """Cascade guard: a rationale that's whitespace-only (e.g. the LLM
    emitted ``"   "``) is treated as empty so the cascade descends to
    evidence rather than surfacing a blank cell.
    """
    model = _make_model()
    candidate = CandidateSchema(
        name="orders",
        description="orders fact table",
        rationale="grain: one row per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale="primary identifier",
                tests=(CandidateTestNotNull(column="order_id", rationale="   \n\t  "),),
            ),
        ),
    )
    decision = PruneDecision(
        test_anchor="column.order_id",
        test=CandidateTestNotNull(column="order_id", rationale="   \n\t  "),
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
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="test.column.order_id.not_null",
                criterion_id="clarity",
                score=0.95,
                passed=True,
                evidence="evidence from grader survives the cascade",
                reasoning="passed",
            ),
        )
    )
    prune_result = _make_prune_result(decisions=(decision,))

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=grading_report,
        project_dir=project_dir,
    )

    kept_test_rows = [e for e in report.entries if e.test_type == "not_null" and e.tier == "kept"]
    assert len(kept_test_rows) == 1
    assert "evidence from grader" in kept_test_rows[0].why


def test_kept_doc_why_prefers_candidate_rationale_over_grading(project_dir: Path) -> None:
    """Issue #41 cascade for doc rows: a kept-with-grading column
    description row prefers the column's ``rationale`` over the
    grader's ``evidence`` and over the pre-#41 ``reasoning`` source.
    """
    model = _make_model()
    column_rationale = "primary identifier — survives FK refactors"
    candidate = CandidateSchema(
        name="orders",
        description="orders fact table",
        rationale="grain: one row per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale=column_rationale,
            ),
            CandidateColumn(
                name="customer_id",
                description="FK to customers",
                rationale="links to dim_customer",
            ),
        ),
    )
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="column.order_id.description",
                criterion_id="clarity",
                score=0.95,
                passed=True,
                evidence="evidence text that must not surface — rationale wins",
                reasoning="reasoning text that must not surface either",
            ),
        )
    )
    prune_result = _make_prune_result()

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=grading_report,
        project_dir=project_dir,
    )

    desc_rows = [e for e in report.entries if e.artifact_id == "column.order_id.description"]
    assert len(desc_rows) == 1
    why = desc_rows[0].why
    assert "primary identifier" in why
    assert "survives FK refactors" in why
    assert "evidence text" not in why
    assert "reasoning text" not in why


def test_kept_doc_why_cascades_to_evidence_when_rationale_missing(
    project_dir: Path,
) -> None:
    """Issue #41 cascade tier 2 for docs: a kept doc row with no
    ``CandidateColumn.rationale`` falls back to the first non-empty
    :attr:`GradingResult.evidence`.
    """
    model = _make_model()
    candidate = CandidateSchema(
        name="orders",
        description="orders fact table",
        rationale="grain: one row per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale=None,
            ),
            CandidateColumn(
                name="customer_id",
                description="FK to customers",
                rationale="links to dim_customer",
            ),
        ),
    )
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="column.order_id.description",
                criterion_id="clarity",
                score=0.95,
                passed=True,
                evidence="grader observed: covers all dbt seed rows",
                reasoning="passed",
            ),
        )
    )
    prune_result = _make_prune_result()

    report = render_diff(
        model,
        candidate,
        prune_result,
        grading_report=grading_report,
        project_dir=project_dir,
    )

    desc_rows = [e for e in report.entries if e.artifact_id == "column.order_id.description"]
    assert len(desc_rows) == 1
    assert "covers all dbt seed rows" in desc_rows[0].why


def test_end_to_end_kept_test_why_threads_rationale(project_dir: Path) -> None:
    """Replacement for the pre-#41 kept-row expectation in
    :func:`test_end_to_end_kept_dropped_flagged_entries`. With grading
    that PASSES, the kept test row's ``why`` carries the candidate's
    rationale rather than the prune boilerplate.
    """
    model = _make_model()
    rationale_text = "not-null guard for the surrogate key — joins fail silently otherwise"
    candidate = CandidateSchema(
        name="orders",
        description="orders fact table",
        rationale="grain: one row per order",
        columns=(
            CandidateColumn(
                name="order_id",
                description="surrogate key",
                rationale="primary identifier",
                tests=(CandidateTestNotNull(column="order_id", rationale=rationale_text),),
            ),
            CandidateColumn(
                name="customer_id",
                description="FK to customers",
                rationale="links to dim_customer",
            ),
        ),
    )
    decision = PruneDecision(
        test_anchor="column.order_id",
        test=CandidateTestNotNull(column="order_id", rationale=rationale_text),
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
    prune_result = _make_prune_result(
        decisions=(
            decision,
            _dropped_decision_for(),
        )
    )
    grading_report = _grading_report_for(
        results=(
            GradingResult(
                artifact_id="test.column.order_id.not_null",
                criterion_id="clarity",
                score=0.9,
                passed=True,
                evidence="0 NULLs in 10k sample",
                reasoning="passed",
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

    kept_test_rows = [e for e in report.entries if e.test_type == "not_null" and e.tier == "kept"]
    assert len(kept_test_rows) == 1
    why = kept_test_rows[0].why
    # Threaded rationale — not the prune boilerplate.
    assert "not-null guard" in why
    assert "joins fail silently" in why
    assert "1k sample" not in why
    # Dropped row keeps its prune why verbatim.
    dropped_rows = [e for e in report.entries if e.tier == "dropped"]
    assert len(dropped_rows) == 1
    assert dropped_rows[0].why == "ran on 1k sample, 0 failing rows"
