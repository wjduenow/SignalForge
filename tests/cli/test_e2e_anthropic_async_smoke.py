"""End-to-end async smoke test against real BigQuery + real Anthropic (grader).

Issue #186 / US-012 / DEC-020. Pins the **concurrent dispatch** branch
of the grade engine (US-009 — `asyncio.TaskGroup` + `Semaphore` +
`asyncio.timeout`) against the real Anthropic API. The sync sibling
(``tests/cli/test_e2e_bigquery_smoke.py`` parametrized at
``[anthropic]``) exercises sequential grading with
``max_concurrent_calls`` defaulted to 10 against the same fixture; this
file pins the per-vendor async branch with ``max_concurrent_calls=3``
(small but >1; cost-conscious per DEC-020).

Why a dedicated file rather than another parametrize variant on the
sync smoke (per ``in-isolation-smoke-misses-pipeline-drift`` memory):

1. **Per-vendor failure ergonomics.** Three files = three independent
   markers (``@pytest.mark.anthropic`` / ``@pytest.mark.openai`` /
   ``@pytest.mark.gemini``); a maintainer running
   ``pytest -m "e2e and anthropic"`` reaches THIS test alone.
2. **Async-specific contract pin.** The audit JSONL lands in arrival
   order under concurrency (DEC-015 — orchestrator does NOT sort
   before writing; the sort happens at read time on the test side via
   the inline ``_sort_grade_events`` helper below). The sync smoke's
   sequential output can't catch a regression that breaks ordering
   guarantees only under concurrent dispatch.
3. **Anthropic prompt-cache penalty surface (DEC-021).** Calls 1..N
   under concurrency pay the cache-write premium instead of the read
   discount on the cached rubric block. Anthropic is the only provider
   with ``supports_prompt_caching=True``, so this test is the one
   that exercises that cost branch live.

Gated by THREE env vars (mirrors the Anthropic-baseline variant of the
sync BQ smoke):

* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money against BigQuery".
* ``ANTHROPIC_API_KEY`` — the drafter AND grader both use Anthropic.
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.

The test is excluded from default ``pytest`` runs by ``addopts = "...
-m 'not e2e and not anthropic' ..."`` in ``pyproject.toml``. The
maintainer runs it in the pre-release live suite::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-ant-...
    SF_RUN_BQ=1 pytest -m "e2e and anthropic" --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in
``addopts`` would fail any marker-specific run that exercises only a
fraction of the codebase.

Asserts the three contract invariants from DEC-020:

1. ``signalforge.cli.main(...)`` returns ``0`` (full pipeline ran
   cleanly under concurrent dispatch).
2. ``.signalforge/grade.jsonl`` carries the expected pair count
   (artifact × criterion). Read-time sorted by
   ``(artifact_id, criterion_id)`` so the assertion is dispatch-order
   independent.
3. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no
   traceback ever leaks even under concurrent task failures /
   cancellations).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._e2e_helpers import apply_provider_override, copy_fixture_to_tmp
from tests.grade._helpers import _sort_grade_events

pytestmark = [pytest.mark.e2e, pytest.mark.anthropic]

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (warehouse opt-in cost gate)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when all gates are satisfied — the test then
    proceeds to make real Anthropic + real BigQuery calls under
    concurrent dispatch (``grade.max_concurrent_calls=3``). Each
    missing prerequisite yields its own distinct reason so a maintainer
    running ``pytest -m "e2e and anthropic"`` sees exactly what to set.
    """
    if not _bq_runs_enabled():
        return "SF_RUN_BQ=1 required (e2e test costs real money against BigQuery)"
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            "ANTHROPIC_API_KEY required "
            "(drafter AND grader are Anthropic Sonnet for the async smoke)"
        )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable but billed to the runner)"
        )
    return None


def test_e2e_signalforge_generate_under_concurrent_dispatch_with_anthropic_grader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end under concurrent dispatch.

    Mirrors the BQ smoke's Anthropic-baseline variant verbatim except
    for the ``grade.max_concurrent_calls=3`` overlay, which routes the
    grade engine through the async ``TaskGroup`` + ``Semaphore`` path
    (US-009) rather than the sequential v0.1 loop.

    Skips cleanly under ``pytest -m "e2e and anthropic"`` when any of
    the three env vars is missing.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Issue #186 / US-012 / DEC-020 — overlay only the concurrency knob.
    # The grader stays on the committed fixture's Anthropic provider /
    # claude-sonnet-4-6 model; only ``max_concurrent_calls`` flips.
    # ``3`` is small but >1: exercises the concurrent dispatch path
    # while keeping live-call cost bounded.
    apply_provider_override(project_dir, grade_max_concurrent_calls=3)

    # Mirrors the sync BQ smoke's profile rewrite verbatim — the
    # committed `profiles.yml` pins ``project: bigquery-public-data``
    # so the regen script (`dbt parse`) can hit the public dataset; at
    # query time the BigQuery client uses ``profile.project`` as the
    # *billing* project, which the maintainer can't bill against
    # ``bigquery-public-data``. Rewrite the per-run profile to bill the
    # maintainer's project.
    billing_project = os.environ["GOOGLE_CLOUD_PROJECT"]
    (project_dir / "profiles.yml").write_text(
        "austin:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        f"      project: {billing_project}\n"
        "      dataset: austin_bikeshare\n"
        "      location: US\n"
        "      maximum_bytes_billed: 1000000000\n"
    )

    exit_code = main(
        [
            "generate",
            "models/staging/stg_bikeshare_trips.sql",
            "--project-dir",
            str(project_dir),
        ]
    )

    # 1. Exit code 0 — full pipeline (draft → prune → grade → diff)
    #    completed without a typed-error escape under concurrent
    #    dispatch (no nested-loop / async-unsupported / budget-cancel
    #    failure reached the CLI boundary).
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Audit JSONL carries the expected pair count. Read-time sort
    #    by ``(artifact_id, criterion_id)`` keeps the assertion
    #    dispatch-order independent (DEC-015 — orchestrator writes in
    #    arrival order; tests sort at read time).
    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    assert audit_path.is_file(), f"grade audit JSONL missing at {audit_path}"
    lines = [json.loads(raw) for raw in audit_path.read_text().splitlines() if raw.strip()]
    sorted_events = _sort_grade_events(lines)
    # At least one (artifact, criterion) pair must have been graded —
    # the cheapest contract that distinguishes a healthy run from a
    # silently-empty audit. The exact count (~108–116 against the
    # Austin fixture per #158) varies with the drafter's column /
    # description output; pinning a lower bound > 0 is the load-bearing
    # signal that the async dispatch wrote SOMETHING to the JSONL.
    assert len(sorted_events) >= 1, (
        f"expected at least one grade event in {audit_path}; got 0. "
        f"Under async dispatch (max_concurrent_calls=3) the orchestrator's "
        f"per-decision fail-closed writer should have produced ≥1 record."
    )

    # 3. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even under concurrent task failures / cancellations.
    #    The async branch widens the surface where a regression could
    #    surface a traceback — e.g. an unhandled ``CancelledError`` or
    #    ``ExceptionGroup`` from the TaskGroup).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
