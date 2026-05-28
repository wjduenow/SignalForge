"""End-to-end ``signalforge generate --estimate`` smoke against the real OpenAI API.

Issue #136 / US-006 — DEC-001, DEC-004, DEC-005, DEC-008. Drives the
``--estimate`` short-circuit with ``llm.provider: openai`` +
``grade.provider: openai`` (both ``gpt-4o`` per DEC-004) and asserts the
rendered report includes a non-zero grader USD figure and the run exits
cleanly with no traceback leak.

Gated by the ``openai`` marker — excluded from default CI by
:file:`pyproject.toml`'s ``addopts = "... -m 'not openai'"``. Requires
``SF_RUN_OPENAI=1`` + ``OPENAI_API_KEY``. The warehouse-bytes leg
(``estimate_query_bytes`` via BigQuery dry-run) gracefully degrades to
``<unavailable: ...>`` if Application Default Credentials / a billing
project aren't available locally — DEC-005 of #36 (the
multi-source-CLI degrade pattern, ``cli-layer.md``); the LLM-cost half
of the report still computes, exit code stays 0, and that's what this
test pins. To exercise the warehouse path too, set
``GOOGLE_CLOUD_PROJECT=<billing-project>`` + ``gcloud auth
application-default login`` first.

What this proves end-to-end:

* The OpenAI tiktoken token counter (DEC-012 of #136 / ``llm-drafter.md``
  § Open notes) plus the four ``gpt-4o`` / ``gpt-4o-mini`` / ``gpt-4.1`` /
  ``gpt-4-turbo`` price-table entries (DEC-007 of #136) compose into a
  non-zero grader USD figure.
* ``OpenAIProvider.estimate_input_tokens`` is wired correctly for both
  the drafter and the grader (per DEC-005 — "scope both stages
  explicitly").
* The CLI's ``--estimate`` short-circuit handles the
  ``supports_token_count=False`` capability flag without raising or
  leaking a traceback (no Anthropic-only ``messages.count_tokens``
  call path).

What this deliberately does NOT assert:

* Specific token counts or USD figures — tiktoken-based estimates are
  deterministic for a given input, but pinning exact values would
  couple the test to the SDK / price-table version. Shape only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._e2e_helpers import copy_fixture_to_tmp

pytestmark = pytest.mark.openai

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _openai_runs_enabled() -> bool:
    """``SF_RUN_OPENAI`` is set to a truthy value (mirrors ``SF_RUN_BQ`` / ``SF_RUN_SNOWFLAKE``)."""
    return os.environ.get("SF_RUN_OPENAI", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` only when both gates are satisfied — the test then
    proceeds to run ``signalforge generate --estimate`` against the real
    OpenAI client. Each missing prerequisite yields its own distinct
    reason so a maintainer running ``pytest -m openai`` sees exactly
    what to set. Treat an empty / whitespace-only ``OPENAI_API_KEY`` as
    "unset" (an empty value would otherwise reach the client and
    produce a noisy auth failure rather than a skip).
    """
    if not _openai_runs_enabled():
        return "SF_RUN_OPENAI=1 required (live --estimate test instantiates a real OpenAI client)"
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return "OPENAI_API_KEY required (live --estimate test instantiates a real OpenAI client)"
    return None


def test_generate_estimate_openai_provider_renders_nonzero_grader_usd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate --estimate`` end-to-end with the OpenAI provider.

    Copies the Austin bikeshare fixture into ``tmp_path`` (the audit
    JSONLs / sidecars must land in temp — DEC-008 of #10) and rewrites
    its ``signalforge.yml`` so the drafter AND grader both target
    ``gpt-4o`` via the ``openai`` provider (DEC-005 of #136 — scope
    both stages). The warehouse-bytes path is allowed to degrade to
    ``<unavailable: ...>`` if no BQ creds are present; the LLM-cost
    half still computes, which is what this test pins.

    Asserts:

    1. Exit code 0 — the four-tier exit-code taxonomy (cli-layer.md
       DEC-008) reserves 0 for clean runs; ``--estimate`` is no-API-call
       on the LLM side (count-tokens / tiktoken only) and the
       warehouse degrade is fail-soft.
    2. ``Estimated grade cost:`` header appears in stdout (the OpenAI
       grader section computed without raising).
    3. At least one non-zero USD figure under the grader block — pins
       the DEC-004 / DEC-007 wiring (gpt-4o pricing entry × tiktoken
       counts × four-criterion fan-out).
    4. ``Traceback`` does NOT appear in stderr (cli-layer.md DEC-016 —
       no traceback ever leaks).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Override signalforge.yml so BOTH drafter and grader target OpenAI
    # (DEC-005 — scope both stages explicitly). gpt-4o is the DEC-004
    # default judge model and is shipped in the price table (DEC-007).
    # We retain the fixture's existing grade thresholds + safety mode
    # so the rest of the pipeline (which --estimate doesn't actually
    # exercise) stays consistent with the fixture's documented shape.
    (project_dir / "signalforge.yml").write_text(
        "llm:\n"
        "  provider: openai\n"
        "  model: gpt-4o\n"
        "safety:\n"
        "  mode: aggregate-only\n"
        "prune:\n"
        "  sample_strategy: materialised\n"
        "grade:\n"
        "  provider: openai\n"
        "  model: gpt-4o\n"
        "  min_pass_rate: 0.95\n"
        "  min_mean_score: 0.95\n"
        "  fail_on_below_threshold: false\n"
        "  total_budget_seconds: 600\n"
    )

    # Bill the maintainer's GCP project if available — lets the
    # warehouse-bytes leg succeed instead of degrading; absent, it
    # degrades to ``<unavailable: ...>`` per DEC-005 of #36. Either
    # path is acceptable for this test.
    billing_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if billing_project:
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
            "--estimate",
            "models/staging/stg_bikeshare_trips.sql",
            "--project-dir",
            str(project_dir),
        ]
    )

    captured = capsys.readouterr()

    # 1. Exit code 0 — clean run.
    assert exit_code == 0, (
        f"expected --estimate to exit 0; got {exit_code}. stderr={captured.err!r}"
    )

    # 2. Grader section rendered.
    assert "Estimated grade cost:" in captured.out, (
        f"expected the OpenAI grader cost section in stdout; got:\n{captured.out}"
    )

    # 3. At least one non-zero grader USD figure — pins the
    #    tiktoken × gpt-4o price-table wiring (DEC-004 / DEC-007).
    #    Captured under the grader block: lines like
    #    ``  cost:           $0.1152`` or per-criterion ``$0.0288``.
    grade_section = captured.out.split("Estimated grade cost:", 1)[1]
    # Truncate at the next blank-line-separated section so we don't
    # match a draft-cost figure that happens to also be non-zero.
    grade_section = grade_section.split("\n\n", 1)[0]
    nonzero_usd_lines = [
        line
        for line in grade_section.splitlines()
        if "$" in line and not line.strip().endswith("$0.0000")
    ]
    assert nonzero_usd_lines, (
        f"expected a non-zero grader USD figure in the grade section; got:\n{grade_section}"
    )

    # 4. No traceback ever leaks (cli-layer.md DEC-016).
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
