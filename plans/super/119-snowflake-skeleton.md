# Super Plan — #119: SnowflakeAdapter skeleton (factory dispatch + Dialect + `_snowflake_client.py` shim)

## Meta

- **Ticket:** [#119](https://github.com/wjduenow/SignalForge/issues/119) — `feat: SnowflakeAdapter skeleton — factory dispatch + Dialect + _snowflake_client.py shim`
- **Epic:** [#118](https://github.com/wjduenow/SignalForge/issues/118) — Snowflake warehouse adapter (v0.2). **#119 lands first; unblocks #120–#124.**
- **Branch:** `feature/119-snowflake-skeleton` (off `dev`; PR targets `dev`)
- **Phase:** devolved (PR [#125](https://github.com/wjduenow/SignalForge/pull/125); beads epic `bd_1-scaffolding-cx3`)
- **Sessions:** 1 (2026-05-25)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** Graduate the `adapters/postgres.py` stub pattern (issue #53) to a second *real* vendor seam: a `SnowflakeAdapter` skeleton, a Snowflake `Dialect` constant, a one-shim-per-vendor `_snowflake_client.py`, factory dispatch for `profile.type == "snowflake"`, and an optional `[snowflake]` dependency. Warehouse-operation methods stay unimplemented (raise) — the *seam*, not the queries, is the deliverable.

**Why.** Architectural Commitment #3 ("warehouse-agnostic by design"). The Postgres stub already proves a second `profile.type` routes through the ABC + factory; #119 carries that proof to the vendor whose full implementation the epic actually targets, so the rest of #118 (#120 profiles, #121 compiler, #122 sampling, #123 estimate, #124 harness/docs) builds on a landed skeleton instead of inventing it inline.

**Who.** Operators with a Snowflake dbt project (v0.2+). Until #120–#124 land they get a clear typed "not yet" signal instead of `UnsupportedProfileTypeError`.

### Codebase findings

- **`adapters/postgres.py` + `tests/warehouse/test_postgres_stub.py`** — near-exact template. Stub captures conn params, `__repr__` shows only safe fields, `dialect()` returns a module constant, the three `@abstractmethod`s raise `NotImplementedError` naming the ticket, `__enter__`/`__exit__` are no-ops.
- **`base.py::WarehouseAdapter.from_profile`** — single dispatch point; `bigquery`/`postgres` branches use **lazy imports** so non-callers don't pull the SDK. `materialise_sample` / `estimate_query_bytes` are **non-abstract** ABC methods with graceful-degrade defaults (`MaterialisationNotSupportedError` / `EstimateNotSupportedError`) — the skeleton inherits both unchanged.
- **`models.py`** — `Dialect` is a frozen dataclass (`name`, `supports_tablesample`, `supports_qualify`, `quote_char`, `identifier_case: Literal["upper","lower","preserve"]`). `BIGQUERY_DIALECT` + `POSTGRES_DIALECT` constants live here and re-export from `warehouse/__init__.py`'s `__all__`.
- **`adapters/_client.py`** — the BigQuery SDK seam: confines every `# type: ignore[import-not-found]` / `# pyright: ignore[...]`; lazy-imports the SDK *inside* functions; defines a narrow duck-typed `_BQClientProtocol`; **no logging** (DEC-027).
- **`profiles.py::DbtProfileTarget`** — Pydantic v2, `extra="forbid"`, BigQuery-shaped (`type`, `method`, `project`, `dataset`/`schema`, `location`, `priority`, `maximum_bytes_billed`). **Cannot represent a Snowflake profile** (account/user/role/warehouse) today — that relaxation is #120.
- **Confinement enforcement precedent** — only the LLM side has an AST gate (`tests/test_audit_completeness.py` scan 3, `anthropic.Anthropic` only in `llm/_client.py`). **No warehouse-side SDK-confinement test exists** — #119 adds a focused one for the Snowflake shim.
- **`tests/warehouse/test_public_api.py::test_all_is_sorted`** — `warehouse.__all__` must stay sorted (capitals before lowercase). `SNOWFLAKE_DIALECT` slots between `QuerySyntaxError` and `SamplingError`.

### Scoping answers (2026-05-25)

- **Factory boundary → Mirror the Postgres stub.** `from_profile` dispatches with only the fields `DbtProfileTarget` exposes today (`project`→`database`, `dataset`→`schema`), constructing `SnowflakeAdapter` with the remaining conn params `None`. **No `profiles.py` change in #119.** #120 grows the profile model and wires `account`/`user`/`role`/`warehouse`.
- **Test tier → Unit only, mirror #53.** In-process unit tests: dialect values + identity, dispatch (no BigQuery SDK import), `__repr__` credential redaction, `NotImplementedError` on the three op methods, SDK-ignore confinement. **No `fakesnow`, no live e2e** — those are #124.

---

## Phase 2 — Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Security | **concern → resolved** | `__init__` captures `password`/secret material; `__repr__` MUST exclude it (DEC-003). No SQL is built in the skeleton (op methods raise) → no injection surface. New dependency `snowflake-connector-python` is a maintained PyPI package; major-pin (`>=3,<4`) and keep the import lazy/optional (DEC-006). |
| Performance | pass | Lazy `SnowflakeAdapter` import in `from_profile` + lazy `snowflake.connector` import in the shim → non-Snowflake users pay nothing. |
| Data Model | pass | Additive only: a new `SNOWFLAKE_DIALECT` constant + a new `__all__` entry. No existing shape changes. Backward compatible. |
| API Design | pass | `from_profile` gains one branch in the established lazy-dispatch pattern. `__all__` stays sorted (guarded by `test_all_is_sorted`). |
| Observability | pass | Shim + skeleton emit no logs (matches `_client.py` DEC-027 and stage-0 reader discipline). Logging lands when real queries do (#122). |
| Testing | **concern → resolved** | "No BigQuery import triggered" is testable only as **"no `google-cloud-bigquery` SDK import"** — the BQ *adapter module* is eagerly imported via `warehouse/__init__.py`, but the SDK itself is lazy in `_client.make_real_client`. Assert against the SDK module, not the adapter module (DEC-007). |

No blockers. Two concerns resolved into DEC-003 / DEC-006 / DEC-007.

---

## Phase 3 — Refinement (Decisions)

- **DEC-001 — Mirror the Postgres-stub dispatch; no `profiles.py` change.** `from_profile`'s `snowflake` branch lazy-imports `SnowflakeAdapter` and passes `database=profile.project, schema=profile.dataset`; all other conn params default `None`. Sets the #119/#120 boundary: #120 owns the `DbtProfileTarget` relaxation. *Rationale:* keeps the skeleton self-contained and byte-faithful to the #53 precedent the issue cites.
- **DEC-002 — `__init__` captures the forward-compat Snowflake conn surface.** Keyword-only: `account`, `user`, `password`, `role`, `warehouse`, `database`, `schema` (all `str | None = None`), stored on `self._<field>`. The three `@abstractmethod`s (`sample_rows`, `column_stats`, `run_test_sql`) raise `NotImplementedError`. *Rationale:* mirrors the Postgres stub; gives #120/#122 a ready field surface without re-touching `__init__`.
- **DEC-003 — `__repr__` redaction.** Renders only non-credential identifying fields: `account` and `warehouse`. **Never** `user` / `password` / key material. *Rationale:* `warehouse-adapters.md` repr-redaction rule; the AC's "(account/region)" is illustrative — Snowflake has no separate `region` param (the account locator encodes it), so `account` + `warehouse` is the safe, useful pair.
- **DEC-004 — `SNOWFLAKE_DIALECT` lives in `models.py`, re-exported from `warehouse/__init__.py`.** Values: `name="snowflake"`, `quote_char='"'`, `identifier_case="upper"`, `supports_qualify=True`, `supports_tablesample=True`. `dialect()` returns the **module constant by identity**. *Rationale:* dialect constants live alongside their siblings (DEC-003 of #3) so the prune compiler (#121) imports every flavour from one place. `identifier_case="upper"` is load-bearing and **opposite** Postgres — Snowflake folds unquoted identifiers to upper-case, which #121's anchor-contract column matching depends on.
- **DEC-005 — `_snowflake_client.py` is the one-shim-per-vendor SDK seam.** Defines a narrow duck-typed `_SnowflakeClientProtocol` (the surface the real adapter will consume — `cursor()` / `execute` / `fetchall` / `close`) and a lazy `make_real_client(...)` that does `import snowflake.connector` **inside the function** with the confined `# type: ignore[import-not-found]`. **Every** `snowflake-connector-python` type-ignore/pyright-ignore lives here. *Rationale:* `warehouse-adapters.md` "`_client.py` contains every `# pyright: ignore`" + the one-shim-per-vendor rule; a focused test enforces confinement.
- **DEC-006 — `snowflake-connector-python` is an optional `[snowflake]` extra + dev group.** Major-pinned `>=3,<4`; added to `[project.optional-dependencies].snowflake` **and** both `[dependency-groups].dev` and `[project.optional-dependencies].dev` (the two dev lists are kept in lockstep per `python-build.md`). Import stays lazy so a default install never pulls the connector. *Rationale:* the issue mandates extra + dev group; dev-group inclusion lets #122+ exercise the real connector under `uv run`.
- **DEC-007 — "no BigQuery import" tested against the SDK module, not the adapter module.** The dispatch test asserts `google.cloud.bigquery` (the SDK) is **not** imported as a side effect of `from_profile(snowflake_profile)`. *Rationale:* the BQ *adapter* module is unavoidably imported via `warehouse/__init__.py`; only the heavy SDK import is genuinely lazy and meaningful to assert.
- **DEC-008 — `NotImplementedError` messages reference the epic #118.** Each of the three abstract methods raises with a message naming `issue #118` (the umbrella) rather than guessing a child issue, since `column_stats` has no dedicated child. `materialise_sample` / `estimate_query_bytes` are **not** overridden — the skeleton inherits the ABC's typed degrade (`MaterialisationNotSupportedError` / `EstimateNotSupportedError`). *Rationale:* gives the v0.x implementation a single grep target without mis-asserting a child boundary; keeps the conservative-bias degrade paths intact.

---

## Phase 4 — Detailed Breakdown

> Validation command (all stories): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — `SNOWFLAKE_DIALECT` constant + public re-export

**Description.** Add the `SNOWFLAKE_DIALECT` `Dialect` constant to `models.py` alongside `POSTGRES_DIALECT`, and re-export it from `warehouse/__init__.py` (keeping `__all__` sorted).

**Traces to:** DEC-004.

**Files:**
- `src/signalforge/warehouse/models.py` — add `SNOWFLAKE_DIALECT = Dialect(name="snowflake", supports_tablesample=True, supports_qualify=True, quote_char='"', identifier_case="upper")` with a docstring noting `identifier_case="upper"` is opposite Postgres and load-bearing for #121; add `"SNOWFLAKE_DIALECT"` to the module `__all__`.
- `src/signalforge/warehouse/__init__.py` — import `SNOWFLAKE_DIALECT` and insert `"SNOWFLAKE_DIALECT"` into `__all__` between `"QuerySyntaxError"` and `"SamplingError"`.

**TDD:**
- `SNOWFLAKE_DIALECT` is a `Dialect` with `name=="snowflake"`, `quote_char=='"'`, `identifier_case=="upper"`, `supports_qualify is True`, `supports_tablesample is True`.
- `sf_warehouse.SNOWFLAKE_DIALECT` is importable from the package top level.
- `test_all_is_sorted` still passes (placement guard).

**AC:** `dialect()`'s constant exists and is unit-pinned; `__all__` sorted; validation passes.
**Done when:** the constant is importable from `signalforge.warehouse` and pinned by tests.
**Depends on:** none.

### US-002 — `_snowflake_client.py` SDK shim + `[snowflake]` dependency

**Description.** Add the one-shim-per-vendor SDK seam confining every `snowflake-connector-python` type-ignore, and register the optional dependency.

**Traces to:** DEC-005, DEC-006.

**Files:**
- `src/signalforge/warehouse/adapters/_snowflake_client.py` (new) — module docstring mirroring `_client.py`'s "confine pyright noise / no logging" framing; `_SnowflakeClientProtocol(Protocol)` duck-typed at the consume surface (`cursor`, `execute`, `fetchall`, `close`); `make_real_client(*, account, user, password, role, warehouse, database, schema) -> _SnowflakeClientProtocol` lazy-importing `snowflake.connector` inside the function body with confined `# type: ignore[import-not-found]`; `__all__`.
- `pyproject.toml` — add `[project.optional-dependencies].snowflake = ["snowflake-connector-python>=3,<4"]`; add `"snowflake-connector-python>=3,<4"` to both `[dependency-groups].dev` and `[project.optional-dependencies].dev`.

**TDD:**
- Importing `signalforge.warehouse.adapters._snowflake_client` does **not** require `snowflake.connector` at import time (the SDK import is lazy inside `make_real_client`).
- `_SnowflakeClientProtocol` is `runtime_checkable`-or-structural as appropriate; a minimal fake satisfies it.
- **Confinement test** (new, in `tests/warehouse/`): scan `src/signalforge/warehouse/adapters/*.py`; assert no `type: ignore`/`pyright: ignore` mentioning `snowflake` appears outside `_snowflake_client.py`. (Lightweight grep/AST mirroring the spirit of `test_audit_completeness.py` scan 3.)

**AC:** all SDK type-ignores confined to `_snowflake_client.py`; `[snowflake]` extra + dev group present; validation passes.
**Done when:** shim imports cleanly without the connector installed; confinement test green.
**Depends on:** none (parallel with US-001).

### US-003 — `SnowflakeAdapter` skeleton + factory dispatch

**Description.** Add the `SnowflakeAdapter(WarehouseAdapter)` skeleton and the `profile.type == "snowflake"` branch in `from_profile`.

**Traces to:** DEC-001, DEC-002, DEC-003, DEC-007, DEC-008.

**Files:**
- `src/signalforge/warehouse/adapters/snowflake.py` (new) — `SnowflakeAdapter`: `__init__` per DEC-002; `__repr__` per DEC-003; no-op `__enter__`/`__exit__`; `dialect()` returns `SNOWFLAKE_DIALECT` (by identity); `sample_rows`/`column_stats`/`run_test_sql` raise `NotImplementedError("…issue #118…")`; `materialise_sample`/`estimate_query_bytes` **not** overridden. Module docstring mirrors `postgres.py`. `__all__ = ["SNOWFLAKE_DIALECT", "SnowflakeAdapter"]`.
- `src/signalforge/warehouse/base.py` — add the `if profile.type == "snowflake":` branch: lazy-import `SnowflakeAdapter`, return `SnowflakeAdapter(database=profile.project, schema=profile.dataset)`; update the `from_profile` docstring's supported-types list.

**TDD:**
- `from_profile(DbtProfileTarget(type="snowflake", project="db", schema="sch"))` returns a `SnowflakeAdapter` with `_database=="db"`, `_schema=="sch"`. (No `dataset`/`schema` mismatch: `DbtProfileTarget.dataset` carries `alias="schema"` with `populate_by_name=True`, so the dbt-facing `schema="sch"` YAML key populates the `dataset` field — `profile.dataset` then yields `"sch"`, which the dispatch passes as `schema=`.)
- The dispatch does **not** import `google.cloud.bigquery` (DEC-007 — assert SDK module absent from `sys.modules` after a clean dispatch, or via an import tracker).
- `dialect()` returns `SNOWFLAKE_DIALECT` by identity.
- `repr(SnowflakeAdapter(account="ac", user="u", password="secret", warehouse="wh"))` contains `account`/`warehouse`, contains neither `"u"`-as-user nor `"secret"`.
- `sample_rows` / `column_stats` / `run_test_sql` raise `NotImplementedError` with `"issue #118"` in the message.
- `with SnowflakeAdapter() as a:` works (no-op CM parity).
- `materialise_sample(...)` raises `MaterialisationNotSupportedError`; `estimate_query_bytes(...)` raises `EstimateNotSupportedError` (inherited ABC defaults).

**AC:** all four issue ACs met; validation passes.
**Done when:** dispatch + skeleton are pinned by the tests above.
**Depends on:** US-001 (dialect), US-002 (shim).

### US-004 — Quality Gate

**Description.** Run the code reviewer 4× across the full changeset, fixing every real bug each pass; run CodeRabbit if available; validation must pass after fixes.

**Traces to:** all DECs.
**AC:** 4 review passes complete with fixes applied; CodeRabbit findings triaged; `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` green.
**Done when:** no real findings remain; validation passes.
**Depends on:** US-003.

### US-005 — Patterns & Memory

**Description.** Record the second-real-adapter pattern across the doc/rule surfaces.

**Traces to:** DEC-001 … DEC-008.

**Files:**
- `.claude/rules/warehouse-adapters.md` — add a "Snowflake skeleton (#119)" note under the second-adapter-stub section: `identifier_case="upper"` divergence, one-shim-per-vendor confinement now has a warehouse-side test, `from_profile`/`profiles.py` boundary deferred to #120.
- `CLAUDE.md` — public-API surface: add `SnowflakeAdapter`, `SNOWFLAKE_DIALECT` to the warehouse re-export list with a one-line v0.2 note.
- `docs/warehouse-adapter-ops.md` — note the Snowflake skeleton + the `[snowflake]` extra install (`pip install "signalforge-dbt[snowflake]"`).

**AC:** rule + CLAUDE.md + ops doc updated in lockstep; validation passes.
**Done when:** the four-surface parity (rule / CLAUDE.md / ops doc / tests) is consistent.
**Depends on:** US-004.

---

## Rules compliance

- **`warehouse-adapters.md`** — subpackage layout (concrete under `adapters/`, per-vendor `_<vendor>_client.py` shim, ABC + lazy factory), repr redaction, no logging in shim. ✔ US-002/US-003.
- **`python-build.md`** — `[project.optional-dependencies].dev` mirrors `[dependency-groups].dev`; major-pin third-party deps. ✔ US-002 (DEC-006).
- **`manifest-readers.md` / stage-0 discipline** — no logging in the skeleton/shim. ✔ DEC-005.
- **`testing-signal.md`** — every test can fail (dialect drift, dispatch regression, repr leak, confinement). No `assert True`. ✔ all TDD blocks.
- **`cli-layer.md` exit-code scan** — N/A: #119 adds no new `*Error` subclass (inherits existing `WarehouseError` degrade types).

## Beads Manifest

- **Epic:** `bd_1-scaffolding-cx3`
- **US-001** dialect → `bd_1-scaffolding-cx3.1` (ready)
- **US-002** shim + dep → `bd_1-scaffolding-cx3.2` (ready)
- **US-003** adapter + dispatch → `bd_1-scaffolding-cx3.3` (blocked on .1, .2)
- **Quality Gate** → `bd_1-scaffolding-cx3.4` (blocked on .3)
- **Patterns & Memory** → `bd_1-scaffolding-cx3.5` (blocked on .4)
- **Branch:** `feature/119-snowflake-skeleton`
