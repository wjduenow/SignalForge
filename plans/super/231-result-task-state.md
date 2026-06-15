# Super Plan — #231: Airflow result → task-state mapping + XCom contract

## Meta

- **Ticket:** [#231](https://github.com/wjduenow/SignalForge/issues/231)
- **Epic:** [#228](https://github.com/wjduenow/SignalForge/issues/228) (Airflow integration, v0.7)
- **Depends on:** #230 (skeleton — CLOSED), #229 (local + CI test env — landed)
- **Branch:** `feature/231-result-task-state`
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/231-result-task-state`
- **Phase:** detailing
- **Sessions:** 1 (2026-06-15)

### Beads Manifest

_(filled on devolve)_

---

## Discovery

### What / Why

This is the **core "act on the graded diff" contract** of epic #228 — the shared
logic every feature operator (#GenerateOperator, #PruneExistingOperator,
drift child) AND the planned v0.8 GitHub Action reuse. It defines how a
SignalForge run's outcome becomes:

1. an Airflow **task state** (via the four-tier CLI exit-code taxonomy), and
2. an **XCom** payload (tier counts + sidecar paths, projected from the existing
   `.signalforge/diff.json` / `grade.json` sidecars).

The example DAG shipped in #229 (`examples/airflow/signalforge_generate_dag.py`)
is the *informal* version of this contract (a `PythonOperator` shelling out to
the CLI, mapping exit code → `AirflowFailException`, projecting tier counts →
XCom). This ticket **formalises and unit-tests it** so the dedicated operators
are a drop-in swap.

### Acceptance (from the ticket)

- A unit-tested **`run_signalforge(...) -> SignalForgeRunResult`** helper —
  **Airflow-free**, testable without an Airflow install — that takes argv +
  project dir, invokes the pipeline, parses the sidecar, returns the typed
  result.
- An **Airflow-side adapter** that turns result + exit code into task state per
  the four-tier table.
- **Pinned tests** for each exit-code → outcome row and the `on_flagged`
  branches.

### Codebase findings (file:line precedent)

- **`signalforge.cli.main(argv) -> int`** (`cli/__init__.py:93`) — the in-process
  reuse seam. Returns the four-tier exit code; catches `SystemExit` from argparse;
  installs `sys.excepthook = _safe_excepthook` (global mutation, no restore) unless
  `--verbose`. `cmd_generate` mutates `os.environ` (`NO_COLOR`, `DBT_PROFILES_DIR`)
  and does **not** restore (cli-layer.md DEC-023, "one-process-per-invocation").
- **`cmd_generate` writes the rendered diff to stdout** (`sys.stdout.write`) and
  progress/footer to stderr. In-process capture needs `redirect_stdout/stderr`.
- **Diff sidecar = `DiffReport.model_dump_json(by_alias=True)`** to
  `<project>/.signalforge/diff.json` (`diff/_renderers.py:1084` JsonRenderer;
  `diff/_sidecar.py`). Keys: `model_unique_id`, `run_id`, `duration_seconds`,
  `schema_version`, `audit_schema_version` (>=2 for the four-tier taxonomy),
  `kept_count`, `kept_uncertain_count`, `dropped_count`, `flagged_count`,
  `entries`, `proposed_test_files`, three repro hashes, + large YAML/diff text.
- **Grade sidecar = `GradingReport.model_dump_json`** to `grade.json`
  (`grade/audit.py:306` `write_grading_report`). Keys incl. `model_unique_id`,
  `pass_rate`, `mean_score`, `passed`, `aggregate_complete`, `thresholds`,
  `duration_seconds`. `passed` = aggregate threshold verdict (grade-layer.md).
- **`GradeBelowThresholdError` → exit tier 2** (`cli/_helpers.py:391`), but only
  raised when `grade.fail_on_below_threshold=true` (default `false`). **By
  default a flagged run exits 0** and surfaces flagged artifacts only via
  `diff.json.flagged_count > 0`.
- **Airflow skeleton (#230):** `signalforge.airflow` is airflow-free except the
  one shim `_airflow_compat.py` (sole home for `from airflow ...`, lazy factories,
  `# pragma: no cover`). `__init__.py` uses PEP 562 lazy `__getattr__` for
  operator/hook names; errors are eager re-exports. `[airflow]` extra is NOT in
  the dev group. Three UNGATED gate tests + gated `airflow`-marker DAG tests.
- **No `run_signalforge` / `SignalForgeRunResult` / `decide_task_outcome` exists
  yet** — greenfield within `signalforge.airflow`.

### Rules consulted (`.claude/rules/`)

- **cli-layer.md** — four-tier exit-code taxonomy (DO NOT invent a fifth / collapse
  2 & 3); `main(argv)` reuse; no-traceback panic path; 7th AST scan (every typed
  error mapped); 5-surface parity for new flags; `os.environ` mutate-don't-restore.
- **diff-renderer.md** — sidecar = `model_dump_json(by_alias=True)`,
  `audit_schema_version >= 2`; sidecar IS the contract (don't mint a parallel format).
- **grade-layer.md** — `passed` / `mean_score` / `aggregate_complete`;
  `GradeBelowThresholdError` semantics; degrade-not-crash posture.
- **testing-signal.md** — no `assert True` tests; gated markers + runtime skip;
  planted-violation self-checks; engineered determinism; "signal over volume".
- **python-build.md** — `[airflow]` extra stays out of dev group; wheel never
  vendors airflow.
- **Memories:** [[signalforge-airflow-skeleton-pattern]] (#230 wiring),
  [[signalforge-airflow-local-e2e]] (#229 env + the shipped example DAG).

### Key tensions surfaced (drive the scoping questions)

1. **`on_flagged` keys on the SIDECAR, not the exit code.** A flagged run exits 0
   by default (`fail_on_below_threshold=false`). So "fail/branch on flagged" must
   be a sidecar-count decision layered on a successful run — NOT a tier-2 mapping.
   Conflating tier-2 (which also carries hard `ModelNotFoundError` / anchor-contract
   failures) with "reviewable flagged" would mis-route hard errors to the review
   branch. → **Decision: run with `fail_on_below_threshold` effectively off inside
   the helper; detect flagged via `flagged_count` / `below_threshold`; tier 2 always
   = hard input error → fail.**

2. **Decision-table vs airflow-exception translation split.** The acceptance wants
   the mapping unit-tested WITHOUT airflow. So the *decision* (`exit_code` + result
   + `on_flagged` → a neutral `TaskOutcome` enum) must be airflow-free and pure;
   a thin airflow-side translator turns `TaskOutcome` → the real
   `AirflowSkipException` / `AirflowFailException` / `AirflowException`. Mirrors the
   codebase's "neutral typed discriminator, not vendor-shaped" philosophy
   (`ExceptionCategory`). The translator's `from airflow.exceptions import ...`
   lives in the one shim (`_airflow_compat`).

3. **`--select` batch sidecar last-writer-wins.** Sidecars are overwritten per
   model; a batch's `diff.json` reflects only the LAST model. So a single
   `run_signalforge` call can't reconstruct accurate per-model batch counts from
   the sidecar alone. → scope question below.

4. **In-process global side effects in a long-lived worker.** `main()` mutates
   `sys.excepthook` and `os.environ` without restoring. An Airflow worker runs many
   tasks in one process. → save/restore around the in-process call? (refinement.)

5. **Where the helper lives + eager-import safety.** `run_signalforge` /
   `SignalForgeRunResult` / `decide_task_outcome` are airflow-free → new module(s)
   under `signalforge.airflow` (e.g. `runner.py` + `result.py`), eagerly importable,
   must not import airflow (gate). The translator is the only airflow-touching piece.

---

## Decisions (DEC log)

**Scoping answers (session 1):**

- **DEC-001 — `on_flagged` keys on `flagged_count > 0`; default `"fail"`.**
  `below_threshold` is derived from the diff JSON's `flagged_count > 0` (the
  per-test below-threshold tier). `on_flagged: Literal["fail","skip","succeed"]
  = "fail"` (signal over volume). **Tier 2 stays purely hard input errors** — the
  helper does NOT route GradeBelowThreshold through tier 2; flagged is detected on
  a *successful* (exit-0) run via the sidecar count. Rationale: a flagged run exits
  0 by default; conflating it with tier 2 would mis-route `ModelNotFoundError` /
  anchor-contract failures to the review branch.

- **DEC-002 — single-model accurate; `--select` batch = last-model + documented.**
  `run_signalforge` returns accurate counts for a single-model argv. For `--select`
  it returns the LAST model's JSON (sidecar last-writer-wins) with
  `model_unique_ids` listing the full match set, and documents the limitation.
  Accurate per-model batch XCom is the GenerateOperator child's job (loop per model).

- **DEC-003 — save/restore `sys.excepthook` + the 3 mutated env keys.**
  In-process `run_signalforge` snapshots `sys.excepthook` and exactly
  `NO_COLOR` / `FORCE_COLOR` / `DBT_PROFILES_DIR` (the only keys `cmd_generate` /
  `cmd_prune_existing` mutate, verified), restores them in a `finally`. Keeps the
  long-lived Airflow worker process clean across tasks.

- **DEC-004 — ship both invocation modes; `in_process` default.**
  `run_signalforge(..., invocation: Literal["in_process","subprocess"] =
  "in_process")`. `in_process` reuses `cli.main(argv)` (no subprocess overhead,
  panic-path + exit-code mapping for free). `subprocess` runs
  `[sys.executable, "-m", "signalforge", *argv]` via `subprocess.run` (list-form,
  no `shell=True`), captures stdout/stderr/returncode. Both parse the same JSON
  shape. Both unit-tested.

**Refinement decisions:**

- **DEC-005 — JSON transport: capture stdout `--format json` as PRIMARY; read
  `grade.json` opportunistically.** The JSON *shape* (`DiffReport.model_dump_json`)
  is the contract, not the file specifically. `run_signalforge` ensures
  `--format json` is on the argv (injects if absent) and parses **captured
  stdout** for the diff counts — this is the only source that survives `--dry-run`
  (the read-only scheduled drift mode the epic headlines; `--dry-run` suppresses
  both sidecar files). `mean_grade` is read from `grade.json` on disk **when
  present** (i.e. NOT `--dry-run` and NOT `--no-grade`); `None` otherwise. stderr
  (progress + footer summary, format-independent) is captured for the
  human-readable Airflow log. The diff JSON on stdout is byte-identical to the
  sidecar file, so "the sidecar IS the contract" holds — we just read the
  sidecar's *shape* off the transport that always exists.

- **DEC-006 — decision-table / translation split.**
  `decide_task_outcome(result, *, on_flagged) -> TaskOutcome` is pure + airflow-free
  (fully unit-testable per the acceptance). `TaskOutcome` is a neutral enum
  (`SUCCESS`, `SKIP`, `FAIL_NO_RETRY`, `FAIL_RETRYABLE`) + a message — a SEPARATE
  axis from the four-tier exit code, NOT a fifth tier. A thin airflow-side
  translator (`raise_for_outcome` / the operator's `execute`) maps `TaskOutcome` →
  `AirflowSkipException` / `AirflowFailException` / `AirflowException`; its
  `from airflow.exceptions import ...` lives in the one shim `_airflow_compat`.
  Exit→outcome table:
  | exit | + condition | TaskOutcome | Airflow |
  |---|---|---|---|
  | 0 | flagged==0 | SUCCESS | (task success) |
  | 0 | flagged>0, on_flagged=fail | FAIL_NO_RETRY | AirflowFailException |
  | 0 | flagged>0, on_flagged=skip | SKIP | AirflowSkipException |
  | 0 | flagged>0, on_flagged=succeed | SUCCESS | (task success) |
  | 1 | — | FAIL_NO_RETRY | AirflowFailException |
  | 2 | — | FAIL_NO_RETRY | AirflowFailException |
  | 3 | — | FAIL_RETRYABLE | AirflowException (retryable) |

- **DEC-007 — module layout (airflow-free core + shim-confined translator).**
  `signalforge/airflow/runner.py` (`run_signalforge`), `result.py`
  (`SignalForgeRunResult` + `TaskOutcome` + `decide_task_outcome`), both
  airflow-free and eagerly importable (no `airflow` import → no-eager-import gate
  stays green; added to `__init__`'s eager re-exports like the error classes). The
  airflow-exception translator lives in `_airflow_compat` (shim) +/or the operator;
  covered by the gated `airflow` marker.

- **DEC-008 — XCom payload = small dict always; full sidecar behind opt-in.**
  `SignalForgeRunResult.to_xcom() -> dict` returns the JSON-serialisable counts +
  paths (the ticket's shape). No secrets (sidecars/audits carry only `blake2b-8`
  hashes; model SQL stays in the sidecar FILE, surfaced only by path). A
  `push_full_sidecar` opt-in on the operator can attach the full `diff.json`
  contents (size-warned). User-supplied strings in any error route through
  `errors._format_value` (log-injection defence).

---

## Architecture Review

| Area | Rating | Finding |
|---|---|---|
| **Security** | pass | XCom carries counts + paths only (DEC-008); no secrets (hashes only in audits; SQL stays in the file). `subprocess` mode uses list-form `subprocess.run`, never `shell=True`. Log-injection defence via `errors._format_value`. Sidecar/grade paths canonicalised via `_common.path_safety` before read-back. |
| **Exit-code taxonomy** | concern→resolved | MUST NOT invent a 5th tier or collapse 2 & 3 (cli-layer.md). `TaskOutcome` is a separate axis layered on the four tiers (DEC-006), not a tier. `on_flagged` only applies to tier 0. Pinned by a parametrized exit→outcome test. |
| **Testing strategy** | pass | Pure `decide_task_outcome` table is airflow-free → unit-tested in the DEFAULT suite (acceptance). `run_signalforge` tested via a fake/stub argv path (no live LLM/warehouse) using a fixture sidecar + a monkeypatched `main`. Airflow-exception translation tested under the gated `airflow` marker. Planted-violation self-check for the no-eager-import gate already exists; add `runner`/`result` to its scan surface. |
| **Observability** | pass | Captured stdout (JSON) + stderr (progress/footer) returned on the result for the operator to emit into Airflow task logs. No new logger in the airflow-free core beyond what the operator owns (skeleton convention). |
| **Data model** | pass | `SignalForgeRunResult` = frozen dataclass (airflow-free; NOT pydantic — avoids a pydantic import in the lean airflow path? pydantic is already a core dep, so a frozen pydantic model is fine too — decide in detailing). `to_xcom()` returns a plain JSON-serialisable dict. |
| **Performance** | pass | `in_process` default avoids per-task subprocess + interpreter-startup overhead. `redirect_stdout/stderr` is process-global → **concurrent in-process invocations in one worker would clobber capture**; documented, with `subprocess` mode as the isolation answer (concern, mitigated by docs + the mode param). |
| **API design** | pass | Signatures mirror adjacent stages (keyword-only optionals; `project_dir`). `run_signalforge(argv, *, project_dir, invocation="in_process", ...) -> SignalForgeRunResult`. Naming matches the ticket verbatim. |
| **Packaging** | pass | All new modules airflow-free → base wheel unchanged, `[airflow]` extra stays out of dev group, no-eager-import + wheel-deps gates stay green (python-build.md, #230 memory). |

**No blockers.** One concern (concurrent in-process capture clobber) is mitigated
by documentation + the `subprocess` mode. Exit-code-taxonomy concern resolved by
the separate-axis `TaskOutcome` (DEC-006).

---

## Detailed Breakdown

**Validation command (every story's AC):**
`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

**Module map (all new code under `signalforge.airflow`, airflow-free except US-003's translator):**
- `result.py` — `SignalForgeRunResult`, `TaskOutcome`, `decide_task_outcome`, `OnFlagged` (US-001)
- `runner.py` — `run_signalforge` (US-002)
- `_airflow_compat.py` — `raise_for_outcome` translator (US-003)
- `__init__.py` — eager re-exports + docstring (US-003)

### US-001 — `result.py`: airflow-free result type + pure decision table

**Description:** Ship the pure, airflow-free heart of the contract: the typed
result, the neutral outcome enum, and the exit→outcome decision function. This is
the testable core the acceptance names.

**Traces to:** DEC-001, DEC-006, DEC-008.

**Implementation:**
- `OnFlagged = Literal["fail", "skip", "succeed"]`.
- `TaskOutcome(str, Enum)` — `SUCCESS`, `SKIP`, `FAIL_NO_RETRY`, `FAIL_RETRYABLE`.
  A neutral discriminator (separate axis from the exit code, NOT a 5th tier).
- `SignalForgeRunResult` — frozen dataclass: `exit_code: int`,
  `model_unique_ids: tuple[str, ...]`, `kept: int`, `kept_uncertain: int`,
  `dropped: int`, `flagged: int`, `mean_grade: float | None`,
  `below_threshold: bool`, `diff_sidecar_path: str | None`,
  `grade_sidecar_path: str | None`, `duration_seconds: float | None`,
  `stdout: str`, `stderr: str`. `below_threshold` property/field derived from
  `flagged > 0` (DEC-001). `to_xcom() -> dict[str, object]` returns the
  JSON-serialisable counts+paths shape from the ticket (excludes `stdout`/`stderr`
  bulk text; secrets never present).
- `decide_task_outcome(result, *, on_flagged="fail") -> TaskOutcome` — the table
  in DEC-006: exit 0 + flagged→on_flagged branch; 1/2→FAIL_NO_RETRY;
  3→FAIL_RETRYABLE. Pure; no airflow, no I/O.

**TDD (default suite — no marker, no airflow):**
- Parametrized exit→outcome table: each of the 7 rows in DEC-006 (`(exit, flagged,
  on_flagged) -> TaskOutcome`), incl. all three `on_flagged` branches at exit 0.
- `below_threshold == (flagged > 0)`.
- `to_xcom()` returns exactly the documented keys, is `json.dumps`-round-trippable,
  and contains no `stdout`/`stderr`/secret-shaped fields.
- A guard test asserting `len(TaskOutcome) == 4` (no silent 5th-tier creep).

**Done when:** `decide_task_outcome` + `SignalForgeRunResult` exist, airflow-free,
all decision-table rows pinned, validation green.

**Files:** `src/signalforge/airflow/result.py` (new);
`tests/airflow/test_result.py` (new, UNGATED).

**Depends on:** none.

### US-002 — `runner.py`: `run_signalforge` (in-process + subprocess, JSON transport, isolation)

**Description:** The invocation seam — build/normalise argv, run the pipeline
(in-process default or subprocess), capture stdout/stderr + exit code, parse the
JSON shape into `SignalForgeRunResult`.

**Traces to:** DEC-002, DEC-003, DEC-004, DEC-005.

**Implementation:**
- `run_signalforge(argv, *, project_dir, invocation="in_process",
  on_flagged="fail", timeout_seconds=None) -> SignalForgeRunResult`.
  (`on_flagged` is carried for the operator but the OUTCOME mapping is the
  operator's call via `decide_task_outcome`; `run_signalforge` itself just runs +
  parses. Keep `on_flagged` off `run_signalforge` unless needed — decide in impl;
  default plan: `run_signalforge` does NOT take `on_flagged`, the operator calls
  `decide_task_outcome(result, on_flagged=...)`.)
- Ensure `--format json` present on argv (inject if absent) so stdout is parseable
  (DEC-005). Ensure `--project-dir` reflects `project_dir`.
- **in_process:** snapshot `sys.excepthook` + env keys `NO_COLOR`/`FORCE_COLOR`/
  `DBT_PROFILES_DIR` (DEC-003); `redirect_stdout`/`redirect_stderr` to buffers;
  call `signalforge.cli.main(argv)` (imported **lazily inside the function** so
  `import signalforge.airflow` stays light); restore in `finally`.
- **subprocess:** `subprocess.run([sys.executable, "-m", "signalforge", *argv],
  capture_output=True, text=True, timeout=timeout_seconds)` — list-form, no
  `shell=True`; exit code from `returncode`.
- Parse captured stdout as JSON → diff counts + `model_unique_ids` + `run_id` +
  `duration_seconds`. Read `grade.json` from `<project_dir>/.signalforge/grade.json`
  (canonicalised via `_common.path_safety`) for `mean_grade` **when present**;
  else `None`. Set `diff_sidecar_path`/`grade_sidecar_path` to the conventional
  paths **when the files exist**, else `None` (`--dry-run` → both `None`).
- Defensive JSON parse: a non-zero exit with unparseable stdout still returns a
  result carrying the exit code + captured streams (so the operator can map
  tier 1/2/3 to a failure even when no JSON was produced).

**TDD:**
- in_process: monkeypatch `signalforge.cli.main` to write a fixture JSON to stdout
  + return an exit code; assert the parsed `SignalForgeRunResult` fields. Use a
  committed `diff.json`-shaped fixture (mirror `tests/fixtures/diff/`).
- Isolation: assert `sys.excepthook` and the 3 env keys are byte-restored after
  the call (set sentinels before, assert after) — incl. the exception path
  (`main` raising / returning non-zero).
- `--format json` injection: argv without `--format` gets it; argv with an
  explicit `--format markdown` is left alone OR overridden — pin the chosen rule.
- grade.json presence: with a fixture `grade.json` → `mean_grade` populated; with
  none (dry-run) → `None`, paths `None`.
- subprocess: monkeypatch `subprocess.run` with a fake returning canned
  stdout/returncode; assert list-form argv (`[sys.executable, "-m",
  "signalforge", ...]`, no `shell=True`) and parsed result.

**Done when:** both invocation modes return a correct `SignalForgeRunResult` from
fixture-driven runs; isolation restore pinned; validation green.

**Files:** `src/signalforge/airflow/runner.py` (new);
`tests/airflow/test_runner.py` (new, UNGATED — no airflow import);
`tests/fixtures/airflow/diff_json_sample.json` + `grade_json_sample.json` (new, or
reuse `tests/fixtures/diff/` + `tests/fixtures/grade/`).

**Depends on:** US-001.

### US-003 — Airflow-side translation + `__init__` eager wiring + gate-scan update

**Description:** The thin airflow-touching layer: translate `TaskOutcome` → the
real Airflow exceptions (confined to the one shim), eagerly re-export the new
airflow-free names from `signalforge.airflow`, and extend the no-eager-import gate
to the new modules.

**Traces to:** DEC-006, DEC-007.

**Implementation:**
- `_airflow_compat.raise_for_outcome(outcome, *, message)` — lazy
  `from airflow.exceptions import AirflowException, AirflowFailException,
  AirflowSkipException` **inside the function body** (`# pragma: no cover` like the
  existing factories); maps `FAIL_NO_RETRY`→`AirflowFailException`,
  `SKIP`→`AirflowSkipException`, `FAIL_RETRYABLE`→`AirflowException`,
  `SUCCESS`→return (no raise). The sole new `from airflow ...` site — confinement
  test stays green.
- `__init__.py`: add `run_signalforge`, `SignalForgeRunResult`, `TaskOutcome`,
  `decide_task_outcome`, `OnFlagged` to **eager** re-exports (airflow-free, like
  the error classes) + `__all__`; update the module docstring to describe the
  result contract. Keep operator/hook names lazy (unchanged).
- Extend the no-eager-import gate (`tests/airflow/test_airflow_no_eager_import.py`)
  / confinement scan surface to cover `result.py` + `runner.py` (assert neither
  imports airflow). Keep the existing planted-violation self-check shape.

**TDD:**
- **Gated (`airflow` marker, `importorskip("airflow")` inside the test):**
  `raise_for_outcome(FAIL_NO_RETRY)` raises `AirflowFailException`;
  `SKIP`→`AirflowSkipException`; `FAIL_RETRYABLE`→`AirflowException`;
  `SUCCESS`→no raise. Assert `AirflowSkipException`/`FailException` are distinct.
- **Ungated:** `import signalforge.airflow` then access `run_signalforge` /
  `SignalForgeRunResult` / `decide_task_outcome` — resolve eagerly, airflow NOT in
  `sys.modules` after the import (extends the existing no-eager gate).
- Confinement: `_airflow_compat` is still the ONLY airflow-importing module
  (existing scan, now also asserting `result`/`runner` are clean).

**Done when:** translation raises the right airflow exceptions under the marker;
eager re-exports resolve without dragging airflow; gates (confinement, no-eager,
wheel-deps) green; validation green.

**Files:** `src/signalforge/airflow/_airflow_compat.py` (edit),
`src/signalforge/airflow/__init__.py` (edit),
`tests/airflow/test_airflow_no_eager_import.py` (edit),
`tests/airflow/test_outcome_translation.py` (new, GATED `airflow`),
`tests/airflow/test_airflow_import_confinement.py` (edit if needed).

**Depends on:** US-001, US-002.

### US-004 — Docs + example DAG refresh + CHANGELOG

**Description:** Document the result→task-state + XCom contract for operators, and
show the helper in the shipped example.

**Traces to:** DEC-001…DEC-008.

**Implementation:**
- `docs/airflow-ops.md`: add a "Result → task-state + XCom contract" section —
  the exit→outcome table (DEC-006), `on_flagged` semantics + default (DEC-001),
  invocation modes (DEC-004) + the concurrent-in-process capture caveat, the
  XCom payload shape + `to_xcom()` (DEC-008), the `--dry-run`/`--format json`
  transport + `mean_grade is None` under dry-run note (DEC-005), the `--select`
  batch last-model limitation (DEC-002). NO `##` ATX heading inside fenced code
  blocks (use 4-space indented blocks — mkdocs anchor trap).
- `examples/airflow/signalforge_generate_dag.py`: refactor the callable to use
  `run_signalforge` + `decide_task_outcome` + `raise_for_outcome` (drop-in over
  the raw `subprocess`/exit-parse), keeping the same dag_id + XCom shape. Keep it
  runnable under the gated DAG-parse test (#229).
- `CHANGELOG.md` `[Unreleased]` § Added: the result contract + helper.
- MkDocs nav: `airflow-ops.md` already in nav (#229) — no change unless a new doc.

**Done when:** docs render (`uv run mkdocs build` clean of new warnings), example
DAG still parses under the gated test, CHANGELOG updated, validation green.

**Files:** `docs/airflow-ops.md` (edit),
`examples/airflow/signalforge_generate_dag.py` (edit), `CHANGELOG.md` (edit).

**Depends on:** US-001, US-002, US-003.

### US-005 — Quality Gate (code review x4 + CodeRabbit)

**Description:** Run the code reviewer 4× across the full changeset, fixing all
real bugs each pass; run CodeRabbit; validation passes after all fixes. Use 4
diverse reviewer angles (correctness / conventions+exit-code-taxonomy /
tests+gating / docs+UX) per the cross-surface-drift lesson.

**Done when:** 4 review passes complete, all real findings fixed, CodeRabbit clean
or addressed, full validation green.

**Depends on:** US-001, US-002, US-003, US-004.

### US-006 — Patterns & Memory (priority 99)

**Description:** Capture the durable patterns: the airflow-free-core +
shim-confined-translator split (DEC-006/007), the `TaskOutcome`-as-separate-axis
rule (don't grow a 5th tier), the `--dry-run`/`--format json` transport reframing
(DEC-005), the in-process side-effect isolation set. Update `.claude/rules/`
(cli-layer.md or a new airflow rule) + the airflow memory + `docs/` as needed.
Note the potential v0.8 hoist of `run_signalforge` to a neutral package for the
GitHub Action.

**Done when:** rules/memory/docs updated; validation green.

**Depends on:** US-005.
