# Super Plan — #222: parse `type: databricks` dbt profile target (`DbtProfileTarget`)

## Meta

- **Ticket:** [#222](https://github.com/wjduenow/SignalForge/issues/222) — `Databricks: parse 'type: databricks' dbt profile target`
- **Epic:** [#219](https://github.com/wjduenow/SignalForge/issues/219) — Databricks warehouse adapter (v0.x). **#221 (skeleton) landed; #222 grows the profile model so the rest of the epic (#223 compiler, #224 sampling, #226 live e2e) has typed inputs.**
- **Depends on:** #221 (skeleton — landed). **Models on:** #120 (Snowflake profile — landed; this is the per-warehouse twin).
- **Branch:** `feature/222-databricks-profile` (off `dev`; PR targets `dev`)
- **PR:** [#255](https://github.com/wjduenow/SignalForge/pull/255) (draft)
- **Phase:** published
- **Sessions:** 1 (2026-06-24)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** Teach `signalforge.warehouse.profiles.DbtProfileTarget` / `load_profile` to parse a `type: databricks` dbt target into the existing unified profile model (single class, discriminated-by-validator — **not** a union), so `WarehouseAdapter.from_profile`'s `databricks` branch (landed with placeholder `catalog=profile.project, schema=profile.dataset` in #221) has the real `host` / `http_path` / `token` / `catalog` (+ forward-compat OAuth `client_id` / `client_secret` / `auth_type`) inputs.

**Why.** #221 left a documented boundary: the unified `DbtProfileTarget` (`extra="forbid"`) cannot represent a Databricks profile. A real dbt-databricks `profiles.yml` (with `host:` / `http_path:` / `token:`) fails `extra="forbid"` validation today. #222 is the unblocker for #223/#224/#226 — they need a typed profile to construct a working adapter.

**Who.** Operators with a Databricks dbt project (v0.x). After #222 their `profiles.yml` parses; the adapter still raises `NotImplementedError` on warehouse ops until #224.

### Codebase findings

- **`profiles.py::DbtProfileTarget`** — Pydantic v2 `frozen=True, extra="forbid", populate_by_name=True`. Already unified across BigQuery + Snowflake (#120). Carries `_BIGQUERY_ONLY` / `_SNOWFLAKE_ONLY` frozensets, `_SNOWFLAKE_REQUIRED`, and a `@model_validator(mode="after") _validate_type_field_coherence` that (per type) checks required keys → `IncompleteProfileError`, rejects foreign fields → plain `ValueError`, runs identifier hygiene, and validates the auth scope. **#222 adds a `databricks` arm to this exact validator** + the new fields + `_DATABRICKS_ONLY` / `_DATABRICKS_REQUIRED`.
- **`base.py::from_profile`** — single dispatch point. `databricks` branch currently `DatabricksAdapter(catalog=profile.project, schema=profile.dataset)` with a `#222 will wire…` comment. `DatabricksAdapter.__init__` (skeleton, #221) **already accepts** `host` / `http_path` / `token` / `catalog` / `schema` / `auth_type` / `client_id` / `client_secret` (all `str | None`) + an injectable `connection=`. So the adapter surface is ready — #222 just wires real parsed values.
- **`_sql_safety.py`** — `validate_identifier` (strict `^[A-Za-z_][A-Za-z0-9_]*$`, no length bound) for SQL identifiers; `validate_project_id` (hyphen-permissive, 6–30 chars) for GCP; `validate_snowflake_account` (permissive `^[A-Za-z0-9][A-Za-z0-9._-]{1,253}$`, **never SQL** — log-injection hygiene only). **No host / http_path validator exists** — http_path's `/sql/1.0/warehouses/<id>` slashes are rejected by every current validator.
- **`errors.py`** — `WarehouseError` base + `default_remediation` + `__str__`. `IncompleteProfileError(profile_type, missing)` (collect-all), `UnsupportedAuthMethodError(method, *, remediation=...)`, `InvalidIdentifierError(field, value)` all exist and are reused as-is. `_SNOWFLAKE_DEFERRED_AUTH_REMEDIATION` is a shared deferred-auth remediation constant. `__all__` is alphabetically sorted, guarded by `tests/warehouse/test_errors.py`. **No new error class is needed** (the ticket reuses the existing three).
- **`adapters/databricks.py`** (skeleton, #221) — `DATABRICKS_DIALECT` (`quote_char='`'`, `identifier_case='lower'`, sign-bit-masked `xxhash64` sampling); `__repr__` already redacts `token`/`schema`/`client_secret` (shows only `host`/`http_path`/`catalog`); `make_real_client` uses PAT (`token`) only; `_databricks_client.py` confines the SDK.
- **`test_profiles.py`** — the drift-detector pattern (per-type): a `StrictSnowflakeModel(extra="forbid")` mirror validates `dbt_snowflake_drift_v1_x.yml`; `_snowflake_target(**overrides)` helper builds representative dicts; parametrized tests cover required/foreign/identifier/auth paths + an end-to-end `load_profile` parse. **#222 mirrors this for Databricks** (`StrictDatabricksModel`, `dbt_databricks_drift_v1_x.yml`, `_databricks_target` helper).
- **`TableRef.project` gotcha (⚠️, #224 not #222).** `TableRef.project` validates as a GCP project id (6–30 chars), so a Unity Catalog catalog like `main` would fail `TableRef`. This does **not** affect #222: the **profile** field `catalog` becomes SQL via `validate_identifier` (no length bound — `main`/`workspace` pass). The `TableRef` collision is a sampling-path (#224) concern.
- **dbt-databricks target keys (reference).** Required: `host`, `http_path`, `schema`. Auth: PAT (`token`) or OAuth-M2M (`auth_type: oauth` + `client_id` + `client_secret`). Optional: `catalog` (Unity Catalog), `threads`. `host` is a workspace hostname (`dbc-xxxx.cloud.databricks.com`, no scheme); `http_path` is `/sql/1.0/warehouses/<id>` or `/sql/protocolv1/...`.

### Key design points (drive the scoping questions)

1. **Auth-scope tension.** The ticket scope says "resolve the token-vs-OAuth required-set logic here (`client_id`/`client_secret` required when `auth_type` is OAuth)" AND "`auth_type ∈ {None/"pat", "oauth"}`; defer anything else." But epic open-decision #3 + the #221 skeleton (`make_real_client` PAT-only) say **PAT only for v0.x**. So OAuth fields can be *validated coherently* now while the OAuth *connection* stays deferred — or OAuth can be deferred entirely at parse time. (Q1.)
2. **`http_path` / `host` validator design.** The ticket flags ⚠️ "decide whether `http_path`'s `/` needs a dedicated permissive regex like `validate_snowflake_account`." `host`/`http_path` are **not SQL** (the connector consumes them) → permissive validators, but the shape choice (two dedicated vs one shared vs none) is open. (Q2.)
3. **Test depth.** #222 acceptance is purely parse-level; live connection is #226. Confirm parsing-only scope. (Q3.)

### Scoping answers (2026-06-24)

- **Q1 Auth scope → Validate both, connect via PAT.** `auth_type ∈ {None, "pat", "oauth"}`. PAT path (`None`/`"pat"`) → require `token`. OAuth path (`"oauth"`) → require `client_id` + `client_secret`. Anything else → `UnsupportedAuthMethodError` (reuse the shared deferred-auth remediation, generalised wording). OAuth fields parse/validate coherently now; the OAuth *connection* stays deferred (skeleton `make_real_client` is PAT-only). `_DATABRICKS_REQUIRED` is resolved conditionally in the validator, not a flat tuple.
- **Q2 host/http_path → Two dedicated permissive validators.** New `validate_databricks_hostname` + `validate_databricks_http_path` in `_sql_safety.py`, both reusing `InvalidIdentifierError`, both **non-SQL** (log-injection hygiene + fail-loud-on-garbage). `host`: hostname shape, no scheme/path/whitespace/quotes/control. `http_path`: leading `/`, `/sql/...`-shaped permissive path chars. Mirrors the `validate_snowflake_account` precedent.
- **Q3 Test depth → Parse-only.** Drift fixture + `StrictDatabricksModel` mirror + validator/required/foreign/identifier/auth tests + end-to-end `load_profile` parse. Live connection smoke stays in #226.

---

## Phase 2 — Architecture Review

Focused review — the change is pure parsing (no network, no SQL built, no new logging), additively extending an established unified model + per-type validator (#120 precedent). Areas not tabled below (Performance, API Design, Observability) are trivially **pass**: no queries, no endpoints, the public return type `DbtProfileTarget` is unchanged, and the only logging is the pre-existing `_maybe_warn_large_profile` path.

| Area | Rating | Finding |
|---|---|---|
| Security | **concern → resolved** | `token` + `client_secret` are secret material → `Field(repr=False)` (mirrors the Snowflake `password`/`private_key_passphrase` precedent + `DatabricksAdapter.__repr__` redaction, which #221 already pins). `host`/`http_path` → permissive validators block log-injection (whitespace/quotes/`;`/backticks/control). `catalog`/`schema` → strict `validate_identifier` blocks SQL-injection downstream (#223+ interpolate them as `USE CATALOG` / qualified refs). **No SQL is built in #222.** All error messages render user input via `_format_value` (`repr()`). |
| Data Model | **concern → resolved** | Additive: 7 new `Optional` fields on the one `DbtProfileTarget`; `threads`/`dataset` already shared. All existing BigQuery + Snowflake profiles parse unchanged — none set the new fields, so the new foreign-field rejection arms can't break them. Unified model keeps the single return type → zero consumer/CLAUDE.md-surface churn. The new fields land on a read-back model → **drift detector mandatory** (`StrictDatabricksModel` + fixture). |
| Testing Strategy | **concern → resolved** | Mirror the #120 test matrix: drift detector, `_databricks_target` helper, required-set (PAT + OAuth variants) → `IncompleteProfileError` collect-all, foreign-field both directions (BQ `location:` AND SF `account:` on a databricks target → `ValidationError`; databricks fields on bq/sf targets → `ValidationError`), identifier hygiene (`catalog`/`schema`), host/http_path permissive validators (good + garbage), auth scope (pat/oauth/deferred), secrets-excluded-from-repr, end-to-end `load_profile`. |
| Cross-field validator complexity | **concern → resolved** | The `databricks` arm adds conditional required-set logic (token-vs-OAuth) the bigquery/snowflake arms don't have. Mitigation: resolve `_DATABRICKS_REQUIRED` inside the arm (base `("host","http_path")` + auth-conditional extension) and collect-all missing keys into one `IncompleteProfileError`, exactly matching the existing collect-all shape. `auth_type` itself is a databricks-only field (goes in `_DATABRICKS_ONLY`). |
| Convention compliance | pass | `warehouse-adapters.md` § "Unified multi-warehouse `DbtProfileTarget`" is followed verbatim: per-type `_<X>_ONLY` frozensets drive foreign-field rejection; `IncompleteProfileError` for missing, plain `ValueError` for foreign; `mode="after"` validator inspects-and-raises only (never `model_copy`); identifier hygiene at the validator; drift detector mandatory; `from_profile` wires every field with lazy SDK import. Pydantic-v2-validator-error-wrapping caveat respected (typed `WarehouseError` propagates raw; plain `ValueError` surfaces as `ValidationError`). |

**No blockers.** All concerns resolved by the #120 precedent. Proceeding to detailing.

---

## Phase 3 — Refinement Log

| DEC | Decision | Rationale |
|---|---|---|
| **DEC-001** | Unified model + new `databricks` validator arm (NOT a union). | Ticket mandates it; keeps `from_profile`, every consumer, the public return type, and CLAUDE.md surface unchanged. Mirrors #120 DEC exactly. |
| **DEC-002** | Add `_DATABRICKS_ONLY = {host, http_path, token, catalog, client_id, client_secret, auth_type}` + conditional `_DATABRICKS_REQUIRED`. | `catalog` is the databricks analogue of BQ `project` / SF `database` → databricks-only. `auth_type` is databricks-only. Required base = `host`+`http_path`; auth-conditional adds `token` (PAT) or `client_id`+`client_secret` (OAuth). (Q1) |
| **DEC-003** | Foreign-field rejection both directions: add `_DATABRICKS_ONLY` to the bigquery + snowflake arms; the databricks arm rejects `_BIGQUERY_ONLY` ∪ `_SNOWFLAKE_ONLY`. | AC: a `location:` (BQ) or `account:` (SF) field on a `databricks` target must fail loud. Symmetric with how #120 made bigquery reject snowflake fields. |
| **DEC-004** | Auth scope: `auth_type ∈ {None,"pat","oauth"}`; pat→`token` required, oauth→`client_id`+`client_secret` required; else `UnsupportedAuthMethodError`. OAuth *connection* deferred (PAT-only `make_real_client`). | Q1=A. Validate forward-compat OAuth fields coherently now; defer the connection wiring (epic decision #3). Reuse the shared deferred-auth remediation constant, generalised to name Databricks PAT/OAuth. |
| **DEC-005** | Two dedicated permissive validators: `validate_databricks_hostname` + `validate_databricks_http_path` in `_sql_safety.py`, reusing `InvalidIdentifierError`. | Q2=A. `host`/`http_path` are connection params (never SQL) → permissive, anti-log-injection. `http_path`'s `/` needs its own regex (strict identifier rejects it). Mirrors `validate_snowflake_account`. |
| **DEC-006** | `catalog`/`schema` → strict `validate_identifier` (when present). | They become SQL downstream (`USE CATALOG`, qualified refs in #223). `validate_identifier` has no length bound, so short Unity catalogs (`main`, `workspace`) pass. `TableRef.project` length collision is a #224 concern, not #222. |
| **DEC-007** | `token` + `client_secret` → `Field(repr=False)`. | Secret material. Mirrors Snowflake `password`/`private_key_passphrase` + the `DatabricksAdapter.__repr__` redaction #221 already ships. |
| **DEC-008** | Drift detector mandatory: `StrictDatabricksModel(extra="forbid")` + `tests/fixtures/profiles/dbt_databricks_drift_v1_x.yml`. Strict mirror uses `dataset = Field(alias="schema")`. | `warehouse-adapters.md` + ticket. New read-back fields need forward-compat drift coverage; the `schema` alias avoids the Pydantic `BaseModel.schema` shadow. |
| **DEC-009** | Parse-only scope; no live connection test. | Q3=A. Live e2e is #226. #222 acceptance is purely parse-level. |
| **DEC-010** | No new error class; reuse `IncompleteProfileError` / `UnsupportedAuthMethodError` / `InvalidIdentifierError`. | The three existing errors cover missing-required / deferred-auth / bad-identifier. Avoids exit-code-table + `__all__`-sort + scan-7 churn (the #105 "don't add ceremony when the taxonomy already fits" lesson). |

---

## Phase 4 — Detailed Breakdown

Validation command (all stories): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — host/http_path permissive validators in `_sql_safety.py`

- **Description:** Add `validate_databricks_hostname(field, value)` and `validate_databricks_http_path(field, value)` — permissive, non-SQL, log-injection-hygiene validators mirroring `validate_snowflake_account`. Both raise `InvalidIdentifierError` on no-match.
- **Traces to:** DEC-005.
- **Files:** `src/signalforge/warehouse/_sql_safety.py` (2 regexes + 2 functions, with the same "never SQL — permissive by design" docstring posture as `validate_snowflake_account`).
- **Acceptance:** `host` accepts `dbc-ab12.cloud.databricks.com` / `adb-123.4.azuredatabricks.net`, rejects scheme prefix (`https://...`), whitespace, quotes, `;`, backticks, control chars, empty. `http_path` accepts `/sql/1.0/warehouses/abc123` / `/sql/protocolv1/o/0/abc`, rejects no-leading-slash, whitespace, quotes, control chars, empty. Validation command passes.
- **Done when:** Both functions exist, are unit-tested (good + adversarial), and reject garbage via `InvalidIdentifierError`.
- **Depends on:** none.
- **TDD:** good-host, scheme-prefixed host rejected, whitespace host rejected; good-http_path, no-leading-slash rejected, quoted rejected, control-char rejected.

### US-002 — Databricks fields + validator arm on `DbtProfileTarget`

- **Description:** Add the 7 new fields (`host`, `http_path`, `token` `repr=False`, `catalog`, `client_id`, `client_secret` `repr=False`, `auth_type`); add `_DATABRICKS_ONLY` frozenset + the conditional required-set logic; add the `databricks` arm to `_validate_type_field_coherence` (required → `IncompleteProfileError`; foreign BQ∪SF fields → `ValueError`; `catalog`/`schema` → `validate_identifier`; `host`/`http_path` → the US-001 validators; auth scope per DEC-004); extend the bigquery + snowflake arms to also reject `_DATABRICKS_ONLY` fields.
- **Traces to:** DEC-001, DEC-002, DEC-003, DEC-004, DEC-006, DEC-007, DEC-010.
- **Files:** `src/signalforge/warehouse/profiles.py` (fields, frozenset, validator arm, foreign-field extension to existing arms); possibly generalise `_SNOWFLAKE_DEFERRED_AUTH_REMEDIATION` usage or add a Databricks-specific deferred-auth remediation string in `errors.py`.
- **Acceptance:** A representative `type: databricks` PAT target parses; missing `http_path` → `IncompleteProfileError` listing it; `auth_type: oauth` without `client_id`/`client_secret` → `IncompleteProfileError` listing them; `auth_type: oauth` with both → parses; unknown `auth_type` → `UnsupportedAuthMethodError`; `location:` (BQ) or `account:` (SF) on a databricks target → `ValidationError`; a databricks field on a bigquery/snowflake target → `ValidationError`; `token`/`client_secret` absent from `repr()`. Validation command passes.
- **Done when:** All the above hold and existing BigQuery/Snowflake profile tests still pass unchanged.
- **Depends on:** US-001.
- **TDD:** full PAT parse; full OAuth parse; missing-http_path → IncompleteProfileError; missing OAuth creds → IncompleteProfileError; bad auth_type → UnsupportedAuthMethodError; foreign BQ field rejected; foreign SF field rejected; databricks field on bq target rejected; bad catalog identifier rejected; secrets excluded from repr.

### US-003 — Wire `from_profile` databricks branch + drift detector + `load_profile` e2e

- **Description:** Replace the #221 placeholder (`catalog=profile.project, schema=profile.dataset`) with the real field wiring (`host`/`http_path`/`token`/`catalog`/`schema=profile.dataset`/`auth_type`/`client_id`/`client_secret`); SDK import stays lazy in the branch. Add the drift fixture `dbt_databricks_drift_v1_x.yml`, the `StrictDatabricksModel` mirror + drift test, the `_databricks_target` helper, a `databricks` `load_profile` fixture (`databricks_pat.yml`), and the end-to-end `load_profile` parse test.
- **Traces to:** DEC-001, DEC-008.
- **Files:** `src/signalforge/warehouse/base.py` (databricks branch); `tests/warehouse/test_profiles.py` (`StrictDatabricksModel`, drift test, `_databricks_target`, `load_profile` test); `tests/fixtures/profiles/dbt_databricks_drift_v1_x.yml`; `tests/fixtures/profiles/databricks_pat.yml`.
- **Acceptance:** `from_profile` on a databricks target constructs a `DatabricksAdapter` with every field populated; the drift fixture validates against `StrictDatabricksModel`; `load_profile` parses a `databricks_pat.yml` end-to-end (`host`/`http_path`/`catalog` populated, `schema:`→`dataset` via alias). Validation command passes.
- **Done when:** All the above hold; drift detector trips if a fixture field is added without updating the strict mirror.
- **Depends on:** US-002.
- **TDD:** drift fixture validates; `load_profile` end-to-end parse; `from_profile` wires every field (assert on adapter `repr` / non-secret attrs).

### US-004 — Quality Gate

- **Description:** Run the code reviewer 4× across the full changeset, fixing all real bugs each pass; run CodeRabbit if available. Validation command must pass after all fixes.
- **Depends on:** US-003.

### US-005 — Patterns & Memory (priority 99)

- **Description:** Update `.claude/rules/warehouse-adapters.md` § "Unified multi-warehouse `DbtProfileTarget`" to record the Databricks arm (conditional token-vs-OAuth required-set, the two new permissive validators, `_DATABRICKS_ONLY`). Add a memory note if the conditional-required-set pattern is novel vs the flat `_SNOWFLAKE_REQUIRED`. Update `docs/warehouse-adapter-ops.md` if it enumerates supported profile types.
- **Depends on:** US-004.

---

## Beads Manifest

_Pending devolve._
