# Super Plan — #230: Airflow packaging + `signalforge.airflow` skeleton + `[airflow]` extra

## Meta

- **Ticket:** [#230](https://github.com/wjduenow/SignalForge/issues/230)
- **Epic:** #228 (Airflow operator, v0.7 roadmap)
- **Depends on:** #229 (Airflow local + CI test-environment spike — landed, commit `258f731`)
- **Branch / worktree:** `feature/230-airflow-skeleton` @ `../worktrees/SignalForge/230-airflow-skeleton`
- **Phase:** detailing (awaiting approval)
- **Sessions:** 1 (2026-06-15)

---

## Discovery

### What / Why

Stand up the `signalforge.airflow` subpackage **skeleton** + the `[airflow]` optional extra, with **zero** Airflow weight in the base install. No operator behaviour yet — just the seam every later epic-#228 child plugs into. This is the second child of epic #228; #229 already decided the test-environment + version contract.

### The #229 contract (inherited, NOT re-litigated)

From `docs/research/airflow-test-environment.md` (DEC-1..DEC-5):

- **Version floor:** `apache-airflow>=2.8,<3` for v0.7; certified against `2.10.4` on **py3.11**. 3.x deferred (moved import paths + split packaging) — tracked epic follow-up, not a v0.7 blocker.
- **Constraints-pinned install:** Airflow installs into an **isolated `.venv-airflow`** under the Apache constraints file (`constraints-2.10.4/constraints-3.11.txt`). The `--constraint` is **load-bearing** (without it uv upgrades protobuf 4→5 / pydantic / typing-extensions and breaks Airflow 2.10.4).
- **`airflow` pytest marker already registered** (#229) — excluded from default `addopts`; belt-and-suspenders (marker + runtime `importorskip("airflow")`). Run via `uv run --no-sync pytest -m airflow --no-cov`.
- **`tests/airflow` excluded from pyright** (#229) — Airflow is **not** a typecheck dependency; pyright runs without it.
- **DAG parse = `DagBag(...).dags`**, never `get_dag()` (latter needs `airflow db init`).
- **CI:** label-gated `airflow` job + `workflow_dispatch` already wired; currently `uv pip install -e . --constraint …`. #229 says explicitly: **"Use `-e .` until the skeleton child ships the `[airflow]` extra, then `.[airflow]`."**
- **#229 explicitly deferred to THIS child:** *"The `[airflow]` optional extra + `signalforge.airflow` package skeleton."*

### Codebase patterns to mirror (file:line precedent)

| Concern | Precedent | Mirror as |
|---|---|---|
| One-shim-per-vendor (lazy import + `# type: ignore` confinement + duck-typed Protocol + `make_*` factory + `__all__`) | `src/signalforge/warehouse/adapters/_snowflake_client.py`, `src/signalforge/llm/_openai_client.py` | `src/signalforge/airflow/_airflow_compat.py` |
| Confinement test (line-scan + planted-violation self-check) | `tests/warehouse/test_snowflake_client_confinement.py` | `tests/airflow/test_airflow_import_confinement.py` |
| No-eager-import (`X not in sys.modules` after clean import) | `tests/warehouse/test_snowflake_client.py::test_importing_shim_does_not_import_snowflake_connector` | `tests/airflow/test_airflow_client.py` |
| Optional-extra declaration | `pyproject.toml [project.optional-dependencies]` (`snowflake`/`openai`/`gemini`) | add `airflow = […]` |
| Wheel-deps negative assertion | `tests/test_wheel_packaging.py` | add "no `apache-airflow` in core deps" |
| `errors.py` base + remediation | `src/signalforge/ingest/errors.py` | `src/signalforge/airflow/errors.py` (scope TBD — see DEC-002) |
| Exit-code table + scan-7 | `cli/_helpers.py::_EXCEPTION_TO_EXIT_CODE`, `tests/test_audit_completeness.py` (13 `errors.py` files today; `_EXCEPTION_MAPPING_EXCLUDED_BASES`) | TBD — see DEC-002 |

### Key tensions surfaced in research (drive the scoping questions)

1. **dev-group mirror vs #229 isolated venv.** `python-build.md` says optional-extras mirror into `[dependency-groups].dev`. But #229 deliberately keeps Airflow OUT of the default dev env (isolated `.venv-airflow`, pyright excludes `tests/airflow`), and #230's own acceptance criterion is *"pyright + default pytest green WITHOUT Airflow installed."* Mirroring `apache-airflow` into the dev group would break that. → **deviation required** (DEC-001).
2. **Eager-import trap in operator stubs.** The issue says stubs "subclass the shim's `BaseOperator`." A literal `class X(BaseOperator)` at module scope imports airflow at import time → breaks the no-eager-import gate. The stub shape must defer the airflow import (DEC-003).
3. **errors.py / exit-code coupling.** Airflow maps task outcomes itself (`AirflowFailException`); signalforge-airflow errors never flow through the `signalforge` CLI. Whether/how they register in `_EXCEPTION_TO_EXIT_CODE` + scan-7 is a genuine decision (DEC-002).
4. **Provider-discovery entry point.** `apache_airflow_provider` entry point vs plain importable operators. Issue recommends plain importable for v0.7 (DEC-004).

---

## Decisions (DEC log)

- **DEC-001 — `[airflow]` extra only; NO dev-group mirror.** Add `apache-airflow` to `[project.optional-dependencies].airflow` only. Do **not** mirror into `[dependency-groups].dev`. This is a **deliberate, documented deviation** from `python-build.md`'s optional-extra mirror rule, forced by two things: #229's isolated constraints-pinned `.venv-airflow` (Airflow is too heavy + version-pinned for the default dev env) and #230's own acceptance criterion *"pyright + default pytest green WITHOUT Airflow installed."* The Patterns & Memory story records the carve-out in `python-build.md`. *Rationale: chose "extra only" over a dedicated non-default group — a second sync target is maintenance with no caller; the `.venv-airflow` recipe from #229 already covers maintainer testing.*

- **DEC-002 — Version pin inherits #229 DEC-1: `apache-airflow>=2.8,<3`.** The extra is `airflow = ["apache-airflow>=2.8,<3"]` — no sibling deps (Airflow pulls its own tree under the constraints file). Certified cell stays 2.10.4/py3.11. 3.x deferred per #229.

- **DEC-003 — Ship `errors.py` (base + one concrete), register the concrete.** `AirflowIntegrationError` (abstract base, carries `remediation` per `manifest-readers.md`) + one concrete `AirflowConfigError` (operator misconfiguration — the first thing the children will raise when reading `project_dir`/`model` from env/Variables). Register `AirflowConfigError` in `_EXCEPTION_TO_EXIT_CODE` at **tier 2** (input-validation); add `AirflowIntegrationError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`; bump the scan-7 errors.py count **13 → 14**. *Note: registration is defensive / scan-7 compliance — these errors surface through Airflow's task runner (`AirflowFailException`), not the `signalforge` CLI panic path, so the tier is notional. `errors.py` is pure-Python and airflow-free, so it is an **eager** re-export from `__init__.py` (only operators/hooks are lazy).*

- **DEC-004 — Plain stub operators/hooks; no eager `BaseOperator` subclass.** `operators.py` / `hooks.py` define placeholder classes that do **not** subclass the real Airflow base at module scope (that would import airflow at import time and break the no-eager-import gate). Their `__init__` raises `NotImplementedError` naming the child issue. The shim exposes the lazy seam (`_BaseOperatorProtocol` + a `make_*` factory) that the implementing children use to do the real subclassing inside a function. *Resolves the issue's literal "subclass the shim's BaseOperator" wording against the no-eager-import gate.*

- **DEC-005 — Plain importable operators; no provider entry point in v0.7.** No `apache_airflow_provider` / `get_provider_info` entry point. Operators ship as plain importables (`from signalforge.airflow import SignalForgeGenerateOperator`). Provider registration is purely additive later.

- **DEC-006 — `__init__.py` lazy re-export via PEP 562 `__getattr__`.** `import signalforge.airflow` imports no airflow. Operator/hook names resolve lazily through module-level `__getattr__` (importing the shim only on attribute access); error classes are eager (airflow-free). `__all__` lists the full public surface.

- **DEC-007 — One shim: `_airflow_compat.py`.** SOLE home for every `from airflow …` import and every airflow `# type: ignore` / `# pyright: ignore`. All imports are lazy (inside `make_*` / `_load_*` functions). Exposes duck-typed `_BaseOperatorProtocol` / `_BaseHookProtocol` so orchestration code type-checks without Airflow installed (mirrors `_BQClientProtocol`).

- **DEC-008 — Confinement is a standalone LINE-SCAN test, not a new `test_audit_completeness` AST scan.** Mirror `tests/warehouse/test_snowflake_client_confinement.py`: scan every `.py` under `src/signalforge/airflow/` for an `airflow`-mentioning `import` or `type: ignore`/`pyright: ignore` outside `_airflow_compat.py`. Ships the mandatory planted-violation self-check + a "shim actually carries the seam" sanity check (`testing-signal.md`). The project's **AST-scan count (12) is unchanged** — those scans confine a vendor *client-class construction* (`anthropic.Anthropic(...)`); the skeleton constructs no such class, it confines imports. *(Corrects a recon suggestion of a new AST scan #11.)*

- **DEC-009 — The three gate tests run UNGATED (default suite); only DAG-parse/live tests carry the `airflow` marker.** `test_airflow_import_confinement`, the no-eager-import test, and the wheel-deps negative assertion must pass *without Airflow installed* — they assert absence/confinement and never import airflow. This is what makes them the load-bearing "core stays lean" gates in default CI.

---

## Architecture Review

Most baseline areas are N/A for a packaging skeleton (no endpoints, queries, schema, or runtime data paths). Focused ratings on what actually bears weight:

| Area | Rating | Finding |
|---|---|---|
| Security | **pass** | No auth/input surface. `apache-airflow` is a heavy supply-chain add but isolated behind the optional extra (not in base install). |
| Performance | **pass** | No runtime code paths in the skeleton. Lazy imports keep base-install import time unchanged. |
| Data Model | **pass** | None. |
| API Design (Python public surface) | **concern → DEC-006** | The "API" is `__all__` + lazy `__getattr__`. Getting the lazy re-export wrong silently breaks the no-eager-import gate. Pinned by DEC-006 + the no-eager-import test (DEC-009). |
| Observability | **pass** | Skeleton emits no logs (no behaviour yet). |
| Testing Strategy | **concern → DEC-008/009** | The three gates ARE the deliverable. Each needs a real-failure path: confinement → planted-violation self-check; no-eager-import → `sys.modules` assertion in a clean import; wheel-deps → negative assertion against built wheel members. All ungated (run without Airflow). |
| Packaging / supply-chain | **concern → DEC-001/002** | CI flips `-e .` → `.[airflow]` under the constraints file (`--constraint` stays load-bearing). Wheel must not vendor `apache-airflow`. Hatch `packages = ["src/signalforge"]` auto-discovers the new subpackage (no `include` needed — pure `.py`). |
| Convention compliance | **concern → DEC-001/003** | dev-group deviation must be recorded in `python-build.md`; scan-7 count bump 13→14; one-shim rule mirrored. |

**Blockers:** none. **Concerns:** all resolved by the DECs above.

---

## Detailed Breakdown

Ordering: packaging → package skeleton → errors seam → gate tests → quality gate → patterns. The errors seam (US-003) touches shared registries (exit-code table, scan-7, excluded-bases) so it is its own **serial** story per the `ralph-serialize-shared-registry-beads` convention.

### US-001 — `[airflow]` optional extra + CI install flip
- **Traces to:** DEC-001, DEC-002
- **Description:** Add the `[airflow]` optional extra and flip the CI install from `-e .` to `.[airflow]`.
- **Files:**
  - `pyproject.toml` — add `airflow = ["apache-airflow>=2.8,<3"]` to `[project.optional-dependencies]`. Do NOT touch `[dependency-groups].dev`.
  - `.github/workflows/ci.yml` — in the gated `airflow` job, change `uv pip install -e . --constraint "$CONSTRAINTS"` → `uv pip install -e '.[airflow]' --constraint "$CONSTRAINTS"`. Keep `--constraint` (load-bearing) + the pytest/pytest-cov/pytest-asyncio constrained installs.
- **Acceptance:** `uv pip install '.[airflow]' --constraint <2.10.4/3.11 constraints>` resolves in an isolated venv. Default `uv sync --dev` does NOT pull apache-airflow. `uv run pyright && uv run pytest` (default) stay green. The CI `airflow` job references `.[airflow]`.
- **Done when:** the extra exists, the dev group is untouched, and the CI job installs via the extra.

### US-002 — `signalforge.airflow` package skeleton (shim + `__init__` + stubs)
- **Traces to:** DEC-004, DEC-006, DEC-007
- **Depends on:** US-001
- **Description:** Stand up the subpackage: the one lazy shim, lazy public surface, and stub operators/hooks raising `NotImplementedError`.
- **Files:**
  - `src/signalforge/airflow/__init__.py` — `__all__` (operators, hook, result type placeholder); PEP 562 `__getattr__` lazy-resolves operator/hook names through the shim; error classes eager-imported from `.errors` (added in US-003 — until then, import what exists).
  - `src/signalforge/airflow/_airflow_compat.py` — the SOLE shim: lazy `from airflow …` inside `make_*`/`_load_*` functions; `_BaseOperatorProtocol` / `_BaseHookProtocol` duck-typed; every airflow `# type: ignore`/`# pyright: ignore` confined here; `__all__`.
  - `src/signalforge/airflow/operators.py` — placeholder operator class(es), NOT subclassing `BaseOperator` at module scope; `__init__` raises `NotImplementedError("… lands in epic #228 child #…")`.
  - `src/signalforge/airflow/hooks.py` — placeholder hook class, same shape.
- **Acceptance:** `import signalforge` and `import signalforge.airflow` succeed with Airflow NOT installed and do not import airflow. Instantiating a stub raises `NotImplementedError`. `uv run pyright` green without Airflow (protocols carry the surface).
- **Done when:** the package imports airflow-free and stubs raise.

### US-003 — `errors.py` + exit-code registration + scan-7 bump
- **Traces to:** DEC-003
- **Depends on:** US-002
- **Description:** Add the typed-error seam and wire it into the shared CLI registries. SERIAL (shared-registry edits).
- **Files:**
  - `src/signalforge/airflow/errors.py` — `AirflowIntegrationError` base (remediation-carrying, `__str__` renders `↳ Remediation:`) + concrete `AirflowConfigError`.
  - `src/signalforge/airflow/__init__.py` — eager re-export of both error names.
  - `src/signalforge/cli/_helpers.py` — register `AirflowConfigError` → tier 2 in `_EXCEPTION_TO_EXIT_CODE` (import from `signalforge.airflow`).
  - `tests/test_audit_completeness.py` — add `AirflowIntegrationError` to `_EXCEPTION_MAPPING_EXCLUDED_BASES`; bump `test_scan_7_discovers_every_per_stage_errors_module` count 13 → 14.
- **TDD:** test that `AirflowConfigError` renders remediation; test that it maps to exit code 2 via `map_exception_to_exit_code`.
- **Acceptance:** scan-7 + the exit-code AST scan pass; `uv run pytest` green; importing the error names stays airflow-free.
- **Done when:** errors ship, are registered, and scans pass.

### US-004 — Gate tests (confinement + no-eager-import + wheel-deps)
- **Traces to:** DEC-008, DEC-009
- **Depends on:** US-002, US-003
- **Description:** The three ungated load-bearing gates.
- **Files:**
  - `tests/airflow/test_airflow_import_confinement.py` — line-scan: every `airflow`-mentioning `import` / `type: ignore` lives only in `_airflow_compat.py`; **planted-violation self-check** + "shim carries the seam" sanity check. UNGATED (no airflow import).
  - `tests/airflow/test_airflow_no_eager_import.py` — clean import of `signalforge` and `signalforge.airflow` asserts `"airflow" not in sys.modules` (drop stale entries first, per the snowflake precedent). UNGATED.
  - `tests/test_wheel_packaging.py` — `@pytest.mark.wheel_smoke` negative assertion: built wheel members contain no `apache-airflow`/`airflow/` top-level dist entries; core deps exclude `apache-airflow`.
- **Acceptance:** all three pass in the default suite WITHOUT Airflow installed; planted-violation self-check raises as expected; `uv run pytest -m wheel_smoke --no-cov` green.
- **Done when:** the gates pass and the confinement self-check is proven.

### US-005 — Quality Gate (code review x4 + CodeRabbit)
- **Depends on:** US-001, US-002, US-003, US-004
- Run the code reviewer 4 passes across the full changeset, fixing all real bugs each pass; run CodeRabbit; full validation (`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`) green. Maintainer also runs the gated `uv run --no-sync pytest -m airflow --no-cov` against the `.venv-airflow` to confirm `.[airflow]` install + DAG parse still pass.

### US-006 — Patterns & Memory (priority 99)
- **Depends on:** US-005
- Update `python-build.md` with the DEC-001 dev-group deviation (optional-extra that is deliberately NOT mirrored into the dev group, with the #229 isolated-venv + acceptance-criteria rationale). Add a short `signalforge.airflow` skeleton note to the architecture map in `CLAUDE.md` (subpackage + shim seam). Record the one-shim-applies-to-airflow + no-eager-import-gate pattern in memory. Update the scan-7 count reference (13→14) anywhere it's quoted in rules.
