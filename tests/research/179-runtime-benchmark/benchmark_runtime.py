#!/usr/bin/env python3
"""Standalone per-stage runtime benchmark for the #179 retest.

Companion to the "Runtime benchmark retest" story on epic #179. Measures the
per-stage wall-clock of one ``signalforge generate`` run against the **local
intuit_airflow repo**, so the efficiency improvements that landed since the
2026-05-30 baseline can be attributed:

* **#186** — grade-layer ``asyncio`` refactor (PR #190, ``f0e315b``).
* **#187** — faster grade defaults: Haiku + per-provider fast models
  (PR #193, ``4a70799``).
* **#188** — bulk-mode shared cached prefix for ``--select`` (PR #195,
  ``d280fc6``) — multi-model batch only; see the ``--select`` note below.
* **#202** — grade-to-completion: Stage-1 shared header-honoring rate limiter
  (DEC-205), Stage-2 always-on bounded sweep of transient ``score=None`` pairs
  (US-005), Stage-3 ``grade.require_complete`` (default True) raising tier-2
  ``GradeIncompleteError`` naming ungraded pairs (US-006/US-007), Stage-4
  ``max_retries_429`` 3→6 (US-008). The harness exposes ``--require-complete``
  (version-gated, like ``--no-cache``) and reports the ``aggregate_complete``
  flag, the per-``degrade_reason_type`` split, and the ungraded-pair list — the
  #202 retest's PASS-condition signals. See ``docs/research/179-runtime-benchmark.md``
  § "#202 grade-to-completion retest" for the live-run instructions.

## Why this is a script (not a pytest test) and why it shells out

The A/B is **production PyPI package vs. the code on ``dev``**. You cannot swap
an installed package mid-process, so each side runs in its own venv and the only
stable contract across the two versions is the ``signalforge`` CLI — the
in-process orchestrator API drifts between releases and the prod wheel ships no
test helpers. So this is a pure-stdlib script that invokes the venv-local
``signalforge`` console script via subprocess and reads the durable sidecars it
writes. A prod venv therefore needs ONLY ``pip install signalforge-dbt`` — no
pytest, no pytest-cov, no marker config.

## The A/B: prod PyPI first, then dev

Run the SAME command in two venvs, on ONE machine (wall-clock is machine- and
network-dependent, so both halves must run on the same host):

```bash
# 1. BEFORE — production PyPI package
python -m venv .venv-prod
.venv-prod/bin/pip install signalforge-dbt
.venv-prod/bin/python tests/research/179-runtime-benchmark/benchmark_runtime.py \
    --project-dir ~/Projects/intuit_airflow/plugins/dbt \
    --profiles-dir /tmp/sf-demo-profiles

# 2. AFTER — the code on dev (editable from this checkout)
python -m venv .venv-dev
.venv-dev/bin/pip install -e .
.venv-dev/bin/python tests/research/179-runtime-benchmark/benchmark_runtime.py \
    --project-dir ~/Projects/intuit_airflow/plugins/dbt \
    --profiles-dir /tmp/sf-demo-profiles
```

The script resolves ``signalforge`` from the SAME venv as the interpreter
running it (``<sys.executable dir>/signalforge``), so ``.venv-prod/bin/python``
benchmarks prod and ``.venv-dev/bin/python`` benchmarks dev — unambiguously. It
prints the resolved version in the table so the two runs are self-labelling.
Transcribe both tables into the before/after columns of
``docs/research/179-runtime-benchmark.md``.

### #187 opt-in (Haiku)

The shipped anthropic grade default stays Sonnet, so a default run captures #186
only. To also measure #187, set ``grade.model: claude-haiku-4-5`` in the intuit
project's ``signalforge.yml`` for a third run and record it in the Haiku column.

### Honest caveat — this is a NET release-to-release delta, not pure isolation

The prod PyPI release predates #169/#170/#171, so ``dev`` also drafts 8 test
primitives vs. prod's 5 — dev does *more* grading work, not less. The wall-clock
delta is therefore the **net user-facing change between the last release and
dev**, which conflates the efficiency wins (#186/#187/#188) with the added
primitives. The **budget-exceeded degradation count** is the cleaner isolated
signal (the baseline was 17/34; #186/#187 should drive it toward 0). For a pure
efficiency isolation instead, A/B two git checkouts at the SAME primitive set
(``90af28b`` vs ``dev``) — documented in the research writeup as the alternative.

## Preconditions (one-time, per the epic's retest protocol)

The intuit project must already be prepared per
``docs/research/179-test-primitive-expansion-retest.md`` § Substrate:
``dbt deps`` + ``dbt parse`` run, a synthesised ``_signalforge_*_schema.yml`` so
the target model exposes its columns, a ``signalforge.yml`` with
``safety.mode: schema-only`` + ``prune.enabled: false``, and the
``/tmp/sf-demo-profiles`` override. ``ANTHROPIC_API_KEY`` must be set. The prod
version must be new enough to support ``prune.enabled`` (#35) and the Snowflake
adapter (#53) — any 0.4+ release qualifies.

## What it prints

A per-stage table (grade + diff read from the sidecars' ``duration_seconds``;
``draft + overhead`` derived as ``total − grade − diff``; prune ~0 while
disabled) plus the grade-degradation counts. Records numbers only — it asserts
nothing, because a benchmark measures, it does not gate a build.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Stages reported, in pipeline order. ``draft`` is derived (no sidecar carries
# its duration); ``prune`` reads ~0 while disabled; ``grade`` / ``diff`` come
# from the sidecars' ``duration_seconds``.
_GRADE_SIDECAR = "grade.json"
_DIFF_SIDECAR = "diff.json"

# Cap on the number of ungraded ``(artifact, criterion)`` pairs named inline in
# the harness output before collapsing to ``… and N more``. Mirrors the engine's
# ``GradeIncompleteError._PAIR_DISPLAY_CAP`` (#202) so the two lists line up.
_UNGRADED_DISPLAY_CAP = 20


def _resolve_signalforge_bin(explicit: str | None) -> str:
    """Return the ``signalforge`` executable to benchmark.

    Precedence: explicit ``--signalforge-bin`` > the console script sitting next
    to the running interpreter (so ``.venv-prod/bin/python`` → that venv's
    ``signalforge``) > ``PATH``. Exits with a clear message if none resolves.
    """
    if explicit:
        return explicit
    # Prefer the console script next to the interpreter. Use the UNRESOLVED
    # sys.executable path: a uv/virtualenv `bin/python` is often a symlink to a
    # store-managed interpreter, and `.resolve()` would jump OUT of the venv and
    # miss the venv-local `signalforge`. Check the unresolved parent first, then
    # the resolved one, then PATH.
    for base in (Path(sys.executable).parent, Path(sys.executable).resolve().parent):
        sibling = base / "signalforge"
        if sibling.exists():
            return str(sibling)
    found = shutil.which("signalforge")
    if found:
        return found
    sys.exit(
        "ERROR: no `signalforge` executable found next to "
        f"{sys.executable} or on PATH. Install it in this venv "
        "(`pip install signalforge-dbt` for prod, `pip install -e .` for dev) "
        "or pass --signalforge-bin."
    )


def _signalforge_version(bin_path: str) -> str:
    """Return the version string from ``signalforge version`` (best-effort)."""
    try:
        out = subprocess.run(
            [bin_path, "version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return f"<version lookup failed: {type(exc).__name__}>"
    text = (out.stdout or out.stderr).strip()
    # Shape is "signalforge X.Y.Z"; fall back to the raw line.
    return text.split(" ", 1)[1] if text.startswith("signalforge ") else (text or "<unknown>")


def _generate_supports(bin_path: str, flag: str) -> bool:
    """Return True if ``signalforge generate --help`` advertises ``flag``.

    Used to gate the #189 ``--no-cache`` flag: it exists on ``dev`` but NOT on
    the prod PyPI release (which has no grade cache at all), so the prod run must
    not pass it (argparse would reject an unknown flag and the whole run fails).
    """
    try:
        out = subprocess.run(
            [bin_path, "generate", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False
    return flag in (out.stdout or "")


def _read_sidecar(project_dir: Path, name: str, *, started_at: float) -> dict | None:
    """Load ``<project>/.signalforge/<name>``; warn + return None if stale/missing.

    ``started_at`` is a wall-clock (``time.time``) stamp from just before the
    generate run; a sidecar whose mtime predates it is stale (the run did not
    rewrite it — e.g. the stage errored before its writer) and must NOT be read
    as this run's result.
    """
    path = project_dir / ".signalforge" / name
    if not path.exists():
        print(f"  WARNING: {path} not found — stage did not produce a sidecar")
        return None
    if path.stat().st_mtime < started_at:
        print(f"  WARNING: {path} is stale (not rewritten by this run) — skipping")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARNING: could not parse {path}: {type(exc).__name__}")
        return None


def _grade_degradations(grade_data: dict) -> tuple[int, int, int]:
    """Return ``(comparable, degraded_total, degraded_budget)`` from grade.json.

    * ``degraded_total`` — results with ``score is None`` (could not be
      positively evaluated; DEC-015 of #7).
    * ``degraded_budget`` — the subset classified as a budget degrade. Prefers
      the #202 ``degrade_reason_type == "budget"`` discriminator (US-001) and
      falls back to a ``"budget"`` substring of ``reasoning`` for pre-#202
      sidecars that predate the field. This is the 2026-05-30 baseline's 17/34
      failure mode — the number #186/#187 must drive down.
    """
    comparable = degraded_total = degraded_budget = 0
    for result in grade_data.get("results", []):
        if result.get("score") is None:
            degraded_total += 1
            if _is_budget_degrade(result):
                degraded_budget += 1
        else:
            comparable += 1
    return comparable, degraded_total, degraded_budget


def _is_budget_degrade(result: dict) -> bool:
    """True if a ``score is None`` result degraded for a budget reason.

    Prefers the #202 structured ``degrade_reason_type`` discriminator
    (US-001 — ``"budget"``) over fragile prose matching. Pre-#202 sidecars
    (prod 0.5.0) carry no discriminator, so fall back to a ``"budget"``
    substring of ``reasoning`` — the original detection used for the
    prod arm of the #198 table.
    """
    reason_type = result.get("degrade_reason_type")
    if reason_type is not None:
        return reason_type == "budget"
    return "budget" in (result.get("reasoning") or "").lower()


def _degrade_reason_counts(grade_data: dict) -> dict[str, int]:
    """Tally ``score is None`` results by ``degrade_reason_type`` (#202 US-001).

    Returns a count keyed by the structured discriminator
    (``"transient"`` / ``"budget"`` / ``"ceiling"``) plus ``"unclassified"``
    for any degraded pair missing the field (a pre-#202 sidecar, or a
    sidecar from a release that does not emit it). This is the #202 lens:
    the retest's target is **0 ``GradeLLMError`` (transient) degradations**
    and **0 non-budget ``score=None``** on the 16-col model, so splitting the
    null-score bucket by reason is the load-bearing signal — not the raw
    ``score=None`` total the #186/#198 tables reported.
    """
    counts: dict[str, int] = {}
    for result in grade_data.get("results", []):
        if result.get("score") is not None:
            continue
        key = result.get("degrade_reason_type") or "unclassified"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _ungraded_pairs(grade_data: dict) -> list[tuple[str, str]]:
    """Return the ``(artifact_id, criterion_id)`` of every ``score is None`` pair.

    The #202 ``--require-complete`` PASS condition is that the dev arm either
    reaches ``aggregate_complete=True`` (this list is empty) OR exits non-zero
    naming the ungraded pairs. The harness surfaces this list so the operator
    can cross-check the named pairs in the CLI's ``GradeIncompleteError``
    (exit 2) message against the sidecar.
    """
    pairs: list[tuple[str, str]] = []
    for result in grade_data.get("results", []):
        if result.get("score") is None:
            pairs.append(
                (str(result.get("artifact_id", "?")), str(result.get("criterion_id", "?")))
            )
    return pairs


def _aggregate_complete(grade_data: dict) -> bool | None:
    """Return the sidecar's top-level ``aggregate_complete`` flag, or None.

    ``aggregate_complete`` is a computed field on
    :class:`signalforge.grade.GradingReport` (``True`` iff every result has a
    non-null score) and is serialised into ``grade.json``. Pre-#198 sidecars
    that predate the field return ``None`` — the harness then derives it from
    the per-result scores so the column is never blank.
    """
    flag = grade_data.get("aggregate_complete")
    if isinstance(flag, bool):
        return flag
    results = grade_data.get("results")
    if not isinstance(results, list):
        return None
    return all(r.get("score") is not None for r in results)


def _run_generate(
    bin_path: str,
    *,
    project_dir: Path,
    model: str,
    profiles_dir: str | None,
    fmt: str,
    no_cache: bool,
    require_complete: bool,
) -> tuple[float, int]:
    """Run one ``signalforge generate`` and return ``(wall_seconds, returncode)``.

    Default (dry-run) — does NOT pass ``--write``, so the intuit repo's
    ``schema.yml`` is never mutated; the sidecars still land under
    ``.signalforge/``. ``--verbose`` is on so the run's own per-stage progress
    is visible on stderr as a cross-check. ``no_cache`` appends the #189
    ``--no-cache`` flag (caller gates it on version support) so the dev grade
    stage is measured COLD — fair against the cacheless prod release.

    ``require_complete`` appends the #202 ``--require-complete`` flag (US-007 /
    DEC-208; caller gates it on version support). With it set, the dev arm exits
    **non-zero (tier 2, ``GradeIncompleteError``)** naming every still-ungraded
    pair when the always-on bounded sweep cannot drive the corpus to a complete
    score — the explicit half of the #202 retest PASS condition (the other half
    being ``aggregate_complete=True``). The benchmark still parses the sidecars
    on a non-zero exit (the sidecar JSON is written BEFORE the raise), so the
    grade table is populated either way.
    """
    cmd = [
        bin_path,
        "generate",
        model,
        "--project-dir",
        str(project_dir),
        "--format",
        fmt,
        "--verbose",
    ]
    if profiles_dir:
        cmd += ["--profiles-dir", profiles_dir]
    if no_cache:
        cmd += ["--no-cache"]
    if require_complete:
        cmd += ["--require-complete"]

    print(f"  $ {' '.join(cmd)}")
    start = time.perf_counter()
    completed = subprocess.run(cmd, text=True, check=False)
    wall = time.perf_counter() - start
    return wall, completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-stage runtime benchmark for one `signalforge generate` "
        "run against the local intuit_airflow repo (#179).",
    )
    parser.add_argument(
        "--project-dir",
        default=os.environ.get("SF_BENCH_PROJECT_DIR"),
        help="dbt project dir (e.g. ~/Projects/intuit_airflow/plugins/dbt). "
        "Defaults to $SF_BENCH_PROJECT_DIR.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("SF_BENCH_MODEL", "models/reporting/weekly_query_cost.sql"),
        help="model arg passed to `generate` (file path or unique_id). "
        "Defaults to the 2026-05-30 baseline target.",
    )
    parser.add_argument(
        "--profiles-dir",
        default=os.environ.get("SF_BENCH_PROFILES_DIR", "/tmp/sf-demo-profiles"),
        help="profiles dir override (default /tmp/sf-demo-profiles per the epic).",
    )
    parser.add_argument(
        "--signalforge-bin",
        default=os.environ.get("SF_BENCH_SIGNALFORGE_BIN"),
        help="override the signalforge executable (default: the one in this venv).",
    )
    parser.add_argument(
        "--format",
        default="json",
        choices=("ansi", "markdown", "json"),
        help="diff render format (default json — cheap; sidecars are written regardless).",
    )
    parser.add_argument(
        "--cache-mode",
        default="bypass",
        choices=("bypass", "warm"),
        help="grade-cache (#189) handling. 'bypass' (default) passes --no-cache "
        "when supported so the grade stage is measured COLD — the fair A/B vs the "
        "cacheless prod release. 'warm' allows the cache to read/write so a SECOND "
        "run measures the #189 re-run win (dev only; prod has no cache).",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="pass the #202 --require-complete flag (US-007) when the resolved "
        "signalforge supports it (dev only; prod 0.5.0 does not). The dev arm then "
        "exits non-zero (tier 2, GradeIncompleteError) naming every still-ungraded "
        "pair if the always-on sweep can't reach a complete grade corpus — the "
        "explicit half of the #202 retest PASS condition. Off by default so a "
        "default run still measures (the harness reports aggregate_complete + the "
        "ungraded-pair list regardless).",
    )
    args = parser.parse_args(argv)

    if not args.project_dir:
        sys.exit(
            "ERROR: --project-dir is required (or set $SF_BENCH_PROJECT_DIR). "
            "Point it at the prepared intuit_airflow dbt project — see "
            "docs/research/179-runtime-benchmark.md § Preconditions."
        )
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not (project_dir / "dbt_project.yml").exists():
        sys.exit(
            f"ERROR: {project_dir} does not contain dbt_project.yml — not a dbt "
            "project root. See docs/research/179-runtime-benchmark.md § Preconditions."
        )
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        sys.exit("ERROR: ANTHROPIC_API_KEY not set — `generate` issues real draft + grade calls.")
    profiles_dir = args.profiles_dir if Path(args.profiles_dir).expanduser().exists() else None
    if args.profiles_dir and profiles_dir is None:
        print(f"  NOTE: --profiles-dir {args.profiles_dir} does not exist; omitting it")

    bin_path = _resolve_signalforge_bin(args.signalforge_bin)
    version = _signalforge_version(bin_path)

    # #189 grade cache: bypass it (cold) for the fair A/B unless --cache-mode warm.
    # The flag only exists on dev; prod has no cache, so a missing flag IS cold.
    supports_no_cache = _generate_supports(bin_path, "--no-cache")
    use_no_cache = args.cache_mode == "bypass" and supports_no_cache
    if args.cache_mode == "bypass":
        cache_note = (
            "bypass (--no-cache, cold)"
            if supports_no_cache
            else "bypass (flag unsupported — release has no grade cache, cold by default)"
        )
    else:
        cache_note = "warm (cache read/write enabled — run twice for the #189 re-run win)"

    # #202 --require-complete (US-007): only exists on dev (prod 0.5.0 has no
    # completeness contract). Gate it on `generate --help` advertising the flag,
    # exactly like --no-cache — argparse on the prod arm would reject an unknown
    # flag and fail the whole run.
    supports_require_complete = _generate_supports(bin_path, "--require-complete")
    use_require_complete = args.require_complete and supports_require_complete
    if args.require_complete and not supports_require_complete:
        print(
            "  NOTE: --require-complete requested but the resolved signalforge "
            "does not advertise it (pre-#202 release) — omitting it."
        )
    if use_require_complete:
        require_note = "on (--require-complete — dev exits tier-2 naming ungraded pairs)"
    elif args.require_complete:
        require_note = "off (requested but unsupported by this release)"
    else:
        require_note = "off (report-only; aggregate_complete still reported below)"

    print("\n=== #179 pipeline runtime benchmark ===")
    print(f"signalforge bin       : {bin_path}")
    print(f"signalforge version   : {version}")
    print(f"project-dir           : {project_dir}")
    print(f"model                 : {args.model}")
    print(f"grade cache (#189)    : {cache_note}")
    print(f"require-complete(#202): {require_note}")
    print("--- running generate ---")

    started_at = time.time()
    wall_total, returncode = _run_generate(
        bin_path,
        project_dir=project_dir,
        model=args.model,
        profiles_dir=profiles_dir,
        fmt=args.format,
        no_cache=use_no_cache,
        require_complete=use_require_complete,
    )
    if returncode != 0:
        # Non-zero is not fatal for the benchmark: `generate` writes the grade +
        # diff sidecars BEFORE raising (DEC-021 / DEC-204 raise-after-sidecar
        # ordering), so the timing sidecars are present and valid. Surface it,
        # keep going. Under #202 --require-complete, exit code 2 specifically
        # means GradeIncompleteError — the dev arm named the still-ungraded pairs
        # on stderr above; the harness re-derives that list from the sidecar below.
        hint = (
            " (tier 2 = GradeIncompleteError under --require-complete: pairs named on stderr above)"
            if returncode == 2 and use_require_complete
            else ""
        )
        print(f"  NOTE: generate exited {returncode}{hint} — parsing sidecars anyway")

    grade_data = _read_sidecar(project_dir, _GRADE_SIDECAR, started_at=started_at)
    diff_data = _read_sidecar(project_dir, _DIFF_SIDECAR, started_at=started_at)

    grade_s = float(grade_data["duration_seconds"]) if grade_data else float("nan")
    diff_s = float(diff_data["duration_seconds"]) if diff_data else float("nan")
    measured = sum(x for x in (grade_s, diff_s) if x == x)  # x==x drops NaN
    draft_overhead_s = wall_total - measured

    print("--- per-stage wall-clock ---")
    print(f"  {'draft+overhead':<16}: {draft_overhead_s:8.2f}s  (derived: total − grade − diff)")
    print(f"  {'prune':<16}: {'~0.00':>8}s  (disabled)")
    print(f"  {'grade':<16}: {grade_s:8.2f}s  (sidecar duration_seconds)")
    print(f"  {'diff':<16}: {diff_s:8.2f}s  (sidecar duration_seconds)")
    print(f"  {'TOTAL':<16}: {wall_total:8.2f}s  (subprocess wall-clock)")

    if grade_data:
        comparable, degraded_total, degraded_budget = _grade_degradations(grade_data)
        reason_counts = _degrade_reason_counts(grade_data)
        complete = _aggregate_complete(grade_data)
        pairs = _ungraded_pairs(grade_data)
        # Non-budget null scores are the #202 lens: transient (GradeLLMError),
        # ceiling (operator opt-in), and any unclassified pre-#202 remainder.
        degraded_non_budget = degraded_total - degraded_budget
        print("--- grade degradation (2026-05-30 baseline was 17/34 budget-exceeded) ---")
        print(f"  artifacts graded            : {len(grade_data.get('results', []))}")
        print(f"  comparable (scored)         : {comparable}")
        print(f"  degraded (score=None)       : {degraded_total}")
        print(f"  degraded — budget exceeded  : {degraded_budget}")
        print(f"  degraded — non-budget       : {degraded_non_budget}")
        # #202 lens: split the null-score bucket by the US-001 discriminator.
        by_reason = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items())) or "none"
        print(f"  degraded — by reason (#202) : {by_reason}")
        complete_str = "n/a" if complete is None else str(complete)
        print(f"  aggregate_complete (#202)   : {complete_str}")
        if pairs:
            shown = ", ".join(f"({a}, {c})" for a, c in pairs[:_UNGRADED_DISPLAY_CAP])
            overflow = len(pairs) - min(len(pairs), _UNGRADED_DISPLAY_CAP)
            tail = f" … and {overflow} more" if overflow > 0 else ""
            print(f"  ungraded pairs (#202)       : {shown}{tail}")
        else:
            print("  ungraded pairs (#202)       : none (every pair scored)")
    else:
        print("  (no grade sidecar — degradation counts unavailable)")
    print("=======================================")
    print("Transcribe this table into docs/research/179-runtime-benchmark.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
