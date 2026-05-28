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
* :func:`apply_provider_override` — overlays per-test ``grade:`` block
  knobs (``provider`` / ``model`` / ``max_output_tokens``) onto a copied
  fixture's ``signalforge.yml`` (issue #155 / US-004 / DEC-012). The
  canonical seam the multi-provider e2e smokes (BigQuery+Anthropic /
  +OpenAI / +Gemini) use to swap the grader without maintaining N
  near-duplicate fixtures.

Used only by the gated e2e smokes (``tests/cli/test_e2e_*.py``) plus the
helper's own unit tests under ``tests/cli/test_e2e_helpers.py``. Not
imported from production code.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import yaml

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


def apply_provider_override(
    project_dir: Path,
    *,
    grade_provider: str | None = None,
    grade_model: str | None = None,
    grade_max_output_tokens: int | None = None,
) -> None:
    """Overlay ``grade:`` block provider config onto an existing ``signalforge.yml``.

    Issue #155 / US-004 / DEC-012. The multi-provider e2e smokes
    (BigQuery+Anthropic baseline, +OpenAI, +Gemini) share the committed
    Austin fixture and swap only the grader's provider/model. This helper
    is the canonical seam: read the per-run ``signalforge.yml``, set the
    three ``grade:`` knobs whose argument is non-``None``, write back.

    Non-destructive — unset knobs (default ``None``) are left untouched, so
    existing thresholds (``min_pass_rate``, ``min_mean_score``,
    ``total_budget_seconds``, ``fail_on_below_threshold``) and sibling
    top-level blocks (``llm:``, ``safety:``, ``prune:``) round-trip
    unchanged. If the file has no ``grade:`` block, one is created with
    only the supplied keys.

    Args:
        project_dir: a copied project root (use
            :func:`copy_fixture_to_tmp` first — NEVER call against a
            committed fixture; mutates ``project_dir/signalforge.yml``
            in place).
        grade_provider: optional override for ``grade.provider`` (e.g.
            ``"anthropic"``, ``"openai"``, ``"gemini"``).
        grade_model: optional override for ``grade.model`` (e.g.
            ``"gpt-4o"``, ``"gemini-2.5-flash"``).
        grade_max_output_tokens: optional override for
            ``grade.max_output_tokens`` (e.g. ``2048`` for Gemini to
            avoid mid-response truncation per #155).

    Raises:
        FileNotFoundError: if ``<project_dir>/signalforge.yml`` is
            missing (the helper assumes a real fixture has been copied
            in; silently creating one would mask misconfigured tests).
    """
    config_path = project_dir / "signalforge.yml"
    data = yaml.safe_load(config_path.read_text()) or {}
    grade_block = data.setdefault("grade", {})
    if grade_provider is not None:
        grade_block["provider"] = grade_provider
    if grade_model is not None:
        grade_block["model"] = grade_model
    if grade_max_output_tokens is not None:
        grade_block["max_output_tokens"] = grade_max_output_tokens
    config_path.write_text(yaml.safe_dump(data, sort_keys=False))
