# Super Plan — #120: parse Snowflake target in dbt `profiles.yml` (`DbtProfileTarget` type=snowflake)

## Meta

- **Ticket:** [#120](https://github.com/wjduenow/SignalForge/issues/120) — `feat: parse Snowflake target in dbt profiles.yml (DbtProfileTarget type=snowflake)`
- **Epic:** [#118](https://github.com/wjduenow/SignalForge/issues/118) — Snowflake warehouse adapter (v0.2). **#119 (skeleton) landed; #120 grows the profile model so the rest of the epic has typed inputs.**
- **Branch:** `feature/120-snowflake-profile` (off `dev`; PR targets `dev`)
- **Phase:** published (PR [#126](https://github.com/wjduenow/SignalForge/pull/126))
- **Sessions:** 1 (2026-05-25)

---

## Phase 1 — Discovery

### What / Why / Who

**What.** Teach `signalforge.warehouse.profiles.load_profile` / `DbtProfileTarget` to parse a `type: snowflake` dbt target into a typed object, so `WarehouseAdapter.from_profile`'s `snowflake` branch (landed empty-ish in #119) has real `account` / `user` / `role` / `database` / `warehouse` / `schema` + auth inputs instead of only the BigQuery-shaped `project`/`dataset` mapping.

**Why.** #119 left a documented boundary: the BigQuery-shaped `DbtProfileTarget` (`extra="forbid"`) *cannot represent* a Snowflake profile. Today a real Snowflake `profiles.yml` (with `account:`/`user:`/`warehouse:`) fails `extra="forbid"` validation. #120 is the unblocker for #121 (compiler) / #122 (sampling) / #123 (estimate) — they need a typed profile to construct a working adapter.

**Who.** Operators with a Snowflake dbt project (v0.2). After #120 their `profiles.yml` parses; the adapter still raises `NotImplementedError` on warehouse ops until #122+.

### Codebase findings

- **`profiles.py::DbtProfileTarget`** — Pydantic v2, `frozen=True, extra="forbid", populate_by_name=True`. Fields today: `type`, `method`, `project`, `dataset` (alias `schema`), `location`, `priority`, `maximum_bytes_billed`. A `@field_validator("method")` accepts only `"oauth"`/`None`, else raises `UnsupportedAuthMethodError` — **BigQuery-specific** (remediation names `gcloud ADC`).
- **`extra="forbid"` is load-bearing (DEC-017).** A typo'd auth field must fail loud rather than silently fall back to wrong creds. This is *why* a Snowflake profile can't just ride the current model — its keys are "unknown."
- **`base.py::from_profile`** — single dispatch point. `snowflake` branch currently does `SnowflakeAdapter(database=profile.project, schema=profile.dataset)` with a `# #120 will grow…` comment. `SnowflakeAdapter.__init__` already accepts `account`/`user`/`password`/`role`/`warehouse`/`database`/`schema` (all `str | None`), so the field surface is ready — #120 just wires real values.
- **`_sql_safety.py`** — `validate_identifier(field, value)` (strict `^[A-Za-z_][A-Za-z0-9_]*$`) for dataset/table/column; `validate_project_id` (hyphen-permissive, 6–30 chars) for GCP project IDs. **No Snowflake account-identifier validator exists** — and the issue explicitly says don't run account through the strict regex.
- **`errors.py`** — `WarehouseError` base + `default_remediation` + `__str__` renders `↳ Remediation:`. `UnsupportedAuthMethodError(method=...)` and `InvalidIdentifierError(field, value)` already exist. **15-class hierarchy (DEC-026); `__all__` is alphabetically sorted and guarded by `tests/warehouse/test_errors.py`.** Any new error class slots in sorted order.
- **`test_profiles.py`** — the drift detector: production model is `extra="forbid"`; a test-only `StrictModel(extra="forbid")` mirrors *every* dbt-bigquery 1.9 field against `tests/fixtures/profiles/dbt_bigquery_drift_v1_9.yml`. Convention (`manifest-readers.md`): a new read-back shape needs its own drift fixture + strict mirror.
- **Consumers of `DbtProfileTarget`** — `from_profile` (reads `.type`/`.project`/`.dataset`/`.location`/`.maximum_bytes_billed`), CLI `generate.py` + `prune_existing.py` (type annotations only, then hand to `from_profile`). **No consumer reads Snowflake fields yet** except the `snowflake` factory branch we're wiring. Return type is the single exported name `DbtProfileTarget` (CLAUDE.md public surface).
- **`postgres.yml` fixture + stub** — precedent: postgres reuses `project`→dbname, `schema`→dataset on the *same* BQ-shaped model, with a fixture comment noting "a real adapter will need to relax or split the profile model — that work belongs to the v0.x ticket." #120 is the first ticket to actually grow the model.
- **dbt Snowflake target keys (reference).** Required-ish: `account`, `user`, `role`, `database`, `warehouse`, `schema`, `threads`. Auth (mutually-exclusive families): `password`; key-pair (`private_key_path` + optional `private_key_passphrase`, or inline `private_key`); SSO (`authenticator: externalbrowser`); OAuth (`authenticator: oauth` + `token`); MFA (`authenticator: username_password_mfa`).

### Key design tension (drives the scoping questions)

`extra="forbid"` makes `DbtProfileTarget` a *per-type-strict* shape, but it's a single class carrying BigQuery fields. Three ways to admit Snowflake fields:

- **(A) Unified model + cross-field validator.** Add Snowflake fields as `Optional` to the one class; a `@model_validator(mode="after")` enforces required-by-type presence AND rejects foreign fields by `type` (e.g. `warehouse` on a BigQuery target). Keeps `from_profile` + every consumer + the return type unchanged. Most minimal; matches the postgres trajectory. Cost: one non-trivial validator that hand-rolls "discriminated-union behaviour."
- **(B) Discriminated union** (`BigQueryProfileTarget | SnowflakeProfileTarget`, discriminated on `type`). Cleanest per-type strictness; each subtype keeps its own `extra="forbid"`. Cost: changes the public return type of `load_profile`, touches the drift detector, postgres parsing, CLI annotations, CLAUDE.md surface — a real refactor, and postgres would need an arm too.
- **(C) Base + subtype classes, return base.** Middle ground; more machinery than (A) for similar consumer impact.

(See Q1.)

### Scoping answers (2026-05-25)

- **Model shape → Unified + cross-field validator (Q1=A).** Add Snowflake fields as `Optional` to the one `DbtProfileTarget`; a `@model_validator(mode="after")` enforces required-by-type presence AND rejects foreign fields by `type`. Keeps `from_profile`, every consumer, the public return type, and the CLAUDE.md surface unchanged.
- **Auth scope → password + key-pair + SSO (Q2).** Parse/validate `password`, `private_key_path` (+ optional `private_key_passphrase`), and `authenticator: externalbrowser` (SSO, no stored secret). Defer OAuth (`authenticator: oauth` + `token`), inline `private_key`, and MFA (`authenticator: username_password_mfa`) — fail loud with a remediation that names the deferral.
- **Account check → permissive validator + reuse `InvalidIdentifierError` (Q3).** New `validate_snowflake_account` in `_sql_safety.py`; account is a connection param (never interpolated into SQL), so the check is anti-log-injection hygiene, not SQL safety. Do NOT run it through the strict identifier regex (rejects the hyphens/dots Snowflake account locators need).
- **Required keys → `account` / `user` / `warehouse` (Q4).** Exactly the AC trio. `role` / `database` / `schema` / `threads` / auth fields stay optional. Identifier-validate `warehouse` / `database` / `schema` *if present* via the existing `validate_identifier` (issue text mandates this helper).

---

## Phase 2 — Architecture Review

| Area | Rating | Finding |
|---|---|---|
| Security | **concern → resolved** | `password` + `private_key_passphrase` are secret material. `from_profile` wires them into `SnowflakeAdapter`, whose `__repr__` already redacts (DEC-003 of #119) — #120 **extends the redaction** to the new auth fields and pins it (test_repr_*). `account` → permissive validator blocks log-injection (quotes/newlines/SQL metachars). `database`/`schema`/`warehouse` → `validate_identifier` blocks SQL-injection downstream (#121+ interpolate them). **No SQL is built in #120.** All error messages render user input via `_format_value` (`repr()`, DEC-022). |
| Performance | pass | Pure parsing + regex. No network, no warehouse contact. |
| Data Model | **concern → resolved** | Additive: new `Optional` fields + a shared `threads: int \| None` (production fixtures never carried `threads`, so a real Snowflake profile — which essentially always sets it — would trip `extra="forbid"` today; adding it also fixes the latent BigQuery gap). All existing BigQuery profiles parse unchanged — none set the new fields, so foreign-field rejection can't break them. Unified model keeps the single `DbtProfileTarget` return type → no consumer/drift-detector churn. |
| API Design | pass | `DbtProfileTarget` stays the one exported name; `from_profile`'s `snowflake` branch graduates from `database=project, schema=dataset` to real `account`/`user`/`role`/`warehouse`/`database`/`schema` + auth. One new error class (`IncompleteProfileError`) slots into the alphabetically-sorted `errors.__all__` (guarded by `test_errors.py`). |
| Observability | pass | `profiles.py` keeps its only log (the DEC-023 soft-size WARNING). No new logs — consistent with stage-0-reader discipline. |
| Testing | **concern → resolved** | Drift convention (`manifest-readers.md`): add a Snowflake `StrictModel` mirror + `dbt_snowflake_drift_v1_x.yml` fixture for forward-compat against new dbt-snowflake fields, plus a valid `snowflake_password.yml` fixture. Coverage matrix: valid parse, missing `account`/`user`/`warehouse` → typed error, deferred `authenticator` → typed error, bad `warehouse`/`database`/`schema` identifier → `InvalidIdentifierError`, account accept/reject grammar, foreign-field-by-type rejection (`account` on bigquery; `location` on snowflake). |

**No blockers.** Three concerns resolved into DEC-003/DEC-006/DEC-007/DEC-010.

---

## Phase 3 — Refinement (Decisions)

- **DEC-001 — Unified model + `@model_validator(mode="after")`.** Add Snowflake fields as `str | None = None` to `DbtProfileTarget`; keep `extra="forbid"`. The after-validator does three things for `type == "snowflake"`: (a) require `account`/`user`/`warehouse`; (b) reject BigQuery-only fields; (c) identifier-validate present identifiers + the account. For `type == "bigquery"` it rejects Snowflake-only fields. *Rationale:* Q1=A — discriminated-union *behaviour* without changing the public return type or churning every consumer.
- **DEC-002 — Per-type field sets drive the foreign-field check.** Module constants `_BIGQUERY_ONLY = {"project", "location", "priority", "maximum_bytes_billed", "method"}` and `_SNOWFLAKE_ONLY = {"account", "user", "role", "warehouse", "database", "password", "private_key_path", "private_key_passphrase", "authenticator"}`. Shared: `type`, `dataset` (via the `schema` alias), `threads`. A field set to a non-`None` value that belongs to the *other* type's set → `IncompleteProfileError`-style loud failure. *Rationale:* one declarative source for "which fields belong to which warehouse"; cheap to extend when Postgres grows fields.
- **DEC-003 — Snowflake `schema:` populates the existing `dataset` field (shared alias).** No separate `schema` field. `database:` is a NEW field. So a Snowflake target resolves to `.database` + `.dataset` (from the `schema` key); BigQuery resolves to `.project` + `.dataset`. Mirrors the #119 factory wiring (`schema=profile.dataset`) so that branch needs only `database=profile.database` added. *Rationale:* avoids a `schema`/`dataset` field collision and keeps the alias contract single-valued.
- **DEC-004 — Required-key failure is one typed `IncompleteProfileError(WarehouseError)`.** Carries `profile_type: str` + `missing: list[str]`; remediation lists the missing keys. Generic (not Snowflake-named) so Postgres/Databricks reuse it. Added to `errors.__all__` in sorted position (between `EstimateNotSupportedError` and `InvalidIdentifierError`). *Rationale:* the AC mandates a typed, remediation-carrying error; one collect-all error (lists every missing key at once) beats N single-field errors.
- **DEC-005 — Auth scope = password + key-pair + SSO (Q2).** Fields added: `password`, `private_key_path`, `private_key_passphrase`, `authenticator`. The after-validator accepts `authenticator ∈ {None, "snowflake", "externalbrowser"}`; `"oauth"` / `"username_password_mfa"` / anything else → `UnsupportedAuthMethodError(method=<value>, remediation=<Snowflake-specific deferral text>)`. *Rationale:* reuse the existing auth-method error with an explicit Snowflake remediation rather than grow a near-duplicate class; the message ("Unsupported auth method: 'oauth'") is accurate. The validator runs in `model_validator(mode="after")` (not a field validator) because the accepted set is type-conditional.
- **DEC-006 — `validate_snowflake_account(field, value)` in `_sql_safety.py`.** Regex `^[A-Za-z0-9][A-Za-z0-9._-]{1,253}$` (accepts `myorg-account1`, `xy12345.us-east-1`, legacy `ab12345`; rejects whitespace, quotes, `;`, `--`, backticks). Raises the existing `InvalidIdentifierError`. *Rationale:* Q3 — account is never interpolated into SQL, so this is log-injection hygiene + a fail-loud "obvious garbage" gate, not the strict SQL-identifier rule.
- **DEC-007 — `validate_identifier` (the strict BQ helper) gates `warehouse`/`database`/`schema` when present on a Snowflake target.** The issue text mandates this helper. Known limitation: the strict regex rejects Snowflake's legal `$` in identifiers — **documented as a v0.x deferral** in `profiles.py` (matches the existing domain-scoped-project-ID deferral note). *Rationale:* follow the issue literally; `$` in dbt-managed warehouse/db/schema names is rare and the fail-loud is safer than a silent under-validation.
- **DEC-008 — `from_profile` snowflake branch wires every parsed field.** Graduates to `SnowflakeAdapter(account=profile.account, user=profile.user, password=profile.password, role=profile.role, warehouse=profile.warehouse, database=profile.database, schema=profile.dataset)` plus the key-pair/SSO surface. To avoid "parsed but dropped on the floor," **extend `SnowflakeAdapter.__init__`** with `private_key_path`, `private_key_passphrase`, `authenticator` (all `str | None = None`, stored on `self._*`, forward-compat per #119 DEC-002). `__repr__` redaction extended to exclude the new secret/auth fields; #122 consumes them when it opens a real connection. *Rationale:* keeps a clean #120/#122 boundary while making the parsed auth fields reachable.
- **DEC-009 — Shared `threads: int | None = None` added to the production model.** Backward-compatible; fixes the latent BigQuery gap AND unblocks Snowflake profiles (which carry `threads:`). *Rationale:* discovered during data-model review — without it `extra="forbid"` rejects every real Snowflake profile.
- **DEC-010 — Drift detector for the Snowflake shape.** Add a `StrictSnowflakeModel(extra="forbid")` mirror + `tests/fixtures/profiles/dbt_snowflake_drift_v1_x.yml` covering the documented dbt-snowflake field set, plus a valid `tests/fixtures/profiles/snowflake_password.yml`. *Rationale:* `manifest-readers.md` mandates a drift fixture + strict mirror for any new read-back shape; the unified model means the *fields* are new even though the class isn't.

---

## Phase 4 — Detailed Breakdown

> Validation command (all stories): `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

### US-001 — `validate_snowflake_account` in `_sql_safety.py`

- **Description:** Add a permissive Snowflake account-identifier validator that raises the existing `InvalidIdentifierError` on obvious garbage but accepts the dots/hyphens real account locators use.
- **Traces to:** DEC-006.
- **Files:** `src/signalforge/warehouse/_sql_safety.py` (+ `_SF_ACCOUNT_RE` constant + `validate_snowflake_account`); `tests/warehouse/test_sql_safety.py` (or the existing identifier-test module).
- **TDD:**
  - accepts `myorg-account1`, `xy12345.us-east-1`, `ab12345`, `MY_ORG-acct.us_east_1`
  - rejects `""`, whitespace (`"a b"`), quotes (`a'b`, `a"b`), `a;b`, `a--b`, backticks, a 300-char string
  - rejection raises `InvalidIdentifierError` with the field name + `repr()`-quoted value
- **Acceptance:** validation command passes; both accept and reject cases covered.
- **Done when:** `validate_snowflake_account` exists, is unit-tested, and rejects log-injection metachars.
- **Depends on:** none.

### US-002 — `IncompleteProfileError` + Snowflake auth remediation in `errors.py`

- **Description:** Add a generic typed error for "profile is missing required keys for its type," carrying `profile_type` + `missing` list, and a module-level Snowflake deferred-auth remediation string. Keep `errors.__all__` alphabetically sorted.
- **Traces to:** DEC-004, DEC-005.
- **Files:** `src/signalforge/warehouse/errors.py`; `src/signalforge/cli/_helpers.py` (**register `IncompleteProfileError: 1` in `_EXCEPTION_TO_EXIT_CODE` + import it** — warehouse concretes are individually registered and the 7th AST scan in `tests/test_audit_completeness.py` enforces it; tier 1 aligns with the existing `UnsupportedAuthMethodError: 1`, a sibling profile-config failure); `tests/warehouse/test_errors.py` (sorted-`__all__` guard already exists — extend the class-coverage assertions).
- **TDD:**
  - `IncompleteProfileError(profile_type="snowflake", missing=["account","warehouse"])` renders both missing keys + a `↳ Remediation:` line
  - `profile_type` / `missing` round-trip as attributes; `missing` is a copied list
  - `errors.__all__` stays sorted with the new class included
- **Acceptance:** validation command passes; `test_errors.py`'s sorted/coverage assertions include `IncompleteProfileError`.
- **Done when:** the class exists, is exported in sorted order, and the auth remediation constant is defined for reuse by the model validator.
- **Depends on:** none.

### US-003 — Grow `DbtProfileTarget` with Snowflake fields + cross-field validator

- **Description:** Add the Snowflake fields (`account`, `user`, `role`, `warehouse`, `database`, `password`, `private_key_path`, `private_key_passphrase`, `authenticator`) and a shared `threads` field to `DbtProfileTarget`, plus a `@model_validator(mode="after")` implementing required-by-type, foreign-field-by-type rejection, identifier validation, account validation, and authenticator-scope validation.
- **Traces to:** DEC-001, DEC-002, DEC-003, DEC-005, DEC-006, DEC-007, DEC-009.
- **Files:** `src/signalforge/warehouse/profiles.py` (fields + `_BIGQUERY_ONLY`/`_SNOWFLAKE_ONLY` constants + after-validator + the `$`-in-identifier deferral docstring note); `tests/warehouse/test_profiles.py`.
- **TDD:**
  - a representative Snowflake target (account/user/role/warehouse/database/schema/threads/password) parses; `.database`, `.dataset` (from `schema`), `.warehouse`, `.account` populate correctly
  - missing `account` / `user` / `warehouse` (each, and combined) → `IncompleteProfileError` (or wrapping `ValidationError`) listing the missing key(s)
  - `authenticator: externalbrowser` parses; `authenticator: oauth` and `username_password_mfa` → `UnsupportedAuthMethodError` with Snowflake remediation
  - bad `warehouse`/`database`/`schema` (e.g. `wh;DROP`) → `InvalidIdentifierError`
  - bad `account` (`a'b`) → `InvalidIdentifierError`; good `account` (`xy12345.us-east-1`) passes
  - foreign-field rejection: `account` on a `type: bigquery` target fails; `location`/`maximum_bytes_billed` on a `type: snowflake` target fails
  - existing BigQuery parse tests still pass unchanged (regression guard); a BigQuery profile with `threads: 4` now parses
- **Acceptance:** validation command passes; the matrix above is covered; no existing BigQuery test regresses.
- **Done when:** a valid Snowflake `profiles.yml` round-trips into a typed `DbtProfileTarget` and every invalid case fails loud with a typed, remediation-carrying error.
- **Depends on:** US-001, US-002.

### US-004 — Extend `SnowflakeAdapter.__init__` for key-pair/SSO auth + `__repr__` redaction

- **Description:** Add `private_key_path`, `private_key_passphrase`, `authenticator` (all `str | None = None`) to the adapter constructor, stored on `self._*`. Keep `__repr__` rendering ONLY `account` + `warehouse` — extend the redaction test to assert the new secret/auth fields never leak.
- **Traces to:** DEC-008.
- **Files:** `src/signalforge/warehouse/adapters/snowflake.py`; `tests/warehouse/test_snowflake_stub.py` (extend `test_repr_shows_only_safe_fields_never_credentials`).
- **TDD:**
  - `SnowflakeAdapter(..., private_key_path="/k.p8", private_key_passphrase="pp", authenticator="externalbrowser")` stores each on `self._*`
  - `repr(adapter)` contains `account`/`warehouse` values but NOT `private_key_path`, the passphrase value, or the field labels `private_key`/`passphrase`
- **Acceptance:** validation command passes; redaction test covers the new fields.
- **Done when:** the constructor accepts the full v0.2 auth surface and `__repr__` still leaks nothing.
- **Depends on:** none (parallelisable with US-001/US-002, but see Quality Gate ordering).

### US-005 — Wire `from_profile` snowflake branch + fixtures + drift detector

- **Description:** Graduate the `from_profile` `snowflake` branch to pass every parsed field into `SnowflakeAdapter`. Add a valid `snowflake_password.yml` fixture and a `dbt_snowflake_drift_v1_x.yml` drift fixture with a `StrictSnowflakeModel(extra="forbid")` mirror.
- **Traces to:** DEC-008, DEC-010.
- **Files:** `src/signalforge/warehouse/base.py` (snowflake branch); `tests/fixtures/profiles/snowflake_password.yml`; `tests/fixtures/profiles/dbt_snowflake_drift_v1_x.yml`; `tests/warehouse/test_profiles.py` (drift mirror + a `load_profile`-through-fixture parse test); `tests/warehouse/test_snowflake_stub.py` (update `test_from_profile_dispatches_snowflake_to_skeleton` to assert the new fields wire through).
- **TDD:**
  - `load_profile` on the `snowflake_password.yml` fixture returns a `DbtProfileTarget` with the expected account/user/warehouse/database/dataset
  - `from_profile` on that profile returns a `SnowflakeAdapter` with `_account`/`_user`/`_warehouse`/`_database`/`_schema` (+ auth fields) populated from the profile
  - the drift fixture validates against `StrictSnowflakeModel`; adding a field to the fixture without updating the mirror fails loudly
  - the no-BigQuery-SDK-import subprocess assertion still holds for a fully-populated Snowflake profile
- **Acceptance:** validation command passes; the AC's "representative Snowflake `profiles.yml` parses + dispatches" is demonstrated end-to-end.
- **Done when:** a real-shaped Snowflake profile flows `load_profile → from_profile → SnowflakeAdapter` with every field wired, and the drift detector guards forward-compat.
- **Depends on:** US-003, US-004.

### US-006 — Quality Gate — code review ×4 + CodeRabbit

- **Description:** Run the code-reviewer agent 4 times across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Validation must pass after all fixes.
- **Acceptance:** validation command green; reviewer passes find no remaining real bugs; CodeRabbit findings triaged/fixed.
- **Done when:** all implementation stories complete and the changeset is clean.
- **Depends on:** US-001 … US-005.

### US-007 — Patterns & Memory (priority 99)

- **Description:** Update `.claude/rules/warehouse-adapters.md` (and `CLAUDE.md` public-API surface if `DbtProfileTarget` field set is documented there) with the unified-multi-warehouse-profile pattern, the per-type foreign-field-rejection convention, and the `validate_snowflake_account` precedent. Add the `$`-in-Snowflake-identifier deferral to `docs/warehouse-adapter-ops.md`.
- **Acceptance:** rules/docs reflect the new conventions; validation command passes.
- **Done when:** the conventions a future warehouse (#121+, Databricks) would copy are written down.
- **Depends on:** US-006.

### Rules-compliance notes

- `warehouse-adapters.md` — repr-redaction (DEC-008/US-004), `_sql_safety` as the single identifier-rule home (US-001), errors carry `default_remediation` + `repr()`-quoted values (US-002), no logging added.
- `manifest-readers.md` — `extra="forbid"` retained on the config-shaped profile; drift detector + fixture mandatory for the new read-back shape (US-005/DEC-010).
- `testing-signal.md` — every test capable of failing; no `assert True`; drift fixture pinned.
- `cli-layer.md` — no new CLI surface; `DbtProfileTarget` return type unchanged, so no annotation churn. **`IncompleteProfileError` MUST be explicitly registered in `_EXCEPTION_TO_EXIT_CODE` (tier 1)** — warehouse concretes are individually registered (verified: `InvalidIdentifierError: 2`, `UnsupportedAuthMethodError: 1`, `TableNotFoundError: 2`, …), and `test_audit_completeness.py`'s 7th AST scan fails loud on a missing entry. The dual-registered `WarehouseError: 3` fallback is only a safety net, not a substitute.

---

## Phase 5 — Publish PR

_Pending detailing approval._

## Beads Manifest

_Pending devolve._
