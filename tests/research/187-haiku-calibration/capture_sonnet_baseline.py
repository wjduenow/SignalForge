#!/usr/bin/env python
"""One-shot capture of a REAL ``claude-sonnet-4-6`` baseline for the #187
Haiku-calibration gate, using a real model from the ``intuit_airflow`` repo.

This replaces the original *hand-authored* Sonnet baseline (US-005) with a
genuine one:

1. **Draft** candidate artifacts for the real intuit_airflow model
   ``plugins/dbt/models/analytical/calendar_hour.sql`` with the production
   drafter (default ``claude-sonnet-4-6``). The drafter runs **schema-only**,
   so NO warehouse is contacted (DEC-012(c) of the safety layer) — the
   ``intuit_airflow`` project is Snowflake, but calibration never queries it.
2. **Freeze** the drafted :class:`CandidateSchema` to ``real_candidate.json``
   so the artifacts are deterministic from here on (the LLM draft is the only
   non-deterministic step; the model itself is constructed deterministically by
   :func:`_substrate.build_model`).
3. **Grade** the frozen candidate with ``claude-sonnet-4-6`` and write the
   per-``(artifact_id, criterion_id)`` pass/fail verdicts to
   ``sonnet_baseline_sample.json``.

The gated harness :mod:`test_haiku_calibration` then re-grades the SAME frozen
artifacts with the resolved Haiku default and measures concordance against this
real Sonnet baseline.

Maintainer-run (needs ``ANTHROPIC_API_KEY``; ~1 draft + a few dozen Sonnet grade
calls, well under $1)::

    set -a && source <repo-root>/.env && set +a
    uv run python tests/research/187-haiku-calibration/capture_sonnet_baseline.py

Re-running overwrites ``real_candidate.json`` and ``sonnet_baseline_sample.json``.
The model source is ``intuit_airflow`` HEAD at capture time; the SQL is inlined
in :func:`_substrate.build_model` so the capture is reproducible without that
repo checked out.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]
# The harness keeps ``_substrate`` next to this script (not an importable
# package), and ``tests`` is a namespace package (no ``__init__.py`` per the
# src-layout convention) — put both on the path.
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT))

from _substrate import (  # noqa: E402  (path insert must precede import)
    BASELINE_PATH,
    CANDIDATE_PATH,
    build_model,
    empty_prune_result,
)

import signalforge as _sf  # noqa: E402
from signalforge.draft import draft_schema  # noqa: E402
from signalforge.draft.config import DraftConfig  # noqa: E402
from signalforge.grade import grade_artifacts  # noqa: E402
from signalforge.grade.config import GradeConfig  # noqa: E402
from signalforge.manifest.models import Manifest  # noqa: E402
from signalforge.safety.policy import SafetyPolicy  # noqa: E402

_BASELINE_MODEL = "claude-sonnet-4-6"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY not set. Source your .env first:\n"
            f"  set -a && source {_REPO_ROOT}/.env && set +a",
            file=sys.stderr,
        )
        return 2

    model = build_model()
    manifest = Manifest(
        metadata={"dbt_schema_version": "v12", "project_name": "bi"},
        nodes={model.unique_id: model},
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Schema-only policy → the FakeAdapter is never invoked.
        from tests.safety._fake_adapter import FakeAdapter

        policy = SafetyPolicy(audit_path=tmp / "audit.jsonl")
        adapter = FakeAdapter()

        print(f"Drafting candidate artifacts for {model.unique_id} (schema-only, Sonnet)…")
        outcome = draft_schema(model, adapter, policy, manifest, config=DraftConfig())
        candidate = outcome.candidate

        # Freeze the drafted artifacts BEFORE grading so the baseline is a
        # grade of exactly what gets committed.
        CANDIDATE_PATH.write_text(candidate.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"  froze {len(candidate.columns)} columns -> {CANDIDATE_PATH.name}")

        print(f"Grading the frozen candidate with {_BASELINE_MODEL}…")
        report = grade_artifacts(
            model,
            candidate,
            empty_prune_result(model),
            config=GradeConfig(model=_BASELINE_MODEL),
            audit_path=tmp / "grade.jsonl",
            sidecar_path=tmp / "grade.json",
            project_dir=tmp,
        )

    # A degraded (score=None) Sonnet grade is "could not evaluate" (retry
    # exhaustion / parse failure), NOT a real "Sonnet says fail". Excluding it
    # from the baseline is the honest treatment — the concordance gate then
    # compares Haiku only against pairs where we actually HAVE a Sonnet verdict.
    # (Each excluded pair's artifact retains its other criteria, so coverage of
    # every artifact_id is preserved.)
    verdicts: list[dict[str, object]] = []
    degraded = 0
    for r in report.results:
        if r.score is None:
            degraded += 1
            continue
        verdicts.append(
            {
                "artifact_id": r.artifact_id,
                "criterion_id": r.criterion_id,
                "baseline_passed": bool(r.passed),
                # Informational only; load_baseline() reads baseline_passed.
                "baseline_score": r.score,
            }
        )

    baseline = {
        "_provenance": (
            "REAL claude-sonnet-4-6 baseline for the #187 Haiku-calibration gate "
            "(US-005, recaptured on request). Artifacts were drafted by the "
            "production drafter (schema-only, no warehouse) from the real "
            "intuit_airflow model plugins/dbt/models/analytical/calendar_hour.sql "
            "and frozen to real_candidate.json; the model itself is constructed "
            "deterministically by _substrate.build_model. Verdicts below are live "
            "claude-sonnet-4-6 grades of those frozen artifacts, captured via "
            "capture_sonnet_baseline.py. The gated harness re-grades the SAME "
            "frozen artifacts with the resolved Haiku default and measures "
            "per-(artifact_id, criterion_id) pass/fail concordance against these "
            "verdicts. Pairs Sonnet could not grade (score=None, e.g. retry "
            "exhaustion under rate limiting) are EXCLUDED — see degraded_count. "
            "This is NOT hand-authored — re-run the capture script to regenerate."
        ),
        "baseline_model": _BASELINE_MODEL,
        "source_model": model.unique_id,
        "source_repo_path": "intuit_airflow/plugins/dbt/models/analytical/calendar_hour.sql",
        "signalforge_version": _sf.__version__,
        "degraded_count": degraded,
        "rubric_criteria": ["clarity", "consistency", "rationale", "no-redundant"],
        "verdicts": verdicts,
    }
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    passed = sum(1 for v in verdicts if v["baseline_passed"])
    print(
        f"\nCaptured {len(verdicts)} Sonnet verdicts "
        f"({passed} passed / {len(verdicts) - passed} failed; "
        f"{degraded} degraded pairs excluded) -> {BASELINE_PATH.name}"
    )
    print(f"Frozen artifacts -> {CANDIDATE_PATH.name}")
    print("Next: run the gated concordance gate with a key:")
    print("  uv run pytest -m anthropic --no-cov tests/research/187-haiku-calibration/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
