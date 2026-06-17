# Super Plan — #236: Airflow test harness + gated live e2e DAG + ops docs + example DAGs

## Meta

- **Ticket:** [#236](https://github.com/wjduenow/SignalForge/issues/236) — Airflow: test harness + gated live e2e DAG + ops docs + example DAGs
- **Epic:** [#228](https://github.com/wjduenow/SignalForge/issues/228) (v0.7 Apache Airflow integration) — **this is the closing test+docs child** (modeled on Snowflake/Databricks closers #124/#226)
- **Branch:** `feature/236-airflow-e2e-docs`
- **Worktree:** `../worktrees/SignalForge/236-airflow-e2e-docs`
- **Phase:** Complete (2026-06-16) — PR [#246](https://github.com/wjduenow/SignalForge/pull/246) squash-merged to `dev` (`39b5893`); epic #228 closed. All 8 stories + Quality Gate (4 diverse-angle reviews; live-e2e AIRFLOW_HOME isolation fixed) + Patterns & Memory landed; certified vs Airflow 2.10.4 (77 gated offline tests). Maintainer-only live `dag.test()` cert tracked in [#247](https://github.com/wjduenow/SignalForge/issues/247).
- **Sessions:** 1 (2026-06-16)
- **PR:** [#246](https://github.com/wjduenow/SignalForge/pull/246) (merged, base `dev`)
- **Depends on:** #229–#235 — ALL merged to `dev` (the gitStatus snapshot was stale; #235 landed as `678da32`/PR #245)

---

## The headline finding: ~80% already shipped

Every dependency (#229–#235) already shipped its slice of the test suite, the example DAGs, the ops doc, the CI job, and the public API. **#236 is a consolidation + certification + net-new-gap-fill ticket, NOT a from-scratch build.** Scoping it to the genuine gaps is the central planning act — the risk is a thin wrapper that re-does done work.

### What already exists on `dev` (do NOT rebuild)

| Area | Status | Evidence |
|---|---|---|
| `src/signalforge/airflow/` | SHIPPED | 9 modules, 3,804 lines, 16 public names (operators ×3, hook, drift, result core, runner, errors); zero-eager-import + one-shim design |
| `tests/airflow/` | SHIPPED | 16 files, ~324 test fns; gated `@pytest.mark.airflow` + `importorskip`; ungated skeleton/confinement/no-eager-import gates; runnable as `uv run pytest -m airflow --no-cov` |
| DAG-parse gate | SHIPPED | `test_dag_parse.py::test_example_dag_parses_without_import_errors` — `DagBag(examples/airflow, include_examples=False)`, asserts `import_errors == {}` |
| `docs/airflow-ops.md` | SHIPPED (1 gap) | 877 lines: install, config, exit→TaskOutcome table, both operators' param refs, hook/credentials, drift contract, XCom shape, worked walkthroughs. **Missing: managed-runtime note** |
| `mkdocs.yml` nav | SHIPPED | Line 61 `- Airflow Integration: airflow-ops.md` |
| CHANGELOG `[Unreleased]` | SHIPPED | Entries for #229–#235 |
| CI airflow job | SHIPPED | Label-gated (`airflow` label or `workflow_dispatch`), matrix `2.10.4`/py3.11, runs `-m airflow --no-cov`; offline leg only (live self-skips) |
| `.venv-airflow` rig | SHIPPED | Provisioned py3.11 venv at repo root; PYTHONPATH-shadow certification path |
| Skill parity | SHIPPED | SKILL.md has zero Airflow mentions; parity gate has no airflow token (correct — Airflow is not a CLI subcommand) |

### Genuine gaps / net-new for #236

1. **`signalforge_after_dbt_build.py`** example DAG — prune-existing downstream of a dbt `run`/`build` task. **No equivalent exists.** Genuinely missing.
2. **`signalforge_nightly_drift.py`** — `signalforge_drift_monitor_dag.py` (#235) already exists and is near-equivalent. **Naming-reconciliation decision** (see DEC-002).
3. **Upgraded live e2e** — `test_generate_task_runs_live_against_demo` exists but uses `copy_demo` (init-demo) + a `PythonOperator` DAG. #236's body specifies the **Austin bikeshare source-as-model fixture**, the real **`SignalForgeGenerateOperator`**, and **`dag.test()`**. **Decision** on upgrade-vs-add (see DEC-001).
4. **Managed-runtime note** (Astronomer / MWAA / Composer) — absent from `docs/airflow-ops.md`. Doc-only, "document, don't certify."
5. **README roadmap flip** — move the v0.7 Airflow row from the `Planned:` table to a shipped row (release-date set at v0.7.0 release time).
6. **CHANGELOG rollup** — closing entry for #236.
7. **SKILL.md decision** — add an Airflow pointer or record the deliberate omission (see DEC-003).
8. **DAG-parse gate extension** — cover any newly-added example DAG.
9. **Docs coherence audit** — ensure `airflow-ops.md` reads as one reference, not five appended sections; managed-runtime note slotted in.
10. **Live-debugging certification pass** — maintainer-run against `.venv-airflow` + live creds; file survivors into a follow-up issue (the #124 live-payoff lesson).

---

## Discovery

### Codebase scout (gap analysis) — summary
- Test suite, operators, hook, drift, ops doc, mkdocs nav, CI job, `.venv-airflow` rig: **all SHIPPED**.
- PARTIAL: `examples/airflow/` (5 DAGs present; 2 named-in-ticket DAGs absent); README v0.7 row still "Planned".
- MISSING: managed-runtime note in ops doc; `signalforge_after_dbt_build.py`; an Austin-fixture `dag.test()` live e2e using the real operator.

### Convention checker — load-bearing constraints
- **No `workflow-project.md`** exists — dispersed rule files govern.
- **`airflow-integration.md`:** two-layer split (airflow-free core + shim-confined `_airflow_compat`); no 5th `TaskOutcome`; deferred-class operator pattern; **pure/gated structural split for codecov** (decision logic in ungated helpers; `execute()` gated + `# pragma: no cover`); **UNGATED skeleton test for every deferred-class operator**; example DAGs need gated parse + `render_template_fields` tests; **certify against `.venv-airflow` BEFORE close**.
- **`testing-signal.md`:** belt-and-suspenders gating (marker + runtime `_skip_reason()`); **source-as-model alias trick + natural-NOT-NULL always-pass column** (Austin `trip_id`); `tmp_path` isolation so the committed fixture isn't polluted; engineered-determinism so the always-pass drop is mathematically guaranteed; planted-violation self-check for any new source-scan gate.
- **`docs-publishing.md`:** new user-facing ops doc requires a `nav:` entry (already present); `docs-build` (every PR) + `docs` (main-only deploy) both gate; non-strict local build (internal links to `plans/`, `.claude/rules/` are fine); no dependency-group change needed.
- **`python-build.md`:** `[airflow]` extra stays OUT of the dev group (base install Airflow-free); example DAGs ship via the Hatch `include` for `examples/`? — **verify wheel packaging** (examples may be repo-only, not wheel-shipped).
- **`ci-supply-chain.md`:** `airflow` marker registered in `markers` + excluded in `addopts`; gated CI job is label-gated only, never default matrix.
- **`cli-layer.md`:** logger grep gate already scans `signalforge.airflow`; `AirflowIntegrationError` is excluded-only in the exit-code AST scan.
- **`skill-parity.md`:** SKILL.md is a CLI-parity surface; Airflow is a library/operator surface, so the gate won't force it — the omission is defensible but must be made **explicit**.

### Ticket analyst — epic end-state + ambiguities
- Epic #228 end-state: first-class Airflow integration (v0.7); core install never gains an Airflow dep; operators reuse the four-tier exit taxonomy; `.signalforge/diff.json` sidecar is the XCom contract; secrets never logged; in-process `cli.main(argv)` default invocation. **All 7 deps closed/merged.**
- CI airflow job CONFIRMED present (label-gated; offline leg runs in CI, live leg maintainer-only).
- 6 genuine ambiguities surfaced → distilled into the scoping questions + DECs below.

---

## Architecture Review (Phase 2)

| Area | Rating | Finding |
|---|---|---|
| Live e2e feasibility | **BLOCKER (resolved → DEC-001)** | `dag.test()` requires an initialized Airflow metadata DB (`airflow db migrate`); the existing live test sidesteps it via `python_callable()`. Austin fixture + `.venv-airflow` (airflow 2.10.4 + BQ + Anthropic SDKs) confirmed ready; natural-NOT-NULL `trip_id`/`start_time` give the guaranteed always-pass drop. Resolved: `dag.test()` + `AIRFLOW_HOME=tmp_path` + `airflow db migrate`. |
| Single-model target | **CONCERN (resolved → DEC-001)** | `signalforge_generate_operator_dag.py` uses `--select`, not `--model`. Resolved: live e2e builds its OWN inline DAG with explicit `model="models/staging/stg_bikeshare_trips.sql"` rather than reusing the example DAG or mutating the fixture. |
| Rename blast-radius | **PASS** | 5 reference sites, no hidden coupling. DagBag scans by filesystem path but stores by `dag_id`; the one test call (`test_dag_parse.py:188`) + docs/rules prose are the only correctness-bearing refs. `CHANGELOG.md` clean; other DAGs' local `drift_monitor` task vars are independent. |
| Packaging | **PASS** | `examples/` is NOT wheel-shipped (`pyproject.toml` `packages`/`include` cover only `src/signalforge`, `_demo`, `skills`). No `wheel_smoke` assertion touches `examples/`. Example DAGs are filesystem refs loaded by DagBag, never package data → rename/add has zero packaging consequence. |
| Docs coherence | **PASS** | `airflow-ops.md` (877 lines) is one coherent reference (install → contract → operators → credentials → scheduling → running → testing → caveats). Managed-runtime note slots near "Running it" (L819) before "Testing". `airflow-test-environment.md` exists + already linked (test infra only — link for cert recipe, NOT deployment guidance). Astronomer/MWAA/Composer genuinely absent. |
| Security / Observability / Data-model / API | **PASS (n/a)** | No new endpoints, schema, secrets, or audit surface — all shipped + governed by #231–#234. Logger grep gate already scans `signalforge.airflow`; `AirflowIntegrationError` already in exit-code taxonomy. |

---

## Refinement Log (Phase 3)

### Decisions

- **DEC-001 — Live e2e: upgrade in place via `dag.test()` + tmp metadata DB.** Replace `test_generate_task_runs_live_against_demo` (init-demo + `PythonOperator` + `python_callable()`) with one canonical live test that: copies the **Austin bikeshare fixture** to `tmp_path` (`copy_fixture_to_tmp`); builds an **inline DAG** with the real `SignalForgeGenerateOperator(task_id="generate", project_dir=<tmp austin>, model="models/staging/stg_bikeshare_trips.sql", write=False)`; sets `AIRFLOW_HOME=tmp_path/af` + runs `airflow db migrate` (idempotent) in setup; runs `dag.test()`; asserts task success + XCom tier counts via `ti.xcom_pull()` + **≥1 `always-passes` drop** (mathematically guaranteed by `not_null` on natural-NOT-NULL `trip_id`/`start_time`). Gating: `@pytest.mark.airflow` + e2e/anthropic/bigquery markers + runtime `_live_skip_reason()` over `SF_RUN_AIRFLOW` + `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT` + `SF_RUN_BQ`. *Rationale:* faithful to the ticket's explicit `dag.test()` wording; the live leg is maintainer-run only (self-skips in CI), so the DB-init cost is acceptable; inline DAG sidesteps the `--select`-vs-`--model` example-DAG mismatch.
- **DEC-002 — Example DAGs: rename + add.** Rename `examples/airflow/signalforge_drift_monitor_dag.py` → `signalforge_nightly_drift.py` and `dag_id` `signalforge_drift_monitor` → `signalforge_nightly_drift`. Update the correctness-bearing ref (`tests/airflow/test_dag_parse.py:188`) + docs prose (`docs/airflow-ops.md:642,847`). **`.claude/rules/airflow-integration.md:103,126` is ORCHESTRATOR-ONLY** (workers can't write `.claude/`). Leave `plans/super/235-drift-detection.md` historical. ADD `signalforge_after_dbt_build.py` — `SignalForgePruneExistingOperator` downstream of an upstream dbt `run`/`build` task (an `EmptyOperator`/`BashOperator` stand-in labelled to show the Cosmos/dbt-operator-adjacent shape WITHOUT depending on Cosmos). *Rationale:* matches the ticket's named DAGs literally; the post-dbt-build shape is genuinely absent.
- **DEC-003 — SKILL.md: add a brief Airflow pointer.** Add a short "Scheduled runs (Airflow)" pointer to `src/signalforge/skills/signalforge/SKILL.md` referencing `docs/airflow-ops.md`. Confirm `tests/cli/test_skill_cli_parity.py` stays green (introduce NO new CLI-subcommand/flag tokens — the parity gate scans for those). *Rationale:* user chose discoverability; Airflow is a library/operator surface so the gate doesn't force it, but a pointer helps operators driving Claude Code.
- **DEC-004 — Scope: net-new gaps only.** #236 does NOT re-author the ~80% already shipped by #229–#235. Stories cover only: the rename+add (DEC-002), the live-e2e upgrade (DEC-001), the managed-runtime note (DEC-005), the SKILL pointer (DEC-003), the README flip + CHANGELOG rollup, a docs-coherence pass, the Quality Gate, and Patterns & Memory.
- **DEC-005 — Managed-runtime note: doc-only subsection, "documented not certified."** Add a subsection (Astronomer / MWAA / Composer) near `## Running it` (before `## Testing`) in `docs/airflow-ops.md`: state the `[airflow]`-extra + constraints-pinning caveat under managed schedulers and where secrets live (the `SignalForgeHook`/Connection path from #234), explicitly "documented, not certified." Link `airflow-test-environment.md` ONLY for the test/version recipe, NOT as deployment guidance.
- **DEC-006 — README roadmap flip: reword the v0.7 Planned row, defer the dated Released-table migration to the v0.7.0 release.** #236 reworded the v0.7 `Planned:` row to enumerate the actual shipped scope (operators ×3 + hook + drift + ops docs) and note it's landed on `dev` pending the v0.7.0 release. The Planned→Released table move (with a release date) is a release-manager action at v0.7.0 cut, NOT a #236 action. *(Flagged for user confirmation at PR review.)*

### Orchestrator-only handling (load-bearing)

Per `ralph-worker-claude-dir-perms`: Ralph workers cannot write `.claude/` in worktrees. **The orchestrator** performs the `.claude/rules/airflow-integration.md` edits — the DEC-002 rename refs (L103, L126) AND the Patterns & Memory updates (US-008). Stories that name `.claude/rules/` edits flag them as orchestrator-handled; the worker does everything else in the same story.

### Certification (load-bearing)

Gated `airflow`-marked tests are DESELECTED in the default suite, so a worker's `uv run pytest` cannot exercise them (no airflow in the default env; `tests/airflow` also excluded from pyright). Every airflow-touching story's AC therefore splits: (a) **default validation green offline** (`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` — deselects `airflow`, must stay green incl. the no-eager-import gates), AND (b) **orchestrator certifies the gated suite** against the rig: `SF_RUN_AIRFLOW=1 PYTHONPATH="$PWD/src" /home/wesd/Projects/SignalForge/.venv-airflow/bin/python -m pytest tests/airflow -m airflow --no-cov` (offline gated tests). The **live** leg (`dag.test()`) additionally needs `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT` + `SF_RUN_BQ` and is part of the Quality Gate live-debugging pass (DEC-001 + the #124 budget-a-live-pass lesson; file survivors into a follow-up issue).

---

## Detailed Breakdown (Phase 4)

**Validation command (every story):** `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` (default suite, deselects `airflow`). Airflow-touching stories additionally require orchestrator certification per the Certification note above.

### US-001 — Rename drift-monitor example DAG → `signalforge_nightly_drift`
- **Description:** Rename the #235 drift example DAG file + `dag_id` to the ticket's headline name, updating every correctness- and prose-bearing reference.
- **Traces to:** DEC-002.
- **Files:**
  - `examples/airflow/signalforge_drift_monitor_dag.py` → **rename** to `examples/airflow/signalforge_nightly_drift.py`; change `dag_id="signalforge_drift_monitor"` → `"signalforge_nightly_drift"` (keep internal task_ids `drift_monitor_ergonomic`/`generate`/`drift_check` unchanged).
  - `tests/airflow/test_dag_parse.py` — L188 `_load_example_dag("signalforge_drift_monitor")` → `"signalforge_nightly_drift"`; optionally rename the test fn for consistency (L177).
  - `docs/airflow-ops.md` — L642, L847 update name + dag_id.
  - **`.claude/rules/airflow-integration.md` L103, L126 — ORCHESTRATOR-ONLY** (worker leaves untouched; orchestrator edits post-merge / in QG).
  - `CHANGELOG.md` `[Unreleased]` — one-line note of the rename.
- **AC:** Default validation green; the renamed file parses under the gated DagBag test (orchestrator-certified); no remaining `signalforge_drift_monitor` reference outside `plans/super/235-*` (historical, left intact); `.claude/rules/` ref updated by orchestrator.
- **Done when:** `rg "signalforge_drift_monitor"` returns only `plans/super/235-drift-detection.md`.
- **Depends on:** none.

### US-002 — New example DAG `signalforge_after_dbt_build.py` + parse/template tests
- **Description:** Ship the genuinely-missing post-dbt-build example: `SignalForgePruneExistingOperator` downstream of an upstream dbt `run`/`build` task (a `BashOperator`/`EmptyOperator` stand-in, no Cosmos dep), with a DagBag parse test + `render_template_fields` test.
- **Traces to:** DEC-002.
- **Files:**
  - `examples/airflow/signalforge_after_dbt_build.py` — NEW. `dag_id="signalforge_after_dbt_build"`; upstream `dbt_build` (BashOperator running `dbt build` as illustrative, or EmptyOperator with a comment) `>>` `prune` (`SignalForgePruneExistingOperator`, `schema=`/`model=` templated, read-only). Mirror the env/Variable config pattern + module docstring of the existing example DAGs.
  - `tests/airflow/test_dag_parse.py` — add `test_after_dbt_build_example_dag_parses_without_import_errors` (DagBag, zero import_errors, dag_id present, expected task_ids + upstream→downstream edge) + a `render_template_fields` assertion (gated).
- **AC:** Default validation green (no eager airflow import; the new test self-skips offline); gated parse + template tests green under orchestrator certification; DAG follows the source-as-model/read-only conventions (no Anthropic key needed — prune-existing is no-LLM).
- **Done when:** `DagBag(examples/airflow)` parses all 6 example DAGs with zero import errors (orchestrator-certified).
- **Depends on:** none (independent of US-001; both touch `test_dag_parse.py` — sequence US-002 after US-001 to avoid a merge collision on that file).

### US-003 — Upgrade live e2e to real operator + `dag.test()` on the Austin fixture
- **Description:** Replace `test_generate_task_runs_live_against_demo` with the Austin-fixture + real-`SignalForgeGenerateOperator` + `dag.test()` end-to-end live test per DEC-001.
- **Traces to:** DEC-001.
- **Files:**
  - `tests/airflow/test_dag_parse.py` (or a new `tests/airflow/test_e2e_generate_operator.py` — keep with the other gated airflow e2e) — remove/replace the demo-based test; add the Austin-fixture test: `copy_fixture_to_tmp(_AUSTIN_FIXTURE, tmp_path)`; `monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path/"af"))`; `airflow db migrate` (subprocess or `airflow.utils.db.initdb`, idempotent) in setup; build inline DAG with `SignalForgeGenerateOperator(task_id="generate", project_dir=<tmp>, model="models/staging/stg_bikeshare_trips.sql", write=False, on_flagged="succeed")`; `dag.test()`; assert task success + `ti.xcom_pull()` tier-count keys (`kept`/`kept_uncertain`/`dropped`/`flagged`) + `dropped >= 1` (always-passes).
  - Reuse `tests/cli/_e2e_helpers.py::copy_fixture_to_tmp` + the `_AUSTIN_FIXTURE` path; reuse/extend `_live_skip_reason()` (5-var gate: `SF_RUN_AIRFLOW` + `ANTHROPIC_API_KEY` + `GOOGLE_CLOUD_PROJECT` + `SF_RUN_BQ`).
- **AC:** Default validation green offline (test self-skips with a clear reason when env unset; no eager airflow import). Live leg certified by orchestrator/maintainer against `.venv-airflow` + creds: `dag.test()` completes, task succeeds, XCom carries non-negative tier counts, ≥1 `always-passes` drop. `tmp_path` isolation — committed Austin fixture unmodified.
- **Done when:** the gated live test passes against the rig with real creds (recorded in the closeout); offline default suite green.
- **Depends on:** US-002 (shared `test_dag_parse.py` edits — sequence to avoid collision; if US-003 lands the test in a new file, dependency relaxes to none).

### US-004 — Managed-runtime note + docs coherence pass in `docs/airflow-ops.md`
- **Description:** Add the Astronomer/MWAA/Composer "documented, not certified" subsection and a light coherence pass so the doc reads as one reference.
- **Traces to:** DEC-005.
- **Files:** `docs/airflow-ops.md` — new subsection near `## Running it` (before `## Testing`); managed-runtime caveat (constraints-pinning under managed schedulers, secrets via `SignalForgeHook`/Connection per #234, explicitly not certified); link `docs/research/airflow-test-environment.md` for the test/version recipe only. Light coherence sweep (the rename from US-001 already reflected; ensure operator sections cross-link).
- **AC:** Default validation green; `uv run --only-group docs mkdocs build` clean (no new broken nav/links — non-strict); the note explicitly says "not certified"; no Astronomer/MWAA/Composer support claim.
- **Done when:** `mkdocs build` is clean and the managed-runtime subsection renders under the nav's Airflow Integration page.
- **Depends on:** US-001 (so the rename's doc edits don't collide).

### US-005 — SKILL.md Airflow pointer (parity-gate-safe)
- **Description:** Add a brief Airflow pointer to the bundled skill, keeping the CLI-parity gate green.
- **Traces to:** DEC-003.
- **Files:** `src/signalforge/skills/signalforge/SKILL.md` — short "Scheduled runs (Airflow)" section pointing at `docs/airflow-ops.md`; introduce NO new CLI subcommand/flag tokens.
- **AC:** Default validation green incl. `tests/cli/test_skill_cli_parity.py` (the pointer adds prose, not CLI tokens, so the gate stays green); the wheel-shipped skill still installs (skill is package data — `wheel_smoke` unaffected by content edit).
- **Done when:** parity gate green; SKILL.md names the Airflow ops-doc path.
- **Depends on:** none.

### US-006 — README roadmap flip + CHANGELOG rollup
- **Description:** Reword the v0.7 Planned row to the shipped Airflow scope; add the #236 closing CHANGELOG entry.
- **Traces to:** DEC-006, DEC-004.
- **Files:**
  - `README.md` — reword the v0.7 `Planned:` row to enumerate operators ×3 + hook + drift + ops docs (landed on `dev`, pending v0.7.0 release). Do NOT migrate to the dated Released table (release-manager's job at release).
  - `CHANGELOG.md` `[Unreleased]` — closing #236 entry (test harness consolidation + live e2e + example DAGs + ops docs/managed-runtime note + README flip), referencing epic #228 close.
- **AC:** Default validation green; README v0.7 wording reflects shipped scope; CHANGELOG entry present and consistent with prior #229–#235 entries.
- **Done when:** README + CHANGELOG land; user confirms the README phrasing at PR review (DEC-006 flag).
- **Depends on:** none.

### US-007 — Quality Gate (code review ×4 + CodeRabbit + live certification pass)
- **Description:** Run the code reviewer 4 times across the full changeset (fixing real bugs each pass), run CodeRabbit, ensure default validation passes, AND run the maintainer live-debugging certification pass against `.venv-airflow` + creds (the #124 budget-a-live-pass lesson) — filing survivors into a follow-up issue.
- **Traces to:** all DECs; the epic's "green offline + certified live" acceptance.
- **AC:** Default validation green; 4 reviewer passes + CodeRabbit clean; orchestrator-run `SF_RUN_AIRFLOW=1 PYTHONPATH=... .venv-airflow/bin/python -m pytest tests/airflow -m airflow --no-cov` green (offline gated); live `dag.test()` leg certified with creds; any residual Airflow-version/XCom-backend/templated-field survivors filed as a follow-up issue.
- **Done when:** all reviews clean, validation green, live cert recorded, follow-up filed (if any).
- **Depends on:** US-001 … US-006.

### US-008 — Patterns & Memory (priority 99)
- **Description:** Capture #236's durable lessons. **Orchestrator-handled `.claude/rules/` edits.**
- **Traces to:** all DECs.
- **Files (orchestrator-only for `.claude/`):**
  - `.claude/rules/airflow-integration.md` — note #236 as the epic closer; the `dag.test()`-needs-`db migrate` live-e2e recipe; the rename (already applied in US-001's orchestrator edit); managed-runtime "documented not certified" stance.
  - Memory: update `signalforge-airflow-*` notes / MEMORY.md with the `dag.test()` metadata-DB gotcha + the epic-closed state.
  - `docs/` as needed.
- **AC:** Rules/docs/memory reflect the new patterns; default validation green.
- **Done when:** patterns captured; epic #228 closeout reflected.
- **Depends on:** US-007.

### Rules-compliance gate (Phase 4 self-check)
- Gated marker + belt-and-suspenders skip (US-002, US-003) ✓ `testing-signal.md` / `airflow-integration.md`.
- Source-as-model natural-NOT-NULL always-pass + `tmp_path` isolation (US-003) ✓ `testing-signal.md`.
- `[airflow]` extra stays out of dev group; examples not wheel-shipped (no `pyproject.toml` dep change) ✓ `python-build.md`.
- New ops-doc content keeps `nav:` valid; `docs-build` gate green (US-004) ✓ `docs-publishing.md`.
- No eager airflow import; one-shim confinement untouched (all stories) ✓ `airflow-integration.md`.
- SKILL parity gate green; no new CLI tokens (US-005) ✓ `skill-parity.md`.
- `.claude/rules/` edits orchestrator-only (US-001 ref, US-008) ✓ `ralph-worker-claude-dir-perms`.

---

## Beads Manifest (Phase 7)

- **Epic:** `bd_1-scaffolding-5j1`
- **Worktree:** `../worktrees/SignalForge/236-airflow-e2e-docs` (branch `feature/236-airflow-e2e-docs`)
- **Tasks:**
  | Bead | Story | Depends on |
  |---|---|---|
  | `bd_1-scaffolding-5j1.1` | US-001 Rename drift-monitor → nightly_drift | — |
  | `bd_1-scaffolding-5j1.2` | US-002 New `after_dbt_build` DAG + tests | .1 |
  | `bd_1-scaffolding-5j1.3` | US-003 Live e2e upgrade (`dag.test()` + Austin) | .2 |
  | `bd_1-scaffolding-5j1.4` | US-004 Managed-runtime note + docs coherence | .1 |
  | `bd_1-scaffolding-5j1.5` | US-005 SKILL.md Airflow pointer | — |
  | `bd_1-scaffolding-5j1.6` | US-006 README reword + CHANGELOG | — |
  | `bd_1-scaffolding-5j1.7` | US-007 Quality Gate + live cert pass | .1–.6 |
  | `bd_1-scaffolding-5j1.8` | US-008 Patterns & Memory (orchestrator `.claude/`) | .7 |
- **Ready at devolve:** US-001, US-005, US-006.
- **Orchestrator-only beads/edits:** `.claude/rules/airflow-integration.md` in US-001 (rename refs) + US-008 (Patterns & Memory) — workers cannot write `.claude/`.
