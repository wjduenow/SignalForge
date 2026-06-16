# Super Plan — #234: Airflow `SignalForgeHook` (Connection/Variable → profiles.yml + LLM key)

## Meta

- **Ticket:** [#234](https://github.com/wjduenow/SignalForge/issues/234)
- **Epic:** [#228](https://github.com/wjduenow/SignalForge/issues/228) (v0.7 Airflow operator)
- **Depends on:** #230 (skeleton + shim), #231 (result→task-state), #232 (`SignalForgeGenerateOperator`), **#233 (`SignalForgePruneExistingOperator`) — MERGED to `dev` 2026-06-16**
- **Branch:** `feature/234-signalforge-hook`
- **Worktree:** `../worktrees/SignalForge/234-signalforge-hook`
- **Phase:** complete
- **Sessions:** 2 (2026-06-16)
- **Epic bead:** `bd_1-scaffolding-qhi` (9 task beads `.1`–`.9`) — all closed
- **Status:** **Complete** — PR [#243](https://github.com/wjduenow/SignalForge/pull/243) merged to `dev` 2026-06-16 (squash `0a77d35`); issue #234 closed.

---

## Phase 1: Discovery

### Ticket summary

Ship `SignalForgeHook(BaseHook)` keyed on a `signalforge_conn_id` so DAG authors configure SignalForge the Airflow-native way (one Connection + optional Variable) instead of hand-managing env vars on every task. The hook resolves three things:

1. **Warehouse auth** — locate a dbt `profiles.yml` (the existing `WarehouseAdapter.from_profile` seam consumes it). Issue recommends **(a) on-disk profiles** via a `profiles_dir` for v0.7; **(b) synthesize a profiles.yml from an Airflow Connection** is flagged as a non-trivial follow-up (re-deriving the #120 per-type `DbtProfileTarget` validator from a Connection).
2. **LLM API key** — from an Airflow Variable or the Connection `extra`/`password`, injected into the task env for the in-process pipeline call. **Never logged / XCom'd / repr'd / rendered.**
3. **Connection `extra` schema** — `profiles_dir`, `provider`, optional `cache_scope`. (Cost ceilings are **not** in the v0.7 `extra` schema — they have no CLI landing strip and are trimmed per DEC-011.)

Acceptance (A8): a DAG configures SignalForge via Connection + Variable (no inline per-task env); credentials never leak; documented in `docs/airflow-ops.md`.

### Key codebase findings (seam map)

**Existing state.** `signalforge.airflow` ships: `__init__.py` (lazy `__getattr__` re-exports, incl. a `SignalForgeHook` → `hooks` mapping), `hooks.py` (a `NotImplementedError` **stub** — does NOT subclass `BaseHook` at module scope), `_airflow_compat.py` (the one shim: `make_base_operator` / `make_base_hook` lazy factories + `raise_for_outcome`), `operators.py` (`SignalForgeGenerateOperator` via deferred construction), `result.py` + `runner.py` (airflow-free core), `errors.py` (`AirflowIntegrationError` base + `AirflowConfigError` tier-2 concrete).

- **`make_base_hook()` already exists** (`_airflow_compat.py:101`) — lazy `from airflow.hooks.base import BaseHook`. The real hook is built on it, exactly as `_make_generate_operator_class` builds on `make_base_operator()`.
- **Deferred-construction precedent** (`operators.py:624-651`): module `__getattr__` + `importlib.util.find_spec("airflow")` → real class (built in a `functools.cache`'d factory) when airflow present, else an airflow-free placeholder whose `__init__` raises `ModuleNotFoundError`. The hook mirrors this.
- **Operator `__init__`** (`operators.py:475-517`) already takes `profiles_dir`; **`template_fields`** = `("project_dir","select","model","profiles_dir","as_of")`. `execute()` calls `run_signalforge(argv, project_dir=…, invocation=…)` at `operators.py:546` (single) / `:586` (batch) — the slot where hook resolution + env injection lands.
- **`run_signalforge`** (`runner.py`): in_process reuses `cli.main(argv)`; **the LLM key is NOT injected by the runner** — the vendor SDK reads `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`GOOGLE_API_KEY` from the ambient env (`llm/_anthropic_client.py:97`, `_openai_client.py`, `_gemini_client.py`). So the hook must set the env var **before** `run_signalforge`. **In-process isolation snapshots/restores only `("NO_COLOR","FORCE_COLOR","DBT_PROFILES_DIR")`** (`runner.py:63`) — the provider key env vars are NOT in that set today.
- **Profiles seam.** `load_profile(project_dir, target=None) -> DbtProfileTarget` resolves via `$DBT_PROFILES_DIR` → `<project_dir>/profiles.yml` → `~/.dbt/profiles.yml` (`profiles.py:361-464`). `from_profile(profile)` takes the typed `DbtProfileTarget` (`base.py:273`). The CLI's `--profiles-dir` sets `DBT_PROFILES_DIR`; the operator already emits `--profiles-dir` into argv (`_build_generate_argv`, `operators.py:122`). So **option (a) is "hook supplies `profiles_dir`, which becomes `--profiles-dir`"** — the path already works end-to-end.
- **No provider→env-var mapping table** exists; each SDK reads its own standard var. A small mapping (`{"anthropic":"ANTHROPIC_API_KEY", …}`) is new.
- **`__repr__` redaction precedent.** `DbtProfileTarget` uses `Field(repr=False)` on secrets (`profiles.py:164`); `SnowflakeAdapter.__repr__` shows only `account`+`warehouse` (test: `tests/warehouse/test_snowflake_stub.py:72-112` asserts secret substrings AND field-name labels absent).
- **`SignalForgePruneExistingOperator` now EXISTS** (merged #233 to `dev`, 2026-06-16). `__init__(*, task_id, project_dir, model, schema, profiles_dir=None, manifest=None, scope=None, sample_strategy=None, as_of=None, tests_dir=None, on_flagged="fail", invocation="in_process", **kwargs)`; `template_fields = ("project_dir","model","schema","profiles_dir","as_of","tests_dir")`; pure helpers `_build_prune_existing_argv` / `_validate_prune_existing_config`; `execute()` validates → builds argv → `run_signalforge` → `decide_task_outcome` → `raise_for_outcome` → `to_xcom`. **Read-only, makes NO LLM call** (#233 DEC-001) — so it needs **warehouse auth (`profiles_dir`) ONLY, no LLM key**; it has no `cache_scope` param. The #234 acceptance criterion names BOTH operators → `signalforge_conn_id` now wires through both (see DEC-002, DEC-016).

### Airflow 2.x API facts (research, gated to `apache-airflow>=2.8,<3`)

- `BaseHook.get_connection(conn_id) -> Connection` (classmethod). `Connection` exposes `conn_type/host/login/password/schema/port/extra` + safe `extra_dejson` property (returns `{}` on null, never raises). Put the key in `password` (auto-masked) or in `extra` under a sensitive-keyword key.
- `Variable.get(key, default_var=…, deserialize_json=…)`. **Raises `KeyError` when key absent AND no `default_var`**; pass `default_var=None` for soft lookup.
- **Secrets masker**: `from airflow.utils.log.secrets_masker import mask_secret` — **stable across the entire 2.8–2.11 line** (the `airflow.sdk.*` path is 3.0 only). `mask_secret(value)` registers a process-global log filter. `password` + `extra` values whose **key** matches the sensitive list (`api_key`, `secret`, `token`, `password`, `private_key`, …) are auto-masked; `[core] sensitive_var_conn_names` extends the list.
- **`mask_secret` is log-only** — it does NOT scrub XCom or rendered templates. Keeping the key out of XCom (don't return it) and out of `template_fields` (structural) are separate, necessary disciplines.
- **Testing without a live DB**: inject `AIRFLOW_CONN_<CONN_ID>` env var (URI or JSON form); `get_connection` reads it before the metadata DB. Secret-absence: assert raw key substring absent from a captured log buffer (and assert the masker redacts to `***` via a buffer with the `SecretsMasker` filter attached).

### Applicable convention constraints (from `.claude/rules/`)

- **airflow-integration.md** — airflow-free core vs shim-confined translator; one-shim rule (`_airflow_compat` is the ONLY `from airflow` site); lazy `__getattr__` re-export; deferred construction via `find_spec`; fail-soft derived reads; `[airflow]` extra out of dev group; gated tests (`@pytest.mark.airflow` + in-test `importorskip`); certify against `.venv-airflow`.
- **warehouse-adapters.md** — `from_profile` single entry (don't reinvent warehouse auth); `__repr__` credential redaction; symlink-hardened `canonicalise_path` on every user path.
- **safety-layer.md** — secrets never leave without a receipt (audit records blake2b-8 hash only); `extra="forbid"` on user-input config models; `__repr__` omits credentials.
- **cli-layer.md** — four-tier exit codes; `errors.py` scan-7 (count must stay correct); logger grep-gate covers `signalforge.airflow`? (verify — see open item); `--profiles-dir`/`DBT_PROFILES_DIR` env-mutate-don't-restore pattern.
- **python-build.md / testing-signal.md** — `[airflow]` extra additively in `uv.lock`; no `assert True`; secret-absence assertions; planted-violation self-checks for any new AST scan.

**No `workflow-project.md` found** — rules apply uniformly.

### Open items surfaced (carry into refinement)

1. **Logger grep-gate dir set** — confirm whether `tests/llm/test_logger_grep_gate.py` already scans `src/signalforge/airflow`; if the hook logs, it must use lazy-format JSON regardless.
2. **In-process env-restore set** — if the hook injects a provider key env var for `invocation="in_process"`, the `_ISOLATED_ENV_KEYS` set (or an equivalent restore) must cover the key so a long-lived worker doesn't retain it across tasks. This is a real secrets-hygiene + isolation interaction.
3. **`AirflowConfigError` sufficiency** — whether hook misconfig (missing conn, missing key, bad profiles_dir) reuses `AirflowConfigError` (tier 2) or needs a new concrete (`AirflowConnectionError`). Reuse preferred unless remediation differs materially.

---

## Phase 2: Architecture Review

Reviewed areas (Performance / Data-model omitted — N/A for a credential-resolving hook with no DB/queries).

| Area | Rating | Headline finding |
|---|---|---|
| Security (secrets) | **concern** | Approach sound (key flows env → vendor SDK → audit-hash-only, never stored). 4 mandatory disciplines, all bakeable as ACs: `mask_secret` timing, `__repr__` redaction, `profiles_dir` canonicalisation, **closed provider allowlist**. 2 documented caveats: in-process concurrency window + hard-kill bypasses `finally`. |
| Testing | **pass** | Clean ungated/gated split; pure resolver 100% ungated (codecov patch gate); ~11 ungated + ~17 gated cases enumerated. **No new AST scan** — the existing `_airflow_compat` import-confinement scan already globs `hooks.py`. |
| API design | **concern** | Cost ceilings in `extra` are **undeliverable in v0.7** (no CLI landing strip; #232 DEC-002 deferred `config_overrides`) → trim them. Precedence rule needed; typed `HookResolution`; reuse `AirflowConfigError`; `PROVIDER_ENV_VAR_KEYS` home; `extra="forbid"` extra-model. |
| Observability + isolation | **pass** | Env-restore belongs at the **operator** seam, not the runner (`_ISOLATED_ENV_KEYS` stays unchanged); two-layer restore composes cleanly. Logger grep-gate does **not** yet scan `src/signalforge/airflow` → add it + update `cli-layer.md`. Log shape: conn_id / provider / key_source / `profiles_dir_set` bool — never the key. |

**No blockers that change the approach.** Security "blockers" are mandatory ACs, not redesigns. The one real scope change is trimming cost ceilings from the v0.7 Connection `extra`.

## Phase 3: Refinement Log

### Decisions (from discovery answers + architecture review)

- **DEC-001 — Warehouse auth = on-disk profiles only (option a).** Hook resolves `profiles_dir`; operator passes it as `--profiles-dir` (→ `DBT_PROFILES_DIR`), feeding the existing `load_profile` → `from_profile` seam. Connection-synthesis of a `profiles.yml`/`DbtProfileTarget` (option b) is explicitly deferred. _Rationale: the path already works end-to-end; re-deriving the #120 per-type validator from a Connection is its own ticket._
- **DEC-002 — `signalforge_conn_id` wires through BOTH operators (REVISED after #233 merge).** #234 adds the optional param to `SignalForgeGenerateOperator` AND `SignalForgePruneExistingOperator` (now merged on `dev`). The two consumers differ: Generate needs `profiles_dir` + `provider` + `api_key`; PruneExisting needs `profiles_dir` ONLY (no LLM call → no key, no `provider`, no `cache_scope`). _Supersedes the session-1 deferral; the original "leave a note on #233" action is moot — #233 shipped and #234 owns the wiring for both._
- **DEC-003 — API key source: `Connection.password` primary, Airflow `Variable` fallback.** `provider` + `profiles_dir` come from `extra_dejson`. Key-source recorded (for the log) as `password | variable | absent`. **The resolver is lenient** — it returns `api_key`/`provider` as `None` when absent; *requiredness is enforced by the consumer*: the Generate operator raises `AirflowConfigError` when `api_key`/`provider` is missing, PruneExisting never checks them (it needs neither). This is what lets one resolver serve both operators (see DEC-016).
- **DEC-004 — Pure resolver + typed result; operator injects env.** Airflow-free `resolve_connection(conn, variable_lookup) -> HookResolution(profiles_dir, provider, api_key)` at module scope (100% ungated). Gated `SignalForgeHook.get_conn()` delegates to it. Operator `execute()` snapshots → injects `os.environ[PROVIDER_ENV_VAR]` → `run_signalforge` → restores in `finally` (absent-before→delete-after; prior-value→restore-prior).
- **DEC-005 — Closed provider→env-var allowlist.** `PROVIDER_ENV_VAR_KEYS = {"anthropic":"ANTHROPIC_API_KEY","openai":"OPENAI_API_KEY","gemini":"GOOGLE_API_KEY"}` in `signalforge.llm.providers` (sibling to `PROVIDER_DEFAULT_MODELS`/`PROVIDER_SKU_PREFIXES`, reusable by the v0.8 GH Action). The allowlist is validated **whenever `provider` is present** (regardless of consumer): a non-`None` unknown `provider` raises `AirflowConfigError` — never derives an arbitrary env-var name. The env-var lookup itself is used only by the Generate operator's injection path. _Security: closes the arbitrary-env-var injection vector even for a prune-existing-only Connection that happens to set `provider`._
- **DEC-006 — `mask_secret` confined to the shim, called at the operator seam.** New `_airflow_compat.register_secret(value)` (lazy `from airflow.utils.log.secrets_masker import mask_secret`, `# pragma: no cover`, `# type: ignore[import-not-found]`). Operator `execute()` calls it **immediately after resolution, before any logging or `run_signalforge`**. Belt-and-braces over Airflow's auto-masking of `password`/sensitive-`extra` keys.
- **DEC-007 — Four leak-surface disciplines are ACs, each pinned by a test.** (1) `signalforge_conn_id` NOT in `template_fields` (design-time assertion). (2) Never returned into XCom (`to_xcom()` already counts+paths only; key held in a local, never on `self`). (3) `SignalForgeHook.__repr__` + `HookResolution.__repr__` show only `conn_id`/`provider`, never the key or field-name labels (mirror `tests/warehouse/test_snowflake_stub.py`). (4) `mask_secret` for logs (DEC-006).
- **DEC-008 — `profiles_dir` from `extra` is symlink-hardened.** Route through `signalforge._common.path_safety.canonicalise_path`; `PathContainmentError` → `AirflowConfigError`. Containment anchor = `project_dir` when available (the operator has it), else suffix-only with the documented gap (mirrors the init-demo seam pattern).
- **DEC-009 — Reuse `AirflowConfigError` (tier 2); no new error class.** Missing conn / missing-key (Generate only) / unknown-provider / bad-`profiles_dir` are all input-validation. No `errors.py` scan-7 churn, no exit-code-table change. PruneExisting's own `_validate_prune_existing_config` already raises `AirflowConfigError` — same class, reused.
- **DEC-010 — `extra` validated by an `extra="forbid"` Pydantic model; all fields optional.** `_ConnectionExtra(profiles_dir: str|None = None, provider: str|None = None, cache_scope: str|None = None)`. `provider` is optional (a prune-existing-only Connection legitimately omits it); when present it's allowlist-checked (DEC-005). Typos (`cache_scop`) fail loud at resolution. _Mirrors safety-layer.md DEC-015._
- **DEC-011 — Cost ceilings trimmed from the v0.7 `extra` schema.** No CLI flag delivers `max_grade_*` (#232 DEC-002 deferred `config_overrides`); storing them would be a dead affordance. Documented as a follow-up gated on an operator `--config` overlay landing.
- **DEC-012 — Precedence: explicit operator param > Connection `extra` > default.** Mirrors CLI `flag > YAML > default`. Applies to `profiles_dir` and `cache_scope` (the two knobs that exist on both surfaces).
- **DEC-013 — Logger grep-gate extended to `src/signalforge/airflow`.** Add `"airflow"` to `_SCAN_SUBPACKAGES` in `tests/llm/test_logger_grep_gate.py`; update the dir-set wording in `cli-layer.md`/`diff-renderer.md` (they list 6; the test already scans 10). Any hook `_LOGGER` call uses lazy-format `json.dumps`.

- **DEC-014 — `signalforge_conn_id: str | None = None` (optional).** When `None`, the operator behaves exactly as #232 (ambient env / inline config) — byte-compatible, zero change for existing DAGs. When set, the hook resolves credentials. The Airflow-native path is opt-in.
- **DEC-015 — `invocation` default stays `in_process`; concurrency caveat documented.** No auto-promotion to subprocess. The per-task snapshot/restore `finally` scopes the key; docs state subprocess is the safe choice for concurrent multi-task workers (in-process shares `os.environ` + process-global `redirect_stdout`). Operator keeps explicit control via the existing `invocation` param.
- **DEC-016 — PruneExisting wiring resolves warehouse auth ONLY; no key injection (NEW, post-#233).** `SignalForgePruneExistingOperator` makes no LLM call, so its `signalforge_conn_id` path uses only the resolved `profiles_dir` (precedence: param > `extra` > default, per DEC-012). It does NOT call `register_secret`, does NOT inject any provider env var, and ignores `provider`/`api_key` on the resolution. A shared helper (extracted in US-005, reused in US-006) does the conn→`HookResolution` call + `profiles_dir` precedence; only the Generate path adds the key-injection + masking + env-restore wrapper. `signalforge_conn_id` must NOT enter either operator's `template_fields`.

## Phase 4: Detailed Breakdown

**Validation command (every story's AC):** `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

Architecture ordering: shared infra → grep-gate → pure resolver → gated hook → operator wiring → docs/example → Quality Gate → Patterns.

### US-001 — `PROVIDER_ENV_VAR_KEYS` shared table
- **Traces to:** DEC-005.
- **Description:** Add the closed provider→env-var allowlist to `signalforge.llm.providers`, sibling to `PROVIDER_DEFAULT_MODELS`/`PROVIDER_SKU_PREFIXES`. The single source of truth for "which env var carries provider X's key" — reusable by the v0.8 GH Action.
- **Files:** `src/signalforge/llm/providers.py` (add `PROVIDER_ENV_VAR_KEYS = {"anthropic":"ANTHROPIC_API_KEY","openai":"OPENAI_API_KEY","gemini":"GOOGLE_API_KEY"}` + export); `tests/llm/test_providers.py` (or nearest) — test every key is a registered provider, and the table's keys match `PROVIDER_DEFAULT_MODELS`'s keys.
- **AC:** Table present + exported; test pins parity with the registered providers; validation passes.
- **Done when:** `from signalforge.llm.providers import PROVIDER_ENV_VAR_KEYS` resolves all three providers.
- **Depends on:** none.

### US-002 — Extend logger grep-gate to `signalforge.airflow`
- **Traces to:** DEC-013.
- **Description:** The lazy-format logger gate does not yet scan `src/signalforge/airflow`; the hook + operator will log. Add `"airflow"` to the scan set and correct the dir-set wording in the rule files (they say 6; the test already scans 10).
- **Files:** `tests/llm/test_logger_grep_gate.py` (add `"airflow"` to `_SCAN_SUBPACKAGES`); `.claude/rules/cli-layer.md` + `.claude/rules/diff-renderer.md` (dir-set wording — **orchestrator-applied**, `.claude/` is not worker-writable).
- **AC:** Gate scans `src/signalforge/airflow` and passes against current tree; rule wording matches the actual scan set; validation passes.
- **Done when:** an `_LOGGER.x(f"…")` planted in an airflow module fails the gate.
- **Depends on:** none.

### US-003 — Airflow-free pure resolver + typed models (100% ungated)
- **Traces to:** DEC-003, DEC-004 (pure half), DEC-005, DEC-008, DEC-009, DEC-010.
- **Description:** The airflow-free heart. `HookResolution` frozen dataclass `(profiles_dir: str|None, provider: str, api_key: str|None)` with redacting `__repr__`; `_ConnectionExtra` Pydantic model (`extra="forbid"`); `resolve_connection(conn_like, *, variable_lookup, project_dir=None) -> HookResolution` — reads `password`→Variable-fallback for the key, `provider`/`profiles_dir` from validated `extra`, enforces the `PROVIDER_ENV_VAR_KEYS` allowlist, canonicalises `profiles_dir`, raises `AirflowConfigError` on misconfig. Takes a duck-typed conn (`.password`, `.extra_dejson`) + an injected `variable_lookup` callable so it needs no airflow import.
- **Files:** new `src/signalforge/airflow/_resolve.py` (pure module); `src/signalforge/airflow/errors.py` (confirm `AirflowConfigError` remediation covers the new cases); new `tests/airflow/test_resolve.py` (ungated).
- **TDD cases:** provider+key from `password`; Variable fallback when no `password`; unknown provider → `AirflowConfigError`; missing key from both → `AirflowConfigError`; `extra` typo → `extra="forbid"` raise; `profiles_dir` symlink-escape → `AirflowConfigError`; `__repr__` omits key substring + field-name labels; frozen dataclass.
- **AC:** 100% ungated coverage of `_resolve.py` (codecov patch gate); no `from airflow` import in the module; validation passes.
- **Done when:** every TDD case passes ungated.
- **Depends on:** US-001.

### US-004 — Real `SignalForgeHook(BaseHook)` + `register_secret` shim (gated)
- **Traces to:** DEC-004 (gated half), DEC-006, DEC-007 (repr/logs).
- **Description:** Replace the `hooks.py` stub with the real hook via the deferred-construction pattern (module `__getattr__` + `functools.cache` factory + `find_spec("airflow")` branch + airflow-free placeholder raising `ModuleNotFoundError`). `get_conn()` builds the `variable_lookup` from `Variable.get(k, default_var=None)` and delegates to `resolve_connection`. Add `_airflow_compat.register_secret(value)` (lazy `from airflow.utils.log.secrets_masker import mask_secret`, `# pragma: no cover`, `# type: ignore[import-not-found]`). Redacting `__repr__` on the hook.
- **Files:** `src/signalforge/airflow/hooks.py` (real hook); `src/signalforge/airflow/_airflow_compat.py` (+`register_secret`, +`__all__`); `src/signalforge/airflow/__init__.py` (lazy `__getattr__` already maps `SignalForgeHook` → confirm); new `tests/airflow/test_hooks.py` (gated `@pytest.mark.airflow` + in-test `importorskip`).
- **TDD cases (gated):** `get_conn()` happy path; Variable fallback; missing-conn / unknown-provider / missing-key → `AirflowConfigError`; hook never logs the raw key (caplog substring absent); masker redacts to `***` (SecretsMasker-filtered buffer); `__repr__` redaction; airflow-free access + construction-requires-airflow (ungated skeleton test in `test_skeleton.py`).
- **AC:** import-confinement scan still passes (no module-scope `from airflow` in `hooks.py`); gated tests pass under the airflow rig; validation passes (ungated portion).
- **Done when:** `SignalForgeHook(conn_id).get_conn()` returns a `HookResolution` against a fake connection.
- **Depends on:** US-003, US-002.

### US-005 — Wire `signalforge_conn_id` through `SignalForgeGenerateOperator` (+ shared helper)
- **Traces to:** DEC-002, DEC-006, DEC-007 (template/XCom), DEC-012, DEC-014, DEC-015, DEC-016.
- **Description:** Add `signalforge_conn_id: str | None = None` to the operator `__init__` (NOT in `template_fields`). Extract a **shared, reusable helper** (consumed again by US-006) that, given a `conn_id`, calls the hook → `HookResolution` and applies precedence (param > `extra` > default) for `profiles_dir`/`cache_scope` — e.g. `_apply_hook_resolution(...)` plus a key-injection context manager `_provider_key_env(provider, api_key)`. When `conn_id` is set, Generate's `execute()`: resolve → require `provider`+`api_key` (else `AirflowConfigError`) → `register_secret(api_key)` → precedence-merge profiles_dir/cache_scope → enter `_provider_key_env` (snapshot `os.environ[PROVIDER_ENV_VAR]`, inject, restore in `finally`: absent→delete, prior→restore) → `run_signalforge(...)`. Single + batch paths. INFO log: conn_id/provider/key_source/`profiles_dir_set` (never the key). When `None`, behaviour is byte-identical to #232.
- **Files:** `src/signalforge/airflow/operators.py` (+ shared helper module-level fns); `tests/airflow/test_operators_helpers.py` (ungated — precedence + helper logic); `tests/airflow/test_operators.py` (gated — env inject/restore success+exception, XCom key-absence, `signalforge_conn_id` ∉ `template_fields`, single+batch with conn_id, missing-key→`AirflowConfigError`).
- **AC:** `conn_id=None` path unchanged from #232 (existing tests green); env restored on success AND exception; key absent from XCom + rendered fields; the shared helper is module-level + ungated-testable; validation passes.
- **Done when:** a Generate operator built with a `signalforge_conn_id` injects the right env var around `run_signalforge` and restores it.
- **Depends on:** US-004.

### US-006 — Wire `signalforge_conn_id` through `SignalForgePruneExistingOperator`
- **Traces to:** DEC-002, DEC-007 (template/XCom), DEC-012, DEC-016.
- **Description:** Add `signalforge_conn_id: str | None = None` to the merged-#233 operator `__init__` (NOT in its `template_fields`). When set, `execute()` resolves via the hook and applies **only** the `profiles_dir` precedence (param > `extra` > default) using the US-005 shared helper. **No `register_secret`, no provider env-var injection** — prune-existing makes no LLM call (DEC-016); `provider`/`api_key`/`cache_scope` on the resolution are ignored (an allowlist-invalid `provider`, if present, still raises per DEC-005). INFO log: conn_id/`profiles_dir_set`. When `None`, behaviour is byte-identical to #233.
- **Files:** `src/signalforge/airflow/operators.py` (prune-existing `__init__` + `execute`); `tests/airflow/test_operators_helpers.py` (ungated — prune-existing profiles_dir precedence); `tests/airflow/test_operators.py` (gated — conn-resolved `profiles_dir` → argv, NO provider env var touched, `signalforge_conn_id` ∉ `template_fields`, `conn_id=None` unchanged).
- **AC:** `conn_id=None` path unchanged from #233; resolved `profiles_dir` reaches `--profiles-dir`; no provider env var is set/restored on this path; validation passes.
- **Done when:** a PruneExisting operator with a `signalforge_conn_id` runs with the conn-resolved `profiles_dir` and injects no LLM key.
- **Depends on:** US-005.

### US-007 — `docs/airflow-ops.md` + example DAG (acceptance A8)
- **Traces to:** DEC-001, DEC-003, DEC-010, DEC-011, DEC-012, DEC-015, DEC-016.
- **Description:** Document the Connection `extra` schema (`profiles_dir`, `provider`, `cache_scope`; note cost-ceilings deferral), the `password`+Variable key precedence, secrets-hygiene guarantees (4 surfaces), the in-process concurrency caveat (prefer subprocess for concurrent workers), the `.venv-airflow` certification command, and **the Generate-vs-PruneExisting credential difference** (PruneExisting needs only warehouse auth, no LLM key — a prune-existing-only Connection can omit `provider`/key). Ship an example DAG configuring **both** operators via a Connection + Variable with no inline per-task env.
- **Files:** `docs/airflow-ops.md`; `examples/airflow/signalforge_hook_dag.py`; `tests/airflow/test_dag_parse.py` (gated DAG-parse of the new example).
- **AC:** A8 met (a DAG configures both operators via Connection + Variable, no inline env); example parses via `DagBag` (gated); docs cover the extra schema + hygiene + caveat + cert command + the two-operator credential difference; validation passes.
- **Done when:** the example DAG parses and the ops doc documents the full hook contract for both operators.
- **Depends on:** US-006.

### US-008 — Quality Gate
- **Description:** Run the code reviewer 4× across the full changeset, fixing every real bug each pass; run CodeRabbit; certify the airflow-touching paths against the real `.venv-airflow` rig (`SF_RUN_AIRFLOW=1 PYTHONPATH="$PWD/src" /path/to/.venv-airflow/bin/python -m pytest tests/airflow -m airflow --no-cov`). Project validation passes after all fixes.
- **AC:** 4 review passes complete + fixes applied; CodeRabbit addressed; `.venv-airflow` certification green; full validation passes.
- **Depends on:** US-007 (all implementation complete).

### US-009 — Patterns & Memory (priority 99)
- **Description:** Update `.claude/rules/airflow-integration.md` (hook landed: pure-resolver + shim-confined-`mask_secret` pattern, the four leak-surface disciplines, `PROVIDER_ENV_VAR_KEYS` home, on-disk-profiles DEC, cost-ceiling deferral, the two-consumer wiring where PruneExisting resolves warehouse-auth-only) — **orchestrator-applied**. Add a memory for the secrets-hygiene-across-4-surfaces hook pattern.
- **AC:** rule file reflects the shipped hook; memory written; validation passes.
- **Depends on:** US-008.

## Beads Manifest

- **Epic:** `bd_1-scaffolding-qhi`
- **Worktree:** `../worktrees/SignalForge/234-signalforge-hook` (branch `feature/234-signalforge-hook`, merged up to `dev`)
- **Tasks (dependency-ordered):**
  - `.1` US-001 — `PROVIDER_ENV_VAR_KEYS` shared table — deps: none
  - `.2` US-002 — logger grep-gate → `signalforge.airflow` — deps: none
  - `.3` US-003 — pure resolver + typed models (100% ungated) — deps: `.1`
  - `.4` US-004 — real `SignalForgeHook` + `register_secret` shim (gated) — deps: `.3`, `.2`
  - `.5` US-005 — wire `signalforge_conn_id` → GenerateOperator (+ shared helper) — deps: `.4`
  - `.6` US-006 — wire `signalforge_conn_id` → PruneExistingOperator — deps: `.5`
  - `.7` US-007 — `docs/airflow-ops.md` + example DAG (A8) — deps: `.6`
  - `.8` US-008 — Quality Gate — deps: `.7`
  - `.9` US-009 — Patterns & Memory — deps: `.8`
- **Ready at devolve:** `.1`, `.2`.
