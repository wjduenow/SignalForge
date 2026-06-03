# Issue #187 — Haiku grade-default calibration gate

**Status:** RUN COMPLETE + DECISION IMPLEMENTED (2026-06-02). This gate was run
against a **real Sonnet baseline drafted from a real `intuit_airflow` model** and
**Haiku did NOT clear the ≥ 85% concordance bar** (81.8% and 77.0% on two
independent runs). Per the DEC-005 decision rule (`< 85% → opt-in, not default`),
**the grade default was kept at `claude-sonnet-4-6`** and `claude-haiku-4-5` ships
as an **explicit opt-in** (`grade.model: claude-haiku-4-5`). `PROVIDER_DEFAULT_MODELS`
now maps `anthropic → claude-sonnet-4-6` (OpenAI/Gemini keep their fast defaults).
See § "Result" and § "Disposition".

**Companion artefacts:**

- `tests/research/187-haiku-calibration/capture_sonnet_baseline.py` — one-shot
  capture: drafts artifacts for the real model (schema-only) and grades them
  with `claude-sonnet-4-6` to produce the baseline. Maintainer-run with a key.
- `tests/research/187-haiku-calibration/_substrate.py` — constructs the real
  `Model` deterministically + loads the frozen drafted candidate + the baseline.
- `tests/research/187-haiku-calibration/real_candidate.json` — the frozen
  `CandidateSchema` the production drafter (Sonnet) emitted for the model.
- `tests/research/187-haiku-calibration/sonnet_baseline_sample.json` — **live
  `claude-sonnet-4-6` grades** of the frozen artifacts (NOT hand-authored).
- `tests/research/187-haiku-calibration/test_haiku_calibration.py` — the
  `@pytest.mark.anthropic`-gated concordance gate.
- `tests/research/187-haiku-calibration/test_gemini_1024_no_truncation.py` —
  the `@pytest.mark.gemini`-gated 1024-token no-truncation check (DEC-004).

## tl;dr

- **The question:** does `claude-haiku-4-5` grade rubric artifacts concordantly
  with `claude-sonnet-4-6`? Decision rule: **≥ 85% per-criterion pass/fail
  agreement** over a pinned sample (DEC-005).
- **Real substrate (recaptured on request):** the artifacts are no longer
  synthetic. The production drafter drafted a `CandidateSchema` for the real
  `intuit_airflow` model `plugins/dbt/models/analytical/calendar_hour.sql`
  (schema-only — no warehouse), frozen to `real_candidate.json`. The baseline is
  **live `claude-sonnet-4-6` grades** of those frozen artifacts. Only the Haiku
  re-grade is the live variable.
- **Result — the gate FAILS:** Haiku agreement was **81.8%** (run 1) and
  **77.0%** (run 2), both **below 85%**. The divergence is **systematic, not
  noise**: ~80% of discordances are `sonnet=pass → haiku=fail` — **Haiku grades
  the rubric stricter than Sonnet**, concentrated on the **`no-redundant`** and
  **`clarity`** criteria. Haiku-as-judge would flag column rationales /
  descriptions that Sonnet passes.
- **A second gated check PASSED** — DEC-004's claim that the new
  `max_output_tokens=1024` default leaves Gemini enough headroom holds at the
  single-artifact scale: `gemini-2.5-flash` graded a verbose artifact at 1024
  tokens with no truncation degrade (see § "Gemini check"; full-fixture runs may
  still want 4096 per #158).
- **Default CI is untouched:** both checks are deselected by the `anthropic` /
  `gemini` markers in `pyproject.toml`'s `addopts` and skip-with-reason without
  keys. No live API call happens during normal validation. The concordance gate
  asserting ≥ 85% now **fails when run** — that failure IS the recorded signal
  that the default is mis-calibrated.

## Substrate

### Pinned candidate (the artifacts under grade) — REAL, drafted from intuit_airflow

`_substrate.build_model()` constructs the real `calendar_hour` hour-grain time
dimension deterministically (its SQL + four business columns — `date_id`,
`hour_of_day`, `date_hour`, `prior_year_date_hour` — are inlined so the capture
reproduces without the `intuit_airflow` repo checked out).
`_substrate.build_candidate()` loads `real_candidate.json`: the artifacts the
**production drafter** (`claude-sonnet-4-6`, schema-only) emitted for that model,
frozen by `capture_sonnet_baseline.py`. Freezing the LLM draft makes the
artifacts deterministic.

The engine's `_stable_artifact_pairs(candidate)` derives **21 artifacts** from
the drafted candidate (4 column descriptions + 4 column rationales + model
description + model rationale + the drafted tests' rationales). The harness
derives the `artifact_id` set from the engine itself (via
`_substrate.expected_artifact_ids`) rather than hand-listing it, so the sample
can never drift from the formatter (`.claude/rules/grade-layer.md` §
"`_artifact_id_for` … hoist").

Over the locked four-criterion `DEFAULT_RUBRIC` (`clarity`, `consistency`,
`rationale`, `no-redundant`) this is **21 × 4 = 84 judge calls** per run.

### Real Sonnet baseline

`sonnet_baseline_sample.json` is now a **live `claude-sonnet-4-6` grade** of the
frozen artifacts (replacing the original hand-authored sample). Capture:
`capture_sonnet_baseline.py` drafts → freezes → grades with Sonnet → writes the
per-`(artifact_id, criterion_id)` pass/fail verdicts. Of the 84 pairs, **80 are
genuine Sonnet verdicts (63 pass / 17 fail)**; **4 pairs that Sonnet could not
grade** (`score=None`, retry exhaustion under rate limiting) are **excluded**
(see `degraded_count`) — a pair with no verdict is not a baseline.

This makes the comparison reproducible with the **only live variable being the
Haiku re-grade**: the model is deterministic bytes, the candidate is frozen
bytes, the rubric is locked, the Sonnet baseline is committed.

### Config under test

`GradeConfig(model="claude-haiku-4-5")` — the **Haiku opt-in**. After the
decision below, `GradeConfig()` (no model) resolves to the *Sonnet* default, so
the gate selects Haiku explicitly to measure the opt-in. `max_output_tokens →
1024`, `provider → anthropic`. The harness also asserts `GradeConfig().model ==
"claude-sonnet-4-6"` (the default is Sonnet, not Haiku) so a resolver regression
fails loud.

## Method — the ≥ 85% concordance rule

1. Build the Haiku opt-in `GradeConfig(model="claude-haiku-4-5")`; assert
   `max_output_tokens == 1024` and that the *default* `GradeConfig().model` is
   `claude-sonnet-4-6`.
2. Assert the committed baseline covers every `artifact_id` the engine will
   grade (no silent gaps).
3. Run `grade_artifacts(...)` — 84 live Haiku judge calls.
4. For each `GradingResult`, join to the baseline by `(artifact_id,
   criterion_id)`:
   - `score is None` (degraded, DEC-015 of #7) → **degraded**, excluded from the
     denominator (neither concordant nor discordant).
   - otherwise → **comparable**; `agreement` iff `result.passed ==
     baseline_passed`.
5. `agreement_rate = agreements / comparable`. Assert `>= 0.85`. A guard
   (`comparable >= degraded`) rejects a degraded-dominated run as too noisy to
   trust. The harness prints the full breakdown (each discordance) regardless of
   pass/fail.

### Running the gate (maintainer)

```bash
# 1) (Re)capture the real Sonnet baseline — drafts + grades with Sonnet:
set -a && source <repo-root>/.env && set +a   # provides ANTHROPIC_API_KEY
uv run python tests/research/187-haiku-calibration/capture_sonnet_baseline.py

# 2) Run the Haiku concordance gate against that baseline:
uv run pytest -m anthropic --no-cov -s \
  tests/research/187-haiku-calibration/test_haiku_calibration.py
```

`--no-cov` is required (the gated path exercises a fraction of the codebase and
would trip the 80% coverage floor in `addopts`). `-s` surfaces the printed
breakdown. For the Gemini 1024-token check:

```bash
SF_RUN_GEMINI=1 GOOGLE_API_KEY=... \
  uv run pytest -m gemini --no-cov -s \
  tests/research/187-haiku-calibration/test_gemini_1024_no_truncation.py
```

## Result

**Run metadata** — Date: 2026-06-02 · SignalForge `0.6.0.dev0` · grade model
resolved `claude-haiku-4-5` · `max_output_tokens=1024` · baseline = 80 live
`claude-sonnet-4-6` verdicts (63 pass / 17 fail; 4 Sonnet-degraded excluded) of
the frozen `calendar_hour` artifacts.

**Haiku-vs-Sonnet concordance** (two independent runs — Haiku grading is itself
non-deterministic):

| Metric | Run 1 | Run 2 |
|---|---|---|
| Comparable verdicts | 77 | 74 |
| Agreements | 63 | 57 |
| Degraded (`score=None`, Haiku side) | 3 | 6 |
| **Agreement rate** | **81.8%** | **77.0%** |
| Decision threshold | 85% | 85% |
| **Verdict** | **FAIL** | **FAIL** |

**Both runs fall short of 85%**, and the gap is not a single-run fluke: across
two runs Haiku sits in the ~77–82% band.

**Discordances are systematic — Haiku grades stricter than Sonnet.** Of the 17
discordances in run 2, **14 are `sonnet=pass → haiku=fail`** (Haiku fails what
Sonnet passes) and only 3 are the reverse. They cluster by criterion:

- **`no-redundant` (8 discordances, all sonnet=pass → haiku=fail):**
  `column.date_id.rationale`, `column.hour_of_day.{description,rationale}`,
  `column.date_id.description`, `column.prior_year_date_hour.rationale`,
  `model.description`, `test.column.prior_year_date_hour.custom_sql`. Haiku
  reads column rationales/descriptions as redundant with each other where Sonnet
  tolerates them.
- **`clarity` (4, all sonnet=pass → haiku=fail):** `column.hour_of_day.rationale`
  and three test rationales (`hour_of_day.custom_sql`,
  `prior_year_date_hour.custom_sql`, `model.row_count_anomaly_by_period`).
- **`rationale` (2, sonnet=pass → haiku=fail):** `column.date_id.rationale`,
  `column.hour_of_day.rationale`.
- **3 reverse (`sonnet=fail → haiku=pass`), all `consistency`/`no-redundant` on
  test artifacts** (`column.hour_of_day.description` consistency;
  `test.model.row_count_between` consistency + no-redundant) — Haiku is *more*
  lenient on a couple of test-rationale shapes.

**Interpretation.** Haiku is a stricter rubric judge than Sonnet, especially on
redundancy and clarity of short column rationales. This is a real behavioural
difference, not sampling noise — it reproduces across runs and concentrates on
two specific criteria. For SignalForge that means a Haiku default would flag
more artifacts (lower kept-rate on the grade side) than the Sonnet baseline an
operator calibrated against.

### Gemini check

The `gemini-2.5-flash` @ 1024-token no-truncation check
(`test_gemini_1024_no_truncation.py`) was **run and PASSED** (2026-06-02,
`SF_RUN_GEMINI=1` + `GOOGLE_API_KEY`): grading a deliberately verbose artifact on
`gemini-2.5-flash` at the new `max_output_tokens=1024` default produced a clean
`GradingResult` with **no `score=None` truncation degrade**. So DEC-004's bump
(256 → 1024) is empirically sufficient at the **single-artifact-in-isolation**
scale. The full-fixture caveat still stands: #158 observed a minority of pairs
degrading at 1024/2048 across the whole Austin fixture, so the per-provider
floors recommend **4096** for Gemini-heavy runs — 1024 is a safe default-level
improvement, not a guarantee at volume.

## Disposition

Per the DEC-005 decision rule (**< 85% → opt-in knob, not default**), the real
calibration says **do not ship `claude-haiku-4-5` as the resolved grade
default**. **Decision: Option 1 was chosen and implemented** (2026-06-02).

1. **✅ CHOSEN + IMPLEMENTED — Haiku is opt-in, Sonnet is the grade default.**
   `PROVIDER_DEFAULT_MODELS["anthropic"]` now resolves to `claude-sonnet-4-6`;
   `grade.model: claude-haiku-4-5` is the documented operator opt-in (faster,
   ~3.75× cheaper, but stricter). The per-provider resolver, compat validator,
   and 1024 cap all stand — only the Anthropic *default target* changed.
   OpenAI/Gemini fast defaults are unaffected by this Anthropic-specific finding
   (they were never calibrated against a Sonnet baseline; they're explicit
   operator choices). The constant was renamed `PROVIDER_FAST_MODELS` →
   `PROVIDER_DEFAULT_MODELS` since Sonnet is not "fast". This gated test now
   selects Haiku explicitly and still asserts ≥ 85% (so it fails) — the failure
   is the durable record that Haiku is the stricter opt-in, not the default.
2. **Accept Haiku at ~80% with eyes open** — only if the maintainer judges the
   ~3× speed / ~3.75× cost win worth a stricter judge that flags ~1 in 5 rubric
   verdicts differently. This contradicts the gate's own rule; if taken, lower
   `_CONCORDANCE_THRESHOLD` deliberately and document why here.
3. **Re-calibrate the rubric/prompt for Haiku** (v0.x follow-up) — the
   discordances are concentrated on `no-redundant`/`clarity`, so a Haiku-tuned
   criterion prompt might close the gap. Larger scope than #187.

This is a maintainer product decision; the harness + this writeup record the
evidence. The gated test deliberately still asserts ≥ 85% (it fails on Haiku) so
the signal can't be silently lost.

## References

- Issue **#187** — the epic this writeup gates (Haiku grade default).
- `docs/research/179-test-primitive-expansion-retest.md` — the prior
  empirical-retest writeup whose structure this mirrors; the `intuit_airflow`
  dbt project is the same source repo.
- `tests/grade/test_smoke_real_api.py` — the `anthropic`-gated grade smoke whose
  marker + env-skip pattern the concordance gate reuses.
- `tests/grade/test_gemini_grade_live.py` — the `gemini`-gated smoke whose
  `SF_RUN_GEMINI` + `GOOGLE_API_KEY` gating the truncation check reuses.
- `.claude/rules/testing-signal.md` § "End-to-end gated tests" + § "Engineered
  determinism" + § "Gated calibration/concordance harness" — the conventions.
- `.claude/rules/grade-layer.md` — the grade-layer contract (artifact-id
  formatter, DEC-015 degraded path, four-criterion `DEFAULT_RUBRIC`).
