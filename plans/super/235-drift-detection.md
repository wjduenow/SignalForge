# Super Plan — #235: Airflow drift / signal-rot detection (run-over-run)

## Meta
- **Ticket:** #235 — Airflow: drift / signal-rot detection mode (run-over-run). Part of epic #228 (v0.7 Airflow). Depends on #231 (result/XCom contract — landed) and #232 (`SignalForgeGenerateOperator` — landed).
- **Phase:** detailing (awaiting approval to devolve)
- **Branch:** `feature/235-drift-detection` (worktree `/home/wesd/Projects/worktrees/SignalForge/235-drift-detection`, base `dev`).
- **Sessions:** 1 (2026-06-16).

---

## Discovery

### What
Add **run-over-run drift detection** to the Airflow integration: compare a SignalForge run (N)
against its prior run (N-1) and emit a structured, JSON-serialisable **drift report** delta. The
headline signal is **signal rot** — a test that *used to* catch failing rows (`kept`) but now
**always-passes** (`dropped: always-passes`), i.e. the column it guarded went all-clean / the rule
went vacuous. That transition is a schema-drift alarm worth paging on (Architectural Commitment #1:
an always-pass test is noise; a test that *rotted into* always-pass is a drift signal).

The report also surfaces: tier transitions either direction (`newly_dropped` / `newly_kept`),
**grade regressions** (mean grade fell beyond a threshold), and **schema-shape changes** (columns
added/removed/retyped). It is pushed to XCom and can `fail` / `skip` / `succeed` the Airflow task
via an `on_drift` policy — the run-over-run analogue of #231's `on_flagged`.

### Why
Running `generate` nightly produces a graded diff each night, but the *interesting* signal is
**change over time** — a single run's sidecar can't see it. This is the feature that turns a
SignalForge DAG from "run generate nightly" into a **schema-drift / signal-rot monitor**. It is the
explicit headline scheduled value-add of epic #228.

### Key codebase findings (from research)

**The two input sidecars (what we read back & diff):**
- `DiffReport` (`src/signalforge/diff/models.py:206`) — `model_dump_json(by_alias=True)` lands at
  `.signalforge/diff.json`. Fields we consume: `entries: tuple[DiffEntry, ...]`, the four count
  fields, `model_unique_id`, `audit_schema_version: Literal[3]`, the three `blake2b-8` reproducibility
  hashes (`candidate_hash` / `prune_result_hash` / `grading_report_hash`).
- `DiffEntry` (`:142`) — `artifact_id: str`, `test_type: str | None`, `tier: Tier`,
  `drop_reason: DropReason | None`, `why: str`, `score: float | None`, `passed: bool | None`.
- `Tier = Literal["kept","kept-uncertain","dropped","flagged"]` (`diff/models.py:62`).
- `DropReason = Literal["always-passes","requires-future-data","failed-on-known-clean-data","kept","kept-without-evidence"]`
  (`prune/models.py:55`).
- `GradingReport` (`grade/models.py:182`) — `.signalforge/grade.json`; `mean_score` and `pass_rate`
  are `@computed_field` properties; carries `model_unique_id`, `rubric_hash`, `results`.
- **`artifact_id` encodes the column** (`column.<col>.description`, `test.column.<col>.<type>`, …) so
  the **column SET** (add/remove) is derivable from the union of artifact_ids across the two reports.
  **Column TYPES are NOT in the sidecar** — a retype can't be computed from `diff.json` alone.

**The #231 airflow-free seam we extend:**
- `SignalForgeRunResult` (frozen dataclass, `airflow/result.py:55`) — counts + `mean_grade` +
  `diff_sidecar_path` / `grade_sidecar_path` + `to_xcom()` (counts + paths only). `below_threshold`
  is a read-only property `= flagged > 0`.
- `TaskOutcome(StrEnum)` = `SUCCESS / SKIP / FAIL_NO_RETRY / FAIL_RETRYABLE` (a SEPARATE axis from
  the 4-tier exit code — NOT a 5th tier).
- `decide_task_outcome(result, *, on_flagged: OnFlagged = "fail") -> TaskOutcome` (`:110`) — pure,
  no I/O, no airflow. `OnFlagged = Literal["fail","skip","succeed"]` (`:37`).
- `run_signalforge(argv, *, project_dir, invocation="in_process", timeout_seconds=None)` (`runner.py:324`)
  — reads diff JSON off **stdout**, `grade.json` from disk when present; sidecar reads are fail-soft.
- `raise_for_outcome(outcome, *, message)` — the ONLY `from airflow ...` site, in `_airflow_compat`.

**The #232 operator pattern we mirror:**
- Pure helpers (`_build_generate_argv` / `_validate_operator_config` / `_resolve_select_models` /
  `_aggregate_batch_result`) tested UNGATED; deferred class via module `__getattr__` +
  `find_spec("airflow")` branch + `functools.cache`d factory + airflow-free placeholder; `execute()`
  is a thin `# pragma: no cover` wire-up. `template_fields = ("project_dir","select","model","profiles_dir","as_of")`.
  `__init__` already takes `as_of`, `on_flagged`, `invocation`.
- `--as-of` (`cli/generate.py:459`, `type=date.fromisoformat`) is threaded as a stringified argv token
  (`_build_generate_argv(..., as_of=...)`). It is the **reproducibility carve-out** (#171) the ticket
  cites for time-bound determinism.

**Errors / tests:**
- `AirflowConfigError(AirflowIntegrationError)` — tier 2; base renders `↳ Remediation:`. No new error
  class needed unless a genuinely new failure mode appears.
- Airflow tests: `pytestmark = pytest.mark.airflow` + in-test `pytest.importorskip("airflow")`; pure
  helpers tested ungated; committed sidecar fixtures under `tests/fixtures/diff/` and `tests/fixtures/grade/`.

### Rules constraints in force (from `.claude/rules/`; no `workflow-project.md` exists)

**airflow-integration.md (primary):**
- Pure airflow-free core decides; shim raises. The drift comparison is a pure function; the
  task-state raise stays in `_airflow_compat.raise_for_outcome`.
- `on_drift` is the run-over-run analogue of `on_flagged` — key it on a **drift property of an exit-0
  run**, NOT on the exit code. **Never grow a 5th exit tier.** `TaskOutcome` stays 4-valued.
- XCom hygiene: counts/paths/summary only — never bulk sidecar text, never secrets.
- A dedicated operator (if chosen) follows the deferred-class + pure/gated split verbatim, and needs
  an UNGATED skeleton test (placeholder `__init__` raises, `find_spec is None` factory branch,
  `__getattr__` arm) or the codecov patch gate fails.
- Certify airflow-touching paths against `.venv-airflow` (#229) before closing.

**diff-renderer.md / prune-engine.md:**
- Reuse the existing 4 `tier` literals + 5 `DropReason` literals — the drift report is computed
  *from* that taxonomy, not a new classification. The drift report is a NEW model, not a tier-shaped
  thing.
- Reproducibility: same two sidecars → byte-identical drift report. Carry the two input
  `blake2b-8` hashes so the report is self-describing.
- If a `drift.json` sidecar is written: it's a fail-closed writer (6th) — pre-size-check, `O_TRUNC`,
  single `os.write`, `os.fsync`, no `except` around write/fsync; AST scan 5 pins the `try/finally`
  shape; symlink-hardened canonicalisation at the orchestrator.

**business-rule-tests.md:** `--as-of` reproducibility carve-out is the precedent for time-bound
determinism — surface it so a drift comparison is reproducible at `(model, as_of)`.

**cli-layer.md:** four-tier taxonomy; any new typed error registers in `_EXCEPTION_TO_EXIT_CODE`
(scan-7) — or inherit via MRO if it subclasses `AirflowIntegrationError`. 5-surface parity for any
new flag/param. `errors.py` count is 14 (scan-7).

**testing-signal.md:** determinism via committed fixture PAIRS (run N-1, run N) engineered so each
transition class is mathematically guaranteed; drift-detector (`extra="forbid"` strict mirror +
fixture) for any read-back model; planted-violation self-check for any AST/source-scan gate; no
`assert True` tests.

**python-build.md:** `[airflow]` extra stays OUT of the dev group; base install airflow-free; the
comparison core carries no `from airflow` import.

**docs-publishing.md:** document in `docs/airflow-ops.md` (ticket __A8__); new top-level section may
need a `nav:` entry in `mkdocs.yml`.

### Hard technical constraint surfaced in discovery
**`schema_shape_changes` cannot fully come from the two diff sidecars.** Column SET (add/remove) is
derivable from `artifact_id` prefixes; column TYPE changes are not (sidecars carry no types). Options
are folded into the scoping questions (Q4): derive add/remove only (defer retype), persist a small
shape snapshot alongside the sidecar, or defer schema-shape entirely for v0.7.

---

## Scoping questions (Phase 1) — ANSWERED 2026-06-16
- **Q1 Surface form → BOTH.** `detect_drift_against` flag on `SignalForgeGenerateOperator` (ergonomic
  default) PLUS a dedicated `SignalForgeDriftOperator` (explicit/branchable, downstream). Pure
  compare-logic shared between them.
- **Q2 Prior-run source → TEMPLATED PATH.** Operator takes `detect_drift_against: str` = path to the
  prior `diff.json` (Jinja-templatable, e.g. `{{ prev_ds }}`); grade.json sibling auto-located. Each
  run persists its current sidecar to a per-run path so tomorrow's run finds it.
- **Q3 on_drift trigger → ALARM SUBSET.** Trip only on `newly_always_passes` (signal rot) +
  `grade_regressions`. All categories reported; `newly_kept` / `newly_dropped` / `schema_shape_changes`
  are informational and don't page.
- **Q4 schema_shape scope → ADD/REMOVE ONLY.** Derive column add/remove from `artifact_id` prefixes
  across the two sidecars. Defer retype detection (sidecars carry no types). Zero extra persisted state.

### Discovery-confirmed mechanics (architecture-critical)
- **`artifact_id` is a stable cross-run join key** — pure dotted paths, no `run_id` embedded
  (`_common/artifact_id.py`). Column name embedded ⇒ column SET derivable from prefixes (validates Q4).
- **Current DiffReport is always on `result.stdout`** — `run_signalforge` parses it internally
  (`runner.py:64`) but returns only counts. Under the default `write=False`→`--dry-run` there is NO
  on-disk current sidecar, but the JSON is on stdout (#231 DEC-005). ⇒ the drift core takes parsed
  `DiffReport` objects; the current one is re-parsed from stdout, the prior loaded from the path.
- **Persisting "today for tomorrow" reuses the existing fail-closed `diff._sidecar.write_sidecar`** —
  no new fail-closed writer, no new AST scan (honors "no new audit-event class").
- **Logger grep gate does NOT cover `airflow/`** — drift logging follows lazy-format convention for
  hygiene but is not gated.

---

## Architecture Review (Phase 2)

| Area | Rating | Finding |
|---|---|---|
| Security | **pass** | No network/DB/secrets. Reads two local JSON files. The operator-supplied `detect_drift_against` path needs symlink-hardened `canonicalise_path` + a size cap on read (mirror diff's 10 MB / ingest's 5 MB). **Containment anchor is a concern, not a blocker** — the prior path may legitimately sit OUTSIDE `project_dir` (a history dir / object-store mount), so the project-dir containment used elsewhere can't apply verbatim → see DEC. |
| Performance | **pass** | Two JSON parses + O(artifacts) dict join. Trivial. Size cap on the prior-file read guards a hostile/huge file. |
| Data Model | **concern** | New read-back-able `DriftReport` Pydantic model ⇒ needs a `Strict<DriftReport>(extra="forbid")` drift detector + committed fixture. Open: field set, transition-category representation, grade-regression shape + threshold, schema-shape (add/remove) shape, baseline (empty) shape, the two input `blake2b-8` hashes. → refinement DECs. |
| API Design | **concern** | `decide_task_outcome` gains `on_drift`; an exit-0 run can be BOTH flagged AND drifted → precedence rule needed. `compute_drift(...)` signature. Operator params + a SECOND deferred operator class. These are operator KWARGS (not CLI flags) → "5-surface parity" reduces to operator docstring + `airflow-ops.md` + tests + plan DEC (no argparse/SKILL surface). → refinement DECs. |
| Observability | **pass (minor)** | INFO "baseline established" on no-prior; INFO/structured log on drift detected (categories + counts) via lazy-format JSON. Airflow pkg not under the grep gate; follow convention anyway. |
| Testing Strategy | **concern** | `compute_drift` + `DriftReport` + the `decide_task_outcome` extension are airflow-free ⇒ 100% ungated coverage (codecov patch gate). The deferred `SignalForgeDriftOperator` needs an UNGATED skeleton test (placeholder `__init__` raises, `find_spec is None` factory branch, `__getattr__` arm) + a no-eager-import pin. Engineered fixture PAIRS (run N-1, N) — one per transition class. → story design. |
| Drift correctness (project-specific) | **concern** | Join/transition semantics: flagged↔kept transitions, kept-uncertain handling, artifacts present in only one run (test added/removed), model-set mismatch (`model_unique_id` differs), `--no-grade` ⇒ no grade.json ⇒ `grade_regressions` unavailable (degrade, don't fail). These become DECs. |

**No blockers.** Six concerns, all resolvable in refinement (they become DEC-### below). The design rides
the established #231/#232 airflow-free-core + deferred-operator pattern; nothing here requires inventing
a new architectural seam.

---

## Refinement Log (Phase 3) — ANSWERED 2026-06-16
- **on_flagged vs on_drift → MOST-SEVERE WINS.** Independent knobs; the worse verdict governs the
  single task state. Severity rank `FAIL_NO_RETRY (3) > SKIP (2) > SUCCESS (1)` (combination only ever
  happens among these three on an exit-0 run; the 1/2/3 exit tiers short-circuit before either policy).
- **Grade-regression threshold → 0.05 default, operator-tunable** (`grade_regression_threshold`).
- **Comparison-failure → DEGRADE, NEVER FAIL** (baseline / model-mismatch / corrupt prior / `--no-grade`).
- **Delivery → ONE plan, sequenced Ralph stories, merged to `dev`** (epic-#228 convention, no GitHub PR).

---

## Decisions

- **DEC-001 — Surface form: BOTH.** A `detect_drift_against` flag on `SignalForgeGenerateOperator`
  (run + compare in one task — the ergonomic default) AND a dedicated `SignalForgeDriftOperator`
  (reads two sidecars downstream, explicit/branchable). Both wrap the same airflow-free pure core.
- **DEC-002 — Airflow-free pure core in `signalforge/airflow/drift.py`.** Carries NO `from airflow`
  import; eagerly importable; eager-re-exported from `__init__.py` (alongside `result`/`runner`, not
  the lazy `__getattr__`). Per the v0.8 note (`airflow-integration.md`), it's a candidate to hoist to
  a neutral `signalforge.automation` package later; out of scope here.
- **DEC-003 — `compute_drift` pure signature:**
  `compute_drift(*, previous_diff: DiffReport, current_diff: DiffReport, previous_grade: GradingReport | None = None, current_grade: GradingReport | None = None, as_of: date | None = None, grade_regression_threshold: float = 0.05) -> DriftReport`.
  No I/O, no airflow, deterministic.
- **DEC-004 — `DriftReport` model (frozen, `extra="ignore"`, read-back-able).** Fields:
  `schema_version: Literal[1] = 1`, `signalforge_version: str`, `model_unique_id: str`,
  `as_of: date | None`, `grade_regression_threshold: float`, `baseline: bool = False`,
  `previous_diff_hash: str`, `current_diff_hash: str`,
  `newly_always_passes: tuple[DriftArtifact, ...]`, `newly_dropped: tuple[DriftArtifact, ...]`,
  `newly_kept: tuple[DriftArtifact, ...]`, `added_artifacts: tuple[str, ...]`,
  `removed_artifacts: tuple[str, ...]`, `grade_regressions: tuple[GradeRegression, ...]`,
  `schema_shape_changes: SchemaShapeDelta`, `degrade_reason: str | None = None`.
  Computed property `alarming: bool = bool(newly_always_passes) or bool(grade_regressions)` (drives
  `on_drift`). Custom `__repr__` omitting the long lists (mirrors result-model repr-redaction).
  Sub-types: `DriftArtifact{artifact_id, previous_tier: str|None, current_tier: str|None,
  previous_drop_reason: str|None, current_drop_reason: str|None, why: str}` (why truncated);
  `GradeRegression{model_unique_id, previous_mean: float, current_mean: float, delta: float}`;
  `SchemaShapeDelta{columns_added: tuple[str,...], columns_removed: tuple[str,...]}`.
- **DEC-005 — Transition classification (the join).** Over the UNION of `artifact_id`s in both
  reports, classify by `(prior_tier, current_tier)`; "dropped:always-passes" keyed on
  `drop_reason == "always-passes"`:
  - `newly_always_passes` — in BOTH; prior ∈ {kept, kept-uncertain, flagged}; current `dropped`/`always-passes`. **The signal-rot alarm.**
  - `newly_dropped` — in BOTH; prior ∈ {kept, kept-uncertain, flagged}; current `dropped` with a drop_reason ≠ `always-passes`. (Disjoint from `newly_always_passes`.)
  - `newly_kept` — in BOTH; prior `dropped`; current ∈ {kept, kept-uncertain, flagged}.
  - `added_artifacts` / `removed_artifacts` — `artifact_id`s present in only the current / only the prior report (informational; not alarming).
  `flagged` is treated as a kept-ish tier for transition purposes (it ships). Same-tier pairs are no-ops.
- **DEC-006 — `decide_task_outcome` gains `on_drift` + optional `drift`.**
  `decide_task_outcome(result, *, on_flagged: OnFlagged = "fail", on_drift: OnDrift = "fail", drift: DriftReport | None = None) -> TaskOutcome`.
  `OnDrift = Literal["fail","skip","succeed"]`. When `drift is None` → byte-identical to today (every
  #232/#233 caller unchanged; pinned by existing tests). When `drift is not None and drift.alarming` →
  fold `on_drift` and return the MOST-SEVERE of the flagged-outcome and the drift-outcome. **No field
  added to `SignalForgeRunResult`** (its #231 frozen contract + drift detector stay untouched).
- **DEC-007 — Fail-soft airflow-free loaders.** `load_diff_report(path) -> DiffReport | None`,
  `load_grade_report(path) -> GradingReport | None`, `parse_diff_report(stdout) -> DiffReport | None`
  (current report off `run_signalforge` stdout). All fail-soft: absent / corrupt / oversize → `None`
  (degrade), never raise. Symlink-loop-hardened (`_common.path_safety`) + size-capped (10 MB, mirrors
  diff's `existing_schema` cap).
- **DEC-008 — Prior-read path is operator-trusted; symlink-loop-hardened + size-capped, NOT
  project-contained.** The `detect_drift_against` path may legitimately sit outside `project_dir`
  (a history dir / object-store mount). Resolve + loop-guard + size-cap; do NOT enforce project
  containment on the read (it's operator-supplied config, not attacker input, and read is fail-soft).
- **DEC-009 — Persist current run for the next comparison via the EXISTING writer.** When a
  templatable `drift_history_dir` is set on the generate operator, write the current `DiffReport`
  (parsed from stdout) — and the grade.json sibling if present — to
  `<drift_history_dir>/<as_of-or-ds>/diff.json` via the existing fail-closed
  `diff._sidecar.write_sidecar` (containment anchored to `drift_history_dir`). Default
  `drift_history_dir = <project_dir>/.signalforge/history`. **No new fail-closed writer.**
- **DEC-010 — Dedicated `SignalForgeDriftOperator` follows the #232/#233 deferred-class pattern
  verbatim.** Module `__getattr__` + `find_spec("airflow")` branch + `functools.cache` factory +
  airflow-free placeholder (`__init__` raises `ModuleNotFoundError`). Params: `task_id`,
  `previous_diff_path` (templatable, required), `current_diff_path` (templatable, required),
  `previous_grade_path` / `current_grade_path` (optional; auto-sibling), `on_drift`, `as_of`,
  `grade_regression_threshold`. Pure ungated helpers `_build_drift_inputs` / `_validate_drift_config`;
  `execute()` is the thin `# pragma: no cover` wire-up (load both → `compute_drift` → XCom →
  `raise_for_outcome`). `template_fields` = the path params + `as_of`. Runs downstream of a generate
  task configured to persist sidecars.
- **DEC-011 — No new audit-event class, no new fail-closed writer.** `DriftReport` → XCom only;
  current-run persistence reuses `write_sidecar`. Honors the ticket guardrail.
- **DEC-012 — No new error class.** Reuse `AirflowConfigError` (tier 2) for config faults (bad
  `on_drift`/`on_flagged` value, missing required path on the dedicated operator, leading-dash argv
  injection). Comparison degrades do NOT raise (DEC-013). ⇒ no `_EXCEPTION_TO_EXIT_CODE` / scan-7 /
  errors.py-count churn.
- **DEC-013 — Degrade taxonomy (never fail).** No prior file → `baseline=True`, empty report, INFO
  "baseline established", SUCCESS. `model_unique_id` mismatch → empty report +
  `degrade_reason="model mismatch: prior=… current=…"` + WARNING + SUCCESS. Corrupt/unreadable prior →
  empty report + `degrade_reason="prior sidecar unreadable: <ErrClass>"` + WARNING + SUCCESS.
  `--no-grade` (a grade.json absent) → tier-transition drift still computed; `grade_regressions=()`;
  one INFO "grade comparison skipped (no grade sidecar)". A degraded report is never `alarming`.
- **DEC-014 — `as_of` threading + reproducibility.** Surface `as_of` on both operators; thread to
  `compute_drift`; carry on `DriftReport`. Same two sidecars + same `as_of` → byte-identical report.
- **DEC-015 — XCom hygiene.** `DriftReport.to_xcom()` = per-category counts + `alarming` + `as_of`
  (iso) + the two input hashes + the artifact_id lists (truncated `why`) + `degrade_reason`. No bulk
  sidecar text, no secrets.
- **DEC-016 — Determinism.** `compute_drift` iterates `sorted` artifact_ids; transition tuples sorted
  by `artifact_id`; `columns_added/removed` sorted. Two input hashes use the project's `blake2b-8`
  recipe (`model_dump_json(by_alias=True)` → `json.dumps(sort_keys=True, separators=(",",":"))`).
- **DEC-017 — `DriftReport` read-back drift detector.** `StrictDriftReport(extra="forbid")` mirror +
  committed fixture `tests/fixtures/airflow/drift_report_v1.json`. Ungated (the model is airflow-free).
- **DEC-018 — Docs.** `docs/airflow-ops.md` gains a "Drift / signal-rot detection" section (ticket
  __A8__) with the worked nightly-drift-monitor DAG (both surfaces). `airflow-ops.md` is already in the
  mkdocs nav → no nav change.
- **DEC-019 — Example DAG.** `examples/airflow/signalforge_drift_monitor_dag.py` — the generate-flag
  form + the dedicated-operator branchable form. Gated DagBag-parse + `render_template_fields` test in
  `tests/airflow/test_dag_parse.py`.

---

## Detailed Breakdown

> Natural ordering: airflow-free pure core → read-back gate → loaders + task-state extension →
> generate-operator flag → dedicated operator → gated tests/DAGs/docs → Quality Gate → Patterns.
> Validation command for every story: `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — `DriftReport` model + `compute_drift` pure core (airflow-free, ungated, TDD)
- **Description:** Add `signalforge/airflow/drift.py` with `DriftReport` (+ `DriftArtifact`,
  `GradeRegression`, `SchemaShapeDelta`) and the pure `compute_drift(...)`. Eager-re-export from
  `signalforge/airflow/__init__.py`. Commit the engineered sidecar fixture PAIRS used by the tests.
- **Traces to:** DEC-002, 003, 004, 005, 013 (compute side), 014, 015 (`to_xcom`), 016.
- **TDD (tests first):** each transition category (`newly_always_passes` signal-rot; `newly_dropped`
  non-always-passes; `newly_kept`; `added`/`removed`); grade-regression at/above/below 0.05;
  schema add/remove derived from `artifact_id` prefixes; baseline (empty current vs prior) shape;
  `--no-grade` (grades None) → `grade_regressions=()`; model-mismatch `degrade_reason`; determinism
  (sorted, byte-identical on re-run); `alarming` property truth table; `to_xcom` shape + no bulk text.
- **Files:** `src/signalforge/airflow/drift.py` (new), `src/signalforge/airflow/__init__.py` (re-export),
  `tests/airflow/test_drift_core.py` (ungated), `tests/fixtures/airflow/drift_pairs/*.json` (engineered pairs).
- **Done when:** all transition + degrade + determinism tests pass ungated; 100% line coverage of
  `drift.py`; validation green.
- **Depends on:** none.

### US-002 — `DriftReport` schema-stability drift detector + fixture (ungated)
- **Description:** Pin the read-back shape with a `StrictDriftReport(extra="forbid")` mirror validated
  against a committed fixture.
- **Traces to:** DEC-017.
- **Files:** `tests/airflow/test_drift_report_schema.py`, `tests/fixtures/airflow/drift_report_v1.json`.
- **Done when:** adding a field to `DriftReport` without updating the strict mirror OR the fixture fails
  the test loudly; validation green.
- **Depends on:** US-001.

### US-003 — Fail-soft loaders + `decide_task_outcome` `on_drift` extension (airflow-free, ungated, TDD)
- **Description:** Add `load_diff_report` / `load_grade_report` / `parse_diff_report` (fail-soft,
  symlink-loop-hardened, size-capped). Extend `decide_task_outcome` with `on_drift` + optional `drift`
  + the most-severe combine + `OnDrift` literal — byte-identical when `drift is None`.
- **Traces to:** DEC-006, 007, 008, 013 (load side).
- **TDD:** loaders return `None` on absent/corrupt/oversize; symlink-loop guard; `decide_task_outcome`
  flagged×drift most-severe matrix (incl. `drift=None` identity vs every existing case); `on_drift`
  fail/skip/succeed; degraded (`alarming=False`) report never trips.
- **Files:** `src/signalforge/airflow/drift.py` (loaders), `src/signalforge/airflow/result.py`
  (`decide_task_outcome` + `OnDrift`), `tests/airflow/test_drift_loaders.py`,
  `tests/airflow/test_decide_task_outcome.py` (extend existing).
- **Done when:** existing #231/#232/#233 `decide_task_outcome` tests still pass unchanged; new matrix
  green; loaders 100% covered ungated; validation green.
- **Depends on:** US-001.

### US-004 — `detect_drift_against` on `SignalForgeGenerateOperator` + current-run persistence (gated execute, ungated helpers)
- **Description:** Add operator params `detect_drift_against`, `drift_history_dir`, `on_drift`,
  `grade_regression_threshold`; ungated `_build_*`/`_validate_*` helper updates; `execute()` (gated)
  wires: parse current report from stdout → load prior via templated path → `compute_drift` → persist
  current via `write_sidecar` → `decide_task_outcome(..., drift=...)` → `raise_for_outcome`. Push
  `drift_report.to_xcom()` under a `"drift"` key in the operator's XCom. Extend `template_fields`.
- **Traces to:** DEC-001, 009, 011, 012, 015.
- **Files:** `src/signalforge/airflow/operators.py`, `tests/airflow/test_operators_helpers.py` (ungated
  helper tests).
- **Done when:** helper validation + argv tests pass ungated; `detect_drift_against` unset ⇒ generate
  behavior byte-identical to #232 (pinned); validation green. (Gated execute() tests in US-006.)
- **Depends on:** US-001, US-003.

### US-005 — Dedicated `SignalForgeDriftOperator` (deferred class + ungated helpers + UNGATED skeleton test)
- **Description:** Second deferred operator class (factory + `__getattr__` arm + airflow-free
  placeholder), ungated `_build_drift_inputs`/`_validate_drift_config`, gated `# pragma: no cover`
  `execute()`. Lazy re-export + name pin. UNGATED skeleton test (placeholder raises, `find_spec is None`
  factory branch, `__getattr__` arm) — required for the codecov patch gate.
- **Traces to:** DEC-010, 012, 015.
- **Files:** `src/signalforge/airflow/operators.py`, `src/signalforge/airflow/__init__.py`,
  `tests/airflow/test_skeleton.py` (UNGATED, add `SignalForgeDriftOperator` case),
  `tests/airflow/test_airflow_no_eager_import.py` (pin new name).
- **Done when:** `from signalforge.airflow import SignalForgeDriftOperator` is airflow-free; construction
  without airflow raises; skeleton + no-eager-import pins green; helper tests ungated; validation green.
- **Depends on:** US-001, US-003.

### US-006 — Gated execute() tests (both surfaces) + example DAG + docs (gated + docs)
- **Description:** Gated `@pytest.mark.airflow` execute() tests for the generate-flag path and the
  dedicated operator (fake-backed, using the US-001 fixture pairs): assert drift on XCom, `on_drift`→
  task-state via `raise_for_outcome`, baseline path succeeds, degrade paths succeed + WARNING. Add the
  example DAG + DagBag-parse/render_template_fields gated test. Write the `airflow-ops.md` A8 section.
- **Traces to:** DEC-018, 019 (+ exercises DEC-001/010/013/015 end-to-end).
- **Files:** `tests/airflow/test_drift_operators.py` (gated), `tests/airflow/test_dag_parse.py` (extend),
  `examples/airflow/signalforge_drift_monitor_dag.py`, `docs/airflow-ops.md`.
- **Done when:** gated tests pass under `.venv-airflow`; example DAG parses; docs section renders;
  default-suite validation green (gated tests deselected).
- **Depends on:** US-004, US-005.

### US-007 — Quality Gate (code review ×4 + CodeRabbit + airflow certification)
- **Description:** Run the code reviewer 4× across the full changeset, fixing every real bug each pass;
  run CodeRabbit; **certify the airflow-touching paths against the real `.venv-airflow` rig** (#229):
  `SF_RUN_AIRFLOW=1 PYTHONPATH="$PWD/src" /path/to/.venv-airflow/bin/python -m pytest tests/airflow -m airflow --no-cov`.
- **Traces to:** all DECs.
- **Done when:** all four passes clean; CodeRabbit addressed; gated airflow suite green vs Airflow 2.10.4;
  full validation green.
- **Depends on:** US-001…US-006.

### US-008 — Patterns & Memory (priority 99)
- **Description:** Update `.claude/rules/airflow-integration.md` with the drift section (on_drift
  parallels on_flagged via most-severe combine; `compute_drift` airflow-free core; history-persist
  reuses `write_sidecar`; `DriftReport` read-back drift detector; degrade taxonomy). Cross-ref from
  `diff-renderer.md`/`cli-layer.md` as needed. Save a memory note for the run-over-run pattern.
- **Traces to:** all DECs.
- **Files:** `.claude/rules/airflow-integration.md` (orchestrator-edited per worker `.claude/` perms),
  memory.
- **Depends on:** US-007.

### Rules-compliance gate (validated against Discovery constraints)
- airflow-free pure core, no `from airflow` in `drift.py` ✓ (DEC-002); shim-confined raise via
  `raise_for_outcome` ✓; `TaskOutcome` stays 4-valued, no 5th exit tier ✓ (DEC-006); XCom = counts +
  paths + summary, no secrets ✓ (DEC-015); deferred-operator + UNGATED skeleton test ✓ (US-005);
  `.venv-airflow` certification ✓ (US-007); reuse tiers/DropReason, new model not a tier ✓ (DEC-004/005);
  reproducibility via two `blake2b-8` input hashes + sorted iteration ✓ (DEC-016); no new fail-closed
  writer / audit class ✓ (DEC-011); no new error class / scan-7 untouched ✓ (DEC-012); read-back drift
  detector + fixture ✓ (DEC-017); `--as-of` carve-out surfaced ✓ (DEC-014); docs in airflow-ops.md ✓
  (DEC-018); `[airflow]` extra stays out of dev group (unchanged) ✓.

---

## Beads Manifest (Phase 7) — pending devolve
