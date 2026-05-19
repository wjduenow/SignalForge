# Issue #10 — End-to-end smoke test against bigquery-public-data

## Meta

- **Ticket:** [#10](https://github.com/wjduenow/SignalForge/issues/10)
- **Branch:** `feature/10-e2e-bq-smoke` (off `dev`)
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/10-e2e-bq-smoke`
- **Phase:** devolved (PR #32 draft; epic + 8 tasks live in beads 2026-05-09)
- **PR:** [#32](https://github.com/wjduenow/SignalForge/pull/32) (draft)
- **Sessions:** 1 (started 2026-05-09)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1 (capstone validation — every prior v0.1 ticket is shipped; this proves the seams compose against real Anthropic + real BigQuery)
- **Labels:** `evaluation`

---

## Discovery

### Ticket summary (verbatim from GitHub issue #10)

> **Goal:** Prove the pipeline works end-to-end against a real public dataset before declaring v0.1 shippable.
>
> **Acceptance criteria:**
> - Test fixture: a minimal dbt project pointing at `bigquery-public-data.austin_bikeshare` (or similar)
> - `signalforge generate` produces a non-empty diff
> - At least one candidate test is dropped by the prune engine
> - At least one artifact is flagged below grading threshold
> - Test gated by `SF_RUN_BQ=1` and `ANTHROPIC_API_KEY`; skipped in default CI
> - Documented in README under "Trying it out"
>
> **Notes:** austin_bikeshare is small (4 tables, ~1M rows) and stable — good for repeatable demos.

This ticket is the **v0.1 capstone validation**, not a unit test. Every existing stage is already unit-tested with fakes (`FakeAnthropicClient`, `FakeBigQueryClient`); only this test proves the seams compose end-to-end against real Anthropic + real BigQuery. Issue #21 shipped a silent `TableRef`-vs-string SDK contract bug that lived through two issues' worth of work because every test used `FakeBigQueryClient` and never noticed; the warehouse-adapters rule (lesson "Don't pass our `TableRef` straight into vendor SDK methods") exists because of that incident, and #10 is the discipline that forces the same lesson on every future stage.

The deliverable is **three artefacts in lockstep**:

1. A committed minimal-dbt-project fixture (under `tests/fixtures/`) pointing at `bigquery-public-data.austin_bikeshare`.
2. A gated pytest test (likely `tests/cli/test_e2e_bigquery_smoke.py`) that invokes `signalforge.cli.main(["generate", ...])` end-to-end and asserts on the resulting `DiffReport`.
3. A `## Trying it out` section in `README.md` walking a user through the same flow manually.

NO new production code. The CLI was completed in #9; every stage seam is already in place. This ticket is **fixtures + tests + docs only**.

### Codebase findings (Subagent B — directly verified, file:line cited)

**Existing integration-test pattern.** `tests/warehouse/test_bigquery_integration.py:1-173` is the precedent: belt-and-suspenders gating via `@pytest.mark.bigquery` (filtered out by `pyproject.toml:57` `addopts = "-m 'not bigquery and not anthropic and not cli_subprocess'"`) PLUS a runtime `@pytest.mark.skipif(not _bq_runs_enabled(), ...)` checking `SF_RUN_BQ` env var. The adapter is constructed with no arguments → uses Application Default Credentials. Maintainer-local pattern: `gcloud auth application-default login && SF_RUN_BQ=1 pytest -m bigquery --no-cov`.

**Markers registered in `pyproject.toml:52-70`:**

```text
"bigquery": tests requiring BigQuery credentials (gated by SF_RUN_BQ=1)
"anthropic": real-API smoke test (requires ANTHROPIC_API_KEY; excluded from default CI)
"cli_subprocess": belt-and-braces subprocess-driven CLI smoke (skipped by default)
```

No `@pytest.mark.anthropic` tests exist yet (only registered, not used). No `@pytest.mark.e2e` exists.

**CLI entry point.** `signalforge.cli.main(argv: list[str] | None = None) -> int` at `src/signalforge/cli/__init__.py:77`. The `generate` subcommand at `src/signalforge/cli/generate.py:106-296` accepts:

- `<model>` (positional; unique_id or file path)
- `--project-dir` (absolute assertion per DEC-027 — must directly contain `dbt_project.yml`; default unflagged behaviour walks up from cwd per DEC-001)
- `--manifest` (default `<project_dir>/target/manifest.json`)
- `--profiles-dir` (sets `DBT_PROFILES_DIR` env var per DEC-023)
- `--mode {schema-only,aggregate-only,sample}` (overrides `safety.mode`)
- `--min-score N` (overrides `grade.min_mean_score`; `[0.0, 1.0]`; below-threshold raises `GradeBelowThresholdError` → exit 2)
- `--write` / `--dry-run` (mutex)
- `--format {ansi,markdown,json}` (overrides `diff.render_kind`)
- `--scope {sample,full}` / `--sample-strategy {oneshot,materialised}`
- `--quiet` / `--verbose` / `--no-color`

**Anthropic client wiring.** `src/signalforge/llm/_client.py:72-86` defines `_make_anthropic_client(api_key=None) -> _AnthropicClientProtocol`. Default `api_key=None` → SDK reads `ANTHROPIC_API_KEY` env var (standard SDK behaviour). The CLI's `cmd_generate` calls `_make_anthropic_client()` at `src/signalforge/cli/generate.py:502` and threads it into `draft_module.draft_schema(..., _client=client)` (line 506) and `grade_module.grade_artifacts(..., client=client)` (line 592). For a real-Anthropic smoke test, simply set `ANTHROPIC_API_KEY` in the env and let the CLI's normal seam fire — no test-only injection needed on the production path.

**Existing fixture layout.** `tests/fixtures/dbt_project_small/` and `tests/fixtures/dbt_project_medium/` are committed minimal dbt projects, **but both target DuckDB**, not BigQuery. The DuckDB fixtures commit `target/manifest_v{9,10,11,12}.json` (four schema versions covering dbt-core 1.5–1.8). `tests/fixtures/profiles/bigquery_oauth.yml` exists as a profile fixture but is not paired with a dbt project pointing at a real BQ dataset. **No committed `target/manifest.json` for `bigquery-public-data` exists** — this ticket creates the first one.

**Manifest regeneration script.** `tests/fixtures/regenerate.sh` regenerates DuckDB manifests via `uvx --python 3.11 --from "dbt-core==X.Y.*" --with "dbt-duckdb==Y.*" dbt parse`, with `jq` post-processing to strip non-deterministic fields (`generated_at`, `invocation_id`, `user_id`, etc.). The same pattern adapts to `dbt-bigquery` for the austin_bikeshare manifest. Pinning convention: dev-deps pin the **latest** schema only (currently dbt 1.8); older schemas are summoned ephemerally.

**Per-stage default config behaviour.** All five stage loaders (`load_safety_config`, `load_draft_config`, `load_prune_config`, `load_grade_config`, `load_diff_config`) follow the same resolution contract: explicit `path=` errors if missing; `<project_dir>/signalforge.yml` missing or top-level key absent → defaults silently. **The smoke fixture can ship without a `signalforge.yml`** — the test will run with: `safety.mode=schema-only`, `draft.model=claude-sonnet-4-6`, `prune.scope=sample`, `prune.sample_strategy=materialised`, `grade.min_pass_rate=0.7`, `grade.min_mean_score=0.5`, `grade.fail_on_below_threshold=False`, `diff.render_kind=ansi`. Whether this default mix produces a "non-empty diff with at least one drop and one flag" reliably is the central open question (see SQ-03 / SQ-04).

**README "Trying it out" section.** `README.md:44-63` has a `## Quick start` section with the `pip install -e ".[dev]"` + `signalforge generate models/marts/customer_lifetime_value.sql` shape. **No "Trying it out" section exists yet.** The AC names a new section explicitly.

**austin_bikeshare schema.** No committed catalog/schema dump under `tests/fixtures/`. The four tables (per the ticket): `bikeshare_stations`, `bikeshare_trips`, `citibike_stations`, `citibike_trips_2013_2014`. Real schema is resolved at runtime via `dbt parse` against the live dataset; the regen script will hit the network once per refresh.

**Existing CI.** `.github/workflows/ci.yml:1-45` runs `pytest` on Python 3.11 with default `addopts` — gated tests are excluded. No scheduled / nightly workflow exists; no `ANTHROPIC_API_KEY` or BigQuery credentials are configured in repo secrets. **For v0.1, the smoke test is maintainer-local only.** A v0.2 nightly workflow with billing-capped credentials is out of scope.

### Convention findings (Subagent C — rules and CLAUDE.md)

The Convention Checker reviewed all eleven `.claude/rules/*.md` files and `CLAUDE.md`. No `workflow-project.md` exists. The constraints that apply specifically to a fixtures-and-tests ticket (no production code):

1. **`testing-signal.md` § "Strict markers — both settings required"** — `addopts = "--strict-markers"` AND `strict_markers = true` BOTH required (pytest 9 quirk). New marker (if any) must be registered in `[tool.pytest.ini_options].markers`.
2. **`testing-signal.md` § "No `assert True`-shaped tests"** — Smoke test must be capable of failing if its target is broken. "Always-passes" smoke is worse than no smoke.
3. **`testing-signal.md` § "Fixture regeneration via ephemeral `uvx`"** — Commit the generated manifest; document a `uvx`-pinned regen script; strip non-deterministic fields with `jq`. Pin the tool version in dev-deps for the latest schema only.
4. **`testing-signal.md` § "Seeded determinism over snapshot normalisation"** — Prefer hash-based stable identifiers over post-hoc regex normalisation in test assertions.
5. **`testing-signal.md` coverage gate** — `--cov-fail-under=80` lives in `addopts`; gated runs use `--no-cov` (precedent: `pytest -m bigquery --no-cov`, `pytest -m cli_subprocess --no-cov`).
6. **`ci-supply-chain.md` DEC-003** — v0.1 CI locked to Python 3.11.
7. **`warehouse-adapters.md` DEC-005 cost cap** — `_default_job_config` pins `maximum_bytes_billed = 100 MB` default; the smoke test runs through the adapter so the cap is automatic. **No new cost-control plumbing is needed**, but the fixture's worked example should not blow past the cap (austin_bikeshare's biggest table is ~25 MB scanned for a `LIMIT 10_000` sample).
8. **`warehouse-adapters.md` DEC-022 + DEC-013** — Identifier validation + `repr()`-quoted error fields. Fixture table identifiers (`bikeshare_trips` etc.) pass the strict regex.
9. **`warehouse-adapters.md` lesson "Don't pass our `TableRef` straight into vendor SDK methods" (issue #21 lesson)** — This is precisely the load-bearing reason this ticket exists. The smoke test running against a real `bigquery.Client.get_table(...)` call is the only reliable defence. The fake never reproduces the SDK's input-type contracts.
10. **`cli-layer.md` DEC-016 no-traceback** — Smoke test asserts `"Traceback" not in capsys.readouterr().err` (or the equivalent on subprocess `stderr`).
11. **`cli-layer.md` DEC-018 subprocess-gated smoke** — One in-process `main(argv)` smoke + (optional) one subprocess-gated smoke. The subprocess form is the precedent for catching `[project.scripts]` regressions, but #9 already covers `signalforge --version` via subprocess; the e2e pipeline doesn't strictly need a second subprocess test.
12. **`cli-layer.md` DEC-008/024 four-tier exit codes** — `returncode == 0` on success; `returncode in (1,2,3)` on the four taxonomically distinct failure surfaces. The smoke test asserts `0`.
13. **`cli-layer.md` § "Multi-surface parity for behaviour changes"** — The README "Trying it out" section is one of the five user-facing surfaces. Update in lockstep with the test, the fixture path, the docstring on `_helpers.canonicalise_user_path`, and the DEC in the plan.
14. **`prune-engine.md` DEC-006/011 conservative drop-reason routing** — A test routed to `kept-without-evidence` because the warehouse failed is *kept*, not *dropped*. The smoke test's "at least one dropped" assertion must specifically check for `decision="dropped"` (not just `"kept-without-evidence"` masquerading as dropped). See SQ-02.
15. **`prune-engine.md` DEC-001 (#22 generalisation) `_SESSION._sf_sample_<run_id>` materialisation** — The default `sample_strategy=materialised` will land a temp table. The smoke test should let this fire (it's the v0.2 default and exercises the BQ session-state pattern); assertions should be tolerant of the side effect (the temp table is auto-cleaned by `__exit__`).
16. **`safety-layer.md` DEC-011 fail-closed audit** — `.signalforge/audit.jsonl` will be created at `<project_dir>/.signalforge/`. The smoke test must not assert the directory's absence; should assert the file is non-empty after a successful run.
17. **`grade-layer.md` DEC-002/015 graceful degrade** — A degraded grade (`score=None`) does not abort the run; `aggregate_complete: bool` reports completeness. The smoke test should assert `aggregate_complete=True` on the happy path (no degrade) so flake-prone runs surface as test failures.
18. **`grade-layer.md` DEC-016 locked rubric criterion texts** — The four default criteria (clarity / consistency / rationale / no-redundant) are pinned with a golden hash. The smoke test inherits the default rubric; no rubric authorship needed.
19. **CLAUDE.md Architectural Commitment #1 (signal over volume)** — The smoke test must positively prove that the prune engine dropped *something with evidence* (a real always-passes drop), not just routed everything to kept-without-evidence. This is the **anchor assertion** of the ticket.
20. **CLAUDE.md Architectural Commitment #5 (explainable diffs)** — The smoke test should assert the rendered diff carries per-artifact `why` text (not empty `why` fields).
21. **PR-title transition memory** — When this PR moves from `(plan)` to implementation, drop `(plan)` from the title via REST PATCH (gh pr edit hits GraphQL deprecation noise). Phase-7 process item.

**Top three highest-risk constraints for this ticket:**

- **`testing-signal.md` fixture regeneration** — Without a pinned regen script + `jq` non-determinism strip, the committed `manifest.json` rots on the next dbt-bigquery release.
- **`warehouse-adapters.md` DEC-005 cost cap + the issue-#21 lesson** — The whole *point* of this ticket is to surface real-SDK contract bugs the fakes can't reproduce. Without an explicit local-run discipline (`gcloud auth application-default login && SF_RUN_BQ=1 ANTHROPIC_API_KEY=... pytest -m e2e --no-cov`) documented in the README, maintainers won't run it before tagging v0.1, and we lose the only seam where these bugs surface.
- **Grade-flag determinism** — The default thresholds are tuned for production permissiveness, not for forcing flags. Without tightening thresholds in the fixture's `signalforge.yml grade:` block, the "at least one flagged below threshold" assertion is non-deterministic. See SQ-03.

### Domain research (Subagent A — ticket interpretation)

**Cost shape per run.** BQ side: 1 materialisation CTAS over a `LIMIT 10_000` sample of `bikeshare_trips` (~25 MB scanned with `_default_job_config`'s 100 MB cap) + ~5–10 per-test failing-rows queries against the `_SESSION` temp table (each <1 MB) + 1 `BQ.ABORT_SESSION()`. Total: <100 MB billed, well under the cap. Anthropic side: 1 draft call (~5k input tokens cached + ~2k output) + ~12 grade calls (one per `(artifact × criterion)` per `grade-layer.md` DEC-004; ~4 criteria × ~3 surviving artifacts) at ~500 input + 200 output each. Total: ~$0.10–$0.30 per run on Sonnet 4.6, <1 minute wall clock if cache hits. Safe for repeated local runs; would be safe nightly if anyone signs up for the babysitting (out of scope for v0.1).

**Who runs it.** Maintainers locally before tagging v0.1 release. NOT nightly CI — fork PRs cannot access `ANTHROPIC_API_KEY` (GitHub strips secrets from fork-originated workflows; same constraint as `ci-supply-chain.md` Codecov DEC-007). v0.2 may add a scheduled workflow with a billing-capped GCP project; out of scope.

---

## Scoping questions (answered 2026-05-09)

| ID | Question | Resolution |
|----|----------|------------|
| SQ-01 | Non-empty diff definition | **kept_count >= 1** — strictest signal; complements SQ-02. |
| SQ-02 | Drop-reason specificity | **Must be `drop_reason="always-passes"`** — the v0.1 differentiator; bikeshare's clean high-volume data should reliably surface an always-pass `not_null` or `unique`. |
| SQ-03 | Grade-flag determinism | **Tight thresholds in fixture YAML** — fixture ships `signalforge.yml` with `grade.min_pass_rate=0.95` and `grade.min_mean_score=0.95` so any criterion miss forces a flag. Aligns with `testing-signal.md` (no flaky tests). |
| SQ-04 | Marker strategy | **New `@pytest.mark.e2e` marker** — registered in `pyproject.toml`; added to `addopts` exclusion list. Maintainer command: `pytest -m e2e --no-cov`. Single composable gate; matches `cli_subprocess` precedent. |
| SQ-05 | Sampling mode | **`--mode aggregate-only`** — LLM sees column stats without row contents. PII-safe by default; gives drafter enough signal. Override via fixture YAML `safety.mode: aggregate-only` (default `--mode` stays unset; the fixture YAML is authoritative). |
| SQ-06 | Manifest fixture | **Commit `target/manifest.json` + regen script** — mirrors issue #2 precedent. Fixture under `tests/fixtures/dbt_project_austin/`; regen script at `tests/fixtures/dbt_project_austin/regenerate.sh` invoking `uvx --python 3.11 --from "dbt-bigquery==1.8.*" --with "dbt-core==1.8.*" dbt parse`. |
| SQ-07 | GCP billing project gate | **Skip if `GOOGLE_CLOUD_PROJECT` is unset** — third skip gate alongside `SF_RUN_BQ=1` and `ANTHROPIC_API_KEY`. Surfaces a clear error to maintainers who set the first two but forgot the billing project. ADC alone is insufficient because BQ won't bill `bigquery-public-data` to itself. |
| SQ-08 | README placement | **New H2 `## Trying it out`** after `## Quick start` — matches AC wording verbatim; baked-in (low stakes; no SQ asked). |
| SQ-09 | Subprocess paired smoke | **In-process `main(argv)` only** — #9's `tests/cli/test_subprocess_smoke.py` already covers `[project.scripts]` regressions for `--version`. Doubling the e2e as a subprocess form costs ~30s spawn for no incremental coverage. |

**Baked-in (no SQ):**

- **Target table:** `bigquery-public-data.austin_bikeshare.bikeshare_trips` (~1M rows, the largest of the four tables in the dataset; richest column set for the LLM to draft against).
- **dbt project model count:** one staging model `stg_bikeshare_trips` selecting from the source table with a thin transformation (e.g., `EXTRACT(HOUR FROM start_time) AS start_hour`) so column descriptions have interesting LLM-drafted prose. One model = one `signalforge generate` invocation per test run.
- **Locked invocation in fixture YAML:** `safety.mode: aggregate-only`, `prune.scope: sample`, `prune.sample_strategy: materialised` (the v0.2 default; exercises the `_SESSION._sf_sample_<run_id>` materialisation path), `grade.min_pass_rate: 0.95`, `grade.min_mean_score: 0.95`, `grade.fail_on_below_threshold: false` (the test asserts the report's `flagged_count >= 1` itself; we don't want the CLI to exit 2 on the assertion).
- **README quickstart link target:** the new "Trying it out" section will link the user to the same fixture path under `tests/fixtures/dbt_project_austin/` so the worked example is the same artefact the maintainer tests against.
- **Phase 7 PR-title transition:** when this PR moves from `(plan)` to implementation, drop `(plan)` from the title via REST PATCH (per memory `feedback_pr_title_plan_to_impl.md`).

---

## Architecture review

| Area | Rating | Headline finding |
|------|--------|------------------|
| **Cost & performance** | **pass** | Every BQ query path (`materialise_sample`, `column_stats`, `sample_rows`, `run_test_sql`) routes through `_default_job_config` (verified at `src/signalforge/warehouse/adapters/bigquery.py` lines 533–535, 681–686, 860–861, 927–930). Per-run total: ~50–80 MB billed (≪ 100 MB cap), ~$0.13 Anthropic spend, ~50s wall-clock. No bypass paths. |
| **Security & credentials** | **concern** | (1) Fixture-pollution risk — defaults land `<project_dir>/.signalforge/{audit,prune,grade}.jsonl` and `diff.json` into the committed fixture dir if test runs in place. (2) Committed `profiles.yml` and manifest are non-sensitive (verified against `tests/fixtures/profiles/bigquery_oauth.yml` and `tests/fixtures/dbt_project_small/target/manifest_v12.json`). (3) Anthropic key path is safe (`json.dumps` lazy-format gate; no f-strings). **Mitigation:** copy fixture to `tmp_path` at test entry; assert audits land under `tmp_path/.signalforge/`. |
| **Test reliability & flake** | **concern** | Five-assertion flake-risk analysis: `kept_count >= 1` ~30% (LLM non-det), **`drop_reason="always-passes"` ~40% (HIGH — depends on what the LLM drafts AND warehouse data variability)**, `tier="flagged"` ~5% (low), `aggregate_complete=True` ~25% (default `total_budget_seconds=300` is tight at p99 latency × 12+ grade calls + retries), `"Traceback" not in stderr` <1%. **Mitigations:** (a) bump fixture `grade.total_budget_seconds=600`, (b) engineer the staging model to include at least one "guaranteed always-pass" column so the LLM-drafted `not_null` deterministically drops as `always-passes` — see RQ-01 below. |
| **Docs & fixture lifecycle** | **pass** | Regen-script convention is clear (`tests/fixtures/regenerate.sh:65-95`, the `uvx --from "<adapter>==X.Y.*" --with "dbt-core==X.Y.*" dbt parse` shape; jq strips five non-deterministic fields). For the BigQuery fixture: pin `dbt-bigquery==1.8.*` floating, mirroring DuckDB float convention. CLAUDE.md `## Repository status` adds a `#10` bullet (mirroring the #9 shape). README `## Trying it out` is the new H2; `docs/cli-ops.md` § "Worked example" gets a cross-ref. **No separate `docs/e2e-smoke-ops.md`** — e2e is a test artefact, not a pipeline layer. |

**Cost & performance details:**

- BQ side per run: 1 materialisation CTAS (~25 MB on `LIMIT 10_000` of `bikeshare_trips`) + 1 column-stats query (`safety.mode=aggregate-only`; ~5 MB) + 5–10 per-test failing-rows queries against `_SESSION._sf_sample_<run_id>` (each <1 MB) + 1 `BQ.ABORT_SESSION()`. Total: <100 MB billed (well under the 100 MB `_default_job_config` cap).
- Anthropic side (Sonnet 4.6 pricing — input $3/1M, output $15/1M, cached $0.30/1M): 1 draft (~5k cached + 2k dynamic + 2k output ≈ $0.07) + ~12 grade calls (4 criteria × ~3 surviving artifacts; cached rubric ~1.5k + ~500 input + ~200 output each ≈ $0.06). Total: ~$0.13 per run.
- Wall-clock: ~50s (no `pytest-timeout` plugin configured; `pyproject.toml:52-70` confirmed).

**Security & credentials details:**

- Audit-output paths verified at `src/signalforge/safety/policy.py:77`, `src/signalforge/prune/engine.py:738`, `src/signalforge/grade/engine.py:747-752`, `src/signalforge/diff/engine.py:878`. All default to `<project_dir>/.signalforge/`. **The CLI exposes no flag to override these** — overrides are library-only kwargs. **Fix:** test copies the fixture into `tmp_path / "project"` via `shutil.copytree`, runs the CLI with `--project-dir tmp_path/project`, and asserts the audits land under `tmp_path/project/.signalforge/`.
- README "Trying it out" must document: `gcloud auth application-default login`, `export GOOGLE_CLOUD_PROJECT=...`, `export ANTHROPIC_API_KEY=...`. Operator should be reminded to scrub bash history (or use a temp shell session) so the key doesn't persist.

**Test reliability details (flake-risk analysis):**

- `kept_count >= 1`: LLM non-determinism. Mitigation = no special action; on a clean 8-column staging model the drafter reliably proposes >= 5 candidates and at least one survives prune.
- **`decision="dropped" AND drop_reason="always-passes"`: HIGH FLAKE WITHOUT MITIGATION.** The reliability subagent estimated ~40% failure probability; my counter-estimate is ~10–15% IF the source has any NOT NULL columns AND the LLM drafts `not_null` on at least one of them. Either way, the AC must be made deterministic — see RQ-01.
- `tier="flagged"` (with `min_pass_rate=0.95` / `min_mean_score=0.95`): ~5% — LLM rarely scores >0.95 on every criterion of every artifact at default rubric. Tightening already buys this; no further action.
- `aggregate_complete=True`: default `total_budget_seconds=300` is tight at p99 Anthropic latency. **Fix:** fixture `signalforge.yml` sets `grade.total_budget_seconds: 600`.
- `"Traceback" not in stderr`: deterministic (DEC-016 wraps the whole pipeline). No action.

**Manifest-rot & model-drift:**

- Manifest rot is LOW (Pydantic `extra="ignore"` is forward-compatible across dbt-bigquery minors). Mitigation: regen script pins to `==1.8.*`.
- Anthropic model drift (Sonnet 4.6 deprecation mid-v0.1) is a **MODERATE** risk. **Accepted as-is for v0.1** — when Sonnet 4.6 is deprecated, this fixture's `signalforge.yml llm.model: claude-sonnet-4-6` will fail; maintainer bumps in lockstep with `DraftConfig.model` default. Documented in fixture YAML with a header comment pointing at `src/signalforge/draft/config.py`.

---

---

## Refinement log

### Resolved refinement questions

- **RQ-01 (drop-reason determinism)** — RESOLVED 2026-05-09. The staging model `stg_bikeshare_trips` includes at least one literal-or-`COALESCE`'d column (`'austin' AS region` baseline; possibly a second one like `COALESCE(start_time, TIMESTAMP '1970-01-01') AS start_time_safe`). The LLM drafter reliably proposes `not_null` on every column; `not_null` on a literal column is mathematically guaranteed to always-pass. The fixture's staging SQL carries a one-line header comment explaining the engineering: `-- Literal/COALESCE'd columns deliberately included to give the LLM at least one mathematically-guaranteed always-pass test to drop (issue #10 AC).` Eliminates the ~40% flake risk on the load-bearing AC.

### Decisions

- **DEC-001 — Marker:** Register a new `@pytest.mark.e2e` marker in `pyproject.toml [tool.pytest.ini_options].markers`; add `"and not e2e"` to `addopts` exclusion. Maintainer command: `pytest -m e2e --no-cov`. Mirrors `cli_subprocess` precedent. (Resolves SQ-04.)
- **DEC-002 — Skip gates:** Test is gated by THREE env vars: `SF_RUN_BQ=1`, `ANTHROPIC_API_KEY`, `GOOGLE_CLOUD_PROJECT`. Belt-and-suspenders pattern (`@pytest.mark.e2e` + `@pytest.mark.skipif(...)` for runtime check). Missing any one → `pytest.skip(...)` with a clear message naming the missing var. Mirrors `tests/warehouse/test_bigquery_integration.py:9-17`. (Resolves SQ-07.)
- **DEC-003 — Fixture path:** `tests/fixtures/dbt_project_austin/` (mirrors `dbt_project_small/` and `dbt_project_medium/` shape). Contents: `dbt_project.yml`, `profiles.yml`, `models/staging/stg_bikeshare_trips.sql`, `models/staging/sources.yml`, `signalforge.yml`, `target/manifest.json`, `regenerate.sh`, `.gitignore` excluding `.signalforge/` and `dbt_packages/`.
- **DEC-004 — Manifest fixture:** Commit `target/manifest.json` (mirrors issue #2 precedent). Regenerate via `regenerate.sh` invoking `uvx --python 3.11 --from "dbt-bigquery==1.8.*" --with "dbt-core==1.8.*" dbt parse`. `jq` strips the same five non-deterministic fields the existing `tests/fixtures/regenerate.sh` strips: `metadata.{generated_at, invocation_id, user_id, send_anonymous_usage_stats, adapter_type}` and `metadata.env = {}`. (Resolves SQ-06.)
- **DEC-005 — Sampling mode:** Fixture `signalforge.yml` sets `safety.mode: aggregate-only`. Test invocation does NOT pass `--mode` (fixture YAML is authoritative). LLM sees column statistics (cardinality, null-count, etc.) but no row contents; PII-safe by default. (Resolves SQ-05.)
- **DEC-006 — Grade thresholds:** Fixture `signalforge.yml` sets `grade.min_pass_rate: 0.95` and `grade.min_mean_score: 0.95` so any criterion miss forces `tier="flagged"` on at least one artifact. `grade.fail_on_below_threshold: false` keeps the CLI from exiting 2 on its own; the test asserts `flagged_count >= 1` directly. (Resolves SQ-03.)
- **DEC-007 — Sample strategy:** Fixture YAML omits `prune.sample_strategy` (defaults to `materialised` per v0.2 precedent — `prune-engine.md` v0.2 reservations). Exercises the `_SESSION._sf_sample_<run_id>` materialisation path and the `BQ.ABORT_SESSION()` cleanup boundary. The smoke test's whole reason for existing is to surface real-SDK bugs the fakes can't reproduce; running through the materialised path is mandatory.
- **DEC-008 — Fixture isolation via `tmp_path`:** Test copies the fixture into a per-run `tmp_path / "project"` via `shutil.copytree`, then invokes `signalforge.cli.main(["generate", "...", "--project-dir", str(project_dir)])`. Asserts `(project_dir / ".signalforge" / "diff.json").is_file()` post-run. **Never** writes audits into the committed fixture dir. Mirrors `tests/cli/_factories.py:make_fake_dbt_project` precedent.
- **DEC-009 — Asserted invariants (six assertions, all in one test):**
  1. `cli.main(...) == 0` (exit code 0).
  2. `report.kept_count >= 1` (resolves SQ-01; signal-bearing).
  3. `any(d.decision == "dropped" and d.drop_reason == "always-passes" for d in prune_result.decisions)` (resolves SQ-02; the v0.1 differentiator).
  4. `report.flagged_count >= 1` (forced via tight thresholds).
  5. `grading_report.aggregate_complete is True` (no degraded calls).
  6. `"Traceback" not in capsys.readouterr().err` (DEC-016 of `cli-layer.md`).
  7. `(project_dir / ".signalforge" / "diff.json").is_file()` (sidecar landed).
  - Path: post-run, the test reads `diff.json` and `prune.jsonl` from the temp project dir to perform assertions 3 and 4 (the in-process `cli.main` doesn't return the typed result objects). DiffReport-shape assertions deserialise via the same Pydantic models the production code uses.
- **DEC-010 — Always-pass determinism (resolves RQ-01):** `stg_bikeshare_trips.sql` includes `'austin' AS region` and `COALESCE(start_time, TIMESTAMP '1970-01-01 00:00:00 UTC') AS start_time_safe`. Header comment cites issue #10 AC.
- **DEC-011 — Grade budget bump:** Fixture `signalforge.yml` sets `grade.total_budget_seconds: 600` (default 300 is tight at p99 Anthropic latency × ~12 grade calls + retries). Documented in fixture YAML with a one-line comment.
- **DEC-012 — Target dataset:** `bigquery-public-data.austin_bikeshare.bikeshare_trips` (the largest of the four tables; richest column set for the LLM to draft against). Verbatim per ticket suggestion.
- **DEC-013 — Model surface:** One staging model (`stg_bikeshare_trips`); one `signalforge generate` invocation per test run. Sources defined in `models/staging/sources.yml` pointing at `bigquery-public-data.austin_bikeshare`.
- **DEC-014 — README placement:** New `## Trying it out` H2 after `## Quick start` (matches AC wording verbatim). Walks through: `gcloud auth application-default login`; `export GOOGLE_CLOUD_PROJECT=...`; `export ANTHROPIC_API_KEY=...`; `cd tests/fixtures/dbt_project_austin/`; `signalforge generate models/staging/stg_bikeshare_trips.sql`. The model arg is the **file-path form** (`models/staging/<name>.sql`); the bare model name `stg_bikeshare_trips` does NOT resolve via `Manifest.get_model` (the loader routes bare names to the file-path branch which fails). Reminder line: scrub bash history or use a fresh shell so the API key doesn't persist. (Resolves SQ-08.)
- **DEC-015 — `docs/cli-ops.md` cross-ref:** Add a one-line link from `docs/cli-ops.md` § "Worked example" pointing at the new README "Trying it out" section. Mirrors the multi-surface parity rule (`cli-layer.md` § "Multi-surface parity for behaviour changes").
- **DEC-016 — `CLAUDE.md` "Repository status" update:** Add a `#10` bullet mirroring the `#9` shape: single-paragraph header naming the artifact (`tests/fixtures/dbt_project_austin/`), the gate (`pytest -m e2e --no-cov`), the env-var triple, and the cross-refs to `plans/super/10-e2e-bigquery-smoke.md` + the README section.
- **DEC-017 — Operator-facing docs surface (revised post-live-run):** v0.1 originally shipped no separate ops doc — the README's `## Trying it out` quickstart + the regen script's header comment + the test docstring were the operational surface. Superseded post-live-run by user request: a dedicated `docs/e2e-smoke-test.md` now ships alongside the README quickstart with a business-language intro, prerequisites, run command, cost ceiling, security hygiene, and a troubleshooting matrix. The README quickstart links into it. The test fixture is still a test artefact (not a pipeline layer like `cli-ops.md` / `prune-ops.md`), but it earned its own docs page because the operator-facing setup has enough surface area (three env vars, ADC bootstrap, billing-project nuance, troubleshooting branches) that the quickstart-only treatment was a friction point.
- **DEC-018 — Anthropic model drift accepted as-is for v0.1:** Fixture YAML pins `llm.model: claude-sonnet-4-6` with a one-line header comment: `# Pinned to match DraftConfig.model default; bump in lockstep when Sonnet 4.6 sunsets.` Maintainer is responsible for the bump.
- **DEC-019 — dbt-bigquery floating pin:** Regen script pins `dbt-bigquery==1.8.*` floating (mirrors DuckDB regen's `dbt-duckdb==1.8.*` float at `tests/fixtures/regenerate.sh:73`). Patch-level versions float; minor pin is firm.
- **DEC-020 — In-process `main(argv)` only:** No paired `@pytest.mark.cli_subprocess` test. #9's `tests/cli/test_subprocess_smoke.py` already covers `[project.scripts]` regressions. (Resolves SQ-09.)
- **DEC-021 — `.gitignore` for fixture:** `tests/fixtures/dbt_project_austin/.gitignore` excludes `.signalforge/`, `dbt_packages/`, `target/run_results.json`, `target/partial_parse.msgpack`, `logs/`. Only `target/manifest.json` is committed.
- **DEC-022 — `tests/fixtures/regenerate.sh` integration:** The new `tests/fixtures/dbt_project_austin/regenerate.sh` is a sibling of the existing `tests/fixtures/regenerate.sh` (does NOT modify the existing script). The existing top-level script handles DuckDB fixtures; the new script handles the BigQuery fixture. Both follow the same `uvx`-pinned + jq-strip pattern. (Single source of truth per fixture, not per repo.)
- **DEC-023 — Public-API helper for asserting drop reason:** A small private helper `tests/cli/_e2e_helpers.py::read_prune_decisions(project_dir) -> tuple[PruneDecision, ...]` deserialises `<project_dir>/.signalforge/prune.jsonl` via the public `signalforge.prune.PruneDecision` model (drift-detector-tested at the prune layer; safe to deserialise). The test calls this helper for assertion 3. Mirrors the post-call deserialisation pattern in existing CLI tests; never reaches into private internals.

### Live-run findings + decisions (post-merge follow-ups)

The first live e2e run against real Anthropic + real BigQuery surfaced four real bugs that unit tests had not caught. All four were addressed in-band; the patterns generalise.

- **DEC-024 — Path A pivot (source-as-model alias):** The fixture's manifest now sets `model.alias = "bikeshare_trips"` so `TableRef.from_model(model)` resolves the model's relation directly to `bigquery-public-data.austin_bikeshare.bikeshare_trips`. The model's filename (`stg_bikeshare_trips.sql`) and `unique_id` (`model.signalforge_test_austin.stg_bikeshare_trips`) stay unchanged, so the CLI argument is still `models/staging/stg_bikeshare_trips.sql`; only the materialised relation flips. Sidesteps the `dbt run` materialisation step that would otherwise be required (the maintainer would need write access to a billing-project dataset). Trade-off: the `always-passes` assertion now depends on natural NOT NULL columns in bikeshare data (`trip_id`, `start_time`) rather than engineered literals — bikeshare's data quality makes this reliable in practice. Supersedes DEC-010 (engineered always-pass columns) for v0.1.
- **DEC-025 — Drafter prompt JSON-shape example (cross-layer fix):** The drafter system prompt at `src/signalforge/draft/prompts.py:_SYSTEM_PROMPT` previously said "respond with a single JSON object" without showing the shape. Sonnet 4.6 inferred field names (`model` instead of `name`, `test` instead of `type`, no `schema_version`) from the manifest summary's section labels. Added an explicit `### OUTPUT FORMAT` JSON block + a "Field-name discipline" stanza naming the load-bearing fields. `_PROMPT_VERSION` rotated `1c558064` → `c7d15d59`; `tests/llm/test_prompt_cache_stability.py::_EXPECTED_PROMPT_VERSION` updated in lockstep. The cached-block snapshot stayed unchanged because only the system prompt changed. Issue-#10 catches the bug class issue #21 institutionalised — the smoke test exists for exactly this surface.
- **DEC-026 — `LLMCacheTooSmallError` retired (cache-marker soft-drop):** The pre-send `count_tokens` check used to raise `LLMCacheTooSmallError` when the cached block was below Anthropic's per-model minimum. This blocked any caller whose cached block was naturally small — the grade layer's compact rubric clocks at 291 tokens. Anthropic silently no-ops a sub-minimum cache marker, so the right behaviour is: drop the `cache_control` marker, log INFO once, and let `messages.create` proceed without caching. The class is removed from `signalforge.llm` `__all__` + `errors.py`; the CLI exit-code mapping entry is removed; `tests/draft/test_smoke_real_api.py` (which existed solely to exercise the hard-fail short-circuit) is deleted. Updated rules: `.claude/rules/llm-drafter.md` "Cached-block scope" section now describes the soft-drop semantics. The companion `LLMCacheTooLargeError` stays — DEC-009 of plan #5 explicitly intends the 8000-token cap as a SignalForge cache-stability invariant, not a workaround for a silent Anthropic no-op.
- **DEC-027 — Grade progress-count fix (artifact count, not kept-test count):** `signalforge.cli.generate.cmd_generate` previously emitted `[4/5] grade: scoring {prune_result.kept_count} artifacts × {criteria} criteria...`. That conflated "tests surviving prune" with "artifacts the grader iterates over"; when prune dropped everything, the message read "0 artifacts" but the grade engine still scored every column description, model description, and test rationale (~21 artifacts on a typical 7-column model). Corrected to compute the count from `draft_outcome.candidate`: `2 * len(columns) + 2 + sum(len(c.tests) for c in columns) + len(model.tests)`. Comment in the CLI points at `signalforge.grade.engine._stable_artifact_pairs` (DEC-018 of plan #7) as the source-of-truth contract.
- **DEC-028 — Per-run profile rewrite for billing isolation:** The committed `profiles.yml` pins `project: bigquery-public-data` so the regen script can `dbt parse` against the public dataset without contributors needing a billing project. But at query time, BigQuery uses `profile.project` as the *billing* project, and contributors can't bill `bigquery-public-data`. The e2e test now overwrites `profiles.yml` in `tmp_path` with `project: $GOOGLE_CLOUD_PROJECT` (and `maximum_bytes_billed: 1_000_000_000` to clear the 100 MB default cap so the materialised-sample CTAS over the ~2.27M-row source table can run). The committed profile stays oriented at the regen-script use case; the test rewrites at run time.
- **DEC-029 — `kept + flagged + dropped >= 1` instead of `kept_count >= 1`:** Tight grade thresholds (`min_pass_rate: 0.95`) force-flag every artifact that survives prune, so `kept_count` legitimately lands at 0 in a healthy run. The smoke test's "non-empty diff" invariant (SQ-01) is now `total_entries >= 1`; the independent `dropped_count >= 1` and `flagged_count >= 1` assertions pin the signal-bearing branches separately. Supersedes the `kept_count >= 1` wording of DEC-009 invariant 3 — the spirit (the pipeline produced shippable artifacts) is preserved; the literal count was over-strict given the fixture's deliberately strict thresholds.

---

## Detailed breakdown

Eight stories total: five implementation (US-001 … US-005) + one docs (US-006) + Quality Gate (US-007) + Patterns & Memory (US-008). Each implementation story is right-sized for one Ralph context window.

The natural ordering for a fixtures-and-tests ticket: fixture skeleton → manifest regen → fixture config → marker registration → e2e test → docs.

**Validation command** (every story's AC includes this; from `CLAUDE.md` § Validation):
```bash
pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest
```

Plus the gated maintainer-only run (after US-005 lands):
```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<billing-project>
export ANTHROPIC_API_KEY=sk-...
SF_RUN_BQ=1 pytest -m e2e --no-cov
```

---

### US-001 — Fixture skeleton (dbt project + sources + staging model)

**Description.** Land the bare-bones `tests/fixtures/dbt_project_austin/` directory with `dbt_project.yml`, `profiles.yml`, source definitions, the staging SQL with the engineered always-pass column, and `.gitignore`. Does NOT generate the manifest yet (US-002 does that).

**Traces to:** DEC-003, DEC-010, DEC-012, DEC-013, DEC-021.

**Files:**
- `tests/fixtures/dbt_project_austin/dbt_project.yml` (NEW) — name, profile, models config; no v1.9-specific keys; targets dbt-bigquery 1.8.x.
- `tests/fixtures/dbt_project_austin/profiles.yml` (NEW) — `austin` profile, `dev` target, `method: oauth` (ADC), `project: bigquery-public-data` (source), `dataset: austin_bikeshare`. Billing project comes from `GOOGLE_CLOUD_PROJECT` env var via SDK's standard ADC behaviour (mirrors `tests/fixtures/profiles/bigquery_oauth.yml` shape, but with real public-data values).
- `tests/fixtures/dbt_project_austin/models/staging/sources.yml` (NEW) — declares the source `bigquery-public-data.austin_bikeshare.bikeshare_trips`.
- `tests/fixtures/dbt_project_austin/models/staging/stg_bikeshare_trips.sql` (NEW) — `SELECT trip_id, subscriber_type, bikeid, start_time, start_station_id, end_station_id, duration_minutes, 'austin' AS region, COALESCE(start_time, TIMESTAMP '1970-01-01 00:00:00 UTC') AS start_time_safe FROM {{ source('austin_bikeshare', 'bikeshare_trips') }} LIMIT 100000`. One-line header comment: `-- Literal/COALESCE'd columns deliberately included to give the LLM at least one mathematically-guaranteed always-pass test to drop (issue #10 AC).` LIMIT 100k caps the materialised-sample input size.
- `tests/fixtures/dbt_project_austin/.gitignore` (NEW) — excludes `.signalforge/`, `dbt_packages/`, `target/run_results.json`, `target/partial_parse.msgpack`, `logs/` (DEC-021).

**Done when:**
- All four files committed.
- `ruff check .` and `ruff format --check .` pass (no Python files added; the YAML/SQL fixtures are not lint-gated).
- The staging SQL is syntactically valid dbt Jinja (verified by US-002's `dbt parse`; no separate check in this story).

**Depends on:** none.

**TDD:** N/A — fixture files, not logic.

---

### US-002 — Manifest regen script + committed `target/manifest.json`

**Description.** Add `tests/fixtures/dbt_project_austin/regenerate.sh` that runs `uvx`-pinned `dbt parse` against the live `bigquery-public-data.austin_bikeshare` dataset, jq-strips the same five non-deterministic fields the existing top-level regen script strips, and commits the resulting `target/manifest.json`. Run the script once locally with the maintainer's ADC + billing project to produce the committed manifest.

**Traces to:** DEC-004, DEC-019, DEC-022.

**Files:**
- `tests/fixtures/dbt_project_austin/regenerate.sh` (NEW, executable) — header comment block (mirrors `tests/fixtures/regenerate.sh` shape) explaining: requires `gcloud auth application-default login` + `GOOGLE_CLOUD_PROJECT` env var; pins `dbt-bigquery==1.8.*` floating; jq-strip targets the same five fields; not invoked by CI (maintainer-only). Body: `cd "$(dirname "$0")"; uvx --python 3.11 --from "dbt-bigquery==1.8.*" --with "dbt-core==1.8.*" dbt parse --project-dir . --profiles-dir .; jq '.metadata.generated_at = null | .metadata.invocation_id = null | .metadata.user_id = null | .metadata.send_anonymous_usage_stats = null | .metadata.adapter_type = null | .metadata.env = {}' target/manifest.json > target/manifest.json.tmp && mv target/manifest.json.tmp target/manifest.json`.
- `tests/fixtures/dbt_project_austin/target/manifest.json` (NEW) — produced by the script above; committed.

**Done when:**
- Script runs cleanly on the maintainer's machine (one-shot — not exercised in CI).
- `target/manifest.json` is committed and is reproducibly stable (re-running the script produces an identical file modulo intentional non-determinism in dbt).
- The committed manifest validates against `signalforge.manifest.load(project_dir)` — verified by an in-process unit test added in this story under `tests/manifest/test_austin_fixture_loads.py` (gated by NO env vars; the manifest is committed JSON, no network needed).
- The validation command (`ruff check . && ruff format --check . && pyright && pytest`) passes.

**Depends on:** US-001.

**TDD:** Yes — write `tests/manifest/test_austin_fixture_loads.py::test_austin_manifest_loads_via_signalforge` BEFORE running the regen script; assert `manifest = load(fixture_dir); assert manifest.get_model("model.signalforge_test_austin.stg_bikeshare_trips").name == "stg_bikeshare_trips"` (use unique_id form — bare names route to the file-path branch and fail). Then run regen; the test passes once the manifest is committed.

---

### US-003 — Fixture `signalforge.yml` (config block with tight thresholds, mode, budget)

**Description.** Land `tests/fixtures/dbt_project_austin/signalforge.yml` with the locked config that the e2e test depends on: tight grade thresholds (force a flag), aggregate-only safety mode, materialised sample strategy, bumped grade budget, pinned LLM model.

**Traces to:** DEC-005, DEC-006, DEC-007, DEC-011, DEC-018.

**Files:**
- `tests/fixtures/dbt_project_austin/signalforge.yml` (NEW). Content (full file shown for clarity):
  ```yaml
  # Issue #10 e2e fixture config. Locked values are load-bearing for the smoke test
  # — see plans/super/10-e2e-bigquery-smoke.md DEC-005..DEC-018.
  # Pinned to match DraftConfig.model default; bump in lockstep when Sonnet 4.6 sunsets.
  llm:
    model: claude-sonnet-4-6
  safety:
    mode: aggregate-only
  prune:
    sample_strategy: materialised  # v0.2 default; exercises BQ session-state.
  grade:
    min_pass_rate: 0.95
    min_mean_score: 0.95
    fail_on_below_threshold: false
    total_budget_seconds: 600  # default 300 is tight at p99 latency × ~12 calls.
  ```

**Done when:**
- File committed.
- `signalforge lint --project-dir tests/fixtures/dbt_project_austin/` passes (verified by an in-process test added under `tests/cli/test_austin_fixture_config.py::test_austin_signalforge_yml_lints_clean`; calls `cli.main(["lint", "--project-dir", ...])` and asserts exit 0). No env vars required for `lint`.
- Validation command passes.

**Depends on:** US-001, US-002.

**TDD:** Yes — write the lint-clean test BEFORE the YAML; assert the YAML loads cleanly via the five `load_*_config(project_dir)` calls. Then add the YAML; the test passes.

---

### US-004 — `@pytest.mark.e2e` marker + `tests/cli/_e2e_helpers.py`

**Description.** Register the new `e2e` pytest marker; extend `addopts` to exclude it; ship a small private helper module that deserialises `<project_dir>/.signalforge/prune.jsonl` and `diff.json` into typed objects so the e2e test can assert on them.

**Traces to:** DEC-001, DEC-008, DEC-023.

**Files:**
- `pyproject.toml` (EDIT) — under `[tool.pytest.ini_options].markers`, add: `"e2e: end-to-end smoke test against real Anthropic + real BigQuery (gated by SF_RUN_BQ=1, ANTHROPIC_API_KEY, GOOGLE_CLOUD_PROJECT; skipped by default)",`. Under `addopts`, change the exclusion list to `-m 'not bigquery and not anthropic and not cli_subprocess and not e2e'`.
- `tests/cli/_e2e_helpers.py` (NEW). Three functions: `copy_fixture_to_tmp(fixture_dir: Path, tmp_path: Path) -> Path` (calls `shutil.copytree`, returns the new project dir); `read_prune_decisions(project_dir: Path) -> tuple[PruneDecision, ...]` (deserialises `prune.jsonl` via the public `signalforge.prune.PruneEvent` / `PruneDecision` models — DEC-023); `read_diff_report(project_dir: Path) -> DiffReport` (deserialises `diff.json` via `signalforge.diff.DiffReport`).

**Done when:**
- New marker registered; strict-marker enforcement still passes; `pytest -m 'not e2e'` (current default after the addopts change) returns the same test count as before, **minus zero** (no e2e tests ship in this story; US-005 is the first).
- Helpers are typed (pyright-clean), have docstrings, and are exercised by a small unit test under `tests/cli/test_e2e_helpers.py` that uses a hand-built committed fixture under `tests/fixtures/e2e_helpers/` containing a synthetic `prune.jsonl` + `diff.json` (no live env vars required).
- Validation command passes.

**Depends on:** US-002 (helpers import the prune/diff Pydantic models, which are already public).

**TDD:** Yes — write `tests/cli/test_e2e_helpers.py` BEFORE the helper module; tests assert `read_prune_decisions(fixture_dir).decisions[0].decision == "always-passes"` (or similar) and `read_diff_report(fixture_dir).kept_count == 1`. Then implement helpers. The synthetic `prune.jsonl` + `diff.json` fixtures hand-write the JSONL/JSON shapes per the public Pydantic models.

---

### US-005 — The e2e smoke test (`tests/cli/test_e2e_bigquery_smoke.py`)

**Description.** The actual end-to-end test. One test function, gated by `@pytest.mark.e2e + skipif(...)` for the three env vars. Copies the fixture to `tmp_path`, invokes `cli.main(["generate", "models/staging/stg_bikeshare_trips.sql", "--project-dir", str(project_dir)])`, asserts the seven invariants from DEC-009.

**Traces to:** DEC-002, DEC-008, DEC-009, DEC-010, DEC-020.

**Files:**
- `tests/cli/test_e2e_bigquery_smoke.py` (NEW). Contents:
  - Module docstring naming the gate, the three env vars, and the maintainer-run command.
  - Imports `pytest`, `os`, `pathlib.Path`, `signalforge.cli.main`, helpers from `_e2e_helpers`.
  - Three module-scoped constants for the env vars; one helper `_skip_reason()` returning a string when any is missing.
  - One test: `test_e2e_signalforge_generate_against_austin_bikeshare(tmp_path, capsys)`:
    1. `if reason := _skip_reason(): pytest.skip(reason)` (belt-and-suspenders alongside the `@pytest.mark.e2e` decorator).
    2. `project_dir = copy_fixture_to_tmp(FIXTURE_DIR, tmp_path)`.
    3. `exit_code = cli.main(["generate", "models/staging/stg_bikeshare_trips.sql", "--project-dir", str(project_dir)])`.
    4. Assert `exit_code == 0`.
    5. Assert `(project_dir / ".signalforge" / "diff.json").is_file()`.
    6. `report = read_diff_report(project_dir)`; assert `report.kept_count >= 1`; assert `report.flagged_count >= 1`.
    7. `decisions = read_prune_decisions(project_dir)`; assert `any(d.decision == "dropped" and d.drop_reason == "always-passes" for d in decisions)`.
    8. Read `<project_dir>/.signalforge/grade.json` (the GradingReport sidecar) and assert `aggregate_complete is True`.
    9. `captured = capsys.readouterr()`; assert `"Traceback" not in captured.err`.

**Done when:**
- Test file present; pyright-clean; ruff-clean.
- `pytest -m 'not e2e'` excludes it (default CI continues to skip).
- Maintainer runs `SF_RUN_BQ=1 GOOGLE_CLOUD_PROJECT=<...> ANTHROPIC_API_KEY=<...> pytest -m e2e --no-cov` and the test PASSES on first try (no flake on the Pydantic helper deserialisation).
- Validation command passes (the test is excluded by default).

**Depends on:** US-001, US-002, US-003, US-004.

**TDD:** Partial — the test IS the test; the assertion-helper logic was TDD'd in US-004. The test function itself is written directly. Maintainer runs the gated command once and observes a clean pass before declaring done.

---

### US-006 — README "Trying it out" + `docs/cli-ops.md` cross-ref + CLAUDE.md update

**Description.** User-facing docs land last so they describe shipped behaviour, not aspirational. Three surfaces in lockstep (per `cli-layer.md` multi-surface parity):

**Traces to:** DEC-014, DEC-015, DEC-016, DEC-017.

**Files:**
- `README.md` (EDIT) — insert a new H2 `## Trying it out` after the existing `## Quick start` (line ~63). Content:
  - One-paragraph framing ("If you have a BigQuery billing project and an Anthropic API key, you can run the canonical example end-to-end against `bigquery-public-data.austin_bikeshare` …").
  - A four-step shell block: `gcloud auth application-default login`; `export GOOGLE_CLOUD_PROJECT=<billing-project>`; `export ANTHROPIC_API_KEY=sk-...`; `cd tests/fixtures/dbt_project_austin/ && signalforge generate models/staging/stg_bikeshare_trips.sql`.
  - A one-line cost note (~$0.13 + <100 MB BigQuery scanned per run).
  - A reminder about scrubbing bash history (or use a fresh shell session).
  - A pointer to `docs/cli-ops.md` for full-flag reference.
- `docs/cli-ops.md` (EDIT) — add a one-paragraph cross-ref under § "Worked example" (or the existing closest section) pointing at the README "Trying it out" section. Mirrors the multi-surface parity rule.
- `CLAUDE.md` (EDIT) — add a `#10` bullet under `## Repository status` mirroring the `#9` bullet shape: single-paragraph header, `tests/fixtures/dbt_project_austin/` artefact name, the gate (`pytest -m e2e --no-cov`), the env-var triple, cross-refs to `plans/super/10-e2e-bigquery-smoke.md` and the README section. Per DEC-017, NO new `docs/e2e-smoke-ops.md`.

**Done when:**
- All three files updated.
- README's `## Trying it out` shell block is copy-pasteable verbatim and runs cleanly on a fresh maintainer machine (verified by manual run).
- CLAUDE.md `#10` bullet matches the `#9` shape (no inconsistencies).
- `docs/cli-ops.md` cross-ref does not duplicate content; it links and points.
- Validation command passes.

**Depends on:** US-005 (docs describe shipped behaviour).

**TDD:** N/A — docs.

---

### US-007 — Quality Gate

**Description.** Run the project code-review skill four times across the full changeset (per `super-plan` skill convention), fixing all real bugs found each pass. If CodeRabbit is configured, run it too. Validation must pass after all fixes.

**Traces to:** the standard quality-gate convention from prior tickets (see `plans/super/9-cli-entrypoint.md` Quality Gate story).

**Files:** any of the fixture / test / docs files updated by the prior stories may receive fixes; no new files are typical.

**Done when:**
- Four code-review passes complete; every actionable finding either fixed or explicitly accepted with a one-line note in the plan's refinement log.
- CodeRabbit (if available) review run; findings addressed.
- `ruff check . && ruff format --check . && pyright && pytest` (default markers, excludes e2e) all pass.
- Maintainer's gated `pytest -m e2e --no-cov` still passes on a clean machine.

**Depends on:** US-001, US-002, US-003, US-004, US-005, US-006.

**TDD:** N/A.

---

### US-008 — Patterns & Memory

**Description.** Capture any new patterns / lessons / rules surfaced by this ticket in the right durable surface (`.claude/rules/*.md`, `docs/*.md`, or `MEMORY.md`).

**Traces to:** the standard Patterns & Memory convention.

**Likely surfaces (preview):**
- `.claude/rules/testing-signal.md` — possibly a new section "End-to-end gated tests" capturing the three-env-var skip pattern, the `tmp_path`-fixture-isolation pattern for tests that produce on-disk artefacts under `<project_dir>/.signalforge/`, and the engineered-determinism pattern (literal/COALESCE column to make `not_null` deterministic).
- `tests/fixtures/README.md` — possibly extend with a "BigQuery fixtures" subsection contrasting with the existing DuckDB fixture layout.
- Memory file — likely none, but if any cross-session insight surfaced, capture it under `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/`.

**Done when:**
- All new patterns captured in the right durable file(s).
- No memories saved that duplicate existing rules.
- Validation command passes.

**Depends on:** US-007.

**TDD:** N/A.

---

---

## Beads manifest

Devolved 2026-05-09 against the canonical `.beads/` store (worktree-aware).

- **Epic:** `bd_1-scaffolding-91c` — 10: e2e smoke test against bigquery-public-data
- **US-001** `bd_1-scaffolding-7wu` — Fixture skeleton (dbt project + sources + staging SQL with engineered always-pass column). Depends on: none. **Currently READY.**
- **US-002** `bd_1-scaffolding-8be` — Manifest regen script + committed target/manifest.json. Depends on: US-001.
- **US-003** `bd_1-scaffolding-ocg` — Fixture signalforge.yml. Depends on: US-001, US-002.
- **US-004** `bd_1-scaffolding-r92` — @pytest.mark.e2e marker + tests/cli/_e2e_helpers.py. Depends on: US-002.
- **US-005** `bd_1-scaffolding-ivh` — The e2e smoke test. Depends on: US-001, US-002, US-003, US-004.
- **US-006** `bd_1-scaffolding-6ha` — README "Trying it out" + docs/cli-ops.md cross-ref + CLAUDE.md #10 bullet. Depends on: US-005.
- **US-007** `bd_1-scaffolding-ch4` — Quality Gate (code review × 4 + CodeRabbit). Depends on: US-001..US-006 (all).
- **US-008** `bd_1-scaffolding-8jp` — Patterns & Memory. Depends on: US-007. Priority P3.

**Worktree:** `/home/wesd/Projects/worktrees/SignalForge/10-e2e-bq-smoke`
**Branch:** `feature/10-e2e-bq-smoke` (off `dev`)
**PR:** [#32](https://github.com/wjduenow/SignalForge/pull/32) (draft)
