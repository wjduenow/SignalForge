"""Helpers for the gated e2e smoke test (issue #10 / DEC-008, DEC-023).

Public surface:

* :func:`copy_fixture_to_tmp` — copies the e2e fixture into a per-run
  ``tmp_path`` so audit JSONLs land in temp (DEC-008; mirrors
  :func:`tests.cli._factories.make_fake_dbt_project`).
* :func:`read_prune_decisions` — deserialises
  ``<project_dir>/.signalforge/prune.jsonl`` into a typed tuple of
  :class:`signalforge.prune.PruneDecision` so US-005 can assert on
  ``drop_reason`` after a CLI run.
* :func:`read_diff_report` — deserialises
  ``<project_dir>/.signalforge/diff.json`` into a typed
  :class:`signalforge.diff.DiffReport` for kept-count / unified-diff
  assertions.

Used only by ``tests/cli/test_e2e_bigquery_smoke.py`` (US-005) plus the
helper's own unit tests under ``tests/cli/test_e2e_helpers.py``. Not
imported from production code.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from signalforge.diff import DiffReport
from signalforge.prune import PruneDecision, PruneEvent


def copy_fixture_to_tmp(fixture_dir: Path, tmp_path: Path) -> Path:
    """Copy ``fixture_dir`` into ``tmp_path / "project"`` and return the new dir.

    Each e2e run lands its audit JSONLs (``prune.jsonl``, ``grade.jsonl``,
    ``llm_response.jsonl``, ``safety.jsonl``) and its sidecar
    (``diff.json``) under the per-run ``tmp_path`` so the source fixture
    stays read-only across runs (DEC-008). Mirrors the
    :func:`tests.cli._factories.make_fake_dbt_project` precedent — the
    factory plants a synthetic dbt project under ``tmp_path``; this helper
    copies a real one (manifest.json, profiles.yml, signalforge.yml,
    seeds, etc.) verbatim.

    Args:
        fixture_dir: source fixture root (containing ``dbt_project.yml``
            at minimum).
        tmp_path: pytest's per-test ``tmp_path`` fixture.

    Returns:
        The copied project directory: ``tmp_path / "project"``.
    """
    project_dir = tmp_path / "project"
    shutil.copytree(fixture_dir, project_dir)
    return project_dir


def read_prune_decisions(project_dir: Path) -> tuple[PruneDecision, ...]:
    """Deserialise ``<project_dir>/.signalforge/prune.jsonl`` into typed decisions.

    Each JSONL line is a :class:`signalforge.prune.PruneEvent` (the
    fail-closed audit record produced by
    :func:`signalforge.prune.prune_tests`). The event flattens its
    decision's fields rather than nesting them under a ``decision:`` key
    (audit DEC-014), so this helper rebuilds a
    :class:`signalforge.prune.PruneDecision` from the matching subset of
    event fields. Used to assert on ``drop_reason`` / ``decision`` after
    a CLI run (DEC-023).

    Args:
        project_dir: a project root containing ``.signalforge/prune.jsonl``.

    Returns:
        Tuple of every :class:`PruneDecision` recorded in the audit log,
        in JSONL order (i.e. the order tests were evaluated).
    """
    audit = project_dir / ".signalforge" / "prune.jsonl"
    decisions: list[PruneDecision] = []
    with audit.open() as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            event = PruneEvent.model_validate_json(line)
            decisions.append(
                PruneDecision(
                    test_anchor=event.test_anchor,
                    test=event.test,
                    decision=event.decision,
                    reason=event.reason,
                    failures=event.failures,
                    sampled_rows=event.sampled_rows,
                    scope=event.scope,
                    elapsed_ms=event.elapsed_ms,
                    compiled_sql_hash=event.compiled_sql_hash,
                    compiled_sql=event.compiled_sql,
                    why=event.why,
                    sample_failures=event.sample_failures,
                )
            )
    return tuple(decisions)


def read_diff_report(project_dir: Path) -> DiffReport:
    """Deserialise ``<project_dir>/.signalforge/diff.json`` into a typed report.

    The diff sidecar is the single end-of-run JSON document written by
    :func:`signalforge.diff.render_diff`; the e2e smoke (US-005) asserts
    on ``kept_count`` and ``has_existing_schema`` to confirm the pipeline
    produced a non-empty diff (DEC-023).

    Args:
        project_dir: a project root containing ``.signalforge/diff.json``.

    Returns:
        The fully-typed :class:`DiffReport` parsed from the sidecar.
    """
    sidecar = project_dir / ".signalforge" / "diff.json"
    return DiffReport.model_validate_json(sidecar.read_text())
