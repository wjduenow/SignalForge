"""End-to-end smoke test for the ``unique_combination`` test variant (#170).

Issue #170 / US-012. Pins the load-bearing behavioural claim of #170: when
the drafter sees a model whose SELECT body advertises a natural composite
GROUP BY shape, it emits a structured :class:`CandidateTestUniqueCombination`
candidate (NOT a freeform ``custom_sql`` ``GROUP BY HAVING COUNT(*) > 1``).
This freeform → structured translation is the variant's reason for being —
without an e2e against a real LLM + real warehouse, the only assurance we
have is the parser-side anchor contract.

The test runs ``signalforge generate`` (NOT ``prune-existing``) against the
engineered Austin-bikeshare fixture model ``stg_bikeshare_station_pairs``
(US-011). The fixture model GROUP BYs ``(start_station_id, end_station_id,
subscriber_type)`` and aliases its relation back to the source
``bikeshare_trips`` table so SignalForge can run queries without a prior
``dbt run`` (source-as-model trick from issue #10 Path A).

Engineered determinism (per ``.claude/rules/testing-signal.md`` §
"Engineered determinism for LLM-driven assertions"): the drafter is
non-deterministic on a single call, so the assertion is designed so that
ANY reasonable Sonnet 4.6 proposal of a multi-column composite unique test
on this model passes. Specifically:

* The test asserts AT LEAST ONE :class:`PruneDecision` carries
  ``test.type == "unique_combination"`` with ``len(test.columns) >= 2``.
  The exact ``columns`` tuple is NOT pinned because Sonnet may legitimately
  propose ``(start_station_id, end_station_id)`` OR
  ``(start_station_id, end_station_id, subscriber_type)`` OR similar — both
  are valid responses to the model's GROUP BY shape.
* The prune-decision outcome (kept vs. dropped via ``always-passes``) is
  NOT pinned because the source ``bikeshare_trips`` table may or may not
  carry exact duplicate (start, end, subscriber) tuples on the sample the
  prune engine draws — the variant's behaviour through the pipeline is the
  contract, not the specific verdict on bikeshare data.

The assertion that proves the freeform→structured translation is the type
check on ``PruneDecision.test``: ``custom_sql`` is in the catalogue too,
and Sonnet has historically used it for composite uniqueness on prior
fixtures — but this fixture + the #170 prompt-side training is supposed to
steer the model to the structured form. The assertion fails loud if the
drafter falls back to ``custom_sql``.

Gated by the standard three-env-var e2e gate (mirrors
``test_e2e_bigquery_smoke.py`` baseline — drafter is Anthropic Sonnet on
this pipeline; warehouse is BigQuery; no provider overlay):

* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money / talks to a real warehouse".
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.
* ``ANTHROPIC_API_KEY`` — the drafter calls Anthropic Sonnet via the real
  API.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m 'not e2e' ..."`` in ``pyproject.toml``. The maintainer
runs it once before declaring an e2e PR ready::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-...
    SF_RUN_BQ=1 pytest -m e2e -k unique_combination --no-cov

``--no-cov`` is required because ``--cov-fail-under`` in ``addopts`` would
fail any marker-specific run that exercises only a fraction of the codebase.

Asserts the four invariants required by US-012 / AC-1 / AC-8:

1. ``signalforge.cli.main(...)`` returns ``0`` (full pipeline draft →
   prune → grade → diff completed without a typed-error escape).
2. ``<project_dir>/.signalforge/diff.json`` exists and
   ``<project_dir>/.signalforge/grade.json`` exists (pipeline produced
   both end-of-run sidecars).
3. At least one :class:`PruneDecision` has
   ``test.type == "unique_combination"`` and
   ``len(test.columns) >= 2`` (the load-bearing freeform→structured
   translation pin).
4. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no
   traceback ever leaks).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._e2e_helpers import copy_fixture_to_tmp, read_prune_decisions

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (mirrors warehouse integration)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when every gate is satisfied — the test then proceeds
    to make real BigQuery + real Anthropic Sonnet calls (no OpenAI, no
    Gemini; this pipeline uses the default Anthropic drafter and the
    fixture's default Anthropic grader).

    Each missing prerequisite yields its own distinct reason so a
    maintainer running ``pytest -m e2e -k unique_combination`` sees exactly
    what to set. Treat an empty / whitespace-only key as "unset" (an empty
    value would otherwise reach the client and produce a noisy auth
    failure rather than a skip).
    """
    if not _bq_runs_enabled():
        return "SF_RUN_BQ=1 required (e2e test costs real money against BigQuery)"
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            "ANTHROPIC_API_KEY required "
            "(drafter calls Anthropic Sonnet to propose unique_combination)"
        )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable "
            "but billed to the runner)"
        )
    return None


@pytest.mark.e2e
def test_e2e_drafter_emits_structured_unique_combination_against_engineered_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end and pin the structured-variant
    translation: the engineered composite-key model surfaces a
    :class:`CandidateTestUniqueCombination` candidate in the drafter's
    output (NOT a freeform ``custom_sql``).

    Skips cleanly under ``pytest -m e2e`` when any required env var is
    missing — the maintainer runs the gated invocation once before merge
    and the default suite never reaches this test.

    AC traces (per ``plans/super/170-unique-combination.md``):
    AC-1 (drafter proposes structured ``unique_combination``),
    AC-8 (end-to-end pipeline shape).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # Copy the read-only Austin fixture to ``tmp_path`` so the audit JSONLs
    # (prune.jsonl, grade.jsonl, llm_response.jsonl, safety.jsonl) and the
    # sidecars (diff.json, grade.json) land in the per-run temp dir,
    # not the committed fixture. Mirrors every other e2e in this directory
    # — the committed fixture must stay byte-equal to ``src/signalforge/_demo/``
    # to satisfy the ``init-demo`` parity gate (DEC-008 of #10).
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # The committed `profiles.yml` pins ``project: bigquery-public-data``
    # so the regen script (`dbt parse`) can hit the public dataset; but at
    # query time the BigQuery client uses ``profile.project`` as the
    # *billing* project, and the maintainer can't bill ``bigquery-public-data``.
    # Rewrite the per-run profile to bill the maintainer's project (read
    # from ``GOOGLE_CLOUD_PROJECT``); the manifest still resolves the model's
    # ``relation_name`` to ``bigquery-public-data.austin_bikeshare.bikeshare_trips``
    # via the model's own ``database``/``schema`` fields, so the SOURCE
    # table is unchanged. ``maximum_bytes_billed: 1 GB`` bumps the default
    # 100 MB cap so the materialised-sample CTAS can scan the full
    # ~2.27M-row source table. ~$0.005 per run.
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

    # Target the US-011 engineered fixture model whose SELECT body
    # GROUP BYs ``(start_station_id, end_station_id, subscriber_type)`` —
    # the natural composite-key shape that should steer Sonnet 4.6 toward
    # proposing structured ``unique_combination`` rather than freeform
    # ``custom_sql``. The model aliases its relation back to
    # ``bikeshare_trips`` per the source-as-model trick so the prune
    # engine can run queries without a prior ``dbt run`` (issue #10 Path A).
    exit_code = main(
        [
            "generate",
            "models/staging/stg_bikeshare_station_pairs.sql",
            "--project-dir",
            str(project_dir),
        ]
    )

    # 1. Exit code 0 — full pipeline (draft → prune → grade → diff)
    #    completed without a typed-error escape.
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Both end-of-run sidecars landed at their default paths.
    diff_sidecar = project_dir / ".signalforge" / "diff.json"
    assert diff_sidecar.is_file(), f"diff sidecar missing at {diff_sidecar}"
    grade_sidecar = project_dir / ".signalforge" / "grade.json"
    assert grade_sidecar.is_file(), f"grade sidecar missing at {grade_sidecar}"

    # 3. The load-bearing assertion — AT LEAST ONE PruneDecision carries
    #    a structured ``unique_combination`` test with two or more columns.
    #
    #    The ``PruneDecision.test`` field is the typed
    #    ``CandidateTest`` discriminated union (see ``prune.jsonl`` audit
    #    DEC-014 — flat shape, ``test: CandidateTest``). Each candidate
    #    test the drafter proposed flows through prune and gets one
    #    audit record, so the JSONL is the canonical surface for "what
    #    did the drafter actually emit?".
    #
    #    The assertion is deliberately *structural* (type discriminator +
    #    minimum column count), not value-pinning: Sonnet 4.6 may
    #    legitimately propose ``(start_station_id, end_station_id)`` OR
    #    ``(start_station_id, end_station_id, subscriber_type)`` OR
    #    similar variants — all are valid responses to the model's
    #    GROUP BY shape. A regression to freeform ``custom_sql``
    #    (Sonnet's historical pattern before #170's prompt + catalogue
    #    work) would fail this assertion because the test type
    #    discriminator would be ``"custom_sql"``, not ``"unique_combination"``.
    decisions = read_prune_decisions(project_dir)
    unique_combo_decisions = [d for d in decisions if d.test.type == "unique_combination"]
    assert len(unique_combo_decisions) >= 1, (
        "expected at least one structured CandidateTestUniqueCombination "
        "PruneDecision from the drafter (US-012 / AC-1 — the load-bearing "
        "freeform→structured translation). Sonnet 4.6 should propose a "
        "composite unique test against the engineered fixture model's "
        "GROUP BY shape ``(start_station_id, end_station_id, subscriber_type)`` "
        "rather than a freeform ``custom_sql``. Decision test types observed: "
        f"{sorted({d.test.type for d in decisions})}. "
        "Inspect .signalforge/prune.jsonl + .signalforge/llm_responses.jsonl "
        "for the drafted candidate."
    )

    # The ``unique_combination`` variant requires at least two columns
    # (validated at construction time in ``CandidateTestUniqueCombination._columns_min_two``).
    # Surface a clear failure if any drafted ``unique_combination`` lands
    # with fewer — would imply a Pydantic validator regression or a
    # parser-side anchor-contract gap.
    for decision in unique_combo_decisions:
        # ``getattr`` because ``decision.test`` is the discriminated union;
        # the type narrowing on ``test.type == "unique_combination"`` is
        # safe at runtime but pyright may not narrow through the JSONL
        # round-trip when read_prune_decisions returns CandidateTest.
        columns = getattr(decision.test, "columns", ())
        assert len(columns) >= 2, (
            f"CandidateTestUniqueCombination requires len(columns) >= 2 "
            f"(per ``_columns_min_two`` validator); got columns={columns!r} "
            f"on decision {decision!r}"
        )

    # 4. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even if the pipeline raised internally).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
