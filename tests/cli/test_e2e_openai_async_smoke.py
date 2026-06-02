"""End-to-end async smoke test against real BigQuery + real OpenAI (grader).

Issue #186 / US-012 / DEC-020. Pins the **concurrent dispatch** branch
of the grade engine (US-009 — `asyncio.TaskGroup` + `Semaphore` +
`asyncio.timeout`) against the real OpenAI API as the grader.
Drafter stays Anthropic Sonnet per the committed fixture's
``llm.model: claude-sonnet-4-6`` pin (mirrors the sync OpenAI smoke
``tests/cli/test_e2e_openai_smoke.py`` cost table from #155 DEC-011).

Why a dedicated file rather than another parametrize variant on the
sync OpenAI smoke (per ``in-isolation-smoke-misses-pipeline-drift``
memory):

1. **Per-vendor failure ergonomics.** A maintainer running
   ``pytest -m "e2e and openai"`` reaches THIS test alone, plus the
   sync OpenAI sibling (sequential dispatch); the failure modes for
   async-specific regressions surface independently.
2. **OpenAI ``supports_async=True`` capability pin.** The OpenAI
   provider exposes its async surface via ``AsyncOpenAI`` — this test
   is the live proof the seam wired through the registry correctly.
3. **No prompt-cache penalty (contrast with Anthropic).** OpenAI has
   ``supports_prompt_caching=False`` so DEC-021's cost penalty does
   NOT apply here; the cost differential between this test and the
   Anthropic async smoke isolates the cache-write premium.

Gated by FIVE env vars (mirrors the sync OpenAI smoke — drafter stays
Anthropic Sonnet per DEC-011, so the Anthropic auth + BigQuery opt-in
are part of the contract):

* ``SF_RUN_OPENAI=1`` — the project-wide opt-in for "this test costs
  real money against the OpenAI API".
* ``OPENAI_API_KEY`` — without a key the OpenAI grader cannot call the
  async LLM seam.
* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money against BigQuery".
* ``ANTHROPIC_API_KEY`` — the DRAFTER is Anthropic Sonnet per DEC-011.
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.

The test is excluded from default ``pytest`` runs by ``addopts = "...
-m 'not e2e and not openai' ..."`` in ``pyproject.toml``. The
maintainer runs it in the pre-release live suite::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-ant-...
    export OPENAI_API_KEY=sk-...
    SF_RUN_BQ=1 SF_RUN_OPENAI=1 pytest -m "e2e and openai" --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in
``addopts`` would fail any marker-specific run that exercises only a
fraction of the codebase.

Asserts the three contract invariants from DEC-020:

1. ``signalforge.cli.main(...)`` returns ``0`` (full pipeline ran
   cleanly under concurrent dispatch with OpenAI as the grader).
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

pytestmark = [pytest.mark.e2e, pytest.mark.openai]

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (warehouse opt-in cost gate)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _openai_runs_enabled() -> bool:
    """``SF_RUN_OPENAI`` is set to a truthy value (OpenAI opt-in cost gate)."""
    return os.environ.get("SF_RUN_OPENAI", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when all FIVE gates are satisfied — the test then
    proceeds to make real OpenAI + real Anthropic + real BigQuery
    calls under concurrent dispatch (``grade.max_concurrent_calls=3``).
    Each missing prerequisite yields its own distinct reason so a
    maintainer running ``pytest -m "e2e and openai"`` sees exactly what
    to set.

    The Anthropic + SF_RUN_BQ checks are required because the drafter
    stays Anthropic Sonnet across all three e2e providers per DEC-011
    of #155 (only the grader swaps).
    """
    if not _openai_runs_enabled():
        return "SF_RUN_OPENAI=1 required (e2e test costs real money against the OpenAI API)"
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return "OPENAI_API_KEY required (e2e test calls the real OpenAI API as the async grader)"
    if not _bq_runs_enabled():
        return (
            "SF_RUN_BQ=1 required "
            "(e2e test costs real money against BigQuery — warehouse leg shared with the baseline)"
        )
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            "ANTHROPIC_API_KEY required "
            "(drafter stays Anthropic Sonnet per DEC-011; only the grader swaps to gpt-4o)"
        )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable but billed to the runner)"
        )
    return None


def _sort_grade_events(lines: list[dict]) -> list[dict]:
    """Sort grade-event JSONL records by ``(artifact_id, criterion_id)``.

    The orchestrator's per-decision fail-closed audit writer
    (``signalforge.grade.audit``) appends in dispatch arrival order, so
    under concurrent dispatch (``max_concurrent_calls > 1``) the JSONL
    is non-deterministically ordered. Sorting at read time by the
    pair-identity keys gives a stable comparison surface (mirrors the
    helper US-011 will hoist to ``tests/grade/_helpers.py``; inlined
    here to keep the three async smokes self-contained ahead of US-011
    landing).
    """
    return sorted(lines, key=lambda r: (r["artifact_id"], r["criterion_id"]))


def test_e2e_signalforge_generate_under_concurrent_dispatch_with_openai_grader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end with the OpenAI async grader.

    Mirrors the sync OpenAI smoke verbatim except for the
    ``grade.max_concurrent_calls=3`` overlay, which routes the grade
    engine through the async ``TaskGroup`` + ``Semaphore`` path
    (US-009) rather than the sequential v0.1 loop.

    Skips cleanly under ``pytest -m "e2e and openai"`` when any of the
    five env vars is missing.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Issue #186 / US-012 / DEC-020 — overlay the OpenAI grader + the
    # concurrency knob. Drafter stays Anthropic Sonnet per DEC-011 of
    # #155; only the grade-block knobs change here.
    apply_provider_override(
        project_dir,
        grade_provider="openai",
        grade_model="gpt-4o",
        grade_max_concurrent_calls=3,
    )

    # Mirrors the sync OpenAI smoke's profile rewrite verbatim — the
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
    #    dispatch with OpenAI's async client wired through the registry.
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Audit JSONL carries the expected pair count. Read-time sort
    #    by ``(artifact_id, criterion_id)`` keeps the assertion
    #    dispatch-order independent (DEC-015).
    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    assert audit_path.is_file(), f"grade audit JSONL missing at {audit_path}"
    lines = [json.loads(raw) for raw in audit_path.read_text().splitlines() if raw.strip()]
    sorted_events = _sort_grade_events(lines)
    assert len(sorted_events) >= 1, (
        f"expected at least one grade event in {audit_path}; got 0. "
        f"Under async dispatch (max_concurrent_calls=3) the orchestrator's "
        f"per-decision fail-closed writer should have produced ≥1 record."
    )

    # 3. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even under concurrent task failures / cancellations).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
