"""Tests for ``signalforge.diff.render_to_text`` (US-001 of issue #9).

DEC-015 of ``plans/super/9-cli-entrypoint.md``: the CLI needs a
public, in-process helper that returns the same bytes
:func:`signalforge.diff.render_diff` would have written to
``output_path``, so the CLI can pipe to stdout without going through a
disk artefact.

DEC-022: :class:`signalforge.diff.DiffReport` does NOT carry the
:class:`signalforge.diff.DiffConfig` used by the original
:func:`render_diff` call (verified against
:file:`src/signalforge/diff/models.py`), so the helper takes the
config explicitly. The CLI threads its own resolved config through.

The byte-equal property is the load-bearing invariant — operators
piping to stdout get exactly what they would have read off disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.diff import render_diff, render_to_text
from signalforge.diff.config import DiffConfig
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestNotNull,
)
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneDecision, PruneResult

# ---------------------------------------------------------------------------
# Helpers (mirror tests/diff/test_engine.py shape)
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


def _kept_decision_for() -> PruneDecision:
    return PruneDecision(
        test_anchor="column.order_id",
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


def _dropped_decision_for() -> PruneDecision:
    return PruneDecision(
        test_anchor="column.customer_id",
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


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".signalforge").mkdir()
    return project


# ---------------------------------------------------------------------------
# Byte-equal parity across all three renderers (DEC-015 of #9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("render_kind", ["ansi", "markdown"])
def test_render_to_text_byte_equal_to_render_diff_file_output(
    render_kind: str, project_dir: Path
) -> None:
    """``render_to_text(report, config=cfg, project_dir=...)`` returns
    the exact bytes :func:`render_diff` writes to ``output_path`` for
    the ANSI and Markdown surfaces.

    Strategy: invoke :func:`render_diff` once with both
    ``output_path=tmpfile`` (which exercises the file-write path) AND
    captures the returned :class:`DiffReport`. Then call
    :func:`render_to_text` on the SAME report so ``run_id`` /
    reproducibility hashes are stable across the two renderings.

    The JSON surface is exercised separately because :func:`render_diff`
    rebuilds the returned report with a refreshed ``duration_seconds``
    AFTER it writes the file (an existing render-order quirk that
    keeps the sidecar JSON, the returned report, and the INFO log in
    sync at the cost of skewing the ``output_path`` JSON's wall-clock
    field). The ANSI / Markdown renderers do NOT embed
    ``duration_seconds``, so they remain byte-equal across the two
    surfaces.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(decisions=(_kept_decision_for(), _dropped_decision_for()))
    config = DiffConfig(render_kind=render_kind)  # type: ignore[arg-type]
    out = project_dir / f"diff.{render_kind}"

    report = render_diff(
        model,
        candidate,
        prune_result,
        config=config,
        output_path=out,
        project_dir=project_dir,
    )

    file_bytes = out.read_text(encoding="utf-8")
    in_process = render_to_text(report, config=config, project_dir=project_dir)

    assert in_process == file_bytes, (
        f"render_to_text byte-mismatch for render_kind={render_kind!r}: "
        f"len(file)={len(file_bytes)} len(in_process)={len(in_process)}"
    )


def test_render_to_text_json_structural_equal_to_render_diff_file_output(
    project_dir: Path,
) -> None:
    """``render_to_text(report, render_kind="json")`` round-trips
    through :func:`json.loads` to the same structure as the
    :func:`render_diff` ``output_path`` JSON file, modulo the
    ``duration_seconds`` field.

    :func:`render_diff` renders ``output_path`` BEFORE refreshing
    ``duration_seconds`` (post-second-pass review fix in
    :file:`src/signalforge/diff/engine.py:927-935`), so the file's
    JSON carries the early placeholder while the returned report
    carries the refreshed wall-clock value. Every other field — and
    the JSON's structural shape — is identical, which is the
    load-bearing property the CLI consumes.
    """
    import json as _json

    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result(decisions=(_kept_decision_for(), _dropped_decision_for()))
    config = DiffConfig(render_kind="json")
    out = project_dir / "diff.json"

    report = render_diff(
        model,
        candidate,
        prune_result,
        config=config,
        output_path=out,
        project_dir=project_dir,
    )

    file_parsed = _json.loads(out.read_text(encoding="utf-8"))
    in_process_parsed = _json.loads(render_to_text(report, config=config, project_dir=project_dir))

    # Drop the duration-skewed field; assert every other key matches.
    file_parsed.pop("duration_seconds", None)
    in_process_parsed.pop("duration_seconds", None)
    assert file_parsed == in_process_parsed


def test_render_to_text_default_config_uses_ansi_renderer(project_dir: Path) -> None:
    """``config=None`` falls back to :class:`DiffConfig` defaults
    (``render_kind="ansi"``) — same shape as :func:`render_diff` with
    ``config=None``.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    report = render_diff(model, candidate, prune_result, project_dir=project_dir)
    text = render_to_text(report, project_dir=project_dir)

    # ANSI text shape: includes the "diff:" header and "kept=" summary,
    # not JSON or Markdown.
    assert "diff: " in text
    assert "kept=" in text


def test_render_to_text_markdown_tolerates_missing_project_dir() -> None:
    """``render_kind="markdown"`` with ``project_dir=None`` falls
    through to the renderer's existing handling (does not raise).

    DEC-022 of #9: the helper passes ``None`` through; the
    :class:`MarkdownRenderer` already tolerates a missing project_dir.
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    # Build a report with a temporary on-disk project so render_diff
    # can run; then call render_to_text with project_dir=None.
    config = DiffConfig(render_kind="markdown")
    report = render_diff(model, candidate, prune_result, config=config)

    text = render_to_text(report, config=config, project_dir=None)
    assert text.startswith("# Diff:")


def test_render_to_text_does_not_touch_disk(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper is a pure in-process render — no file I/O.

    Pin the contract: the fail-closed write seam stays scoped to
    :func:`signalforge.diff.engine._write_rendered_text` inside
    :func:`render_diff`. Asserting via no-touch on the canonical
    sidecar path is a proxy for "no disk writes".
    """
    model = _make_model()
    candidate = _make_candidate()
    prune_result = _make_prune_result()

    # Build a report via render_diff with write_sidecar=False so the
    # only on-disk artefact is what render_diff itself produces (none,
    # since output_path is None and write_sidecar=False).
    report = render_diff(
        model,
        candidate,
        prune_result,
        project_dir=project_dir,
        write_sidecar=False,
    )

    sidecar = project_dir / ".signalforge" / "diff.json"
    assert not sidecar.exists()

    # render_to_text must not create the sidecar or any file.
    _ = render_to_text(report, project_dir=project_dir)
    assert not sidecar.exists()
