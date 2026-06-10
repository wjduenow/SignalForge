# #171 — `row_count_anomaly_by_period` as the 8th first-class test primitive

## Meta

| Field | Value |
|---|---|
| Ticket | https://github.com/wjduenow/SignalForge/issues/171 |
| Parent epic | #179 — Test Generation Expansion |
| Sequencing dependencies | #169 — `row_count_between` (shipped); #170 — `unique_combination` (shipped 2026-06-01, commit `42eae99`) |
| Sibling | #154 — prune+grade adapter for existing dbt-expectations tests (no overlap — no dbt-ext macro exists for this shape) |
| Branch / worktree | `feature/171-row-count-anomaly` at `/home/wesd/Projects/worktrees/SignalForge/171-row-count-anomaly` |
| Closest precedents | #170 (`unique_combination` — 3rd variant addition, 17 DECs, the freshest end-to-end template); #169 (`row_count_between` — the metadata-aggregate Direction-2 bypass precedent + `_PROMPT_VERSION` rotation contract). #171 is the **4th instance** of the variant-extension pattern. |
| Phase | **devolved** (epic + 19 beads created; PR #181 published as draft) |
| Sessions | 1 (2026-06-01) |

## Phase 1 — Discovery

### Ticket summary (what / why / who)

**What.** Add `row_count_anomaly_by_period` as the 8th `CandidateTest` variant. Unlike #169's static `row_count_between` (`min <= COUNT(*) <= max`), this primitive predicts a per-period row-count band from the model's own history and flags the most-recent period when its count falls outside the band. Four statistical methods (`mad` default / `zscore` / `percentile` / `min_max`) × two seasonality knobs (`none` / `dow`) on a configurable historical lookback.

**Why.** Empirical motivation, in three pieces:
1. The `intuit_airflow` survey surfaced two **unused in-house macros** in this exact shape (`threshold_check_daily_loaded_rows_std_deviation`, `threshold_check_job_loaded_rows_std_deviation`) — the team wrote them, then never deployed them because per-model calibration on 100+ models is friction. SignalForge can both *propose* and *prune* them in one pass.
2. Static `row_count_between` (#169) doesn't catch the failure mode that wakes oncall up — "today's volume is anomalously small vs. recent history" is invisible to a `min=1000, max=1_000_000` bound forever, including the day upstream silently drops to 800 rows.
3. Incremental dbt models partitioned by `loaded_at` / `created_at` / `event_date` fail **partially** far more often than catastrophically; partial-fail mode is invisible to every other primitive SignalForge ships today and to every static `dbt-expectations` test.

After #169 + #170, SignalForge covers ~86 % of `intuit_airflow`'s declared test shapes. This primitive opens the **temporal column** of the remaining gap; `column_values_between` opens the value-range column, and after both the residual is genuinely "needs statistical primitives" territory (separate epic).

**Who.** SignalForge users running `signalforge generate` against incremental fact / event tables on a business calendar. Direct evidence from `intuit_airflow`; general fit is "any team with daily/weekly partitioned facts where missing-load is the canonical pager-page."

### What makes #171 distinct from prior variants

This is the first SignalForge primitive whose decision is **inherently time-bound**: same SQL + same warehouse data + different evaluation day = potentially different pass/fail. That's a deliberate carve-out from Architectural Commitment #5's "same input → same prune decision" contract, executed by threading an explicit `--as-of YYYY-MM-DD` flag through CLI → `prune_tests` → compiled SQL → `PruneEvent` audit. Same input + same `--as-of` = same decision — reproducibility is restored at the `(model, as_of)` granularity instead of `model` alone.

Six distinguishing characteristics vs. prior variants:

1. **Time-bound via `--as-of`** (NEW). No prior variant adds a CLI flag; this needs the 5-surface parity rule (`cli-layer.md`).
2. **Sample-mode is semantically incompatible** — hash-mod sample over a date-partitioned table doesn't preserve per-period counts. Must **always** route to source; this is `business-rule-tests.md` Direction 2 (the metadata/aggregate-bypass route established by #169) with stricter semantics — even an explicit `scope="sample"` must bypass, with a log line.
3. **4 methods × 2 seasonality knobs = 8 SQL shapes** — `_compile_row_count_anomaly_by_period` is materially more complex than any prior variant's compile arm. Stats CTEs vary by method (`PERCENTILE_CONT` for MAD/percentile; `AVG`/`STDDEV` for zscore; `MIN`/`MAX` for min_max).
4. **Date-arithmetic dialect surface** — `DATE_TRUNC`, `INTERVAL`, `EXTRACT(DOW FROM …)` differ between BigQuery and Snowflake. The `Dialect` value object (`warehouse/models.py`) probably needs new fragment fields.
5. **Cold-start routing** — when history sample is too thin, route through `kept-without-evidence` with structured `why="insufficient history: n/min periods"`. **Must not grow `DropReason` literal** (closed at 5 values per `prune-engine.md`).
6. **Cross-stage numerical state (open question)** — the grader needs the prune step's `(median, MAD, n)` or `(μ, σ, n)` per-DOW to score calibration. Today no per-test numerical state flows prune → grade. Two options: (a) define `AnomalyTestStats` and add a path through `PruneDecision` → grader; (b) skip grader access and lean on prune-step evidence + conservative defaults. Lean (a); this is open for Phase 2.

### Discovery findings (parallel subagent research)

#### A. Surface map — every place the 8th variant must touch

Located by the Codebase Scout subagent. Six production dispatch sites + supporting surfaces.

**6 production dispatch sites** (`business-rule-tests.md` § "The 6 production dispatch sites"; all need a new arm except site 4 — deliberate v0.x skip):

| # | Module | Function | File:line | Closest precedent arm |
|---|---|---|---|---|
| 1 | `prune.compiler` | `_compile_test` | `src/signalforge/prune/compiler.py:986–1125` | `row_count_between` (1098–1106) — helper at `_compile_row_count_between`, 810–903 |
| 2 | `_common.artifact_id` | `model_test_args_hash` | `src/signalforge/_common/artifact_id.py:64–142` | `row_count_between` (105–118), `unique_combination` (119–134) |
| 3 | `diff._emitter` | `_render_test` | `src/signalforge/diff/_emitter.py:133–195` | **`custom_sql` (177–178) — routes to `_SKIP` → singular-test SQL** (no dbt-expectations macro exists; see § C) |
| 4 | `ingest.parser` | `_parse_named_test` | `src/signalforge/ingest/parser.py:204–255` | **Deliberate v0.x skip** — no dbt-ext macro to recognise; ingest follows naturally if Elementary's `volume_anomalies` is recognised later |
| 5 | `draft.parser` | `_validate_anchor_contract` | `src/signalforge/draft/parser.py:508–571` | `row_count_between` (508–534) — model-level only, skip column-existence check; thread `model_columns_by_type` for `where`-fragment type-coherence |
| 6 | `ingest.anchor` | `validate_anchor_contract` | `src/signalforge/ingest/anchor.py:82–115` | `row_count_between` `continue` early-out (88–89) |

**Supporting catalogue / fixture / test surfaces** (rotate in lockstep):

| Surface | Location | Change |
|---|---|---|
| `CandidateTest` union | `draft/models.py:206–438` | Add `CandidateTestRowCountAnomalyByPeriod` (frozen, `column: None = None`, `__repr__` + `__repr_args__` redaction per #170 DEC-013) |
| `__all__` export | `draft/models.py:500–502` | Add new class name |
| `VALID_TEST_TYPES` | `draft/config.py:58–68` | Add `"row_count_anomaly_by_period"` |
| `_TEST_CATALOGUE_LINES` | `draft/prompts.py:59–89` | Add entry → **rotates `_PROMPT_VERSION`** |
| Drafter `_PROMPT_VERSION` | `draft/prompts.py:359` | Recompute blake2b-8 |
| Drafter cache-stability snapshot | `tests/llm/test_prompt_cache_stability.py:86` | Update `_EXPECTED_PROMPT_VERSION` + rendered golden |
| Candidate fixture | `tests/fixtures/draft/candidate_schema_v1.json:40–45` | Add new variant row |
| Drift mirror | `tests/draft/test_drift_detector.py:31–110` | Add `StrictCandidateTestRowCountAnomalyByPeriod` |
| SKILL.md | `src/signalforge/skills/signalforge/SKILL.md` | Document new variant + `--as-of` flag (6th parity surface per `skill-parity.md`) |
| Grade `_PROMPT_VERSION` | `grade/prompts.py:227` | **Rotates only if** rubric gains a `calibration` criterion or any existing criterion text changes (see § B.3) |
| Grade cache-stability snapshot | `tests/grade/test_prompt_cache_stability.py:53` | Update only if grade `_PROMPT_VERSION` rotates |

**Time-bound (`--as-of`) surfaces** (NEW — no prior variant did this):

| Surface | Location | Change |
|---|---|---|
| CLI argparse | `cli/generate.py` (`add_parser`); `cli/prune_existing.py` (`add_parser`) | `--as-of YYYY-MM-DD` flag |
| Engine entry | `prune/engine.py:657–666` `prune_tests` | `as_of: date \| None = None` keyword-only |
| Audit event | `prune/audit.py:84–135` `PruneEvent` | Add `as_of: date \| None = None`; bump `_PRUNE_AUDIT_SCHEMA_VERSION` 2 → 3 |
| Audit fixture | `tests/fixtures/prune/prune_event_v1.jsonl` | Update to schema v3 (add `as_of` to each record) |
| `PruneDecision` | `prune/models.py:80–110` | Add `as_of: date \| None = None` |
| Drift detector | `tests/prune/test_drift_detector.py` | Update strict mirror + fixture |

**Engine bypass infrastructure** (extends #169/#170 two-conditional pattern per `prune-engine-two-conditional-routing-pattern` memory):

| Site | Location | Change |
|---|---|---|
| `all_bypass_to_source` short-circuit | `prune/engine.py:987–999` | Add `CandidateTestRowCountAnomalyByPeriod` to isinstance |
| Per-test `per_test_table_ref` override | `prune/engine.py:1111–1130` | Add to isinstance — load-bearing per #170 QG Pass 3 |
| Stricter: ALSO bypass under `scope="sample"` | TBD — likely above the per-test override | NEW shape — even non-materialised sample must route to source for this variant |

**Date-arithmetic dialect fields** (NEW on `Dialect`):

| Surface | Location | Change |
|---|---|---|
| `Dialect` | `warehouse/models.py:115–121` | Add `date_trunc_expr_template`, `interval_expr_template`, `extract_dow_expr_template` (BigQuery defaults, Snowflake overrides). Driven by `prune.compiler` reading these — never branch on `dialect.name` (compiler import-guard) |

**AST scans** (`tests/test_audit_completeness.py`) — 10 existing scans; **none enumerate the `CandidateTest` union**, so adding the 8th variant trips no scan. Scan 7 (every typed `*Error` in exit-code mapping) will fire if any new error class lands; scan 8 (fail-closed writer shape) fires if a new audit-write seam lands. Neither is expected for #171 in v0.x.

#### B. Open design questions (Phase 2 / 3 will adjudicate)

##### B.1 — `--as-of` default and propagation shape

Three reasonable shapes:
- **(a)** `--as-of` defaults to `None`; engine resolves to `date.today()` at prune-time. Operator-friendly; non-reproducible by default but operator-recoverable via the audit (`PruneEvent.as_of`).
- **(b)** `--as-of` is required when ANY anomaly test exists in the candidates; raise loud at orchestrator entry otherwise. Principled; punishes the first-run.
- **(c)** `--as-of` defaults to `None`; engine resolves at prune-time AND emits one INFO log naming the resolved value. Hybrid.

Lean (c). Phase 2 will pin.

##### B.2 — Sample-mode bypass — stricter than #169/#170

`row_count_between` and `unique_combination` bypass to source only under `sample_strategy="materialised"` (the temp-table substitute returns sample-size, not real row count). `row_count_anomaly_by_period` is stricter: it must bypass even under `sample_strategy="oneshot"` because **any** sample over a date-partitioned table breaks per-period counts.

Decision rule: a third condition on the engine override, gating on test type rather than just `sample_strategy`. Or: collapse all metadata-aggregate variants to bypass-on-any-sample (cleaner; revisits #169/#170 behaviour — but both already produce semantically correct results under `oneshot` because the COUNT(*) / GROUP-BY HAVING returns full results regardless of sample size; deferred to Phase 2).

Phase 2 will adjudicate. Cross-reference `business-rule-tests.md` § "Materialised-sample substitution" — the Direction 2 rule already covers this verbatim, just stricter.

##### B.3 — Grade rubric: new `calibration` criterion or reuse existing 4?

Two shapes:
- **(a)** Add a 5th criterion `calibration` that scores `(method, seasonality, threshold)` against the prune step's emitted stats. Needs cross-stage numerical state (see B.4). Bumps drafter + grade `_PROMPT_VERSION` snapshots.
- **(b)** Reuse the existing `no-redundant` criterion with parenthetical anomaly-specific calibration prose (mirrors #169 DEC-009 verbatim). Rotates grade `_PROMPT_VERSION` only if criterion text changes.

Lean (b) for v0.x ship + conservative defaults. Path (a) is the v0.2 graduation. Phase 2 will pin.

##### B.4 — Cross-stage numerical state (`AnomalyTestStats`)

Open even if B.3 lands at (b): the prune-emitted `(median, MAD, n)` (or per-DOW variants) is operationally useful in the audit + diff sidecar regardless of whether the grader consumes it. Three shapes:
- **(i)** Add `stats: dict[str, Any] | None = None` to `PruneDecision`. Loose typing; flexible; downstream consumers parse the dict.
- **(ii)** Define a typed `AnomalyTestStats` value-object and add `stats: AnomalyTestStats | None = None` to `PruneDecision`. Strict typing; needs drift detector + fixture surface.
- **(iii)** Skip — emit nothing structured; the operator reads the audit `compiled_sql` and reconstructs.

Lean (ii) once one primitive needs cross-stage state, future primitives will too. Open in Phase 2.

##### B.5 — Method support in v0.x

Issue body locks `method: Literal["mad", "zscore", "percentile", "min_max"] = "mad"`. All four are SQL-native; MAD is the standard robust replacement for raw z-score per the prior-art appendix (Iglewicz & Hoaglin; Elementary; re_data; InfluxData literature). Phase 2 will confirm we ship all four together vs. ship MAD-only + follow-on for the others.

##### B.6 — Cold-start: degrade-to-non-seasonal-with-WARNING vs. fail-cold-start

Per the issue body's open question: when `seasonality="dow"` + thin per-DOW samples, the proposed behaviour is "degrade to non-seasonal with WARNING" rather than route to `kept-without-evidence`. This grows the engine override slightly (degrade path emits altered SQL + altered audit) but preserves signal. Alternative: route to `kept-without-evidence` with structured `why`.

Lean (degrade-with-WARNING). Phase 2 will pin.

##### B.7 — Diff emission: singular SQL only (no dbt-ext macro path)

Issue body locks: "emits a `tests/*.sql` singular test (this primitive doesn't map cleanly onto a dbt_expectations YAML macro)." Diff emitter routes to `_SKIP` → `proposed_test_files`, same shape as `custom_sql`. Fail-closed writer (`diff/_test_file_writer.py`) already variant-agnostic.

##### B.8 — Bytes-billed gating (operator opt-in?)

Issue body raises: "historical-window scans are materially more expensive than other primitives." Three shapes:
- **(a)** `PruneConfig.enable_anomaly_tests: bool = True` — default on; operator opts out.
- **(b)** Default off; operator opts in.
- **(c)** No gate — document cost honestly + lean on existing `maximum_bytes_billed`.

Lean (c) for v0.x — adding a gate now precludes the "drafter proposes it" → "prune evaluates it" tight loop. Cost framing belongs in `prune-ops.md`. Phase 2 may revisit.

##### B.9 — Docs surface

Issue body proposes either extending `docs/draft-ops.md` + `docs/prune-ops.md` OR creating a new `docs/anomaly-test-ops.md`. #170 created `docs/drafter-catalogue.md` as the README-fed catalogue; #171 extends that catalogue row + paraphrases into the operator-facing ops docs. Lean: NO new ops doc; extend `draft-ops.md` + `prune-ops.md` + `cli-ops.md` (for `--as-of`).

#### C. Conventions (Convention Checker findings — top-5 critical gates)

1. **[GATE] All 6 dispatch sites must grow an arm in lockstep** (`business-rule-tests.md`). A missing arm is a latent runtime crash, not a type error.
2. **[GATE] Two-conditional engine routing** — both `all_bypass_to_source` short-circuit AND per-test `per_test_table_ref` override (`prune-engine-two-conditional-routing-pattern` memory). The mixed-candidate test is load-bearing per #170 QG Pass 3.
3. **[GATE] `--as-of` engineered determinism in e2e** (`testing-signal.md`) — pin `--as-of` to a fixed date with a known anomalous bucket. Without it, e2e can pass silently while the flag is untested.
4. **[GATE] Sample-mode bypass routes through the engine, not the compiler** (`business-rule-tests.md` Direction 2). Test must assert compiled SQL **never** references `_SESSION._sf_sample_*`.
5. **[GATE] Model-level anchor exemption at BOTH sites** (`draft.parser._validate_anchor_contract` + `ingest.anchor.validate_anchor_contract`). `column: None = None` must short-circuit column-existence checks at both — missing one half crashes that path.

Full constraint extraction in the Convention Checker artefact (15 rule files audited; 5-surface parity for the new flag + skill-parity for the 8th catalogue entry + drift detectors for new fixture rows are the additional gates).

#### D. Outstanding from precedents to carry forward (#170 lessons)

- **Two engine conditionals (not one).** Cover both `all_bypass_to_source` short-circuit AND per-test `per_test_table_ref`. Mixed-candidate test (1×anomaly + 1×non-anomaly) is load-bearing.
- **`__repr_args__` redaction on Pydantic v2.** `__repr__` alone leaks `rich.print()` / `devtools.pretty()` / `pprint`. New variant carries LLM-emitted `rationale` + `where`; both need redaction.
- **`model_test_args_hash` canonical-form rules.** No tuple/list args in this variant's identifying surface (4 scalars + 2 enums), so sorting is N/A — but document the decision.
- **mkdocs ATX-in-fenced-block trap.** Any new ops doc with `## METHODS` example must use indented code blocks, not fenced.
- **Drive-by formatting reveals merge drift.** Re-run `ruff format --check .` after every worker merge in Phase 4.

### Convention Checker — full audit summary

15 rule files audited; no `workflow-project.md` found. Active gates above + soft conventions for grading / docs / observability layered into the relevant stories. The 4-tier exit-code taxonomy applies trivially (no new exception types expected for v0.x).

### Proposed scope (for Phase 2 scoping questions)

A minimal v0.x ship surface:
- Single new `CandidateTestRowCountAnomalyByPeriod` variant, 4 methods × 2 seasonality knobs.
- `--as-of YYYY-MM-DD` flag on `generate` (and `prune-existing`?), threaded to compiled SQL + audit.
- Engine bypass to source under ANY sample mode for this variant (Direction-2-stricter).
- Cold-start → `kept-without-evidence` (no new `DropReason`).
- Diff emission as singular `tests/*.sql` (no dbt-ext macro path).
- Drafter prompt extension + `_PROMPT_VERSION` rotation.
- Grade rubric reuse (no new `calibration` criterion in v0.x).
- Cross-stage stats: TBD — Phase 2.
- Docs: extend `draft-ops.md` + `prune-ops.md` + `cli-ops.md`; README catalogue row.
- E2E gated test (Anthropic + BigQuery) with engineered `--as-of` reproducibility check.

### Locked scoping decisions (Phase 1 → Phase 2 handoff)

| ID | Decision | Source |
|---|---|---|
| **DEC-001** | `--as-of` defaults to `date.today()` at prune-time; engine emits **one INFO line** naming the resolved value (lazy-format JSON). Operator-recoverable via the `PruneEvent.as_of` audit field for runs after the fact. | Q1 |
| **DEC-002** | Stricter sample-mode bypass: `row_count_anomaly_by_period` **always** routes to source, even under `sample_strategy="oneshot"`. Engine emits one INFO line per test naming the override. Adds a third condition (variant-aware) on top of #169/#170's two-conditional pattern; the #170 mixed-candidate test shape is extended. | Q2 |
| **DEC-003** | Cross-stage numerical state lands as a **typed `AnomalyTestStats` value-object** on `PruneDecision` (method-tagged: holds `(median, MAD, n)` OR `(μ, σ, n)` OR `(p_lo, p_hi, n)` OR `(min, max, n)`; per-DOW variant when `seasonality="dow"`). Grader reads it via the cross-stage handoff. Paired with `Strict*` drift detector + a fixture row. Future primitives needing cross-stage state inherit the seam. | Q3 |
| **DEC-004** | Grade rubric **reuses** the existing 4 criteria; extend `no-redundant` criterion text with anomaly-specific calibration prose (mirrors #169 DEC-009 verbatim). Grade `_PROMPT_VERSION` rotates in lockstep. **No** new 5th criterion in v0.x. v0.2 graduation tracked separately if empirical retest (#179) shows under-grading. | Q4 |

## Phase 2 — Architecture Review

Three parallel subagents reviewed: (A) data-model + cross-stage state, (B) compiler + dialect + cost, (C) CLI + testing + audit-replay + conventions. Findings consolidated below; subagent C's "implementation missing → blocker" ratings on six surfaces were re-rated (the plan is in design; absence-of-code is the expected state at this phase).

### Consolidated findings

| # | Area | Rating | Source | Key finding (Phase 3 action) |
|---|---|---|---|---|
| 1 | `AnomalyTestStats` typed shape | **concern** | A.1 | Lock the discriminated-union shape (Q5 in refinement: tagged-union vs. flat). |
| 2 | `PruneEvent` schema bump 2 → 3 | **concern** | A.2 | `as_of: date` needs an explicit `@field_serializer` returning `.isoformat()` — no existing precedent for `date` (issue #56 covers `datetime` only). Fixture update in-place; v2 records must replay clean (Pydantic `int` not `Literal` on `audit_schema_version` already supports this). |
| 3 | `as_of` redundancy across `PruneEvent` vs `PruneDecision` | **concern** | A.3 | Put `as_of` on `PruneEvent` only OR on both with redaction. Q6 in refinement. |
| 4 | Variant Pydantic shape — `period` field | **concern** | A.4 + B.2 | Issue body uses `period: Literal["hour", "day", "week"]`; the appendix recommends `lookback_periods: int` (separate field) — confirm: `period` is the bucket size, `lookback_periods` is the count. Q7 in refinement. |
| 5 | `__repr_args__` redaction | **pass** | A.5 | Precedent from #170 applies verbatim. |
| 6 | `model_test_args_hash` content + canonical form | **pass** | A.6 | Scalar-only args; no sort needed; document the decision in code comment. |
| 7 | `Dialect` field additions (5 new fragments) | **pass** | B.1 | `date_trunc_expr_template`, `interval_expr_template`, `extract_dow_expr_template`, `dow_sunday_index`, `percentile_cont_expr_template`. BigQuery defaults + Snowflake overrides. POSTGRES_DIALECT keeps BQ defaults (its adapter raises `NotImplementedError` — corrected in #119 follow-up). |
| 8 | Compile helper signature + `where` validation | **pass** | B.2 | New `as_of: date` kwarg threads through `_compile_test` dispatcher; per-variant helpers ignore. `where` validation reuses `_check_custom_sql_type_coherence` per #159 precedent. |
| 9 | 8 SQL shapes — composition strategy | **pass** | B.3 | Option (b): per-method helpers (`_build_stats_cte_<method>`) + per-seasonality wrapper. ~250–350 LOC for the dispatcher + 4 method × 2 seasonality variants. |
| 10 | Cold-start routing | **concern** | B.4 | Two architectural options: (i) single-query with `n >= min_samples_per_bucket` guard + engine-side variant-aware interpretation of zero-failure result, (ii) two-query split (stats query first; violation check skipped on cold-start). Q8 in refinement. |
| 11 | Bytes-billed cost — partition filter is load-bearing | **concern** | B.5 | The compiled SQL MUST include `WHERE <date_column> >= <as_of> - INTERVAL <historical_days> DAY` to enable BQ/Snowflake partition pruning. Reduces scan 10-100× on partitioned tables; otherwise a 90-day lookback on a 1B-row table scans 100s of GB per test. Document cost framing in `prune-ops.md`. No new opt-in flag (per Phase 1 B.8 lean). |
| 12 | `as_of` threading from engine to compiler | **pass** | B.6 | Confirmed option (a): kwarg on `_compile_test`. |
| 13 | Engine bypass — two sites, asymmetric per variant | **concern** | B.7 | `row_count_anomaly` bypasses under ANY sample mode; `row_count_between`/`unique_combination` bypass only under materialised. Recommend `_test_requires_source_table(test, sample_strategy)` helper in `engine.py` to centralise the per-variant rule (avoids forking the isinstance check across two sites). Q9 in refinement. |
| 14 | CLI `--as-of` argparse pattern | **pass** | C.1 | `type=date.fromisoformat` (stdlib; Python 3.7+); bad-format → argparse SystemExit(2) maps to tier 2 cleanly. |
| 15 | 5-surface parity + skill (6th) | **pass** | C.2 | Pattern well-trodden from #169/#170. Skill parity gate checks subcommand presence, not flags — flag documentation is reviewer-attention. Implementation in Phase 4. |
| 16 | Reproducibility test shape | **pass** | C.3 | Unit: same-`as_of`-byte-equal-compiled-SQL test. E2E (gated): different-`as_of`-different-decision test. Engineered determinism: Austin bikeshare `bikeshare_trips` has a documented public anomaly (dates 2023-04-15/16 ~50% drop vs. rolling mean — verifiable via BQ). Avoids synthetic fixture. |
| 17 | Engineered anomaly fixture | **pass** | C.4 | Use Austin bikeshare (already in `tests/fixtures/dbt_project_austin`); add `inject_model_anomaly_rules` helper in `tests/cli/_e2e_helpers.py` mirroring `inject_model_business_rules`. Unit fakes return canned stats per `expect_query`. |
| 18 | Audit-replay v2 → v3 backward compat | **pass** | C.5 | `audit_schema_version: int` (already) + both new fields `... | None = None` → v2 records load without error. Update fixture in place + add a one-off "v2 dict replays as v3" regression test (mirrors safety/draft precedent). |
| 19 | INFO logging contract | **pass** | C.6 | Three lines: `--as-of` resolved (INFO; per-call), variant-bypassed-sample (INFO; per-test), cold-start (WARNING; per-test; operator-actionable). All lazy-format JSON; grep gate covers `prune/`. |
| 20 | `--quiet` / `--verbose` interactions | **pass** | C.7 | Standard `cli-layer.md` precedent. Cold-start WARNING surfaces under `--quiet` (operator-actionable). |
| 21 | SKILL.md update | **pass** | C.8 | Catalogue entry + `--as-of` flag reference. No gate enforcement — reviewer-attention surface. |
| 22 | README + drafter-catalogue.md | **pass** | C.9, C.10 | One new row each; mirrors #170's catalogue addition. |
| 23 | Stricter-bypass might also apply to #169/#170 | **concern** | B.7 (counter-rec) | Composite-uniqueness on a sample is approximate (#170 B.2 already flags). Worth a Refinement question: do we tighten #169/#170 in this PR too, or scope-discipline to #171 only? Q10. |

**Blockers: 0.** Six concerns flow into Phase 3 refinement questions.

### Open concerns → Refinement questions

- **Q5** — `AnomalyTestStats` shape: tagged union (4 method-specific subclasses) vs. flat optional fields?
- **Q6** — `as_of` placement: `PruneEvent` only, or both `PruneEvent` + `PruneDecision`?
- **Q7** — `period` field: `Literal["hour", "day", "week"]` + separate `lookback_periods: int`, confirming the issue-body appendix?
- **Q8** — Cold-start: single-query + engine-side variant-aware interpretation, OR two-query split with stats-first?
- **Q9** — Engine bypass code organisation: inline isinstance checks at the 2 sites, OR centralise via `_test_requires_source_table(test, sample_strategy)` helper?
- **Q10** — Scope discipline: tighten the stricter-bypass-under-oneshot to also cover `row_count_between` + `unique_combination`, or scope to #171 only?

## Phase 3 — Refinement

DECs locked from Phase 1 scoping (DEC-001 … DEC-004) + Phase 2 architecture review answers (DEC-005 … DEC-013).

| ID | Decision | Source / Rationale |
|---|---|---|
| **DEC-005** | `AnomalyTestStats` is a Pydantic discriminated union: `Annotated[MadStats \| ZscoreStats \| PercentileStats \| MinMaxStats, Field(discriminator="method")]`. Each subclass holds only the fields its method emits. Common fields on each: `method: Literal[...]`, `n_periods: int`, `per_dow: dict[int, <DowStats>] \| None = None`. `extra="ignore"` + paired `Strict*` drift mirror in `tests/prune/test_drift_detector.py`. Lives at `src/signalforge/prune/stats.py`. | Q5 |
| **DEC-006** | `as_of: date \| None = None` lives on BOTH `PruneEvent` (audit-of-record) AND `PruneDecision` (in-memory consumer access). `stats: AnomalyTestStats \| None = None` lives on BOTH likewise. `PruneDecision` custom `__repr__` adds both new fields to the omitted-from-repr set. | Q6 |
| **DEC-007** | Variant Pydantic shape carries: `period: Literal["hour","day","week"] = "day"` + `lookback_periods: int = 28` + `method: Literal["mad","zscore","percentile","min_max"] = "mad"` + `seasonality: Literal["none","dow"] = "none"` + `date_column: str` + `threshold: float = 3.0` (method-specific semantics; ignored for `min_max`) + `min_samples_per_bucket: int = 3` + `where: str \| None = None` + `rationale: str \| None = None` + `column: None = None`. `model_validator(mode="after")` enforces `lookback_periods >= 1`, `min_samples_per_bucket >= 1`, `threshold > 0` for non-`min_max` methods, `where` non-empty after strip. `__repr__` + `__repr_args__` redact `where` + `rationale` per #170 DEC-013. | Q7 |
| **DEC-008** | Cold-start handling is a **two-query split**: Query 1 returns the per-method stats (median/MAD/n for `mad`; μ/σ/n for `zscore`; p_lo/p_hi/n for `percentile`; min/max/n for `min_max`) and populates `AnomalyTestStats` on the `PruneDecision`. Engine inspects `stats.n_periods`; if `< min_samples_per_bucket`, emits `kept-without-evidence` with structured `why="insufficient history: <n>/<min> periods"` and SKIPS Query 2. Otherwise runs Query 2 (the actual band-violation check) and routes per the matrix. Under `seasonality="dow"` + thin per-DOW samples (any DOW bucket below floor) the engine **degrades to non-seasonal**: recomputes the stats query without DOW partitioning, emits one WARNING line per test, and proceeds. The compile helper exports two functions (`_compile_stats_query`, `_compile_violation_query`); both are called from the engine. | Q8 (+ B.6 default) |
| **DEC-009** | Engine bypass routing centralises via `_test_requires_source_table(test: CandidateTest, sample_strategy: str \| None) -> bool` helper in `signalforge.prune.engine`. Both the `all_bypass_to_source` short-circuit AND the per-test `per_test_table_ref` override call this single helper. New behaviour-contract: for **all three metadata-aggregate variants** (`row_count_between`, `unique_combination`, `row_count_anomaly_by_period`) the helper returns `True` under **any** sample mode — tighter than #169/#170 today (which currently bypass only under materialised). Helper has its own unit tests; both engine sites have integration tests; **mixed-candidate test** (1× metadata-aggregate + 1× row-level on the same model under `scope=sample, sample_strategy=oneshot`) is load-bearing per the #170 QG Pass 3 finding. | Q9 + Q10 |
| **DEC-010** | Scope discipline: #171 ships the stricter-bypass-under-`oneshot` change for **all three** metadata-aggregate variants in one PR (NOT scoped to the new variant only). Q10 chose this trade-off explicitly: tighter semantic floor at the cost of touching #169/#170 behaviour. Regression obligations: (a) the oneshot+sample snapshots for `row_count_between` / `unique_combination` may shift — pin both pre-change and post-change; (b) the #169 + #170 e2e gated tests must still pass against the new routing; (c) `prune-ops.md` documents the behaviour change in a CHANGELOG-style bullet plus a per-variant note. CHANGELOG `[Unreleased]` § Changed: "Prune engine: `row_count_between` and `unique_combination` now bypass to source under `sample_strategy=oneshot` as well as `materialised` (was: materialised only). Restores semantic correctness on oneshot sampling; no operator action required." | Q10 |
| **DEC-011** | `Dialect` graduates five new SQL-fragment fields: `date_trunc_expr_template: str` (e.g. BQ `"DATE_TRUNC({date}, {unit})"`, Snowflake `"DATE_TRUNC('{unit}', {date})"`), `interval_expr_template: str` (BQ `"INTERVAL {n} {unit}"`, Snowflake `"INTERVAL '{n} {unit}'"`), `extract_dow_expr_template: str` (BQ `"EXTRACT(DAYOFWEEK FROM {date})"`, Snowflake `"EXTRACT(DOW FROM {date})"`), `dow_sunday_index: int` (BQ `1`, Snowflake `0` — used to align `today.dow` against per-DOW history), `percentile_cont_expr_template: str` (`"PERCENTILE_CONT({p}) WITHIN GROUP (ORDER BY {expr})"` for both BQ and Snowflake; declared for parity + future Postgres). BIGQUERY_DIALECT defaults preserve byte-equality on the 7 existing variants. SNOWFLAKE_DIALECT overrides all five. POSTGRES_DIALECT inherits BQ defaults (its adapter raises `NotImplementedError`; corrected in the future Postgres-ops PR). Prune compiler reads these fields directly — **never** branches on `dialect.name`. Existing AST import-guard at `tests/prune/test_compiler_import_guard.py` continues to gate. | A + B.1 |
| **DEC-012** | Compiled SQL for `row_count_anomaly_by_period` **must** include a partition-pruning WHERE clause: `<date_column> >= <as_of> - INTERVAL <lookback_periods> <period>` AND `<date_column> < <as_of> + INTERVAL 1 <period>` (or equivalent dialect-specific form). Without it, a 28-day lookback on a 1B-row daily-partitioned table scans the entire table; with it, BigQuery/Snowflake partition pruning reduces scan ~30× (28-of-N partitions). Operator-facing cost framing lives in `docs/prune-ops.md` (new section under "Variants"); no new opt-in flag (per Phase 1 B.8). The existing `maximum_bytes_billed` cap remains the safety net (errors → `BytesBilledExceededError` → `kept-without-evidence` per the conservative-bias routing template). | B.5 |
| **DEC-013** | `PruneEvent` audit schema bumps `_PRUNE_AUDIT_SCHEMA_VERSION: 2 → 3`. Two new fields: `as_of: date \| None = None`, `stats: AnomalyTestStats \| None = None`. `audit_schema_version: int` (not `Literal`) preserves replay of v2 records (Pydantic `int` accepts `2` or `3`). `@field_serializer("as_of")` returns `value.isoformat()` (no `datetime.timestamp.iso8601_z` precedent applies — that helper covers `datetime` only per safety-layer.md issue #56). Stats serialise via Pydantic's native discriminated-union JSON. **`config_hash` does NOT include `as_of`** in its input set — config_hash answers "did config change," not "did time pass." The existing `tests/fixtures/prune/prune_event_v1.jsonl` updates in place to v3 schema (per the # 55 / #54 precedent — `_PRUNE_AUDIT_SCHEMA_VERSION` is already int-typed, so the fixture-replay test is a one-off inline-v2-dict-validates-clean assertion in `tests/prune/test_audit.py`). | A.2 + C.5 |

Bytes-billed gating: lean confirmed (no new opt-in flag per Phase 1 B.8); cost framing in `prune-ops.md` per DEC-012. Diff emission: confirmed singular `tests/*.sql` route (per Phase 1 B.7); `_render_test` arm returns `_SKIP` mirroring `custom_sql`. Method support in v0.x: confirmed all 4 ship together (per Phase 1 B.5).

## Phase 4 — Detailed Breakdown

Architecture ordering: typed shapes (foundations) → dialect (compile substrate) → drafter (catalogue + prompts + parser) → prune (compiler + engine) → CLI (flag plumbing) → diff (emission) → grade (rubric refresh) → docs + skill + e2e (5-surface parity sweep + live verification).

**The four locked DECs above (DEC-001 … DEC-013) drive the acceptance criteria on every story.** Every story names its DECs. The canonical validation command is `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — `AnomalyTestStats` typed shape + drift mirror

**Description:** Land the tagged-union `AnomalyTestStats` value-object at `src/signalforge/prune/stats.py` with 4 method-specific subclasses (`MadStats` / `ZscoreStats` / `PercentileStats` / `MinMaxStats`), each carrying `method: Literal[...]`, `n_periods: int`, method-specific fields, and an optional `per_dow: dict[int, <Method>DowStats] \| None = None`. Re-export from `signalforge.prune` `__all__`. Paired `Strict*` drift mirror in `tests/prune/test_drift_detector.py`. Fixture row in a new `tests/fixtures/prune/anomaly_stats_v1.json`.

**Traces to:** DEC-003, DEC-005.

**Acceptance criteria:**
- `AnomalyTestStats = Annotated[MadStats | ZscoreStats | PercentileStats | MinMaxStats, Field(discriminator="method")]` exported from `signalforge.prune`.
- Each subclass `frozen=True`, `extra="ignore"`, `populate_by_name=True`.
- DOW sub-stats classes (`MadDowStats`, etc.) define the per-DOW shape; keyed by `int` 0–6 (POSIX dow convention, dialect-normalised before insertion).
- Strict drift mirrors `(Strict<X>(extra="forbid"))` for each subclass.
- Fixture validates against both production and strict mirror; loaded round-trip in tests.
- Canonical validation command passes.

**Done when:** All four method-tagged subclasses exist, both production and strict mirrors validate the new fixture cleanly, `uv run pytest tests/prune/test_drift_detector.py tests/prune/test_stats.py` passes.

**Files:**
- NEW: `src/signalforge/prune/stats.py`
- `src/signalforge/prune/__init__.py` — add to `__all__`
- NEW: `tests/prune/test_stats.py` — round-trip + discriminator-dispatch tests
- `tests/prune/test_drift_detector.py` — add `Strict*` mirrors
- NEW: `tests/fixtures/prune/anomaly_stats_v1.json` — one row per method + one seasonal row

**Depends on:** none.

**TDD:**
- Test: tagged-union dispatches on `method` field correctly
- Test: each subclass round-trips through `model_dump_json` / `model_validate_json`
- Test: strict mirror rejects unknown fields (drift)
- Test: per-DOW dict serialises with int keys (Pydantic v2 dict-key coercion)
- Test: mismatched method/subclass raises `ValidationError`

### US-002 — `Dialect` 5 new SQL-fragment fields (date arithmetic + percentile)

**Description:** Extend `signalforge.warehouse.models.Dialect` with `date_trunc_expr_template`, `interval_expr_template`, `extract_dow_expr_template`, `dow_sunday_index`, `percentile_cont_expr_template`. Add BigQuery defaults that preserve byte-equality on the 7 existing variants. Add Snowflake overrides. POSTGRES_DIALECT inherits BQ defaults (documented; corrected in Postgres-ops PR).

**Traces to:** DEC-011.

**Acceptance criteria:**
- 5 new fields on `Dialect` with type annotations matching existing convention.
- BIGQUERY_DIALECT, SNOWFLAKE_DIALECT, POSTGRES_DIALECT all instantiate with the new fields.
- Existing compiled-SQL snapshots for `not_null` / `unique` / `accepted_values` / `relationships` / `custom_sql` / `row_count_between` / `unique_combination` are byte-equal (the new fields aren't read by their compile arms).
- `tests/warehouse/test_models.py` pins each new field's value per dialect.
- `tests/prune/test_compiler_import_guard.py` still passes (no new SDK imports under `prune/`).
- Canonical validation command passes.

**Done when:** New fields ship; existing snapshots unchanged; per-dialect value pins green.

**Files:**
- `src/signalforge/warehouse/models.py` — add fields + update three dialect constants
- `tests/warehouse/test_models.py` — pin per-dialect values

**Depends on:** none.

**TDD:**
- Test: BIGQUERY_DIALECT.date_trunc_expr_template == `"DATE_TRUNC({date}, {unit})"`
- Test: SNOWFLAKE_DIALECT.date_trunc_expr_template != BIGQUERY_DIALECT.date_trunc_expr_template (verify divergence)
- Test: dow_sunday_index BQ=1, Snowflake=0
- Test: all three dialect constants still instantiate cleanly

### US-003 — `CandidateTestRowCountAnomalyByPeriod` variant class + drift mirror + fixture row + `VALID_TEST_TYPES` + `__all__`

**Description:** Land the variant class in `src/signalforge/draft/models.py` with all 9 fields per DEC-007, custom `__repr__` + `__repr_args__` redaction per #170 DEC-013, and the `model_validator(mode="after")` enforcing all field constraints. Add to discriminated union. Add `Strict*` drift mirror in `tests/draft/test_drift_detector.py`. Add row to `tests/fixtures/draft/candidate_schema_v1.json`. Add token to `VALID_TEST_TYPES`. Add class to `__all__`.

**Traces to:** DEC-007.

**Acceptance criteria:**
- `CandidateTestRowCountAnomalyByPeriod` exists with all DEC-007 fields, types, defaults, validators.
- `__repr__` shows only `(type, column, method, seasonality)`; `where` + `rationale` redacted.
- `__repr_args__` override mirrors `__repr__` (pinned by `pytest`-asserting `pprint.pformat(instance)` doesn't leak `where` or `rationale`).
- `CandidateTest` union grows a new arm; `Field(discriminator="type")` dispatches.
- `VALID_TEST_TYPES` includes `"row_count_anomaly_by_period"` (auto-validates `exclude_tests`).
- `__all__` adds the new class.
- Drift mirror + fixture row + drift detector test all pass.
- Validator rejects: `lookback_periods=0`, `min_samples_per_bucket=0`, `threshold=0` for `method=mad/zscore/percentile`, empty `where` after strip.
- Validator ACCEPTS `threshold=0` for `method=min_max` (ignored).
- Canonical validation command passes.

**Done when:** New variant validates round-trip; `Strict*` mirror gates; fixture loads.

**Files:**
- `src/signalforge/draft/models.py` — new class + union + `__all__`
- `src/signalforge/draft/config.py` — `VALID_TEST_TYPES`
- `tests/draft/test_drift_detector.py` — `Strict*` mirror
- `tests/fixtures/draft/candidate_schema_v1.json` — new row
- `tests/draft/test_models.py` — variant unit tests (validators, redaction)

**Depends on:** US-001 (no — variant class doesn't reference stats; can land in parallel).

**TDD:**
- Test: every validator path (each field's invalid value)
- Test: `method=min_max` accepts `threshold=0`
- Test: `where` validator strips and rejects empty
- Test: `pprint.pformat()` redacts `where` + `rationale`
- Test: `model_dump_json()` round-trip preserves all fields

### US-004 — `_common.artifact_id` arm + collision rule

**Description:** Add `row_count_anomaly_by_period` arm to `model_test_args_hash` in `src/signalforge/_common/artifact_id.py`. Identifying args: `(method, seasonality, period, lookback_periods, threshold, min_samples_per_bucket, date_column, where)`. All scalars (no tuples) — no sort needed; document the decision in a code comment. Two anomaly tests differing only by `method` must hash apart.

**Traces to:** DEC-007 (variant shape determines hash input set).

**Acceptance criteria:**
- New arm in `model_test_args_hash` with canonical-JSON payload.
- Code comment documents "no tuple args; no sort needed (mirrors #170 DEC-011)".
- Two anomaly tests on the same model differing only by `method` get distinct `artifact_id`s (collision test in `tests/diff/test_artifact_id.py`).
- Cross-stage parity test (per `diff-renderer.md` § "`_artifact_id` parity with grade layer") still passes — function identity across `signalforge._common.artifact_id`, `signalforge.diff._artifact_id`, `signalforge.grade.engine` holds.
- Canonical validation command passes.

**Done when:** Hash arm dispatches correctly; cross-stage parity gate green.

**Files:**
- `src/signalforge/_common/artifact_id.py` — new arm
- `tests/diff/test_artifact_id.py` — anomaly variant tests

**Depends on:** US-003.

**TDD:**
- Test: two anomaly tests differing only by `method` get distinct IDs
- Test: two anomaly tests differing only by `seasonality` get distinct IDs
- Test: two identical anomaly tests get identical IDs (regression — no spurious hash inputs)
- Test: cross-stage parity (function identity)

### US-005 — Drafter prompts catalogue + `_PROMPT_VERSION` rotation + cache-stability snapshot

**Description:** Extend `_TEST_CATALOGUE_LINES` in `src/signalforge/draft/prompts.py` with the new variant's JSON shape illustration (no-where + with-where + with-seasonality forms). Rotate `_PROMPT_VERSION` constant (computed at module load). Update `tests/llm/test_prompt_cache_stability.py::_EXPECTED_PROMPT_VERSION` + the rendered system-prompt golden. Drafter prompt teaches the LLM when to propose this variant (incremental fact tables with partition date columns).

**Traces to:** DEC-007.

**Acceptance criteria:**
- `_TEST_CATALOGUE_LINES` includes the new entry.
- `_PROMPT_VERSION` recomputes to a new blake2b-8 hex; constant updated.
- `tests/llm/test_prompt_cache_stability.py::_EXPECTED_PROMPT_VERSION` updated; rendered system-prompt golden refreshed; snapshot test green.
- Drafter prompt prose teaches: propose when SQL projection includes `loaded_at` / `created_at` / `event_date` / `partition_date`; propose `seasonality="dow"` when SQL suggests a business-calendar grain.
- `exclude_tests=("row_count_anomaly_by_period",)` short-circuits prompt rendering (mirrors #163 pattern) and parser cardinality (if any). Pinned by test.
- Canonical validation command passes.

**Done when:** Catalogue entry visible in rendered system prompt; cache-stability snapshot pinned to new hex; exclude_tests path covered.

**Files:**
- `src/signalforge/draft/prompts.py`
- `tests/llm/test_prompt_cache_stability.py`

**Depends on:** US-003.

**TDD:**
- Test: rendered system prompt contains the new catalogue line
- Test: `_PROMPT_VERSION` equals `_EXPECTED_PROMPT_VERSION`
- Test: `exclude_tests=("row_count_anomaly_by_period",)` removes the catalogue line from the rendered prompt

### US-006 — Drafter parser anchor-contract arm (`date_column` in model_columns, `where`-fragment type-coherence)

**Description:** Add anchor-contract arm in `src/signalforge/draft/parser.py::_validate_anchor_contract` for the new variant. Model-level only (skip column-existence check on `column=None`). Verify `date_column in model_columns`. When `where` non-None, reuse `_check_custom_sql_type_coherence` per #159 — compose a `WHERE <where>` fragment into a full SELECT, validate via sqlglot. Collect-all per existing pattern (`LLMOutputAnchorContractError(violations=...)` with full list).

**Traces to:** DEC-007.

**Acceptance criteria:**
- Hallucinated `date_column` (not in `model_columns`) → violation; collected.
- `column=None` short-circuits the column-existence check.
- `where` clause referencing nonexistent column → violation; collected via type-coherence reuse.
- `where` clause with valid SQL passes.
- Multiple violations (hallucinated `date_column` + bad `where`) all surfaced in one error per the collect-all rule.
- Validator uses the existing `model_columns_by_type` threading from `_draft_from_request` (#159 precedent).
- Canonical validation command passes.

**Done when:** Anchor arm covers all DEC-007 invariants; collect-all preserved.

**Files:**
- `src/signalforge/draft/parser.py` — new arm
- `tests/draft/test_parser.py` — variant anchor-contract tests

**Depends on:** US-003.

**TDD:**
- Test: bad `date_column` raises violation
- Test: bad `where` raises violation
- Test: both bad → both violations in one error
- Test: clean variant passes
- Test: `column=None` skips column-existence check

### US-007 — Ingest anchor exemption (model-level early-out)

**Description:** Add early-`continue` arm in `src/signalforge/ingest/anchor.validate_anchor_contract` for the new variant (mirrors `row_count_between` line 88–89, `unique_combination` lines 108–115). The variant is model-level only (`column=None`); the generic `test.column not in model_columns` check would fire a spurious `"references nonexistent column None"` violation.

**Traces to:** business-rule-tests.md § "The 6 production dispatch sites" #6.

**Acceptance criteria:**
- Ingest of a CandidateSchema containing an anomaly variant does not fire a spurious column violation.
- Test in `tests/ingest/test_anchor.py` pins the early-out.
- Canonical validation command passes.

**Done when:** Ingest accepts the variant cleanly.

**Files:**
- `src/signalforge/ingest/anchor.py`
- `tests/ingest/test_anchor.py`

**Depends on:** US-003.

**TDD:**
- Test: ingest accepts model-level anomaly variant; no anchor violations
- Test: planted invalid `date_column` is still caught by drafter parser (defensive — ingest anchor is the model-level exemption, NOT a free pass)

### US-008 — Prune compiler: `_compile_stats_query` + `_compile_violation_query` (4 methods × 2 seasonality, with partition filter)

**Description:** Add the compile helpers to `src/signalforge/prune/compiler.py`. The variant compiles into **two** SQL queries per DEC-008: a stats query (returns one row of per-method stats; per-DOW when seasonal) and a violation query (returns failing-rows shape consumed by the adapter's COUNT-wrap, runs only if cold-start check passes). Both queries:
- Read date-arithmetic fragments from `Dialect` per DEC-011; never branch on `dialect.name`.
- Include partition-pruning WHERE clause per DEC-012.
- Resolve `{{ this }}` via existing `manifest.resolve_template_refs` (no full Jinja).
- Validate via `_sql_safety.validate_identifier` (date_column) and `validate_test_sql` (composed full SELECT).
- Return `_InvalidIdentifier` on safety reject → engine routes to `kept-without-evidence`.

The dispatcher in `_compile_test` grows an `isinstance(test, CandidateTestRowCountAnomalyByPeriod)` arm. Since this variant compiles into TWO SQL strings, the dispatcher returns a tuple `(stats_sql, violation_sql)` only for this variant; the engine handles the two-query split.

**Traces to:** DEC-008, DEC-011, DEC-012.

**Acceptance criteria:**
- New arm `isinstance(test, CandidateTestRowCountAnomalyByPeriod)` in `_compile_test` dispatcher (returns tuple).
- 4 × 2 = 8 SQL shapes correctly emitted; pinned via byte-exact snapshot fixtures under `tests/fixtures/prune/compiled_sql/anomaly/`.
- Partition filter present in EVERY shape (snapshot pins this).
- Snowflake snapshots in `snowflake/` subdir pin Snowflake dialect output (parsed via `sqlglot.parse_one(dialect="snowflake")`).
- `where` clause with bad identifier → `_InvalidIdentifier`.
- AST import-guard at `tests/prune/test_compiler_import_guard.py` still passes (no `google.cloud` / `snowflake` SDK imports under `prune/`).
- Canonical validation command passes.

**Done when:** All 8 shapes snapshot-pinned; safety rejects route via `_InvalidIdentifier`; dialect imports gated.

**Files:**
- `src/signalforge/prune/compiler.py` — new helpers + dispatcher arm
- NEW: `tests/fixtures/prune/compiled_sql/anomaly/*.sql` (16 fixtures: 8 BigQuery + 8 Snowflake)
- `tests/prune/test_compiler.py` — snapshot tests + safety-reject tests
- `tests/prune/test_compiler_fakesnow.py` — Snowflake parse-validation under existing `@pytest.mark.snowflake`

**Depends on:** US-002, US-003.

**TDD:**
- Test (snapshot, 8 BQ + 8 Snowflake): every method × seasonality shape pinned byte-for-byte
- Test: partition filter present in every shape (regex grep)
- Test: bad `where` clause routes to `_InvalidIdentifier`
- Test: `{{ this }}` resolves correctly
- Test: dispatcher returns tuple `(stats_sql, violation_sql)` for this variant only

### US-009 — Prune engine: `as_of` threading + INFO log + variant detection

**Description:** Extend `signalforge.prune.engine.prune_tests` with `as_of: date | None = None` keyword-only parameter. At orchestrator entry: resolve `as_of = as_of or date.today()`; emit one INFO line `_LOGGER.info("anomaly: as_of resolved", extra={...})` (lazy-format JSON) ONLY if any candidate is an anomaly variant. Thread `as_of` through `_compile_test` (new kwarg). When variant detected, dispatcher returns tuple; engine handles separately.

**Traces to:** DEC-001.

**Acceptance criteria:**
- `prune_tests` accepts `as_of: date | None = None` keyword-only.
- When `None` AND any anomaly candidate present → resolves to `date.today()` AND emits one INFO log naming resolved value.
- When `None` AND NO anomaly candidate → no resolution, no log (clean v0.x: no impact on existing variants).
- When supplied → uses supplied value; no log if no anomaly candidate.
- `--quiet` suppresses the INFO line (standard cli-layer suppression).
- Logger grep gate at `tests/llm/test_logger_grep_gate.py` still green (lazy-format JSON, no f-string).
- Canonical validation command passes.

**Done when:** `as_of` threads cleanly; resolution + log fire only when relevant.

**Files:**
- `src/signalforge/prune/engine.py`
- `tests/prune/test_engine.py` — as_of resolution tests

**Depends on:** US-003 (variant exists).

**TDD:**
- Test: `as_of=None` + anomaly candidate → resolves to today + INFO log present
- Test: `as_of=None` + NO anomaly candidate → no resolution, no log
- Test: `as_of=date(2026, 5, 1)` + anomaly candidate → uses supplied value
- Test: logger lazy-format (grep gate green)

### US-010 — Prune engine: `_test_requires_source_table` helper + two-site routing + Q10 tighter bypass

**Description:** Add `_test_requires_source_table(test, sample_strategy)` helper in `signalforge.prune.engine`. Returns `True` for ALL THREE metadata-aggregate variants (`row_count_between`, `unique_combination`, `row_count_anomaly_by_period`) under ANY sample mode per DEC-009 / DEC-010. Refactor both engine sites (`all_bypass_to_source` short-circuit AND per-test `per_test_table_ref` override) to call the helper instead of inline isinstance. **Behaviour change for #169 + #170**: those variants now also bypass under `sample_strategy="oneshot"` (was: materialised only). Pin a mixed-candidate test (1× metadata-aggregate + 1× row-level on same model, `scope=sample, sample_strategy=oneshot`) per #170 QG Pass 3.

**Traces to:** DEC-009, DEC-010.

**Acceptance criteria:**
- `_test_requires_source_table` defined; unit-tested in isolation against every variant × sample_strategy combination.
- Both engine sites call the helper (no remaining inline isinstance for bypass logic).
- Mixed-candidate test pinned: 1× `row_count_between` + 1× `not_null` on same model, scope=sample/oneshot → `row_count_between` routes to source, `not_null` routes to sampled temp.
- Same shape with 1× `row_count_anomaly_by_period` + 1× `not_null`.
- Same shape with 1× `unique_combination` + 1× `not_null`.
- Pre-change snapshots that captured oneshot-routing for `row_count_between`/`unique_combination` are explicitly updated; pinned in the same commit.
- CHANGELOG `[Unreleased]` § Changed records the behaviour change per DEC-010.
- Canonical validation command passes.

**Done when:** Helper centralises routing; behaviour change documented + tested.

**Files:**
- `src/signalforge/prune/engine.py`
- `tests/prune/test_engine.py`
- `CHANGELOG.md`

**Depends on:** US-003 (anomaly variant exists), US-008 (compile arm exists, otherwise mixed-candidate test crashes on compile).

**TDD:**
- Test: `_test_requires_source_table(RowCountAnomalyByPeriod(), "materialised") == True`
- Test: `_test_requires_source_table(RowCountAnomalyByPeriod(), "oneshot") == True`
- Test: `_test_requires_source_table(RowCountBetween(), "oneshot") == True` (Q10 behaviour change)
- Test: `_test_requires_source_table(UniqueCombination(), "oneshot") == True` (Q10 behaviour change)
- Test: `_test_requires_source_table(NotNull(), "materialised") == False`
- Test: mixed candidates under oneshot: metadata-aggregate routes to source; row-level to compile_table_ref

### US-011 — Prune engine: two-query split + cold-start routing + AnomalyTestStats wiring + DOW degrade

**Description:** Extend `prune_tests` per-test loop: when variant detected, dispatcher returns `(stats_sql, violation_sql)` tuple. Engine runs Query 1 (stats) via `adapter.run_test_sql(stats_sql)` adapted to return a one-row structured result. Parses result into the appropriate `AnomalyTestStats` subclass per the test's `method` field. Checks `stats.n_periods >= test.min_samples_per_bucket`; on fail → `PruneDecision(decision="kept", reason="kept-without-evidence", why=f"insufficient history: {n}/{min} periods", stats=stats)` and SKIPS Query 2. Under `seasonality="dow"` + thin per-DOW samples (any DOW bucket below floor): engine recomputes the stats query without DOW partitioning, emits one WARNING line, and proceeds with the non-seasonal violation check. On non-cold-start: runs Query 2 via the standard `run_test_sql` path; `PruneDecision` carries `stats` + `failures` + standard routing.

**Traces to:** DEC-001, DEC-003, DEC-005, DEC-008.

**Acceptance criteria:**
- Two-query split implemented; non-anomaly variants run single-query path unchanged (byte-equal snapshots).
- Cold-start (n < min_samples_per_bucket) → `kept-without-evidence` with structured `why`; Query 2 skipped (no warehouse call); `stats` populated.
- DOW + thin per-DOW: degrade to non-seasonal, WARNING emitted, proceeds. Pinned by integration test.
- `PruneDecision.stats` populated on every anomaly decision (kept-without-evidence + kept + dropped paths).
- `PruneEvent.stats` populated likewise (audit-of-record).
- Result parsing uses the discriminated-union dispatch per US-001.
- Canonical validation command passes.

**Done when:** Two-query path works end-to-end; cold-start + DOW degrade gated; stats flow through to audit.

**Files:**
- `src/signalforge/prune/engine.py`
- `tests/prune/test_engine.py` — cold-start + DOW degrade + stats-flow tests

**Depends on:** US-001 (stats shape), US-008 (compile arm), US-009 (as_of threading), US-010 (helper exists).

**TDD:**
- Test: non-anomaly variant single-query path byte-equal (regression)
- Test: anomaly variant non-cold-start runs both queries; stats populated
- Test: anomaly variant cold-start runs only stats query; routes kept-without-evidence
- Test: anomaly `seasonality=dow` + thin DOW bucket → degrades + WARNING + proceeds
- Test: per-test INFO log "variant requires source" emitted when bypassing sample mode

### US-012 — `PruneEvent` + `PruneDecision`: `as_of` + `stats` fields + audit schema bump v2 → v3 + serializer + fixture update + drift detector

**Description:** Add `as_of: date | None = None` and `stats: AnomalyTestStats | None = None` to both `PruneDecision` (`src/signalforge/prune/models.py`) and `PruneEvent` (`src/signalforge/prune/audit.py`). Bump `_PRUNE_AUDIT_SCHEMA_VERSION: 2 → 3`. Add `@field_serializer("as_of")` returning `value.isoformat() if value else None` on both. Update existing fixture `tests/fixtures/prune/prune_event_v1.jsonl` in place to v3 shape. Add inline-v2-dict-replays-as-v3 regression test. Update `Strict*` mirrors in drift detector to add both new fields. `PruneDecision.__repr__` adds both new fields to omitted-from-repr set per DEC-006.

**Traces to:** DEC-001, DEC-003, DEC-006, DEC-013.

**Acceptance criteria:**
- Both models carry both new fields.
- `_PRUNE_AUDIT_SCHEMA_VERSION = 3`.
- `as_of` serialises as `YYYY-MM-DD` ISO string (not `datetime.timestamp.iso8601_z`).
- `stats` serialises via Pydantic's native discriminated-union JSON.
- v2-shaped dict loads cleanly into v3 model (replay test).
- Updated fixture validates against both production and strict mirrors.
- `PruneDecision.__repr__` does NOT show `as_of` or `stats` (redaction).
- `config_hash` input set does NOT include `as_of` (verified by hash-stability test).
- Canonical validation command passes.

**Done when:** Both fields present in both models; schema v3; fixture updated; replay test green.

**Files:**
- `src/signalforge/prune/models.py`
- `src/signalforge/prune/audit.py`
- `tests/fixtures/prune/prune_event_v1.jsonl`
- `tests/prune/test_audit.py` — replay test, serializer test
- `tests/prune/test_drift_detector.py` — strict mirror update
- `tests/prune/test_models.py` — __repr__ redaction test

**Depends on:** US-001 (AnomalyTestStats).

**TDD:**
- Test: v2-shaped dict (missing `as_of`/`stats`) round-trips into v3 model
- Test: `as_of` serialises as ISO date string
- Test: stats serialises with method discriminator
- Test: `PruneDecision.__repr__` redacts `as_of` + `stats`
- Test: `config_hash` is byte-identical with and without `as_of` set

### US-013 — CLI `--as-of` flag plumbing on `generate` and `prune-existing` + 5-surface parity

**Description:** Add `--as-of YYYY-MM-DD` argparse flag to `signalforge generate` and `signalforge prune-existing`. `type=date.fromisoformat`; default `None`. Threads to `prune_tests(..., as_of=args.as_of)`. Update `cmd_generate` / `cmd_prune_existing` handler docstrings. Update `docs/cli-ops.md` Flag reference. Pin via tests including bad-format → tier 2 exit code.

**Traces to:** DEC-001.

**Acceptance criteria:**
- Both subcommands accept `--as-of YYYY-MM-DD`; bad format → `SystemExit(2)`.
- `cmd_generate` and `cmd_prune_existing` docstrings document the flag.
- `docs/cli-ops.md` Flag reference section grows entries for both subcommands.
- 5-surface parity sweep complete: (1) argparse help, (2) handler docstring, (3) docs/cli-ops.md, (4) test name, (5) DEC reference.
- Multi-model batch (`--select`): same `--as-of` applies to every model; resolved once at orchestrator entry.
- Canonical validation command passes.

**Done when:** Flag works end-to-end; 5 surfaces consistent; tests pin behaviour.

**Files:**
- `src/signalforge/cli/generate.py`
- `src/signalforge/cli/prune_existing.py`
- `docs/cli-ops.md`
- `tests/cli/test_generate.py` — flag test
- `tests/cli/test_prune_existing.py` — flag test

**Depends on:** US-009 (engine accepts the kwarg).

**TDD:**
- Test: `signalforge generate --as-of 2026-05-01 ...` parses cleanly; threads to engine
- Test: `signalforge generate --as-of not-a-date ...` exits 2
- Test: `signalforge prune-existing --as-of 2026-05-01 ...` parses cleanly
- Test: multi-model batch (`--select`) uses one `as_of` across all models
- Test: 5-surface parity (CLI help string mentions `--as-of`; docstring does; ops doc does)

### US-014 — Diff renderer arm: route to `_SKIP` (singular SQL emission)

**Description:** Add arm in `src/signalforge/diff/_emitter._render_test` for the new variant. Routes to `_SKIP` (mirrors `custom_sql` line 177–178), so the test emits as a singular `tests/*.sql` file via `proposed_test_files` not as a YAML block. The fail-closed `_test_file_writer.write_test_file` is variant-agnostic — no changes needed there.

**Traces to:** Phase 1 B.7 (locked: singular SQL only).

**Acceptance criteria:**
- `_render_test` arm returns `_SKIP` for the new variant.
- `proposed_test_files` includes a `.sql` file per anomaly test under `tests/<model_name>__row_count_anomaly_by_period__<args_hash>.sql`.
- Filename uses `_test_file_writer.anchor_to_filename` slugger (existing).
- `-- signalforge:generated <hash>` header marker present (existing writer).
- Pinned by snapshot fixture under `tests/fixtures/diff/proposed_test_files/anomaly/`.
- Canonical validation command passes.

**Done when:** Diff emits singular SQL for the variant; snapshot pinned.

**Files:**
- `src/signalforge/diff/_emitter.py`
- `tests/diff/test_emitter.py`
- NEW: `tests/fixtures/diff/proposed_test_files/anomaly/*.sql`

**Depends on:** US-003, US-004, US-008.

**TDD:**
- Test: `_render_test(RowCountAnomalyByPeriod(...))` returns `_SKIP`
- Test: rendered diff carries `proposed_test_files` entry with header marker
- Test: filename is slug-safe

### US-015 — Grade rubric: extend `no-redundant` criterion text + grade `_PROMPT_VERSION` rotation + cache-stability snapshot

**Description:** Extend `DEFAULT_RUBRIC` `no-redundant` criterion text in `src/signalforge/grade/rubric.py` with anomaly-specific calibration prose per DEC-004 (mirrors #169 DEC-009 verbatim). The new prose teaches the judge to score: is `(method, seasonality, threshold)` tight enough to catch the failure mode without firing on legitimate seasonal swings? `signalforge.grade.prompts._PROMPT_VERSION` rotates (computed at import via `prompt_version_template(DEFAULT_RUBRIC)`). Update `tests/grade/test_prompt_cache_stability.py::_EXPECTED_PROMPT_VERSION` + the rendered rubric-block golden.

**Traces to:** DEC-004.

**Acceptance criteria:**
- `DEFAULT_RUBRIC` `no-redundant` criterion text includes anomaly calibration prose.
- `signalforge.grade.prompts._PROMPT_VERSION` recomputes to new hex; constant updated.
- `tests/grade/test_prompt_cache_stability.py::_EXPECTED_PROMPT_VERSION` updated; rendered golden refreshed.
- Other 3 criteria texts unchanged (their hashes stable).
- Canonical validation command passes.

**Done when:** Rubric prose extended; grade snapshot pinned to new hex.

**Files:**
- `src/signalforge/grade/rubric.py`
- `src/signalforge/grade/prompts.py` (no code change — `_PROMPT_VERSION` recomputes at import time)
- `tests/grade/test_prompt_cache_stability.py`

**Depends on:** none (parallel-safe).

**TDD:**
- Test: `_PROMPT_VERSION` equals `_EXPECTED_PROMPT_VERSION`
- Test: `no-redundant` criterion text contains the anomaly prose
- Test: other criteria texts unchanged (per-criterion hash stability)

### US-016 — Docs: README catalogue row + drafter-catalogue.md section + prune-ops.md cost section + SKILL.md update

**Description:** Add the 8th catalogue row to `README.md`'s "What tests SignalForge generates" table. Mirror in `docs/drafter-catalogue.md` with a new section under `unique_combination` (semantics, YAML/SQL shape, drafter heuristics, cold-start behaviour). Add new section to `docs/prune-ops.md` "Variants" covering: two-query split, `--as-of` reproducibility carve-out, partition-filter cost mechanics with a worked example (90-day lookback on 1B-row daily-partitioned table: 9 GB unfiltered → ~300 MB partition-pruned), cold-start routing, DOW degrade WARNING. Add `--as-of` to `docs/cli-ops.md` Flag reference. Update `src/signalforge/skills/signalforge/SKILL.md` with the 8th catalogue entry + `--as-of` flag note. Avoid the mkdocs-ATX-in-fenced-block bug — use indented (4-space) code blocks for example heading content.

**Traces to:** DEC-001, DEC-004, DEC-010, DEC-012, Phase 1 B.9, skill-parity.md.

**Acceptance criteria:**
- README table grows the row; row flags this variant as the first time-bound primitive (carve-out from reproducibility contract).
- `drafter-catalogue.md` section ships; single source of truth for catalogue prose.
- `prune-ops.md` ships cost-framing section with worked example.
- `cli-ops.md` Flag reference includes `--as-of` for both subcommands.
- `SKILL.md` 8th variant entry + flag note ships.
- CHANGELOG `[Unreleased]` § Added: "row_count_anomaly_by_period primitive (drafter + prune + grade); --as-of flag for time-bound reproducibility carve-out." § Changed: per DEC-010 entry. § Documentation: catalogue + ops-doc updates.
- mkdocs build (`uv run mkdocs build`) succeeds with no broken-anchor warnings.
- Canonical validation command passes.

**Done when:** All 5 doc surfaces extended; mkdocs builds cleanly; CHANGELOG entries land.

**Files:**
- `README.md`
- `docs/drafter-catalogue.md`
- `docs/prune-ops.md`
- `docs/cli-ops.md`
- `src/signalforge/skills/signalforge/SKILL.md`
- `CHANGELOG.md`

**Depends on:** US-003 (variant), US-013 (flag exists for docs).

### US-017 — E2E gated test: BigQuery + Austin bikeshare + engineered `--as-of` anomaly + unit determinism

**Description:** Add two new tests for the variant:
1. **Unit determinism**: `tests/prune/test_engine.py::test_as_of_reproducibility_byte_equal_compiled_sql` — run `prune_tests` twice with the same `as_of=date(2026, 5, 1)` on a fixed fake-adapter fixture; assert `PruneEvent.compiled_sql` byte-equal across runs.
2. **Live e2e (gated)**: extend `tests/cli/test_e2e_bigquery_smoke.py` with an anomaly-variant parametrize. Uses `inject_model_anomaly_rules` (new helper in `tests/cli/_e2e_helpers.py`) to inject `meta.signalforge.business_rules` pointing the drafter at a specific date column. Runs `signalforge generate --as-of <date>` against Austin bikeshare `bikeshare_trips` with a hand-picked `--as-of` that catches a known volume anomaly in the public data (verifiable via BQ). Asserts: the drafter proposes a structured `row_count_anomaly_by_period`, prune evaluates it (not always-passes, not kept-uncertain), `PruneEvent.as_of` carries the supplied value, `AnomalyTestStats` populates per DEC-005 shape, decision is `kept` (real anomaly caught).

Marker: `@pytest.mark.e2e and @pytest.mark.anthropic and @pytest.mark.bigquery`. Belt-and-suspenders gating with runtime `_skip_reason()` per testing-signal.md.

**Traces to:** DEC-001, DEC-003, DEC-008.

**Acceptance criteria:**
- Unit determinism test pinned; uses fake adapter with `expect_query` queue.
- `inject_model_anomaly_rules` helper added; mirrors `inject_model_business_rules`.
- E2E gated test selects an anomaly date verifiable in `bigquery-public-data.austin_bikeshare.bikeshare_trips`.
- E2E test passes under live BigQuery run; skips cleanly when `SF_RUN_BQ` / `ANTHROPIC_API_KEY` / `GOOGLE_CLOUD_PROJECT` absent.
- E2E test asserts `PruneEvent.as_of` + populated `AnomalyTestStats` + `decision="kept"`.
- E2E test follows `tmp_path` isolation pattern (committed fixture untouched).
- Canonical validation command passes (e2e excluded by addopts).

**Done when:** Unit test green in CI; e2e test passes on maintainer's live run; helper added.

**Files:**
- `tests/prune/test_engine.py` — unit determinism test
- `tests/cli/test_e2e_bigquery_smoke.py` — e2e parametrize
- `tests/cli/_e2e_helpers.py` — `inject_model_anomaly_rules`

**Depends on:** US-009, US-010, US-011, US-012, US-013.

**TDD:**
- Unit: same `as_of` twice → byte-equal compiled SQL
- Unit: different `as_of` → compiled SQL differs (proves param threads through to the compile step)
- E2E (live, gated): full pipeline catches a real anomaly

### US-018 — Quality Gate (code reviewer × 4 + CodeRabbit)

**Description:** Run the code-review skill four times across the full changeset with diverse reviewer angles per the `qg-diverse-reviewer-angles-catch-cross-surface-drift` memory:
1. **Correctness** — every code path; engine routing; SQL composition; `as_of` threading; cold-start; DOW degrade
2. **Conventions** — `business-rule-tests.md` § 6 dispatch sites; `prune-engine.md` two-conditional routing; `pydantic-v2-repr-args-redaction-required`; lockstep `_PROMPT_VERSION` rotation
3. **Tests** — mixed-candidate test exists; engineered determinism real; replay test for v2 → v3; 5-surface parity sweep; cross-stage parity
4. **Docs + UX** — README + drafter-catalogue + prune-ops + cli-ops + SKILL.md + CHANGELOG; cost framing with worked example; carve-out wording; mkdocs no broken anchors

Fix every real bug found each pass. Re-run validation after each pass. Run CodeRabbit review on the PR diff if available; fix all findings.

**Done when:** All four passes complete with all findings either fixed or explicitly waived with reasoning; CodeRabbit review surfaces no remaining real issues; canonical validation command passes.

**Files:** Any file touched in US-001 … US-017 may receive fixes.

**Depends on:** US-017.

### US-019 — Patterns & Memory

**Description:** Update `.claude/rules/business-rule-tests.md` to reflect #171 as the **4th instance** of the variant-extension precedent. Add a new section "#171 lessons worth carrying forward" mirroring the existing "#170 lessons" section. Cover:
- 6 dispatch sites are now well-trodden; the next variant follows the same template.
- Cross-stage state seam pattern (typed `AnomalyTestStats` via discriminator) — reusable for any future primitive needing prune → grade numerical handoff.
- Stricter-bypass-under-any-sample-mode is the new metadata-aggregate default per DEC-010 (#169/#170 graduated in lockstep).
- Two-query split (stats + violation) is the cold-start template for any future primitive whose decision depends on a per-test statistical state.
- `Dialect` graduates 5 date-arithmetic fields; future primitives needing date math read these fields.
- Time-bound reproducibility carve-out via `--as-of` is a precedent for any future time-bound primitive.

Update `.claude/rules/prune-engine.md` to document:
- `_test_requires_source_table` helper as the centralised bypass seam.
- Updated routing table reflecting `oneshot` bypass for ALL three metadata-aggregate variants.
- `_PRUNE_AUDIT_SCHEMA_VERSION = 3` history note.

Add memory entries for:
- The two-query split template.
- The `inject_model_anomaly_rules` e2e helper precedent.
- Any new gotcha discovered during implementation (e.g., Pydantic `date` serializer pattern).

Update `MEMORY.md` index.

**Done when:** Rule files reflect #171; new memories added; index updated.

**Files:**
- `.claude/rules/business-rule-tests.md`
- `.claude/rules/prune-engine.md`
- `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/*.md` (memory files)
- `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/MEMORY.md`

**Depends on:** US-018.

## Beads Manifest

Devolved 2026-06-01. PR #181 (draft) — https://github.com/wjduenow/SignalForge/pull/181.
Worktree: `/home/wesd/Projects/worktrees/SignalForge/171-row-count-anomaly`.

**Epic:** `bd_1-scaffolding-1r7` — #171: row_count_anomaly_by_period epic

| Story | Bead ID | Depends on | Ready? |
|---|---|---|---|
| US-001 — AnomalyTestStats typed shape | `.1` | — | ✅ ready |
| US-002 — Dialect 5 new SQL-fragment fields | `.2` | — | ✅ ready |
| US-003 — Variant class + union + drift + fixture | `.3` | — | ✅ ready |
| US-015 — Grade rubric `no-redundant` + `_PROMPT_VERSION` | `.4` | — | ✅ ready |
| US-004 — `_common.artifact_id` arm | `.5` | `.3` | blocked |
| US-005 — Drafter prompts + `_PROMPT_VERSION` | `.6` | `.3` | blocked |
| US-006 — Drafter parser anchor arm | `.7` | `.3` | blocked |
| US-007 — Ingest anchor exemption | `.8` | `.3` | blocked |
| US-008 — Prune compiler (8 SQL shapes) | `.9` | `.2, .3` | blocked |
| US-009 — Engine `as_of` threading | `.10` | `.3` | blocked |
| US-012 — `PruneEvent`/`Decision` + audit v2→v3 | `.11` | `.1` | blocked |
| US-010 — Engine `_test_requires_source_table` + DEC-010 | `.12` | `.3, .9` | blocked |
| US-011 — Engine two-query split + cold-start + DOW degrade | `.13` | `.1, .9, .10, .11, .12` | blocked |
| US-013 — CLI `--as-of` flag | `.14` | `.10` | blocked |
| US-014 — Diff renderer `_SKIP` arm | `.15` | `.3, .5, .9` | blocked |
| US-016 — Docs sweep | `.16` | `.3, .12, .13, .14, .15` | blocked |
| US-017 — E2E gated + unit determinism | `.17` | `.10, .11, .12, .13, .14` | blocked |
| US-018 — Quality Gate × 4 + CodeRabbit | `.18` | `.16, .17` | blocked |
| US-019 — Patterns & Memory | `.19` | `.18` | blocked |

**20 beads total** (1 epic + 19 stories). Initial ready set: 4 beads (`.1`, `.2`, `.3`, `.4`).

**Serialisation reminders for Ralph (per `ralph-serialize-shared-registry-beads` memory):**
- `.6` (US-005) + `.4` (US-015) both edit `_PROMPT_VERSION` cache-stability goldens — do NOT run concurrently.
- `.12` (US-010) + `.13` (US-011) both edit `signalforge.prune.engine` — do NOT run concurrently.
- `.11` (US-012) before `.13` (US-011) is wired via the dep graph (US-011 waits on `.11`).

Story-count expectation calibration: #169 shipped 15 DECs, #170 shipped 17 DECs. #171 has 13 DECs locked in design but a larger implementation surface (new CLI flag, audit-schema bump, cross-stage typed state, 4 × 2 SQL shapes, stricter-bypass behaviour change affecting 2 prior variants, 2-query split) — 19 implementation stories is the right scale.

### Concurrency notes for Ralph execution (`ralph-serialize-shared-registry-beads` memory)

- **US-005 (drafter prompts + `_PROMPT_VERSION`)** and **US-015 (grade rubric + `_PROMPT_VERSION`)** both touch `_PROMPT_VERSION` cache-stability snapshots — **must serialize** (separate workers will both regenerate goldens and collide).
- **US-010 (engine helper + behaviour change)** and **US-011 (two-query split)** both edit `signalforge.prune.engine` — **must serialize**.
- **US-012 (audit/models bump)** and **US-011 (engine consumes stats)** both touch stats wiring — **US-012 must complete first**.
- **US-016 (docs sweep)** edits CHANGELOG which several earlier stories also touch — **schedule last** before US-018.

