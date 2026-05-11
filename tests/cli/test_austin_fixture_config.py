"""Tests for the Austin e2e fixture's ``signalforge.yml`` (issue #10, US-003).

The fixture lives at ``tests/fixtures/dbt_project_austin/`` and is the
target of the issue-#10 e2e BigQuery smoke test (DEC-005..DEC-018 of
``plans/super/10-e2e-bigquery-smoke.md``). The locked config values in
``signalforge.yml`` are load-bearing for the smoke test:

* ``llm.model: claude-sonnet-4-6`` matches :class:`DraftConfig` /
  :class:`GradeConfig` defaults (DEC-018).
* ``safety.mode: aggregate-only`` exercises the aggregate redaction path
  without touching raw rows (DEC-007).
* ``prune.sample_strategy: materialised`` (the v0.2 default) exercises
  the BigQuery session/temp-table seam end-to-end (DEC-005).
* ``grade.min_pass_rate / min_mean_score = 0.95`` pins the threshold the
  e2e assertion keys on (DEC-006).
* ``grade.fail_on_below_threshold: false`` keeps the e2e run from
  short-circuiting before the diff renders (DEC-006).
* ``grade.total_budget_seconds: 600`` lifts the default 300 s headroom
  for p99 latency × ~12 grader calls (DEC-011).

Two tests:

#. The fixture lints clean via ``signalforge lint --project-dir <fixture>``
   (in-process ``main([...])``; no traceback on stderr per
   ``cli-layer.md`` DEC-016).
#. Each per-stage loader (``load_safety_config`` etc.) parses the file
   and returns a config object whose locked fields match the YAML.

Both run on the default pytest set (no env vars, no markers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.cli import main
from signalforge.diff.config import load_diff_config
from signalforge.draft.config import load_draft_config
from signalforge.grade.config import load_grade_config
from signalforge.prune.config import load_prune_config
from signalforge.safety.config import load_safety_config
from signalforge.safety.models import SamplingMode

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"


def test_austin_signalforge_yml_lints_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge lint --project-dir <fixture>`` returns exit 0.

    Pins US-003: the locked ``signalforge.yml`` is valid against every
    per-stage loader. A regression — a stage adding a required field, a
    typo in the fixture, a default change — breaks this loudly before
    the e2e smoke test even spins up a BigQuery client.

    Mirrors the no-traceback floor from ``cli-layer.md`` DEC-016: every
    CLI test asserts ``"Traceback" not in stderr`` so a panic-path
    regression breaks the test, not the operator's eyeballs.
    """
    code = main(["lint", "--project-dir", str(_FIXTURE_DIR)])
    captured = capsys.readouterr()

    assert code == 0, f"expected exit 0; got {code}; stderr={captured.err!r}"
    assert "Traceback" not in captured.err
    assert "ERROR" not in captured.err


def test_austin_signalforge_yml_loads_via_each_loader() -> None:
    """Every per-stage loader returns the locked values from the fixture.

    Direct-loader assertions defend the contract that the YAML field
    names + values track the production model fields. A drift in any
    one would silently degrade the e2e smoke test to a different
    behaviour than the plan locks.
    """
    safety = load_safety_config(_FIXTURE_DIR)
    assert safety.mode is SamplingMode.AGGREGATE_ONLY

    draft = load_draft_config(_FIXTURE_DIR)
    assert draft.model == "claude-sonnet-4-6"

    prune = load_prune_config(_FIXTURE_DIR)
    assert prune.sample_strategy == "materialised"

    grade = load_grade_config(_FIXTURE_DIR)
    assert grade.min_pass_rate == 0.95
    assert grade.min_mean_score == 0.95
    assert grade.fail_on_below_threshold is False
    assert grade.total_budget_seconds == 600

    # Diff block intentionally absent from the fixture; loader returns
    # defaults silently per the resolution order shared across stages.
    diff = load_diff_config(_FIXTURE_DIR)
    assert diff is not None
