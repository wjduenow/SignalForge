# Issue #187 — Haiku grade-default calibration gate

**Status:** harness BUILT (2026-06-02); maintainer RUN pending. The #187
plan ships `claude-haiku-4-5` as the new grade-default SKU (US-001..US-003 —
the provider fast-model resolver now resolves `model=None` to
`claude-haiku-4-5` on the Anthropic provider, and `max_output_tokens`
defaults to `1024`). The default ships behind this empirical gate: a
maintainer runs the gated harness below with a live `ANTHROPIC_API_KEY`
and transcribes the result into the **"Result (maintainer-filled)"**
section. Until then this writeup records the substrate and method only.

**Companion artefacts:**

- `tests/research/187-haiku-calibration/_substrate.py` — pinned `Model`
  + `CandidateSchema` + Sonnet-baseline loader.
- `tests/research/187-haiku-calibration/sonnet_baseline_sample.json` —
  the curated baseline verdict sample.
- `tests/research/187-haiku-calibration/test_haiku_calibration.py` — the
  `@pytest.mark.anthropic`-gated concordance gate.
- `tests/research/187-haiku-calibration/test_gemini_1024_no_truncation.py` —
  the `@pytest.mark.gemini`-gated 1024-token no-truncation check (DEC-004).

## tl;dr

- **The question:** does `claude-haiku-4-5` grade rubric artifacts
  concordantly with the prior `claude-sonnet-4-6` baseline? The decision
  rule is **≥ 85% per-criterion pass/fail agreement** over a pinned
  sample.
- **The harness:** re-grades a hand-authored, calibration-spanning
  candidate (strong / adequate / vague artifacts) with the resolved
  Haiku default over the locked four-criterion `DEFAULT_RUBRIC`, joins
  each `GradingResult` to a curated Sonnet baseline by
  `(artifact_id, criterion_id)`, and asserts the agreement rate clears
  85%. Degraded (`score=None`) pairs are excluded from the denominator
  and reported separately.
- **A second gated check** verifies DEC-004's claim that the new
  `max_output_tokens=1024` default leaves Gemini enough headroom: it
  grades a deliberately verbose artifact on `gemini-2.5-flash` @ 1024
  tokens and asserts no `GradingResult` degraded to `score=None` from a
  truncation.
- **Default CI is untouched:** both checks are deselected by the
  existing `anthropic` / `gemini` markers in `pyproject.toml`'s
  `addopts`, and skip-with-reason at runtime if collected without keys.
  No live API call happens during normal validation.

## Substrate

### Pinned candidate (the artifacts under grade)

`_substrate.build_candidate()` returns a `CandidateSchema` for a fictional
`dim_customers` model, hand-authored to span the rubric's calibration
space (engineered determinism per `.claude/rules/testing-signal.md`
§ "Engineered determinism over snapshot normalisation"):

| Artifact | Shape | Intended baseline signal |
|---|---|---|
| `customer_id` description | Strong, specific, sourced | passes every criterion |
| `customer_id` rationale | Strong, names downstream consumers | passes every criterion |
| `email` description | Adequate, concrete | passes clarity / consistency |
| `email` rationale | Thin ("Contact channel.") | fails clarity / rationale |
| `status` description | Deliberately vague ("A status field…") | fails clarity / rationale |
| `status` rationale | Restates the description | fails clarity / rationale / no-redundant |
| `model` description / rationale | Strong, conformed-dimension framing | passes every criterion |
| `customer_id` `not_null` / `unique` tests | Well-justified | passes every criterion |
| `status` `accepted_values` test | Well-justified closed set | passes every criterion |

The engine's `_stable_artifact_pairs(candidate)` derives **11 artifacts**
from this shape (3 column descriptions + 3 column rationales + model
description + model rationale + 3 test rationales). The harness derives
the `artifact_id` set from the engine itself (via
`_substrate.expected_artifact_ids`) rather than hand-listing it, so the
sample can never silently drift from the formatter
(`.claude/rules/grade-layer.md` § "`_artifact_id_for` … hoist").

Over the locked four-criterion `DEFAULT_RUBRIC` (`clarity`,
`consistency`, `rationale`, `no-redundant`) this is **11 × 4 = 44 judge
calls** per run — a reasonable maintainer-gate budget on Haiku
(materially cheaper than the Sonnet baseline; cf. the ~$0.005/call Sonnet
figure in `docs/research/179-test-primitive-expansion-retest.md`).

### Curated Sonnet baseline

`sonnet_baseline_sample.json` is a **curated sample, NOT the raw #179
Phase-B `grade.jsonl` dump**. That dump is not committed anywhere in this
repo (`find . -name grade.jsonl` finds only the drift-detector fixture at
`tests/fixtures/grade/grade_event_v1.jsonl`), and the #179 retest was run
against a private `intuit_airflow` fixture with transient `/tmp/phaseB/`
sidecars (see `docs/research/179-test-primitive-expansion-retest.md`
§ "Reproducing this retest"). Rather than depend on an un-committed dump,
the baseline here is a small representative sample of **44 hand-assigned
plausible `claude-sonnet-4-6` pass/fail verdicts** — one per
`(artifact_id, criterion_id)` pair — whose distribution tracks the
engineered candidate shape above (strong artifacts pass; vague / thin /
redundant artifacts fail on the relevant criteria).

This makes the comparison reproducible with the **only live variable
being the Haiku re-grade**: the candidate is pinned bytes, the rubric is
locked, the baseline is committed. A concordant Haiku run reproduces the
same verdict distribution; a discordant one surfaces the specific
`(artifact, criterion)` pairs where Haiku and the baseline disagree.

### Config under test

`GradeConfig()` with all defaults — after US-002 this resolves to:

- `model` → `claude-haiku-4-5` (provider fast-model resolver,
  `provider="anthropic"`),
- `max_output_tokens` → `1024`,
- `provider` → `anthropic`.

The harness asserts both resolved values before grading, so a regression
in the resolver fails the gate loud rather than silently measuring the
wrong SKU.

## Method — the ≥ 85% concordance rule

1. Build the resolved Haiku-default `GradeConfig()`; assert
   `model == "claude-haiku-4-5"` and `max_output_tokens == 1024`.
2. Assert the committed baseline covers every `artifact_id` the engine
   will grade (no silent gaps).
3. Run `grade_artifacts(model, candidate, prune_result, config=...)` —
   44 live Haiku judge calls.
4. For each returned `GradingResult`, join to the baseline by
   `(artifact_id, criterion_id)`:
   - `score is None` (degraded, DEC-015 of #7) → counted as **degraded**,
     excluded from the agreement denominator (neither concordant nor
     discordant — the pair could not be positively evaluated).
   - otherwise → **comparable**; `agreement` iff
     `result.passed == baseline_passed`.
5. `agreement_rate = agreements / comparable`. Assert
   `agreement_rate >= 0.85`. The harness prints the full breakdown
   (model, comparable count, agreements, degraded count, rate, and each
   discordance) regardless of pass/fail so a sub-threshold run still
   surfaces the disagreements for the writeup.

**Decision:** if the rate clears 85%, the Haiku default ships as planned.
If it falls short, the printed discordances name the specific
`(artifact, criterion)` shapes where Haiku diverges — those become the
follow-on (prompt-engineering, rubric-tuning, or reconsidering the
default), not silent acceptance. (Same disposition as the #179 epic's
"name the shapes that fell through" acceptance criterion.)

### Running the gate (maintainer)

```bash
# From the repo root, with a live key:
ANTHROPIC_API_KEY=sk-... \
  uv run pytest -m anthropic --no-cov -s \
  tests/research/187-haiku-calibration/test_haiku_calibration.py
```

`--no-cov` is required because the gated path exercises only a fraction
of the codebase and would trip the 80% coverage floor in `addopts`
(mirrors the `pytest -m bigquery --no-cov` precedent in
`.claude/rules/testing-signal.md`). `-s` surfaces the printed concordance
breakdown.

For the Gemini 1024-token check:

```bash
SF_RUN_GEMINI=1 GOOGLE_API_KEY=... \
  uv run pytest -m gemini --no-cov -s \
  tests/research/187-haiku-calibration/test_gemini_1024_no_truncation.py
```

## Result (maintainer-filled)

> **TODO (maintainer):** run `pytest -m anthropic --no-cov -s
> tests/research/187-haiku-calibration/test_haiku_calibration.py` with a
> live `ANTHROPIC_API_KEY` and fill in the table + verdict below from the
> printed breakdown. Then run the Gemini check and record its outcome.

**Run metadata**

- Date run: `TODO`
- SignalForge version: `TODO` (e.g. `0.x.y.dev0`)
- Grade model resolved: `claude-haiku-4-5` (assert in-test)
- `max_output_tokens`: `1024`

**Haiku-vs-Sonnet concordance**

| Metric | Value |
|---|---|
| Comparable verdicts | `TODO / 44` |
| Agreements | `TODO` |
| Degraded (`score=None`) | `TODO` |
| **Agreement rate** | `TODO %` |
| Decision threshold | 85% |
| **Verdict** | `TODO` PASS / FAIL |

**Discordances** (if any — `(artifact_id, criterion, sonnet_passed, haiku_passed)`):

- `TODO` (or "none — full concordance")

**Gemini 1024-token no-truncation check**

- Outcome: `TODO` (PASS = no `score=None` degrade / FAIL = truncation observed)
- Notes: `TODO`

**Disposition:** `TODO` — ship the Haiku default as planned, OR name the
follow-on if concordance fell short.

## References

- Issue **#187** — the epic this writeup gates (Haiku grade default).
  US-001..US-003 ship the resolver + 1024 cap; US-005 (this) ships the
  gated harness + writeup; US-006 owns the docs/rules/CHANGELOG updates.
- `docs/research/179-test-primitive-expansion-retest.md` — the prior
  empirical-retest writeup whose structure this mirrors; source of the
  "name the shapes that fell through" disposition and the cost reference.
- `tests/grade/test_smoke_real_api.py` — the `anthropic`-gated grade
  smoke whose marker + env-skip pattern the concordance gate reuses.
- `tests/grade/test_gemini_grade_live.py` — the `gemini`-gated grade
  smoke whose `SF_RUN_GEMINI` + `GOOGLE_API_KEY` env gating the
  truncation check reuses.
- `.claude/rules/testing-signal.md` § "End-to-end gated tests" +
  § "Engineered determinism" — the gating + determinism conventions.
- `.claude/rules/grade-layer.md` — the grade-layer contract (artifact-id
  formatter, DEC-015 degraded path, four-criterion `DEFAULT_RUBRIC`).
