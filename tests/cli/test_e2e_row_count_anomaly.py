"""End-to-end smoke test for the ``row_count_anomaly_by_period`` variant (#171).

Issue #171 / US-017. Pins the load-bearing behavioural claim of #171: when
the operator declares a per-period row-count baseline on a model carrying a
date column, SignalForge's drafter proposes a structured
:class:`CandidateTestRowCountAnomalyByPeriod` candidate, the engine threads
the operator-supplied ``--as-of`` through to the compiled SQL + the
:class:`PruneEvent` audit, and the variant evaluates against real warehouse
data — catching a known volume anomaly on a hand-picked historical date.

The test runs ``signalforge generate --as-of 2023-04-16`` against the Austin
bikeshare ``bikeshare_trips`` model. The Austin public dataset has a
documented ~50% drop in daily trip volume on 2023-04-15 / 2023-04-16 — the
exact failure shape the variant exists to catch (cited in #171's plan,
``plans/super/171-row-count-anomaly.md`` § Refinement Q9 / DEC-009). Pinning
``--as-of`` to a date with a real anomalous bucket is the engineered
determinism that makes the assertion robust per
``.claude/rules/testing-signal.md`` § "Engineered determinism for LLM-driven
assertions": the LLM's exact prompt-to-SQL bytes are non-deterministic, but
the variant's behavioural contract through the pipeline IS — the drafter
must propose structured anomaly (not freeform ``custom_sql``), the engine
must evaluate it against the source table (not a sample), and the
``--as-of`` must carry through to the audit verbatim.

The engineered :func:`inject_model_anomaly_rules` helper steers the drafter
toward the structured variant by injecting a natural-language business rule
(``meta.signalforge.business_rules``) that names the date column and the
per-period baseline pattern. Without the operator hint the drafter has no
load-bearing signal to choose anomaly detection over a static
``row_count_between`` band.

Gated by the standard three-env-var e2e gate (mirrors
``test_e2e_bigquery_smoke.py`` baseline + ``test_e2e_business_rules.py``):

* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money / talks to a real warehouse".
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.
* ``ANTHROPIC_API_KEY`` — the drafter (and grader) calls Anthropic Sonnet
  via the real API.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m 'not e2e' ..."`` in ``pyproject.toml``. The maintainer
runs it once before declaring an e2e PR ready::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-...
    SF_RUN_BQ=1 pytest -m e2e -k row_count_anomaly --no-cov

``--no-cov`` is required because ``--cov-fail-under`` in ``addopts`` would
fail any marker-specific run that exercises only a fraction of the codebase.

Asserts the load-bearing invariants from US-017 / DEC-001 / DEC-003 / DEC-008:

1. ``signalforge.cli.main(...)`` returns ``0`` (full pipeline draft →
   prune → grade → diff completed without a typed-error escape).
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. At least one :class:`PruneDecision` carries
   ``test.type == "row_count_anomaly_by_period"`` (the structured
   freeform→catalogue translation pin — a regression to freeform
   ``custom_sql`` fails this assertion).
4. The matched :class:`PruneEvent.as_of` carries the supplied date
   verbatim (the audit field is the operator-recovery surface for
   Architectural Commitment #5's time-bound carve-out).
5. The matched :class:`PruneEvent.stats` is populated and carries the
   per-method discriminator (DEC-005 — the cross-stage numerical state
   handoff is the load-bearing seam for future grader calibration).
6. The matched :class:`PruneDecision.decision == "kept"` (the engineered
   anomaly date — 2023-04-16 — is a real ~50% volume drop in the public
   data; the variant must catch it as failing rows in today's bucket vs.
   the lookback band).
7. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no
   traceback ever leaks).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._e2e_helpers import (
    copy_fixture_to_tmp,
    inject_model_anomaly_rules,
    read_prune_decisions,
)

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_MODEL_UNIQUE_ID = "model.signalforge_test_austin.stg_bikeshare_trips"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# 2023-04-16 is a date with a documented ~50% volume drop in the public
# Austin bikeshare dataset (cited in ``plans/super/171-row-count-anomaly.md``
# § Refinement Q9). Pinning ``--as-of`` here makes the kept-vs-dropped
# assertion deterministic regardless of how the model's lookback window
# happens to align with seasonality (a healthy date would routinely
# always-pass and flip the assertion arm).
_AS_OF_DATE = date(2023, 4, 16)

# Engineered anomaly rule. Names the date column AND the per-period
# baseline pattern explicitly so the drafter (Sonnet 4.6) is steered to
# the structured ``row_count_anomaly_by_period`` variant rather than a
# freeform ``custom_sql`` ``COUNT(*)``. Mirrors the `inject_model_business_rules`
# shape the #116 / #163 / #169 e2e tests use for variant steering.
_ANOMALY_RULE = (
    "The daily count of trips in bikeshare_trips (grouped by the start_time "
    "date column) should be stable relative to recent history — a sudden "
    "drop or spike vs. the rolling baseline of the prior 28 days is a "
    "volume anomaly worth flagging. Use a per-period (day-grain) anomaly "
    "check on start_time with a 28-day lookback."
)


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (mirrors warehouse integration)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when every gate is satisfied — the test then proceeds
    to make real BigQuery + real Anthropic calls (the drafter proposes
    the structured variant; the engine evaluates it against the source;
    the grader scores the diff).

    Each missing prerequisite yields its own distinct reason so a
    maintainer running ``pytest -m e2e -k row_count_anomaly`` sees exactly
    what to set. Treat an empty / whitespace-only key as "unset" (an empty
    value would otherwise reach the client and produce a noisy auth
    failure rather than a skip).
    """
    if not _bq_runs_enabled():
        return "SF_RUN_BQ=1 required (e2e test costs real money against BigQuery)"
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            "ANTHROPIC_API_KEY required "
            "(drafter calls Anthropic Sonnet to propose row_count_anomaly_by_period; "
            "grader also uses Anthropic by default)"
        )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable "
            "but billed to the runner)"
        )
    return None


@pytest.mark.e2e
@pytest.mark.anthropic
@pytest.mark.bigquery
def test_e2e_row_count_anomaly_catches_engineered_as_of_volume_drop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate --as-of <picked-date>`` end-to-end and
    pin: the drafter proposes structured anomaly, the engine threads
    ``--as-of`` to the compiled SQL + audit, the variant catches a real
    volume drop in the Austin bikeshare data.

    Skips cleanly under ``pytest -m e2e`` when any required env var is
    missing — the maintainer runs the gated invocation once before merge
    and the default suite never reaches this test.

    AC traces (per ``plans/super/171-row-count-anomaly.md``):
    DEC-001 (``--as-of`` threading), DEC-003 (typed
    :class:`AnomalyTestStats` cross-stage handoff), DEC-008 (two-query
    split: stats + violation), DEC-013 (audit schema v3).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # Copy the read-only Austin fixture to ``tmp_path`` so the audit JSONLs
    # (prune.jsonl, grade.jsonl, llm_response.jsonl, safety.jsonl) and the
    # sidecars (diff.json, grade.json) land in the per-run temp dir, not
    # the committed fixture. Mirrors every other e2e in this directory —
    # the committed fixture must stay byte-equal to ``src/signalforge/_demo/``
    # to satisfy the ``init-demo`` parity gate (DEC-008 of #10).
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Inject the engineered anomaly rule into the per-run manifest copy.
    # The committed Austin manifest ships an empty config.meta; injecting
    # at tmp_path keeps the e2e fixture decoupled from the init-demo
    # parity tree. The rule names ``start_time`` and the 28-day lookback
    # so the drafter has a load-bearing signal to choose the structured
    # variant.
    inject_model_anomaly_rules(
        project_dir,
        _MODEL_UNIQUE_ID,
        [_ANOMALY_RULE],
    )

    # Bill the maintainer's project (bigquery-public-data can't bill itself);
    # bump the bytes cap so the source-table scan (anomaly bypasses sampling
    # per DEC-009 / DEC-010 — ALL three metadata-aggregate variants route to
    # source) clears the default 100 MB cap. ~$0.005 per run on ``bikeshare_trips``.
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

    # ``--as-of`` is the load-bearing flag for THIS variant — pin to a
    # date with a real volume anomaly so the kept-decision assertion is
    # robust regardless of which sample the drafter inspects. See the
    # module-level ``_AS_OF_DATE`` for the rationale.
    exit_code = main(
        [
            "generate",
            "models/staging/stg_bikeshare_trips.sql",
            "--project-dir",
            str(project_dir),
            "--as-of",
            _AS_OF_DATE.isoformat(),
        ]
    )

    # 1. Exit code 0 — full pipeline (draft → prune → grade → diff)
    #    completed without a typed-error escape.
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Diff sidecar landed at the default path.
    sidecar = project_dir / ".signalforge" / "diff.json"
    assert sidecar.is_file(), f"diff sidecar missing at {sidecar}"

    # 3. AT LEAST ONE structured row_count_anomaly_by_period decision —
    #    proves the drafter chose the catalogue variant over freeform
    #    ``custom_sql``. The injected business rule explicitly mentions
    #    the date column + per-period baseline so Sonnet has a clear
    #    signal; a regression that breaks the drafter→catalogue
    #    translation (e.g. a missing catalogue entry, a broken prompt)
    #    fails this assertion loud.
    decisions = read_prune_decisions(project_dir)
    anomaly_decisions = [d for d in decisions if d.test.type == "row_count_anomaly_by_period"]
    assert len(anomaly_decisions) >= 1, (
        "expected at least one structured CandidateTestRowCountAnomalyByPeriod "
        "PruneDecision from the drafter (US-017 / DEC-001 — the load-bearing "
        "freeform→structured translation). The injected business rule names "
        "the ``start_time`` date column and the 28-day lookback explicitly so "
        "Sonnet 4.6 should propose the structured anomaly variant rather than "
        "freeform ``custom_sql``. Decision test types observed: "
        f"{sorted({d.test.type for d in decisions})}. "
        "Inspect .signalforge/prune.jsonl + .signalforge/llm_responses.jsonl "
        "for the drafted candidate."
    )

    # Pick the first structured anomaly decision for the remaining
    # field-level pins. The drafter may legitimately propose more than
    # one (e.g. per-DOW and overall) — every one of them must carry the
    # supplied ``as_of`` so iterating-and-asserting per decision is
    # safer than reaching for ``[0]``.
    for decision in anomaly_decisions:
        # 4. ``PruneEvent.as_of`` carries the supplied date verbatim —
        #    the audit field is the operator-recovery surface for
        #    Architectural Commitment #5's time-bound carve-out.
        assert decision.as_of == _AS_OF_DATE, (
            f"PruneEvent.as_of must equal the supplied --as-of value "
            f"({_AS_OF_DATE!r}); got {decision.as_of!r}. The threading "
            "from CLI → prune_tests → compiled SQL → audit is the load-bearing "
            "contract for the time-bound reproducibility carve-out (DEC-001)."
        )

        # 5. ``AnomalyTestStats`` populated on the decision — the
        #    cross-stage numerical state seam (DEC-003 / DEC-005). The
        #    discriminated-union ``method`` field must be present and
        #    point at one of the four supported methods.
        assert decision.stats is not None, (
            "expected AnomalyTestStats to populate on a row_count_anomaly_by_period "
            "decision (DEC-005 — cross-stage numerical handoff is the load-bearing "
            "seam for grader calibration + audit forensics). Got stats=None on "
            f"decision {decision!r}."
        )
        assert decision.stats.method in {"mad", "zscore", "percentile", "min_max"}, (
            f"AnomalyTestStats.method must be one of the four supported methods; "
            f"got {decision.stats.method!r}"
        )
        assert decision.stats.n_periods >= 1, (
            "AnomalyTestStats.n_periods must be >= 1 on a successfully-evaluated "
            f"decision (cold-start would route to kept-without-evidence); got "
            f"n_periods={decision.stats.n_periods!r}"
        )

    # 6. The variant must catch the engineered anomaly: at least one of
    #    the anomaly decisions lands ``decision="kept"`` (real evidence
    #    of failing rows in today's bucket vs. the lookback band). The
    #    2023-04-16 ``--as-of`` is a date with a documented ~50% volume
    #    drop in Austin bikeshare; the variant exists to catch exactly
    #    this failure mode. A regression that produces
    #    ``decision="dropped" / reason="always-passes"`` (band too wide)
    #    OR ``decision="kept" / reason="kept-without-evidence"`` (engine
    #    couldn't evaluate) fails this assertion. Both arms are real
    #    signal worth catching at code-review time.
    has_kept_with_evidence = any(
        d.decision == "kept" and d.reason == "kept" for d in anomaly_decisions
    )
    assert has_kept_with_evidence, (
        "expected at least one row_count_anomaly_by_period PruneDecision kept "
        "with reason='kept' (the engineered ``--as-of 2023-04-16`` lands on a "
        "documented ~50% volume drop in Austin bikeshare — the variant must "
        "catch the anomaly as failing rows in today's bucket vs. the 28-day "
        "lookback band). Anomaly decisions: "
        f"{[(d.decision, d.reason, d.failures) for d in anomaly_decisions]}. "
        "If the kept-without-evidence arm fired, inspect ``why`` for cold-start "
        "/ warehouse-error routing; if always-passes fired, the engineered date "
        "may need to be revisited against the live public data."
    )

    # 7. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even if the pipeline raised internally).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
