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
* :func:`inject_model_business_rules` — patches a copied fixture's
  ``target/manifest.json`` so the model node carries
  ``config.meta.signalforge.business_rules`` (issue #116 / US-015). Lets
  the custom-business-rule e2e reuse the committed Austin manifest
  verbatim without coupling the e2e fixture to the ``init-demo`` parity
  tree (``tests/test_demo_fixture_parity.py``) — the rules are injected
  into the per-run ``tmp_path`` copy, never the committed fixture.

Used only by the gated e2e smokes (``tests/cli/test_e2e_*.py``) plus the
helper's own unit tests under ``tests/cli/test_e2e_helpers.py``. Not
imported from production code.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
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


def inject_model_business_rules(
    project_dir: Path,
    model_unique_id: str,
    rules: Sequence[str],
) -> None:
    """Patch ``config.meta.signalforge.business_rules`` on a manifest model node.

    Issue #116 / US-015. The drafter reads model-level business rules from
    ``Model.config.meta["signalforge"]["business_rules"]`` (see
    :func:`signalforge.draft.prompts._read_business_rules`). The committed
    Austin manifest ships an empty ``config.meta`` for the staging model,
    so the custom-business-rule e2e injects the engineered rules into the
    per-run ``tmp_path`` copy of the manifest rather than maintaining a
    second hand-crafted manifest fixture (which would also have to be
    mirrored into ``src/signalforge/_demo/`` to satisfy the
    ``init-demo`` parity gate).

    The mutation is idempotent and surgical: it loads
    ``<project_dir>/target/manifest.json``, sets the named model node's
    ``config.meta.signalforge.business_rules`` to the supplied list, and
    writes the file back. ``meta`` and ``config.meta`` are also set in
    lockstep so a future loader change reading either path still sees the
    rules.

    Args:
        project_dir: a copied project root (use
            :func:`copy_fixture_to_tmp` first — NEVER call against a
            committed fixture).
        model_unique_id: the dbt ``unique_id`` of the model node to patch
            (e.g. ``"model.signalforge_test_austin.stg_bikeshare_trips"``).
        rules: the natural-language business rules to inject. Each becomes
            one ``custom_sql`` candidate test the drafter is expected to
            propose.

    Raises:
        KeyError: if ``model_unique_id`` is not present in the manifest's
            ``nodes`` map (a typo in the unique_id surfaces loud rather
            than silently injecting nothing).
    """
    manifest_path = project_dir / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    node = manifest["nodes"][model_unique_id]
    rules_list = list(rules)

    config = node.setdefault("config", {})
    config_meta = config.setdefault("meta", {})
    config_meta.setdefault("signalforge", {})["business_rules"] = rules_list

    node_meta = node.setdefault("meta", {})
    node_meta.setdefault("signalforge", {})["business_rules"] = rules_list

    manifest_path.write_text(json.dumps(manifest))
