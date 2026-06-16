# Airflow integration (`signalforge.airflow`)

Epic [#228](https://github.com/wjduenow/SignalForge/issues/228) (v0.7). Apply to every module under `signalforge.airflow` and any code that turns a SignalForge run into Airflow task state / XCom. The base install MUST stay Apache-Airflow-free (Architectural Commitment #4); Airflow ships only behind the `[airflow]` optional extra.

## Two layers: airflow-free core + shim-confined translator (#231 DEC-006/007)

The integration splits cleanly so the "act on the graded diff" logic is unit-testable WITHOUT an Airflow install (this is also what the v0.8 GitHub Action reuses):

- **Airflow-free core** — `signalforge/airflow/result.py` + `runner.py`. Eagerly importable; carries NO `from airflow ...` import. Eager re-exports from `__init__.py` (alongside the error classes), NOT the lazy `__getattr__` (which stays for the operator/hook names — see [[signalforge-airflow-skeleton-pattern]] / #230).
  - `SignalForgeRunResult` — frozen `@dataclass`. Fields: `exit_code`, `model_unique_ids: tuple[str,...]`, `kept`/`kept_uncertain`/`dropped`/`flagged`, `mean_grade: float|None`, `diff_sidecar_path`/`grade_sidecar_path: str|None`, `duration_seconds: float|None`, `stdout`, `stderr`. `below_threshold` is a **read-only `@property` = `flagged > 0`** (never a stored field; never pass it to the constructor — the example-DAG gate rebuild omits it). `to_xcom() -> dict` returns counts + paths ONLY (no `stdout`/`stderr` bulk text, no secrets).
  - `TaskOutcome(StrEnum)` = `SUCCESS` / `SKIP` / `FAIL_NO_RETRY` / `FAIL_RETRYABLE`. **A SEPARATE AXIS from the four-tier CLI exit code — NOT a fifth tier** (`cli-layer.md`). `len(TaskOutcome) == 4` is pinned by a guard test.
  - `decide_task_outcome(result, *, on_flagged="fail") -> TaskOutcome` — pure, no I/O, no airflow.
  - `run_signalforge(argv, *, project_dir, invocation="in_process"|"subprocess", timeout_seconds=None) -> SignalForgeRunResult` — builds/normalises argv, runs the pipeline, parses the result. Does NOT compute the outcome (the operator calls `decide_task_outcome`).
- **Shim-confined translator** — `_airflow_compat.raise_for_outcome(outcome, *, message)`. The ONLY new `from airflow.exceptions import ...` site (lazy, inside the body, `# type: ignore[import-not-found]`, `# pragma: no cover`), per the one-shim-per-vendor rule (`llm-drafter.md` §"One SDK seam"). Maps `FAIL_NO_RETRY`→`AirflowFailException` (no retry), `SKIP`→`AirflowSkipException`, `FAIL_RETRYABLE`→`AirflowException` (retryable — Airflow's `retries`/`retry_delay` apply), `SUCCESS`→return. Deliberately NOT re-exported from the package top — the operator calls it via `_airflow_compat`.

**Rule for the remaining epic-#228 children (operators, hook, drift):** put the decision in the pure core, the airflow-exception raise in the shim. Never re-derive the outcome by string-matching; carry the typed `TaskOutcome`. Never grow a fifth exit tier.

## Exit → TaskOutcome → Airflow (the #231 contract table)

| CLI exit | + condition | TaskOutcome | Airflow effect |
|---|---|---|---|
| 0 | `flagged == 0` | `SUCCESS` | task success |
| 0 | `flagged > 0`, `on_flagged="fail"` (default) | `FAIL_NO_RETRY` | `AirflowFailException` |
| 0 | `flagged > 0`, `on_flagged="skip"` | `SKIP` | `AirflowSkipException` (route to a review branch) |
| 0 | `flagged > 0`, `on_flagged="succeed"` | `SUCCESS` | task success |
| 1 | — (load/parse) | `FAIL_NO_RETRY` | `AirflowFailException` |
| 2 | — (input-validation, incl. hard `ModelNotFoundError`) | `FAIL_NO_RETRY` | `AirflowFailException` |
| 3 | — (LLM/warehouse/auth) | `FAIL_RETRYABLE` | `AirflowException` (retryable) |
| else | unexpected code (<0, >3) | `FAIL_NO_RETRY` | conservative — fail, don't retry forever |

**`on_flagged` keys on the diff's `flagged_count`, NOT the exit code (#231 DEC-001).** A flagged run exits **0** by default (`grade.fail_on_below_threshold=false`), so "fail/branch on flagged" is a decision layered on a *successful* run via `result.below_threshold` (= `flagged > 0`). **Tier 2 stays purely hard input errors** — do NOT route below-threshold through tier 2 (that would mis-route `ModelNotFoundError`/anchor-contract failures to the review branch).

## JSON transport: the SHAPE is the contract, read off stdout (#231 DEC-005)

`run_signalforge` injects `--format json` and parses the diff JSON from **captured stdout**, not the `.signalforge/diff.json` file. Reason: `--dry-run` (the read-only scheduled drift mode the epic headlines) **suppresses both sidecar files**, so the file isn't a reliable transport. The JSON shape (`DiffReport.model_dump_json(by_alias=True)`) is byte-identical on stdout and in the file. `mean_grade` is read from `grade.json` on disk **only when present** (NOT `--dry-run`, NOT `--no-grade`) → `None` otherwise. Sidecar path fields are set only when the files exist. **Diff-key contract is pinned by a default-suite structural guard** asserting the runner's parsed keys ⊆ `DiffReport` schema properties and `mean_score ∈ GradingReport.model_computed_fields` — the fake-driven-byte-identity defence (`testing-signal.md`; [[fake-driven-byte-identity-blind-spot]]).

**`--select` batch (#231 DEC-002):** sidecars are last-writer-wins, so one `run_signalforge` over a `--select` batch reflects the LAST model; `model_unique_ids` contains at most that last model's id (NOT the full match set). Accurate per-model batch XCom is the `GenerateOperator` child's job (loop per model).

## In-process invocation isolation (#231 DEC-003/004 + QG)

`invocation="in_process"` (default) reuses `cli.main(argv)` (no subprocess overhead; panic-path + exit-code mapping for free; `cli.main` imported **lazily** inside the function so `import signalforge.airflow` stays light). It MUST snapshot and restore process-global state in a `finally` because an Airflow worker is long-lived:

- `sys.excepthook` (CLI installs `_safe_excepthook`).
- env keys **`NO_COLOR` / `FORCE_COLOR` / `DBT_PROFILES_DIR`** — the exact set `cmd_generate`/`cmd_prune_existing` mutate (verified; "absent before → delete after", not set-to-empty).
- **root logger handlers + level (best-effort)** — `setup_logging()` calls `logging.basicConfig(force=True)`, which removes+closes the root logger's existing handlers (Airflow's per-task file/remote handlers) and persists across tasks. Snapshot `logging.getLogger().handlers[:]` + `.level` + `.disabled`, restore in `finally`. **Residual limitation:** `force=True` *closes* the removed handler objects, so a faithful revival isn't guaranteed — document it and recommend `invocation="subprocess"` for full worker log hygiene. Do NOT claim "restores ALL".

`invocation="subprocess"` runs `[sys.executable, "-m", "signalforge", *argv]` (list-form, NEVER `shell=True`; needs the top-level `signalforge/__main__.py` added in #231 — distinct from the `cli/` subpackage's deliberate no-`__main__.py`). The clean-process isolation answer for concurrency (in-process capture uses process-global `redirect_stdout` → concurrent in-process calls in one worker clobber each other).

## Fail-soft reads (derived state)

The diff-stdout parse and the `grade.json` / sidecar-path reads are **fail-SOFT** (swallow `OSError`/`json.JSONDecodeError`/`PathContainmentError` → `None`/empty counts; never crash the task) — they're derived read inputs, not audit writes (the audit-vs-cleanup posture split in `grade-layer.md`/`warehouse-adapters.md`). Sidecar reads still route through symlink-hardened `_common.path_safety.canonicalise_path` (whose bare-`OSError` path is caught alongside `PathContainmentError`).

## XCom hygiene

`to_xcom()` carries counts + sidecar **paths** only — never secrets (audit JSONLs carry only `blake2b-8` hashes; model SQL stays in the sidecar file, surfaced by path). Pushing the full sidecar JSON is an opt-in a future operator may add (size-warned — XCom backends have limits).

## Testing + certification

- Airflow-free core tested in the DEFAULT suite (no marker): full decision table, isolation restore (incl. main-raises + key-absent), transport/parse, `to_xcom`. `result.py`/`runner.py` are 100%-covered ungated (codecov patch gate).
- Airflow-touching tests (`raise_for_outcome`, DAG-parse) carry `@pytest.mark.airflow` + **in-test** `pytest.importorskip("airflow")` (NOT module-scope) and are deselected by default — mirrors `tests/airflow/test_dag_parse.py` and the #229 gating convention ([[signalforge-airflow-local-e2e]]).
- **Certify the airflow-touching paths against the real `.venv-airflow` rig** (#229) before closing — workers can't (airflow isn't in the feature worktree). Non-destructive: `SF_RUN_AIRFLOW=1 PYTHONPATH="$PWD/src" /path/to/.venv-airflow/bin/python -m pytest tests/airflow -m airflow --no-cov` (PYTHONPATH-shadow, no editable reinstall — avoids repointing the rig per [[ralph-editable-install-race]]). #231's translator + refactored example DAG were certified this way (6 passed vs airflow 2.10.4).

## v0.8 note

`run_signalforge` is airflow-free and meant for the v0.8 GitHub Action too. If/when that lands, consider hoisting `result.py`/`runner.py` to a neutral package (e.g. `signalforge.automation`) so the GH Action doesn't import from a package named `airflow`. Out of scope for v0.7 — the modules are airflow-free so `import signalforge.airflow.runner` works without airflow today.

## Reference

`plans/super/231-result-task-state.md` — DEC-001…DEC-008. `plans/super/230-airflow-skeleton.md` — skeleton wiring. `docs/airflow-ops.md` — operator-facing contract + example DAG. `src/signalforge/airflow/{result,runner,_airflow_compat,__init__}.py`, `src/signalforge/__main__.py`. `tests/airflow/`. See-Also: `cli-layer.md` (four-tier exit codes, the no-5th-tier rule, the `[airflow]` `errors.py`), `python-build.md` (`[airflow]` extra out of the dev group), `llm-drafter.md` (one-shim-per-vendor), `grade-layer.md`/`warehouse-adapters.md` (fail-soft vs fail-closed posture).
