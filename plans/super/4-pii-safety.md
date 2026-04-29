# Issue #4 — PII safety: schema-only default, sample opt-in, redaction patterns

## Meta

- **Ticket:** [#4](https://github.com/wjduenow/SignalForge/issues/4)
- **Branch:** `feature/4-pii-safety` (off `dev`)
- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/4-pii-safety` (created via `git worktree add`)
- **Phase:** devolved (epic `bd_1-scaffolding-o6f` + 14 tasks live; PR [#18](https://github.com/wjduenow/SignalForge/pull/18) draft)
- **Sessions:** 1 (started 2026-04-28)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1 (deployment blocker for any team with PII concerns)
- **Labels:** `safety`

## Discovery

### Ticket summary

Default to the safest data-access posture; require explicit opt-in to expose row-level data to the LLM. Three sampling **modes** govern what the LLM ever sees:

- `schema-only` (default) — column names + types, never queries data
- `aggregate-only` — `count`, `count(distinct)`, `min`, `max`, null-rate per column; no raw values
- `sample` — row-level data subject to redaction patterns

Plus: configurable redaction patterns (`*_email`, `*_phone`, `*_ssn`), per-column opt-out via dbt `meta.signalforge.sample: false`, an audit log line per LLM call, and a "Data safety" section in the README.

This is a **deployment blocker** for any team with PII concerns; v0.1 must ship even if minimal. Reference: `docs/research/dbt-claude-technical-surface.md` Section 4.5 ("PII / safety") explicitly recommends "default to schema-only with explicit opt-in for sampling — the only defensible posture."

### Acceptance criteria (from ticket)

1. Three sampling modes: `schema-only` (default), `aggregate-only`, `sample`.
2. CLI flag `--mode {schema-only,aggregate-only,sample}` and config-file equivalent.
3. In `sample` mode, redact columns matching configurable patterns (`*_email`, `*_phone`, `*_ssn`) before sending to the LLM.
4. Per-column opt-out via dbt `meta.signalforge.sample: false`.
5. Audit log line per LLM call describing which mode + which columns were sent.
6. README "Data safety" section documenting the model.

### Codebase findings (Subagent B equivalent — verified directly)

- **Warehouse adapter (#3) shipped** with `WarehouseAdapter.sample_rows`, `column_stats`, and `run_test_sql`. The safety layer wraps these — `schema-only` calls neither, `aggregate-only` calls `column_stats`, `sample` calls `sample_rows` then redacts. No new adapter methods needed.
- **`signalforge.manifest.Model.meta` already exists** as `dict[str, Any]` (`src/signalforge/manifest/models.py:78`). Per-column meta also lives on `Column.meta` (line 61). Both are surfaced through `Manifest.get_model(...)` already — `meta.signalforge.sample` is readable from day one. **No manifest-layer changes required.**
- **No LLM client seam exists yet.** `grep -r 'llm\|claude\|anthropic\|prompt' src/` returns only incidental matches in errors/path_safety. Issue [#5 — LLM draft pipeline](https://github.com/wjduenow/SignalForge/issues/5) is the home for the actual `client.complete(...)` call. **#4 must produce a contract #5 can consume without dictating #5's design** (see scoping Q9 below).
- **No CLI exists yet.** Issue [#9 — CLI](https://github.com/wjduenow/SignalForge/issues/9) is the home for `signalforge generate --mode ...`. **#4 must lock the policy/config shape; CLI flag wiring is #9's job** (see scoping Q6).
- **No project-level config file exists yet.** `signalforge.yml` is greenfield. PyYAML is already a runtime dep (added by #3 for `profiles.yml`); reusing it is free.
- **Sibling open issues** confirm scope boundaries: #5 (LLM draft), #6 (prune), #7 (grader), #8 (diff renderer), #9 (CLI), #10 (smoke), #11 (README), #12 (release). #4 is library-only; downstream tickets consume the contract.
- **Validation command** (per CLAUDE.md): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.

### Project rules (`.claude/rules/`) audit

- **`python-build.md`** — Hatchling + src layout + explicit wheel `packages = ["src/signalforge"]`. New subpackage requires no wheel-target edit (already covers all of `src/signalforge/**`). Editable install via quoted `".[dev]"`.
- **`manifest-readers.md`** — Targets external-format readers. The safety layer is **not** a reader (it consumes already-typed data from `signalforge.manifest` and `signalforge.warehouse`), so the symlink-hardening / Pydantic-frozen rules don't directly bind. **Three carry-over principles do apply:** (a) every typed exception subclasses a module base (`SafetyError`) and accepts a `remediation: str` kwarg; (b) `extra="forbid"` is for one-off drift-detection tests only; production policy models use `extra="ignore"` for forward-compat; (c) `repr()`-quote user input in error messages (DEC-022 from #3).
- **`testing-signal.md`** — Hard-applies. No `assert True`-shaped tests. Strict markers (both settings — pytest-9 quirk). No `tests/__init__.py`. **`unittest.mock.MagicMock` is implicitly forbidden** for the LLM-call audit shim — use explicit fakes that fail loudly. The redaction layer is exactly the kind of code where a `MagicMock`-style fake would silently auto-pass while real code leaks PII; explicit `FakeLLMClient` with `expect_*` helpers (mirrors #3's `FakeBigQueryClient`).
- **`ci-supply-chain.md`** — No new CI workflow needed (no integration tests against external services). Single Python 3.11 holds.
- **`warehouse-adapters.md`** — The safety layer **calls** the warehouse adapter, never bypasses it. Path-safety duplication precedent (US-014 from #3) suggests: if the safety layer needs path canonicalisation for `signalforge.yml`, it copies the helper from `signalforge.warehouse._path_safety` rather than imports — keeps each layer's exception surface homogeneous. (Q2 below decides whether we need this.)
- **No `workflow-project.md`** — baseline review areas only in Phase 2.

### CLAUDE.md commitments that bite this ticket

- **#1 — Signal over volume.** Audit log is the user-facing receipt that explains *why* the LLM saw what it saw. Every redacted column, every skipped column (per-column opt-out), every mode-driven omission must surface in the audit line; black-box "the LLM saw something" violates the commitment.
- **#3 — Warehouse-agnostic by design.** The safety layer **must not** call BigQuery-specific code. It calls `WarehouseAdapter` only — through the ABC. Tests use the adapter ABC, not `BigQueryAdapter` (use a `FakeAdapter` in tests; v0.2 Snowflake/Postgres get safety for free).
- **#4 — OSS-first / Core-friendly.** No dbt Cloud. Reads dbt `meta` directly from the typed `Manifest.Model.meta` and `Column.meta` dicts (already loaded by `signalforge.manifest`). No `dbt-core` runtime call.
- **#5 — Explainable diffs.** The audit log is the explainable-diff vehicle for *what data the model saw.* Every record carries: mode, columns sent, columns redacted (and why — pattern match? per-column opt-out? model-level opt-out?), row count if `sample` mode. The prune layer (#6) gets a separate audit; this one covers the LLM input boundary.
- **Roadmap anchor.** v0.1 = single-model draft + warehouse prune. The safety layer ships **before** the LLM-drafting pipeline (#5) so the contract is locked when #5 lands.

### Section 4.5 of the design doc — recommendations already on the table

The clauditor-repo design doc's PII section (`docs/research/dbt-claude-technical-surface.md` §4.5) lays out the exact posture the ticket asks for, plus several knobs we should explicitly accept or reject for v0.1:

- **Mode hierarchy: schema-only → aggregate-only → sample** (matches the ticket).
- **`tags: ["pii"]` and `meta.contains_pii: true`** are existing dbt conventions worth honouring as implicit opt-outs (Q3 below).
- **Server-side redaction (Snowflake `AI_REDACT`)** — out of scope; client-side regex only for v0.1.
- **Explicit allowlist (`models/marts/safe_for_review/`)** — interesting, but not in the ticket. Out of scope for v0.1; revisit if users ask.
- **Token budget framing** — "100 rows × 20 cols × ~5 tokens = 10k tokens" — informs the default sample size. Default to 100 rows, configurable.

### Out of scope (explicit)

- **The LLM client itself** — issue #5. This ticket builds the contract #5 will consume, not the client.
- **The CLI flag** — issue #9. This ticket locks the policy shape; #9 wires `argparse`.
- **Server-side redaction** (Snowflake `AI_REDACT`, BQ Cortex) — v0.2+; client-side regex only for v0.1.
- **Path-allowlist policy** (`models/marts/safe_for_review/` style) — design-doc inspiration but not in ticket; out of scope.
- **Statistical aggregates beyond `ColumnStats`** (top-N, percentiles, value-distribution histograms) — `aggregate-only` mode delivers what `column_stats` already produces. Histograms land in v0.2 if users ask.
- **Differential-privacy noise** on aggregates — out of scope; far beyond ticket.
- **PII detection on actual *values*** (regex on row contents, ML classifiers) — column-name pattern matching only for v0.1. The design doc explicitly notes this trade-off.
- **Multi-language redaction** (Unicode lookalikes for `email`, etc.) — pattern matching is ASCII-pragmatic for v0.1.
- **The prune-step audit log** — separate from the LLM-call audit; lives with the prune ticket (#6).

### Phase 1 housekeeping defaults (set unless flagged in Phase 2/3)

- New subpackage layout TBD by Q1.
- New `signalforge.yml` config file TBD by Q2; default location is project root.
- Per-column opt-out via `meta.signalforge.sample: false` (Q3 expands the alias set).
- Audit log emits via `logging.getLogger("signalforge.safety")` AND a JSONL file persisted per run (Q5).
- Default sample size: 100 rows (matches design doc's token-budget framing). Configurable.
- Default redaction patterns: `*_email`, `*_phone`, `*_ssn` (Q7 decides override semantics).
- No CLI code in this ticket. No LLM client code. Only the contract.
- Unit tests use `FakeAdapter` (a tiny in-memory `WarehouseAdapter` impl) and `FakeLLMClient` with `expect_*` helpers.

### Scoping questions (Phase 1 — awaiting answers)

**Q1. Subpackage placement**

A) New `signalforge.safety` subpackage (sibling to `manifest` / `warehouse`).
B) Inside `signalforge.warehouse` as `safety.py` (data-access policy lives next to data access).
C) New top-level `signalforge.policy` (broader name; room for non-PII policy later — quotas, caching).

*Lean: A. Mirrors the precedent of one-subpackage-per-stage (`manifest`, `warehouse`); the safety layer sits between them and #5; co-locating in `warehouse` blurs the warehouse-agnostic seam.*

**Q2. Config-file shape (no config file exists today)**

A) `signalforge.yml` at project root. YAML, parsed by PyYAML (already a dep). Loaded via new `signalforge.config` module.
B) Piggyback dbt's `dbt_project.yml` — read keys from its `vars:` block. No new file.
C) Defer config-file decision entirely to CLI ticket (#9). Ship typed `SafetyPolicy` ADT only.

*Lean: A. #9 needs a config file regardless; ship the loader now. Piggybacking dbt is cute but couples us to dbt's loader semantics for non-dbt config.*

**Q3. Per-column opt-out granularity**

A) Read column-level `meta.signalforge.sample: false` from `Column.meta` only.
B) Also read MODEL-level `meta.signalforge.sample: false` ("schema-only for this model regardless of mode").
C) Also honour `tags: ["pii"]` and `meta.contains_pii: true` (existing dbt conventions per design doc) as implicit opt-outs.
D) All of A + B + C.

*Lean: D. Cheap to implement, generous to users who already use existing dbt conventions; audit log records *which* signal triggered the redaction so behaviour stays explainable.*

**Q4. Redaction strategy in `sample` mode**

A) Drop redacted columns entirely from the LLM payload (column never appears).
B) Replace values with `"<REDACTED>"` constant (column appears, values masked).
C) Per-type placeholder (e.g. `"<REDACTED:email>"`) — column appears, values masked, type hint preserved.
D) Hash values (deterministic SHA-256 truncated) — uniqueness signal preserved without content.

*Lean: B. Drops violate "the LLM should know the column exists" — schema-yml drafting needs to mention the column. Hashing leaks cardinality which the LLM can't usefully act on for redacted columns. Constant `"<REDACTED>"` is the simplest safe default; ops doc notes the trade-off.*

**Q5. Audit log destination + format**

A) Standard `logging.getLogger("signalforge.safety").info(...)` line; structured `extra={...}` per call. No file.
B) JSONL file at a configured path (default `.signalforge/audit.jsonl`); one record per LLM call. Logger is secondary.
C) Both: human-readable WARNING/INFO log line AND structured JSONL record persisted next to the diff output.

*Lean: C. The "explainable diffs" commitment requires a durable, machine-readable record. The log line is for the developer in the moment; the JSONL file is for the reviewer / compliance auditor / regression test.*

**Q6. CLI flag wiring scope (CLI doesn't exist yet — #9)**

A) Lock the `SafetyPolicy` API + config-file shape now; #9 wires `--mode` to load the policy. **No CLI code in this ticket.**
B) Build a thin `signalforge.config` loader with both `from_file()` and `from_args(argv)` entrypoints, so #9 just calls `load_config(argv)`. Some plumbing in this ticket.
C) Defer entirely to #9; ship only the redactor + policy primitives.

*Lean: A. Mirrors how #3 handled "no CLI yet" — locks the contract, leaves CLI plumbing to #9. Building `from_args` here without a real CLI is design-on-spec.*

**Q7. Default redaction patterns + override semantics**

A) Hard-coded built-ins `*_email`, `*_phone`, `*_ssn`; config can `extend` only (never replace).
B) Hard-coded built-ins as defaults; config supports both `extend: [...]` and `replace: [...]`.
C) No built-ins; config MUST list patterns explicitly. Empty list allowed but emits WARNING.

*Lean: B. Gives users escape hatch for false positives (`my_phone_number_format` column) without forcing every project to re-list the obvious patterns. `replace` is rare but should exist for users with strict allowlist regimes.*

**Q8. Aggregate-only mode implementation**

A) Reuse `WarehouseAdapter.column_stats` directly; aggregate-only mode delivers `ColumnStats` to the LLM.
B) Add a convenience `aggregate_columns(table, columns) -> dict[str, ColumnStats]` on the safety layer that wraps `column_stats` AND respects per-column opt-out.
C) Aggregate-only also adds value-distribution buckets (top-N, percentiles) beyond `column_stats`. Bigger scope.

*Lean: B. (A) leaks per-column-opt-out enforcement to every caller. (C) is v0.2 scope creep — the design doc's "value distribution buckets" suggestion isn't in the ticket.*

**Q9. LLM-call seam (LLM client doesn't exist yet — #5)**

A) Define a typed `LLMRequest` ADT in this ticket — `LLMRequest(model_unique_id, mode, columns_sent, redacted_columns, sampled_rows, ...)` — that the safety layer produces. #5 consumes this shape; the actual `client.complete(request)` call lands in #5.
B) Define only an `audit_record(...)` helper that emits the audit log given the inputs. #5 builds its own request shape and calls `audit_record` at its boundary.
C) Build a stub `LLMClient` Protocol with one `complete(request) -> str` method, plus an audit shim that wraps every call. #5 implements the Protocol.

*Lean: A. (B) leaves the audit-line shape unfixed and lets #5 forget to call it. (C) over-specifies #5's design (e.g. forces sync; #5 may want streaming / batching). The typed `LLMRequest` is small, exercised by tests in this ticket, and #5 inherits a stable contract.*

### Scoping decisions (Phase 1 — locked 2026-04-28, "use defaults")

- **DEC-001 — Subpackage placement: `signalforge.safety`** (Q1=A). Sibling to `manifest` and `warehouse`. Public modules: `signalforge.safety.{__init__, policy, redact, audit, request, errors, config}`. *Why:* mirrors one-subpackage-per-stage precedent; co-locating in `warehouse` blurs the warehouse-agnostic seam (Architectural Commitment #3); `policy` as top-level is too broad (room for non-PII concerns we don't yet have). *How to apply:* `__init__.py` re-exports the public surface (`SafetyPolicy`, `SamplingMode`, `LLMRequest`, `RedactionRecord`, `AuditEvent`, errors); `_`-prefixed helpers stay reachable via dotted import only (DEC-017 from #2).
- **DEC-002 — Config file: `signalforge.yml` at project root + new `signalforge.safety.config` module** (Q2=A). PyYAML `safe_load` only. Resolution order: explicit path arg → `<project_dir>/signalforge.yml` → defaults (no error if absent). *Why:* CLI ticket #9 will need a config file regardless; piggybacking `dbt_project.yml`'s `vars:` couples non-dbt config to dbt's loader; PyYAML is already a runtime dep from #3 so cost is zero. *How to apply:* `load_safety_config(project_dir, path=None) -> SafetyPolicy`; symlink-hardened path canonicalisation copied (not imported) from `signalforge.warehouse._path_safety` per the duplication precedent in `warehouse-adapters.md`.
- **DEC-003 — Per-column opt-out: column meta + model meta + `tags:[pii]` + `meta.contains_pii`** (Q3=D). Triggering signals (any one redacts the column): (a) `Column.meta.signalforge.sample == false`; (b) `Model.meta.signalforge.sample == false` (forces schema-only for the entire model regardless of policy mode); (c) `"pii" in Column.tags` or `"pii" in Model.tags`; (d) `Column.meta.contains_pii == true` or `Model.meta.contains_pii == true`. *Why:* honours existing dbt conventions per design doc §4.5; cheap to implement; audit log records *which* signal fired so behaviour stays explainable (Commitment #5). *How to apply:* a `_classify_column(column, model, policy) -> RedactionDecision` pure function returns `(redact: bool, reason: Literal["column_meta_optout", "model_meta_optout", "tag_pii", "meta_contains_pii", "pattern_match", None])`; tests parametrise across the matrix.
- **DEC-004 — Redaction renders `"<REDACTED>"` constant in `sample` mode** (Q4=B). Column appears in the LLM payload (so schema.yml drafting can mention it) but every value is the literal string `"<REDACTED>"`. No type-tagged variant for v0.1; no hashing. *Why:* dropping the column violates "the LLM should know the column exists for schema drafting"; type-tagged placeholders are scope creep with no demonstrated value; hashing leaks cardinality but the LLM can't usefully act on cardinality of a redacted column. *How to apply:* `_REDACTED_VALUE: Final = "<REDACTED>"` module constant; `redact_rows(rows, redacted_columns) -> list[dict]` returns new dicts with values replaced (never mutates input). Ops doc records the trade-off so future debate references it.
- **DEC-005 — Audit log: BOTH human-readable logger AND structured JSONL file** (Q5=C). Logger: `signalforge.safety` at `INFO` level emits a one-line summary per LLM call (`"LLM call for {unique_id}: mode={mode}, columns_sent={n}, redacted={m}"`). File: JSONL at `<project_dir>/.signalforge/audit.jsonl` (path configurable); one record per LLM call, schema-stable, machine-readable. *Why:* the developer-in-the-moment uses the log line; the reviewer / compliance auditor / regression test uses the JSONL. Single channel forces a trade-off neither audience tolerates. *How to apply:* `AuditEvent` is a frozen Pydantic v2 model with `timestamp`, `model_unique_id`, `mode`, `columns_sent: list[str]`, `redactions: list[RedactionRecord]`, `row_count: int | None`; `audit.write(event)` appends to JSONL and emits the log line. Atomic-append via `O_APPEND` open mode (no locking — JSONL appends are atomic on POSIX up to PIPE_BUF).
- **DEC-006 — CLI scope: lock policy + config shape; no CLI code in this ticket** (Q6=A). `signalforge.safety.config.load_safety_config(project_dir, path=None) -> SafetyPolicy` is the loader; #9's CLI will pass `--mode` to override `SafetyPolicy.mode` after loading from file. No `from_args` plumbing here. *Why:* mirrors how #3 handled "no CLI yet"; building `from_args` without a real CLI is design-on-spec; #9 has freedom to choose `argparse` / `click` / `typer`. *How to apply:* add a "Used by #9" comment on `load_safety_config` so the next implementer knows the seam is intentional.
- **DEC-007 — Built-in redaction patterns + `extend`/`replace` config semantics** (Q7=B). Built-ins: `*_email`, `email`, `*_phone`, `phone`, `*_ssn`, `ssn` (six patterns; covers both prefixed `customer_email` and bare `email`). Config schema: `redact: { extend: [...], replace: [...] }`; `extend` and `replace` are mutually exclusive — `extend` appends to built-ins, `replace` substitutes them entirely. Empty `replace: []` is allowed (disables all redaction) but emits a `WARNING` log line on policy load. *Why:* gives users escape from false positives without forcing every project to re-list defaults; `replace` is rare but exists for strict-allowlist regimes; the warning on empty-replace is signal-not-noise (it fires only when someone explicitly opts out of all PII redaction). *How to apply:* patterns matched via `fnmatch.fnmatchcase` (case-sensitive glob) against column names; `SafetyPolicy.redact_patterns: tuple[str, ...]` is the resolved final list (frozen at construction); a `policy.matches_redaction_pattern(column_name) -> bool` method.
- **DEC-008 — Aggregate-only mode: `aggregate_columns(adapter, model, columns)` wrapper enforces opt-out** (Q8=B). Public: `aggregate_columns(adapter: WarehouseAdapter, model: Model, columns: list[str], policy: SafetyPolicy) -> dict[str, ColumnStats | None]`. Returns `None` for columns that the policy redacts (per-column opt-out signals). Internally it calls `adapter.column_stats(table, column)` for each non-redacted column. *Why:* (A) leaks per-column-opt-out enforcement to every caller and risks noise in the audit log; (C)'s top-N / percentiles aren't in the ticket and would require new adapter methods. *How to apply:* the `dict` value is `None` when redacted (so the caller knows the column was acknowledged but skipped); the audit log captures both the columns sent (`ColumnStats` non-None) and the redacted column names with their reason.
- **DEC-009 — LLM-call seam: typed `LLMRequest` ADT** (Q9=A). Frozen Pydantic v2 model: `LLMRequest(model_unique_id: str, mode: SamplingMode, columns_sent: list[str], redactions: list[RedactionRecord], sampled_rows: list[dict] | None, aggregates: dict[str, ColumnStats | None] | None, schema: list[tuple[str, str]])`. The safety layer produces the request; #5 consumes it (`client.complete(request) -> str`). The `audit.write(event)` call is invoked by the **request-builder** (not by #5), so #5 cannot accidentally skip the audit. *Why:* (B) leaves the audit-line shape unfixed and trusts #5 to remember; (C) over-specifies #5 (forces sync, may want streaming/batching); typed `LLMRequest` is small, exercised by this ticket's tests, and gives #5 a stable contract. *How to apply:* `build_llm_request(model, adapter, policy) -> LLMRequest` is the single entry point; it calls `audit.write` before returning. #5 receives the request, calls its LLM client, and never touches the safety layer's internals.

---

## Architecture Review

Reviewed 2026-04-28 by seven parallel subagents (privacy/compliance, security, performance, data model, API design, observability, testing strategy) against the locked Phase 1 shape (DEC-001 … DEC-009). Result: **10 unique blockers, 16 concerns** spanning fail-closed semantics, data-model gaps, and API contract precision. Privacy review surfaced the load-bearing finding (B1 below) — column *names* leak PII even when no values are sent.

### Findings table

| Area | Rating | Notes |
| --- | --- | --- |
| Privacy — `schema-only` mode leaks PII via column NAMES | **blocker** | Default mode sends column names to LLM. A column named `customer_ssn` or `john_smith_email` leaks PII even when no values are sampled. Resolution: in schema-only AND aggregate-only modes, redact column **names** that match patterns/tags/meta — replace with `"<REDACTED>"` (drops field) or `"<REDACTED:col_h7f3a9>"` (stable hash for the LLM to reference). |
| Privacy/Obs — Audit write failure semantics undefined | **blocker** | If `.signalforge/audit.jsonl` append fails (disk full, perms, missing dir), DEC-005 doesn't say what happens. Must be fail-closed: any audit-write exception aborts `build_llm_request`, raising `AuditWriteError`. The LLM call **never** proceeds without a durable audit record. |
| Privacy — Default-mode regression not enforced at every layer | **blocker** | If a future bug makes `load_safety_config` return `mode=sample` instead of `schema-only`, every team's PII silently leaves the warehouse. Need (a) `SafetyPolicy.mode: SamplingMode = SamplingMode.SCHEMA_ONLY` default at the field level, (b) explicit regression test for "no config → schema-only", (c) explicit regression test for "no config + adapter calls inspected → zero warehouse calls in default policy". |
| Security — `audit_path` path-traversal | **blocker** | `audit_path` is config-supplied. Setting `audit_path: /etc/passwd` or `audit_path: ../../../escape.jsonl` is currently unconstrained. Must canonicalise via the `_path_safety` helper, reject `..` segments, and require the resolved path stay inside `project_dir`. |
| Data model — `AuditEvent` missing reproducibility fields | **blocker** | DEC-005's `AuditEvent` lacks `signalforge_version: str`, `policy_hash: str`, and `audit_schema_version: int`. Without these: (a) v0.2 cannot evolve the audit schema without breaking consumers, (b) reviewers cannot verify "all records in this run came from the same policy," (c) #5 cannot deterministically reproduce a redaction decision on re-run. All three are mandatory. |
| Data model / API — `SafetyPolicy` lacks `extra="forbid"` | **blocker** | `signalforge.yml` is user-authored. A typo like `redacts:` (vs `redact:`) currently silently no-ops with `extra="ignore"` — exactly the kind of fail-open bug this ticket exists to prevent. Override the manifest-readers default and use `extra="forbid"` on every config-shaped model (`SafetyPolicy`, the inner `redact:` block). Reader-shaped models (`AuditEvent` deserialised from old JSONL records) keep `extra="ignore"`. |
| API — `load_safety_config` error semantics ambiguous | **blocker** | DEC-002 says "no error if absent" but doesn't disambiguate four cases: (a) explicit `path=` arg points at missing file → raise `ConfigNotFoundError`; (b) implicit project-dir lookup misses → silent fallback to defaults; (c) file exists but is empty → silent fallback to defaults; (d) file exists but YAML is malformed or schema-invalid → raise `InvalidConfigError` / `InvalidSamplingModeError`. Lock the contract. |
| API — `extend` / `replace` resolution mechanism undefined | **blocker** | DEC-007 specifies the YAML shape `redact: { extend: [...], replace: [...] }` but doesn't say how Pydantic deserialises it into `redact_patterns: tuple[str, ...]`. Use a Pydantic v2 `@model_validator(mode="before")` that pops `redact`, resolves `extend` (append to built-ins) or `replace` (substitute), and assigns `redact_patterns`. Mutual exclusion is enforced in the same validator. |
| API — `policy.with_mode()` factory missing | **blocker** | `SafetyPolicy` is frozen, so `--mode` from #9 cannot mutate it. DEC-006 says "wires `--mode` to load the policy" but provides no method. Add `policy.with_mode(mode: SamplingMode) -> SafetyPolicy` using `self.model_copy(update={"mode": mode})`. Document as the canonical override path; #9 has no other route. |
| Obs — `docs/safety-ops.md` not committed | **blocker** | `docs/manifest-loader-ops.md` and `docs/warehouse-adapter-ops.md` are precedent — every public-API subpackage gets an ops guide. Must commit to the doc in this ticket: mode semantics, redaction-pattern syntax, audit JSONL schema with `audit_schema_version`, opt-out mechanisms (column meta, model meta, tags, `meta.contains_pii`), debugging, the "Data safety" README cross-link. |
| Privacy — Single-entry audit not type-enforced | concern | DEC-009 says `build_llm_request` is the single entry point that calls `audit.write` internally. Convention only; nothing prevents future contributors from constructing `LLMRequest` directly. Mitigation: add a module-level test `test_llm_request_construction_requires_audit` that scans for direct `LLMRequest(...)` calls outside `request.py` (lightweight AST check), or document the convention in the `LLMRequest` docstring with `# private constructor` discipline. Lean: docstring + AST test. |
| Privacy — `tags`/`meta.contains_pii` semantics | concern | What if `tags: [PII]` (uppercase)? `meta.contains_pii: "yes"` (string)? `meta.contains_pii: 1` (int)? Lean: tags case-insensitive (normalise to lowercase on read; emit DEBUG when normalisation kicks in); `contains_pii` accepts any truthy value (DEBUG-log when non-bool). Document precedence: column-level beats model-level in case of conflict. |
| Privacy/Security — Pattern matching case-insensitive | concern | `fnmatch.fnmatchcase` is case-sensitive; `*_email` misses `Customer_Email` and `EMAIL`. Lean: lowercase both column name and pattern at match time (`fnmatch.fnmatchcase(name.lower(), pat.lower())`). Built-in patterns become `*email`, `email`, `*phone`, `phone`, `*ssn`, `ssn` (still case-insensitive in matching). Add a "suspicious unmatched columns" WARNING heuristic — flag columns whose name contains substrings like `email`/`phone`/`ssn`/`password`/`token`/`secret` but didn't match any pattern. |
| Privacy — Audit JSONL contains plaintext column names | concern | A column literally named `customer_ssn` is a PII metadata leak even when its values are redacted. Two postures: (A) document the audit JSONL itself as sensitive (Gitignore, treat at-rest as PII); (B) hash the column name in `RedactionRecord` (loses debuggability). Lean: A. The audit log's purpose is human review for compliance — hashing defeats it. Document loudly in `safety-ops.md`. |
| Privacy — `sample` mode guard rails | concern | No env-var override for `mode` (config + CLI flag only). On `SafetyPolicy` load, emit a single WARNING when `mode == SAMPLE`: `"Sample mode enabled — raw row data will be sent to the LLM. Verify column tags/meta opt-outs."`. Persist a corresponding `policy_flags: ["sample_mode_enabled"]` field on every `AuditEvent` from that run so reviewers can scan. |
| Privacy — `LLMRequest.sampled_rows` deep immutability | concern | Pydantic `frozen=True` blocks attribute reassignment but not list-mutation (`request.sampled_rows.append(...)`). Switch the type to `tuple[dict[str, Any], ...] | None` (and `tuple[tuple[str, str], ...]` for `schema`) so #5 cannot accidentally mutate the audit-pinned payload. |
| Security — Pattern-injection rejection | concern | An empty pattern `""` matches everything via `fnmatch`; `*` matches all column names, defeating the redactor by appearing to over-redact (which an LLM-context-builder may then "helpfully" un-redact). Validate each user-supplied pattern at policy-load time: reject empty strings; reject patterns equal to `*` or `?`. Raise `InvalidPatternError(value, reason)`. |
| Security — Atomic-append size bound | concern | POSIX `O_APPEND` is atomic only up to PIPE_BUF (4096 bytes Linux, 512 macOS). A typical `AuditEvent` is ~200–500 bytes; pathological cases (huge column lists, many redactions) could exceed. Add a runtime assertion in `audit.write`: `if len(line) > 4000: raise AuditRecordTooLargeError(size, limit)`. Documents the constraint and catches feature creep. |
| Security — ANSI/log-injection in logger line | concern | `_LOGGER.info(f"LLM call for {model_unique_id}: ...")` with a model-id containing ANSI escapes (`\x1b[31mFAKE\x1b[0m`) injects into log viewers. JSON encoding handles this for the JSONL; the logger line must use lazy-format `_LOGGER.info("...: %s", json.dumps(...))` or repr-quote user-controlled values. |
| Performance — Audit JSONL unbounded growth | concern | One record per LLM call, no rotation. After 10 000 runs the file is megabytes. Lean: document user-side rotation in `safety-ops.md` (logrotate, manual archive); do not implement rotation in v0.1. |
| Data model — `SamplingMode` as `StrEnum` + case-insensitive load | concern | DEC-001 implies a string-typed mode. Use `class SamplingMode(StrEnum): SCHEMA_ONLY = "schema-only"; ...` (Python 3.11+, project minimum is 3.10 — verify, or fall back to a `str, Enum` mixin on 3.10). Add a `@field_validator("mode", mode="before")` on `SafetyPolicy` that normalises `"Schema-Only"` / `"schema_only"` / `"SCHEMA-ONLY"` → `SamplingMode.SCHEMA_ONLY`; raises `InvalidSamplingModeError` on unknown. |
| Data model — `RedactionRecord.reason` as `Literal[...]` | concern | DEC-003 lists the seven reasons informally. Encode as `Literal["column_meta_optout", "model_meta_optout", "tag_pii_column", "tag_pii_model", "meta_contains_pii_column", "meta_contains_pii_model", "pattern_match"]` so audit-log consumers can pattern-match exhaustively. |
| Data model — `signalforge.yml` top-level namespace | concern | Use `{ safety: { mode: ..., redact: ..., ... } }` rather than flat-at-top. Reserves room for future stages (`llm:`, `prune:`, `grade:`) without a v2-config migration. The config loader extracts the `safety:` key. |
| Obs — `policy_flags` and empty-redaction persistence | concern | If `redact: { replace: [] }` disables all redaction, the WARNING fires once on load — but each `AuditEvent` in that run should also carry `policy_flags: ["redaction_disabled"]` so a reviewer scanning the JSONL doesn't conclude "no redactions = no PII columns" (could be "all PII columns intentionally un-redacted"). |
| API — Error hierarchy enumeration | concern | Plan implies but doesn't list typed errors. Lock the set: `SafetyError` (base), `ConfigNotFoundError`, `InvalidConfigError` (parent), `InvalidSamplingModeError`, `InvalidPatternError`, `ColumnNotInModelError`, `AuditWriteError`, `AuditRecordTooLargeError`, `PolicyValidationError`. Each subclasses `SafetyError`, ships a class-level `default_remediation`, and renders user-supplied strings via `repr()` (DEC-022 from #3). Total: ~9 typed subclasses + 1 base. |
| Testing — Drift-detector fixture for `AuditEvent` | concern | Per `testing-signal.md`, production `AuditEvent` uses `extra="ignore"`; pair it with a one-off `StrictAuditEvent` (`extra="forbid"`) test against a committed JSONL fixture (`tests/fixtures/safety/audit_events_sample.jsonl`). Adding a field to production without updating the fixture or the strict model breaks the test loudly. |

### Blockers (must resolve in Phase 3)

1. **B1** — Schema-only & aggregate-only modes redact column NAMES (not just values) when names match patterns/tags/meta. Decide: drop column entirely vs. stable-hash placeholder vs. `"<REDACTED>"` literal.
2. **B2** — Fail-closed audit semantics: any `audit.write` failure aborts `build_llm_request` with `AuditWriteError`. LLM call never proceeds without a durable audit record.
3. **B3** — Default-mode regression: `SafetyPolicy.mode: SamplingMode = SamplingMode.SCHEMA_ONLY` default at field level + dedicated regression tests at policy, config, and request layers.
4. **B4** — `audit_path` security: canonicalise via copied `_path_safety`, reject `..` segments, require resolved path inside `project_dir`.
5. **B5** — `AuditEvent` adds `signalforge_version: str`, `policy_hash: str`, `audit_schema_version: int = 1` as mandatory fields.
6. **B6** — `SafetyPolicy` (and inner config-shaped models) use `extra="forbid"`. `AuditEvent` (read-back path) keeps `extra="ignore"` + drift-detector test.
7. **B7** — `load_safety_config` error contract: explicit-path miss → `ConfigNotFoundError`; implicit-path miss → silent defaults; empty file → silent defaults; malformed YAML → `InvalidConfigError`; schema-invalid → `InvalidSamplingModeError` / `InvalidPatternError`.
8. **B8** — `@model_validator(mode="before")` resolves `redact: {extend, replace}` into `redact_patterns: tuple[str, ...]`. Mutual exclusion enforced; empty `replace: []` allowed but emits WARNING.
9. **B9** — `policy.with_mode(mode) -> SafetyPolicy` via `model_copy(update={"mode": mode})`. Documented as #9's CLI override seam.
10. **B10** — Commit to `docs/safety-ops.md` (parallel to manifest-/warehouse-ops); cross-linked from README "Data safety" section.

### Concerns to resolve in Phase 3

C1 — Single-entry audit enforced via `LLMRequest` docstring discipline + an AST-scan test that catches direct construction outside `request.py`.
C2 — `tags` case-insensitive normalisation; `meta.contains_pii` truthy coercion; column-level beats model-level on conflict; document precedence.
C3 — Pattern matching case-insensitive (lowercase both sides); built-ins become `*email`/`email`/`*phone`/`phone`/`*ssn`/`ssn`; "suspicious unmatched column" WARNING heuristic.
C4 — Audit JSONL contains plaintext column names; documented as sensitive in `safety-ops.md` rather than hashed.
C5 — No env-var override for `mode`; WARNING on `mode == SAMPLE` policy load; `policy_flags: ["sample_mode_enabled"]` on every `AuditEvent` from that run.
C6 — `LLMRequest.sampled_rows: tuple[dict[str, Any], ...] | None`; `schema: tuple[tuple[str, str], ...]`; deep immutability.
C7 — Pattern-injection rejection: empty / `*` / `?` patterns rejected with `InvalidPatternError`.
C8 — `audit.write` asserts `len(line) <= 4000` (PIPE_BUF margin); raises `AuditRecordTooLargeError` otherwise.
C9 — Logger lines use lazy-format `_LOGGER.info("...: %s", json.dumps(value))` for any user-controlled string; no f-string interpolation of user input.
C10 — Audit JSONL rotation documented as user responsibility in `safety-ops.md`; no rotation logic in v0.1.
C11 — `SamplingMode(StrEnum)`; `@field_validator("mode", mode="before")` accepts `"schema-only"` / `"schema_only"` / `"SCHEMA-ONLY"` etc.
C12 — `RedactionRecord.reason` is `Literal[...]` of seven explicit values.
C13 — Top-level `signalforge.yml` namespace: `{ safety: { ... } }`; loader extracts the `safety:` key; other top-level keys reserved for future stages.
C14 — `policy_flags: list[str]` on `AuditEvent`; populated with `"redaction_disabled"`, `"sample_mode_enabled"` etc. as policy state warrants.
C15 — Error hierarchy: `SafetyError` base + 9 subclasses; each has class-level `default_remediation` and `repr()`-quotes user input (DEC-022).
C16 — Drift-detector test: one-off `StrictAuditEvent(extra="forbid")` validates committed `tests/fixtures/safety/audit_events_sample.jsonl` fixture.

## Refinement Log

### Phase 3 decisions (resolved 2026-04-28, "all defaults" — every blocker and every concern)

Seventeen decisions consolidating the ten blockers and sixteen concerns from the Architecture Review.

- **DEC-010 — Column-name redaction in `schema-only` and `aggregate-only` modes via stable hash** (resolves B1). When a column is classified as redacted (any of the seven `RedactionRecord.reason` signals fires), the column's NAME is replaced in the LLM-bound payload with `f"col_{hashlib.blake2b(name.encode(), digest_size=4).hexdigest()}"` (e.g. `customer_ssn` → `col_a3f29c61`). Schema-only mode sends `[(hashed_name, type)]`; aggregate-only mode keys `aggregates` dict on the hashed name; sample mode redacts both NAME (hashed) AND VALUES (`"<REDACTED>"` constant). The `(real_name, hashed_name)` mapping is recorded in `RedactionRecord.hashed_name`, persisted to the JSONL audit log, and **not** sent to the LLM. *Why:* drops defeat the point of schema-only (LLM can't draft a `schema.yml` entry for a column it doesn't know exists); bare `"<REDACTED>"` collides for every redacted column in the same model; blake2b-4 (8 hex chars) gives stable, collision-resistant identifiers and lets downstream tooling (in #5 / #6) map back to real names via the audit log. *How to apply:* `signalforge.safety.redact.hash_column_name(name) -> str` is the single helper; tests assert determinism (same name → same hash) and stability across runs.
- **DEC-011 — Fail-closed audit semantics** (resolves B2). Any exception raised inside `audit.write(event)` (`OSError`, `PermissionError`, encoding failure, size-cap breach) propagates as `AuditWriteError` from `build_llm_request`. The function never returns an `LLMRequest` whose audit record didn't durably hit disk. *Why:* an unaudited LLM call is, by definition, PII leaving the warehouse without a receipt — the entire point of this ticket is to prevent that. *How to apply:* `audit.write` opens the file with `O_APPEND | O_CREAT`, writes one `json.dumps(event.model_dump(mode="json")) + "\n"`, calls `fsync()` before close, and catches *no* exceptions internally. `build_llm_request` calls `audit.write(event)` AFTER constructing the request but BEFORE returning it; on exception, the partial request is dropped. Tests inject `IOError` via a `chmod 000` parent dir under `tmp_path` and assert `AuditWriteError`.
- **DEC-012 — Default-mode regression enforced at field + three layers** (resolves B3). `class SafetyPolicy(BaseModel): mode: SamplingMode = SamplingMode.SCHEMA_ONLY`. Three dedicated regression tests: (a) `test_safety_policy_no_args_is_schema_only` — `SafetyPolicy().mode is SamplingMode.SCHEMA_ONLY`. (b) `test_load_safety_config_no_file_is_schema_only` — `load_safety_config(empty_tmp_path).mode is SamplingMode.SCHEMA_ONLY`. (c) `test_build_llm_request_default_policy_zero_warehouse_calls` — `FakeAdapter` records calls; default policy + `build_llm_request` issues zero `column_stats` / `sample_rows` calls. *Why:* a future bug that flips the default to `sample` silently leaks PII for every team that hasn't authored a `signalforge.yml`. Three layers of defence make a single regression catastrophic-test-fail-loud, not silent-data-leak. *How to apply:* the three tests live in `tests/safety/test_default_mode_regression.py` (a single dedicated file makes the regression cluster greppable for future-you).
- **DEC-013 — `audit_path` path-traversal hardening** (resolves B4). `audit_path: Path` defaults to `<project_dir>/.signalforge/audit.jsonl`. Override via `signalforge.yml`'s `safety.audit_path` is supported but constrained: (a) reject any path containing `..` segments at policy-load time (`InvalidConfigError` with remediation); (b) canonicalise the path via `_path_safety.canonicalise_path` (copied from `signalforge.warehouse._path_safety`, per `warehouse-adapters.md`'s duplication precedent); (c) require the canonicalised path to be within `project_dir` (`is_relative_to`). Symlinks are followed to their target before the containment check. *Why:* `audit_path: /etc/passwd` and `audit_path: ../../escape.jsonl` are both currently unconstrained; both must fail loudly. *How to apply:* the helper lives in `signalforge.safety._path_safety`; the three traps from `manifest-readers.md` (resolve before containment, catch `RuntimeError` on cycles, gate the default path through the same helper) all apply.
- **DEC-014 — `AuditEvent` reproducibility fields** (resolves B5). Mandatory fields beyond DEC-005's baseline: `signalforge_version: str` (from `signalforge.__version__` at write time); `policy_hash: str` (SHA-256 of `SafetyPolicy.model_dump_json()` with sorted keys, hex-encoded, truncated to 16 chars); `audit_schema_version: int = 1` (frozen at the constant — bump in v0.2). *Why:* without `signalforge_version` an audit record can't be reproduced under the same code; without `policy_hash` reviewers can't verify "all records in this run came from the same policy"; without `audit_schema_version` v0.2 can't add fields without breaking JSONL consumers. *How to apply:* `AuditEvent` adds three frozen fields; a `_compute_policy_hash(policy: SafetyPolicy) -> str` helper lives in `signalforge.safety.policy`; tests assert that two policies with identical fields produce the same hash and two semantically-different policies produce different hashes.
- **DEC-015 — `extra="forbid"` on config-shaped models; `extra="ignore"` on read-back models** (resolves B6). `SafetyPolicy`, the inner `_RedactConfig` (the `redact: { extend, replace }` block), and the top-level `_SafetyConfigFile` (the deserialised `signalforge.yml`) all use `ConfigDict(frozen=True, extra="forbid", populate_by_name=True)`. `AuditEvent` (which is *deserialised back* from old JSONL files for drift detection and external audit-log tooling) keeps `ConfigDict(frozen=True, extra="ignore", populate_by_name=True)` and is paired with a `StrictAuditEvent(extra="forbid")` drift-detector test (DEC-026). *Why:* a typo in `signalforge.yml` (`redacts:` vs `redact:`) silently no-ops with `extra="ignore"` — exactly the failure mode this ticket exists to prevent. The read-back path needs forward-compat. *How to apply:* explicit `model_config` declaration on each class; tests parametrise typo cases and assert `ValidationError`.
- **DEC-016 — `load_safety_config` error contract** (resolves B7). Resolution: (1) explicit `path=` arg → file MUST exist; missing → `ConfigNotFoundError(path)`. (2) implicit `<project_dir>/signalforge.yml` → missing is fine; fall through to defaults. (3) Empty file (zero bytes or whitespace-only) → fall through to defaults; emit DEBUG `"signalforge.yml is empty; using defaults"`. (4) File parses but `safe_load` returns non-dict → `InvalidConfigError("expected mapping at top level")`. (5) File parses but the `safety:` key is absent → fall through to defaults (other top-level keys reserved for future stages, per DEC-025). (6) Schema-invalid contents → typed errors per validator (`InvalidSamplingModeError`, `InvalidPatternError`, `PolicyValidationError`). *Why:* explicit-path failures must fail loud (user said they wanted that file); implicit failures must fall through silently to defaults (otherwise every project must author `signalforge.yml` to use the tool); empty files behave like missing files (least-surprise). *How to apply:* `tests/safety/test_config.py` covers all six branches with parametrised fixtures.
- **DEC-017 — `@model_validator(mode="before")` resolves `extend` / `replace`** (resolves B8). On `SafetyPolicy`, a `@model_validator(mode="before")` named `_resolve_redact_patterns` consumes the inbound `redact: {extend?, replace?}` dict, resolves it into `redact_patterns: tuple[str, ...]`, and removes the original `redact` key. Mutual exclusion: `extend` and `replace` simultaneously present → `ValidationError`. Empty `replace: []` → resolved to empty tuple, but emit module-level WARNING `"signalforge.yml: redact.replace=[] disables all redaction patterns"` once at policy-load time. Built-in defaults (after DEC-020 case-insensitivity): `("*email", "email", "*phone", "phone", "*ssn", "ssn")`. *Why:* Pydantic v2's `@model_validator(mode="before")` is the idiomatic place to translate user-facing config shape into typed-field shape; it runs before field validation so downstream invariants (DEC-024 case-insensitive normalisation) see the resolved tuple. *How to apply:* `_resolve_redact_patterns` lives on `SafetyPolicy`; tests cover extend, replace, both-error, neither-error (defaults), and empty-replace warning.
- **DEC-018 — `policy.with_mode()` override factory** (resolves B9). `SafetyPolicy.with_mode(self, mode: SamplingMode) -> SafetyPolicy` returns `self.model_copy(update={"mode": mode})`. Documented in the docstring as the **canonical** path for CLI / programmatic mode override; #9 calls it after `load_safety_config` to apply `--mode`. *Why:* `SafetyPolicy` is frozen; without an explicit override path, callers either reach for hacks (mutating private dicts) or re-construct the entire policy (losing fields). `model_copy` is cheap and Pydantic-idiomatic. *How to apply:* one-line method on `SafetyPolicy`; tests assert frozen-ness of original and equality of all other fields after override.
- **DEC-019 — `docs/safety-ops.md` + README "Data safety" cross-link** (resolves B10). New file `docs/safety-ops.md` (parallel to `docs/manifest-loader-ops.md` and `docs/warehouse-adapter-ops.md`). Sections: mode semantics + when to use each; redaction-pattern syntax (case-insensitive `fnmatch` glob, built-ins, `extend` vs `replace`); per-column opt-out (the four signals from DEC-003); audit JSONL schema with `audit_schema_version` reference; `audit_path` security constraints; debugging (logger names, levels); typed-error reference cross-linked to `signalforge.safety.errors`; the at-rest-sensitivity caveat for the audit log itself (DEC-020 C4); rotation guidance (DEC-026). README gains a "Data safety" section between "Configuration" and "Roadmap" that links to the ops doc and summarises the schema-only-default posture in three sentences. *How to apply:* doc lands in US-012; cross-link in README in same story.
- **DEC-020 — Audit-completeness, semantic-coercion, and audit-log sensitivity** (resolves C1 + C2 + C3 + C4). (a) **Audit completeness:** `LLMRequest`'s docstring states `"Construct only via signalforge.safety.request.build_llm_request — direct construction bypasses the audit log."`; a test in `test_public_api.py` AST-scans the `signalforge.safety` package and asserts `LLMRequest(...)` does not appear outside `request.py`. (b) **`tags`/`meta.contains_pii` coercion:** tag matching is case-insensitive (compare lowercased `column.tags + model.tags` against lowercased `"pii"`); `meta.contains_pii` accepts any truthy value (bool / non-empty string / non-zero int) — non-bool values trigger a DEBUG log noting the coercion; column-level signals beat model-level on conflict and the precedence is asserted by parametrised tests. (c) **Pattern matching:** `_matches_redaction_pattern(name, pattern) := fnmatch.fnmatchcase(name.lower(), pattern.lower())`; built-ins become `("*email", "email", "*phone", "phone", "*ssn", "ssn")`; a "suspicious unmatched column" WARNING fires when a column's lowercased name contains any of `{"email", "phone", "ssn", "password", "token", "secret", "api_key"}` AND no pattern matched. (d) **Audit log sensitivity:** `safety-ops.md` documents the audit JSONL itself as sensitive (Gitignore precedent in `.signalforge/`); column names are stored plaintext in `RedactionRecord.column_name` for debuggability — hashing them defeats the audit's review purpose. *How to apply:* `_classify_column` is the central pure function; `tests/safety/test_classify.py` parametrises across the matrix.
- **DEC-021 — `sample` mode guard rails + `policy_flags` on every audit event** (resolves C5 + C14). On `SafetyPolicy` load, if `mode == SamplingMode.SAMPLE`, emit one WARNING: `"Sample mode enabled — raw row data will be sent to the LLM. Verify column tags/meta opt-outs."`. No env-var override path for `mode`; only file + `with_mode()` programmatic override (which #9's CLI uses). `AuditEvent` gains `policy_flags: tuple[str, ...]`; populated by the request builder from policy state with values from a closed set: `"sample_mode_enabled"` (when `mode == SAMPLE`), `"redaction_disabled"` (when resolved `redact_patterns` is empty), `"audit_path_overridden"` (when `audit_path` differs from the default). Tests parametrise across the flag combinations. *Why:* a reviewer scanning the JSONL needs to distinguish "no redactions because no PII columns" from "no redactions because policy disabled them" — the flags make policy state legible at the per-record level.
- **DEC-022 — `LLMRequest` deep immutability + audit-write size cap + ANSI-safe logger** (resolves C6 + C8 + C9). (a) **Immutability:** `LLMRequest` field types switch to tuples — `columns_sent: tuple[str, ...]`, `redactions: tuple[RedactionRecord, ...]`, `sampled_rows: tuple[dict[str, Any], ...] | None`, `aggregates: dict[str, ColumnStats | None] | None` (dict is fine; `ColumnStats` is already frozen), `schema: tuple[tuple[str, str], ...]`. The Pydantic `frozen=True` plus tuple-typed sequences make the request transitively-immutable for downstream consumers (#5 cannot accidentally append a row to a request that's already been audited). (b) **Size cap:** `audit.write` asserts `len(line.encode("utf-8")) <= 4000` (PIPE_BUF margin on Linux's 4096); breach raises `AuditRecordTooLargeError(size, limit)` so feature creep can't silently break atomic appends. (c) **ANSI / log-injection guard:** every logger call in `signalforge.safety` uses lazy-format with json-encoded user-controlled values — `_LOGGER.info("LLM call: %s", json.dumps({"unique_id": uid, "mode": mode.value, ...}))` — never f-string interpolation of strings that came from manifests/configs/columns. Tests inject `\x1b[31mFAKE\x1b[0m` into a column name and assert the logger output contains the JSON-escape sequence (``), not the raw escape.
- **DEC-023 — Pattern-injection rejection at policy-load time** (resolves C7). `SafetyPolicy._resolve_redact_patterns` validates each pattern after `extend`/`replace` resolution: empty string → `InvalidPatternError(value="", reason="empty pattern")`; `*` alone → `InvalidPatternError(value="*", reason="matches all column names; use redact: {replace: []} to disable redaction explicitly")`; `?` alone → `InvalidPatternError(value="?", reason="matches every single-character column name")`. All other `fnmatch` glob expressions are accepted — including `*` as a sub-pattern (e.g. `*_email` is fine). *Why:* a config-injection that broadens redaction may seem safe but actually defeats it — an over-broad pattern can prompt downstream code to "helpfully" un-redact ("everything is PII? must be a misconfiguration").
- **DEC-024 — `SamplingMode` as `StrEnum` + `RedactionRecord.reason` as `Literal[...]`** (resolves C11 + C12). `SamplingMode(StrEnum)` with members `SCHEMA_ONLY = "schema-only"`, `AGGREGATE_ONLY = "aggregate-only"`, `SAMPLE = "sample"`. (Python 3.11+ — verify project's runtime floor; v0.1 currently targets 3.10 per pyproject.toml DEC-001 from #1, so use `class SamplingMode(str, Enum)` mixin if 3.11 isn't the floor yet.) `@field_validator("mode", mode="before")` on `SafetyPolicy` accepts `"schema-only"` / `"schema_only"` / `"SCHEMA-ONLY"` / `"Schema-Only"` (lowercase + replace `_` with `-`); unknown values raise `InvalidSamplingModeError(value, allowed=tuple(SamplingMode))`. `RedactionRecord.reason: Literal["column_meta_optout", "model_meta_optout", "tag_pii_column", "tag_pii_model", "meta_contains_pii_column", "meta_contains_pii_model", "pattern_match"]`. *Why:* StrEnum gives type-safe `is`-comparison and clean YAML round-trip; `Literal` makes audit-log consumers exhaustively pattern-match. *How to apply:* `tests/safety/test_models.py` parametrises across all enum members and the seven literal reasons.
- **DEC-025 — Top-level `signalforge.yml` namespace: `{ safety: { ... } }`** (resolves C13). The config file's top-level shape: `{ safety: { mode, redact, audit_path, sample_size, ... } }`. `load_safety_config` extracts the `safety:` key and validates only that subtree against `SafetyPolicy`. Other top-level keys (`llm:`, `prune:`, `grade:`, ...) are reserved for future stages and silently ignored by the safety loader (each future stage validates its own subtree). The `_SafetyConfigFile` pydantic model has `safety: _SafetyPolicyContent` and `extra="ignore"` at the top level (so future top-level keys don't fail today's loads); `_SafetyPolicyContent` has `extra="forbid"` (so typos *inside* `safety:` fail loud, per DEC-015). *Why:* the warehouse adapter's `dbt profiles.yml` reader (#3) lived alongside dbt's existing keys without claiming the whole file; `signalforge.yml` is greenfield, but reserving namespaces upfront avoids a v2-config migration. *How to apply:* documented in `safety-ops.md` and an inline comment on `_SafetyConfigFile`.
- **DEC-026 — Error hierarchy + drift-detector + JSONL rotation as user responsibility** (resolves C15 + C16 + C10). (a) **Errors:** `signalforge.safety.errors` ships `SafetyError` (base; mirrors `WarehouseError` / `ManifestError` patterns from #3 + #2: `default_remediation` ClassVar, `↳ Remediation:` rendering in `__str__`, `_format_value(v) := repr(v)` helper for user-input quoting per DEC-022 from #3) plus 9 subclasses: `ConfigNotFoundError(path)`, `InvalidConfigError(message)`, `InvalidSamplingModeError(value, allowed)`, `InvalidPatternError(value, reason)`, `ColumnNotInModelError(model, column)`, `AuditWriteError(path, cause)`, `AuditRecordTooLargeError(size, limit)`, `PolicyValidationError(field, value, reason)`, `UnknownConfigKeyError(key, scope)` (raised by the `extra="forbid"` validators). Total: 9 typed subclasses + 1 base. (b) **Drift detector:** `tests/fixtures/safety/audit_events_sample.jsonl` commits one canonical `AuditEvent` JSONL line. `tests/safety/test_drift_detector.py` defines a one-off `class StrictAuditEvent(BaseModel): model_config = ConfigDict(extra="forbid"); ...` mirroring production `AuditEvent`'s field set, and validates the fixture against it. Adding a field to production `AuditEvent` without updating the fixture or the strict model breaks the test loudly. The fixture has a regeneration script `tests/fixtures/safety/regenerate.sh` that rebuilds it from a small in-process construction (no external tool needed). (c) **JSONL rotation:** documented in `safety-ops.md` as user responsibility (logrotate, manual archive, or external log shipper); no rotation logic in v0.1 — adding it would force a code path that surfaces at the worst moment (audit write) and conflicts with fail-closed semantics. *How to apply:* errors module is the first implementation story after fixtures (US-003); drift detector lands in US-011; JSONL rotation is a doc-only paragraph in US-012.

## Detailed Breakdown

Thirteen stories. Architecture order: deps-and-config → fixtures → errors → models → policy → config loader → audit → redact → aggregate → request builder → public API → docs → quality gate → patterns. Validation command (run after every story): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`. The `docs/research/dbt-claude-technical-surface.md` §4.5 is the design-doc reference for posture decisions throughout.

### US-001 — Subpackage scaffolding + pytest config

**Description:** Create the empty `signalforge.safety` subpackage skeleton, verify `pyproject.toml`'s wheel target + pytest config still cover it without changes (the existing `packages = ["src/signalforge"]` already includes everything under `src/signalforge/**`), and add the `safety` pytest marker.

**Traces to:** DEC-001, `python-build.md`, `testing-signal.md`.

**Acceptance criteria:**
- `src/signalforge/safety/__init__.py` exists (empty placeholder; full re-exports in US-011).
- `[tool.pytest.ini_options].markers` adds `"safety: tests for the PII safety layer"`.
- `pip install -e ".[dev]"` still succeeds; `pytest --collect-only` returns the existing test set with no marker errors.
- Validation command passes.

**Done when:** `from signalforge import safety` works (no error); `pytest -m safety --collect-only` returns 0 tests (none exist yet, no error).

**Files:** `src/signalforge/safety/__init__.py` (new), `pyproject.toml` (markers).

**Depends on:** none.

**TDD:** N/A (scaffolding only).

---

### US-002 — Test fixtures: `signalforge.yml` variants + manifest with PII meta + audit JSONL sample

**Description:** Hand-author the YAML/JSON fixtures consumed by US-005 through US-011. The manifest fixture is hand-derived (no `dbt parse` needed — only the `meta`/`tags` shape matters for these tests); the JSONL audit sample is regenerated via a tiny in-process script.

**Traces to:** DEC-016 (config branches), DEC-017 (`extend`/`replace`), DEC-021 (sample-mode flag), DEC-026 (drift-detector fixture).

**Acceptance criteria:**
- `tests/fixtures/safety/signalforge_minimal.yml` — `{ safety: { mode: schema-only } }` (happy path; everything else defaults).
- `tests/fixtures/safety/signalforge_extend.yml` — `{ safety: { redact: { extend: ["*custom_*"] } } }`.
- `tests/fixtures/safety/signalforge_replace_empty.yml` — `{ safety: { redact: { replace: [] } } }` (warning case).
- `tests/fixtures/safety/signalforge_extend_replace_conflict.yml` — both keys present.
- `tests/fixtures/safety/signalforge_unknown_mode.yml` — `mode: phantom`.
- `tests/fixtures/safety/signalforge_typo.yml` — `redacts: ...` (typo) for `extra="forbid"` test.
- `tests/fixtures/safety/signalforge_audit_path_traversal.yml` — `audit_path: ../../escape.jsonl` for DEC-013 test.
- `tests/fixtures/safety/signalforge_unknown_top_level.yml` — `{ safety: {...}, llm: {...} }` (unknown top-level key, must be ignored per DEC-025).
- `tests/fixtures/safety/manifest_with_pii_meta.json` — small manifest snippet with one model exhibiting all four opt-out signals: a column with `meta.signalforge.sample: false`, a column with `tags: ["pii"]`, a column with `meta.contains_pii: true`, and a column matching `*_email` pattern. Plus a model-level `tags: ["pii"]` variant on a sibling model.
- `tests/fixtures/safety/audit_events_sample.jsonl` — one canonical `AuditEvent` line for the drift detector.
- `tests/fixtures/safety/regenerate.sh` — emits `audit_events_sample.jsonl` via `python -c "from signalforge.safety.models import AuditEvent; ..."`.
- `tests/fixtures/README.md` updated with a new "Safety" section (regeneration trigger, hand-author note for the `.yml` files).

**Done when:** all eight YAML files load via `yaml.safe_load` without errors; the manifest JSON parses; the JSONL has exactly one record.

**Files:** `tests/fixtures/safety/*.yml`, `tests/fixtures/safety/manifest_with_pii_meta.json`, `tests/fixtures/safety/audit_events_sample.jsonl`, `tests/fixtures/safety/regenerate.sh`, `tests/fixtures/README.md` (modified).

**Depends on:** US-001.

**TDD:** N/A (fixtures only; consumed by US-006, US-008, US-011).

---

### US-003 — Errors module

**Description:** Implement `signalforge.safety.errors` with the full 10-class hierarchy from DEC-026.

**Traces to:** DEC-026, DEC-022 (user-input quoting via `_format_value`), `manifest-readers.md` (remediation pattern).

**Acceptance criteria:**
- `signalforge/safety/errors.py` defines `SafetyError(Exception)` with `default_remediation: ClassVar[str]`, `message`, `remediation` instance attrs, `__str__` rendering `"{message}\n  ↳ Remediation: {remediation}"`, and a `_format_value(v) -> str := repr(v)` helper.
- All 9 subclasses listed in DEC-026 implemented with class-level `default_remediation` and discriminating attributes.
- User-supplied strings rendered via `_format_value` in messages: e.g. `InvalidPatternError(value="\x1b[31m", reason="empty")` renders the value repr-quoted with control chars visible.
- `signalforge.safety.errors.__all__` lists all 10 classes.
- Validation command passes.

**Done when:** `from signalforge.safety.errors import SafetyError, AuditWriteError, ...` works; `tests/safety/test_errors.py` covers each class for remediation rendering and adversarial-input quoting.

**Files:** `src/signalforge/safety/errors.py` (new), `tests/safety/test_errors.py` (new).

**Depends on:** US-001.

**TDD:** Yes. Test cases first:
- `test_safety_error_renders_remediation` — base class `__str__` includes `↳ Remediation:`
- `test_each_subclass_has_default_remediation` — parametrised over the 9 subclasses
- `test_invalid_pattern_error_quotes_user_input` — adversarial value with control chars
- `test_audit_write_error_carries_cause` — `cause: BaseException | None` exposed for chaining
- `test_audit_record_too_large_error_includes_size_and_limit`

---

### US-004 — Typed models: `SamplingMode`, `RedactionRecord`, `AuditEvent`, `LLMRequest`

**Description:** Implement the read-back-stable typed shapes in `signalforge.safety.models`. `SafetyPolicy` lands separately in US-005 because it carries the `@model_validator` for `extend`/`replace` resolution.

**Traces to:** DEC-014 (audit reproducibility), DEC-015 (`extra="ignore"` on read-back), DEC-022 (immutability via tuples), DEC-024 (StrEnum + Literal).

**Acceptance criteria:**
- `SamplingMode` is `StrEnum` (or `str, Enum` mixin if Python floor is 3.10) with `SCHEMA_ONLY = "schema-only"`, `AGGREGATE_ONLY = "aggregate-only"`, `SAMPLE = "sample"`.
- `RedactionRecord` is frozen Pydantic v2 with `column_name: str`, `hashed_name: str`, `redacted: bool`, `reason: Literal[...]` (the seven values from DEC-024).
- `AuditEvent` is frozen Pydantic v2 with: `timestamp: datetime`, `model_unique_id: str`, `mode: SamplingMode`, `columns_sent: tuple[str, ...]`, `redactions: tuple[RedactionRecord, ...]`, `row_count: int | None`, `signalforge_version: str`, `policy_hash: str`, `audit_schema_version: int = 1`, `policy_flags: tuple[str, ...] = ()`. `extra="ignore"`.
- `LLMRequest` is frozen Pydantic v2 with: `model_unique_id: str`, `mode: SamplingMode`, `columns_sent: tuple[str, ...]`, `redactions: tuple[RedactionRecord, ...]`, `sampled_rows: tuple[dict[str, Any], ...] | None`, `aggregates: dict[str, ColumnStats | None] | None`, `schema: tuple[tuple[str, str], ...]`. Imports `ColumnStats` from `signalforge.warehouse.models`.
- `LLMRequest` docstring includes: `"Construct only via signalforge.safety.request.build_llm_request — direct construction bypasses the audit log."`
- Validation command passes.

**Done when:** `from signalforge.safety.models import SamplingMode, RedactionRecord, AuditEvent, LLMRequest` works; round-trip JSON serialisation preserves field order and types; mutating `request.columns_sent` raises (tuple, not list).

**Files:** `src/signalforge/safety/models.py` (new), `tests/safety/test_models.py` (new).

**Depends on:** US-003.

**TDD:** Yes. Test cases first:
- `test_sampling_mode_enum_values_exact_strings`
- `test_redaction_record_reason_literal_rejects_unknown` — `RedactionRecord(reason="phantom", ...)` raises `ValidationError`
- `test_audit_event_schema_version_default_is_1`
- `test_audit_event_extra_ignore_drops_unknown_field`
- `test_llm_request_columns_sent_immutable` — `request.columns_sent.__class__ is tuple`; `request.columns_sent[0] = "x"` raises `TypeError`
- `test_llm_request_sampled_rows_immutable_when_none` and `test_llm_request_sampled_rows_immutable_when_present`

---

### US-005 — `SafetyPolicy` + `_resolve_redact_patterns` + `_compute_policy_hash`

**Description:** Implement `signalforge.safety.policy.SafetyPolicy` — the user-facing config-shaped model — with `@model_validator(mode="before")` for `extend`/`replace`, the `with_mode` factory, and the `policy_hash` helper.

**Traces to:** DEC-007 (built-ins + override semantics), DEC-014 (`policy_hash`), DEC-017 (`@model_validator`), DEC-018 (`with_mode`), DEC-021 (sample-mode warning), DEC-023 (pattern-injection rejection), DEC-024 (`@field_validator` for mode case-insensitive load).

**Acceptance criteria:**
- `SafetyPolicy` is frozen Pydantic v2 with `mode: SamplingMode = SamplingMode.SCHEMA_ONLY`, `redact_patterns: tuple[str, ...]`, `sample_size: int = 100`, `audit_path: Path` (default `Path(".signalforge/audit.jsonl")`).
- `model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)`.
- `@model_validator(mode="before")` named `_resolve_redact_patterns` consumes `redact: {extend?, replace?}` and resolves into `redact_patterns`. Mutual exclusion enforced. Empty `replace: []` allowed but emits one WARNING.
- `@field_validator("mode", mode="before")` accepts case-insensitive variants (`"schema-only"`, `"Schema-Only"`, `"SCHEMA_ONLY"`, etc.); unknown raises `InvalidSamplingModeError`.
- `@field_validator("redact_patterns")` rejects empty / `*` / `?` patterns with `InvalidPatternError`.
- `with_mode(self, mode: SamplingMode) -> SafetyPolicy` via `self.model_copy(update={"mode": mode})`. Documented as #9's CLI override seam.
- Module emits one WARNING on construction when `mode == SAMPLE`: `"Sample mode enabled — raw row data will be sent to the LLM. Verify column tags/meta opt-outs."`
- `_compute_policy_hash(policy: SafetyPolicy) -> str` — SHA-256 of `policy.model_dump_json(by_alias=True)` with sorted keys, hex-truncated to 16 chars.
- Validation command passes.

**Done when:** `SafetyPolicy()` constructs with defaults; `SafetyPolicy.model_validate({"mode": "Schema-Only", "redact": {"extend": ["*custom"]}})` resolves correctly; `_compute_policy_hash` is deterministic across two equal policies.

**Files:** `src/signalforge/safety/policy.py` (new), `tests/safety/test_policy.py` (new).

**Depends on:** US-003, US-004.

**TDD:** Yes. Test cases first:
- `test_safety_policy_no_args_is_schema_only` *(default-mode regression — DEC-012(a))*
- `test_safety_policy_extra_forbid_rejects_typo` — `redacts:` typo raises `ValidationError`
- `test_safety_policy_redact_extend_appends_to_builtins`
- `test_safety_policy_redact_replace_substitutes`
- `test_safety_policy_redact_extend_and_replace_simultaneously_errors`
- `test_safety_policy_redact_replace_empty_warns_once`
- `test_safety_policy_mode_case_insensitive_load` — parametrised across 6 case variants
- `test_safety_policy_mode_unknown_raises_invalid_sampling_mode_error`
- `test_safety_policy_pattern_empty_raises_invalid_pattern_error`
- `test_safety_policy_pattern_star_alone_raises_invalid_pattern_error`
- `test_safety_policy_pattern_question_mark_alone_raises`
- `test_safety_policy_with_mode_returns_new_frozen_policy`
- `test_safety_policy_with_mode_preserves_other_fields`
- `test_safety_policy_sample_mode_emits_warning_on_construction`
- `test_compute_policy_hash_deterministic_for_equal_policies`
- `test_compute_policy_hash_differs_for_semantically_different_policies`

---

### US-006 — Config loader + path safety

**Description:** Implement `signalforge.safety.config.load_safety_config(project_dir, path=None) -> SafetyPolicy` with the full DEC-016 error contract and the audit-path traversal hardening (DEC-013).

**Traces to:** DEC-002, DEC-013 (`audit_path` security), DEC-016 (error contract), DEC-025 (top-level namespace).

**Acceptance criteria:**
- `load_safety_config(project_dir: Path, path: Path | None = None) -> SafetyPolicy`. Resolution: explicit-path miss → `ConfigNotFoundError`; implicit-path miss → defaults; empty file → defaults (DEBUG log); non-mapping top level → `InvalidConfigError`; missing `safety:` key → defaults; schema-invalid contents → typed errors.
- `_SafetyConfigFile` Pydantic model has `safety: SafetyPolicyContent` with `extra="ignore"` at top level (other top-level keys reserved for future stages); `SafetyPolicyContent` is `SafetyPolicy` itself (re-exported under that internal name for clarity).
- `audit_path` security: reject `..` segments at validation time (`InvalidConfigError`); canonicalise via `signalforge.safety._path_safety.canonicalise_path`; require resolved path inside `project_dir`.
- `signalforge/safety/_path_safety.py` is copied (not imported) from `signalforge.warehouse._path_safety`, with safety-layer-specific error class `InvalidConfigError` for traversal failures (per `warehouse-adapters.md`'s duplication precedent).
- `yaml.safe_load` only — never `yaml.load`.
- Validation command passes.

**Done when:** all six DEC-016 branches covered by tests; traversal attempts fail loudly.

**Files:** `src/signalforge/safety/config.py` (new), `src/signalforge/safety/_path_safety.py` (new, copied from warehouse), `tests/safety/test_config.py` (new).

**Depends on:** US-002, US-003, US-005.

**TDD:** Yes. Test cases first:
- `test_load_safety_config_no_file_is_schema_only` *(default-mode regression — DEC-012(b))*
- `test_load_safety_config_empty_file_returns_defaults`
- `test_load_safety_config_missing_safety_key_returns_defaults`
- `test_load_safety_config_unknown_top_level_key_ignored`
- `test_load_safety_config_explicit_path_missing_raises_config_not_found`
- `test_load_safety_config_implicit_path_missing_returns_defaults`
- `test_load_safety_config_malformed_yaml_raises_invalid_config`
- `test_load_safety_config_non_mapping_top_level_raises_invalid_config`
- `test_load_safety_config_unknown_mode_raises_invalid_sampling_mode`
- `test_load_safety_config_typo_in_safety_block_raises_validation_error`
- `test_load_safety_config_audit_path_with_dotdot_raises`
- `test_load_safety_config_audit_path_outside_project_raises`
- `test_load_safety_config_audit_path_symlink_to_outside_raises`
- `test_load_safety_config_extend_resolution_via_yaml`
- `test_load_safety_config_replace_empty_warns_once`

---

### US-007 — Audit module: write + fail-closed + size cap + ANSI-safe logger

**Description:** Implement `signalforge.safety.audit.write(event, audit_path)` with O_APPEND atomic write, fsync, size assertion, and the lazy-format ANSI-safe logger pattern.

**Traces to:** DEC-005, DEC-011 (fail-closed), DEC-022 (size cap + ANSI guard).

**Acceptance criteria:**
- `audit.write(event: AuditEvent, audit_path: Path) -> None` opens with `O_APPEND | O_CREAT | 0o600`, writes `json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n"`, calls `os.fsync(fd)`, closes.
- Catches NO exceptions internally; any `OSError` / `PermissionError` / `IOError` / encoding error propagates as `AuditWriteError(path, cause)`.
- Size assertion: `if len(line.encode("utf-8")) > 4000: raise AuditRecordTooLargeError(size=..., limit=4000)`.
- Parent directory created via `audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)` before open; mkdir failures also propagate as `AuditWriteError`.
- Logger emits one INFO line per write: `_LOGGER.info("audit event: %s", json.dumps({"unique_id": event.model_unique_id, "mode": event.mode.value, "columns_sent": len(event.columns_sent), "redacted": len(event.redactions)}))`. Lazy-format with json.dumps; never f-string interpolation of user-controlled strings.
- Validation command passes.

**Done when:** concurrent-thread test produces a JSONL whose every line round-trips through `json.loads`; failure-injection test (`chmod 000` on parent dir under `tmp_path`) raises `AuditWriteError`.

**Files:** `src/signalforge/safety/audit.py` (new), `tests/safety/test_audit.py` (new).

**Depends on:** US-003, US-004.

**TDD:** Yes. Test cases first:
- `test_audit_write_appends_one_jsonl_line`
- `test_audit_write_fsyncs_before_returning` *(use `mock` to assert fsync is called — but only for fsync, not the whole open/write chain)*
- `test_audit_write_round_trips_through_json_loads`
- `test_audit_write_creates_parent_dir`
- `test_audit_write_failure_raises_audit_write_error` *(chmod 000 on parent under tmp_path)*
- `test_audit_write_oversize_record_raises_too_large` *(craft an event with a 5000-byte field via the `model_unique_id` string)*
- `test_audit_write_concurrent_threads_no_interleave` *(spawn 10 threads, 100 records each; assert exactly 1000 valid JSONL lines)*
- `test_audit_logger_line_escapes_ansi_in_user_input` *(model_unique_id = `"\x1b[31mFAKE\x1b[0m"`; log capture asserts `` appears, raw escape does not)*

---

### US-008 — Redaction: `_classify_column`, `redact_rows`, `redact_column_names`, `hash_column_name`

**Description:** Implement the pure redaction helpers in `signalforge.safety.redact`. `_classify_column` is the central function called by both the request builder and the aggregate wrapper.

**Traces to:** DEC-003 (the four opt-out signals + pattern), DEC-010 (column-name redaction via blake2b hash), DEC-020 (case-insensitivity + suspicious-column heuristic), DEC-024 (Literal reasons).

**Acceptance criteria:**
- `hash_column_name(name: str) -> str` returns `"col_" + blake2b(name.encode(), digest_size=4).hexdigest()` (8 hex chars). Deterministic.
- `_classify_column(column: Column, model: Model, policy: SafetyPolicy) -> RedactionRecord` (pure). Precedence: column-level signals beat model-level on conflict; first-match-wins among the seven reasons.
- Tag matching is case-insensitive (lowercase both sides); `meta.contains_pii` accepts truthy values (DEBUG-log on coercion); `tags: [PII]` normalises to lowercase.
- Pattern matching: `fnmatch.fnmatchcase(name.lower(), pattern.lower())`.
- "Suspicious unmatched column" WARNING heuristic: if a column's lowercased name contains any of `{"email", "phone", "ssn", "password", "token", "secret", "api_key"}` AND `_classify_column` returned `redacted=False`, emit WARNING once per (model, column) pair.
- `redact_rows(rows: tuple[dict[str, Any], ...], hashed_to_real: dict[str, str]) -> tuple[dict[str, Any], ...]` — replaces values for keys whose name is being redacted with `"<REDACTED>"`. Does not mutate input. Missing-key-in-row is silently ignored.
- `redact_column_names(columns: tuple[tuple[str, str], ...], records: tuple[RedactionRecord, ...]) -> tuple[tuple[str, str], ...]` — substitutes hashed names for redacted columns in a `(name, type)` tuple sequence.
- Validation command passes.

**Done when:** `_classify_column` parametrised matrix passes (column meta opt-out, model meta opt-out, column tag pii, model tag pii, column meta.contains_pii, model meta.contains_pii, pattern match, none-of-the-above); precedence assertions pass.

**Files:** `src/signalforge/safety/redact.py` (new), `tests/safety/test_redact.py` (new), `tests/safety/test_classify.py` (new).

**Depends on:** US-004, US-005.

**TDD:** Yes. Test cases first:
- `test_hash_column_name_deterministic` and `test_hash_column_name_distinct_for_distinct_inputs`
- `test_classify_column_matrix` — parametrised seven reasons
- `test_classify_column_precedence_column_over_model`
- `test_classify_tag_pii_uppercase_normalised`
- `test_classify_meta_contains_pii_truthy_string`
- `test_classify_meta_contains_pii_truthy_int`
- `test_classify_meta_contains_pii_falsy_zero_does_not_redact`
- `test_classify_pattern_case_insensitive`
- `test_classify_suspicious_unmatched_column_warns`
- `test_redact_rows_replaces_values`
- `test_redact_rows_does_not_mutate_input`
- `test_redact_rows_missing_column_in_row_silent`
- `test_redact_column_names_substitutes_hashed`

---

### US-009 — Aggregate wrapper: `aggregate_columns`

**Description:** Implement `signalforge.safety.aggregate.aggregate_columns(adapter, model, columns, policy)`. Calls `_classify_column` for each requested column; for non-redacted columns, calls `adapter.column_stats` inside the adapter's `with` context (per #3's DEC-008 batching). For redacted columns, returns `None` and records a `RedactionRecord`.

**Traces to:** DEC-008.

**Acceptance criteria:**
- `aggregate_columns(adapter: WarehouseAdapter, model: Model, columns: list[str], policy: SafetyPolicy) -> tuple[dict[str, ColumnStats | None], tuple[RedactionRecord, ...]]`.
- Non-redacted columns invoke `adapter.column_stats(table=TableRef.from_model(model), column=name)` inside `with adapter:` context.
- Redacted columns yield `None` in the returned dict (keyed by hashed name, not real name) and one `RedactionRecord` in the returned tuple.
- Tests use a `FakeAdapter` (US-009.5 file `tests/safety/_fake_adapter.py`) that mirrors `tests/warehouse/_fake.py`'s `expect_*` API. NOT `MagicMock`.
- Validation command passes.

**Done when:** 50-column model with 20 redacted + 30 non-redacted produces one batched query (verified by `FakeAdapter` expecting exactly one `column_stats` call per non-redacted column inside the context); redacted columns produce `None` values.

**Files:** `src/signalforge/safety/aggregate.py` (new), `tests/safety/_fake_adapter.py` (new), `tests/safety/test_aggregate.py` (new), `tests/safety/test_fake_adapter.py` (new — verifies the fake satisfies the ABC).

**Depends on:** US-005, US-008.

**TDD:** Yes. Test cases first:
- `test_fake_adapter_satisfies_warehouse_adapter_abc`
- `test_fake_adapter_unexpected_call_raises_assertion_error`
- `test_aggregate_columns_redacted_returns_none`
- `test_aggregate_columns_calls_adapter_for_non_redacted`
- `test_aggregate_columns_does_not_call_adapter_for_redacted`
- `test_aggregate_columns_uses_with_adapter_context`
- `test_aggregate_columns_returns_redaction_records`

---

### US-010 — Request builder: `build_llm_request`

**Description:** Implement `signalforge.safety.request.build_llm_request(model, adapter, policy)` — the single entry point that produces an `LLMRequest`, calls `audit.write` before returning, and orchestrates the per-mode behaviour.

**Traces to:** DEC-009 (single entry), DEC-010 (column-name hashing), DEC-011 (fail-closed audit), DEC-012(c) (zero adapter calls in schema-only).

**Acceptance criteria:**
- `build_llm_request(model: Model, adapter: WarehouseAdapter, policy: SafetyPolicy) -> LLMRequest`.
- Per-mode behaviour:
  - `SCHEMA_ONLY`: zero `adapter.column_stats` and zero `adapter.sample_rows` calls. `LLMRequest.sampled_rows = None`, `aggregates = None`. `schema` carries `(hashed_name, type)` for redacted columns and `(real_name, type)` for non-redacted.
  - `AGGREGATE_ONLY`: calls `aggregate_columns`. `sampled_rows = None`. `schema` as above. `aggregates` populated (with `None` for redacted-column keys).
  - `SAMPLE`: calls `adapter.sample_rows(TableRef.from_model(model), n=policy.sample_size)`; redacts values via `redact_rows`; redacts NAMES in the row dicts (replaces real key with hashed key) so the LLM sees the same hashed identifiers as `schema`.
- Constructs `AuditEvent` with `signalforge_version`, `policy_hash` (via `_compute_policy_hash`), `audit_schema_version=1`, `policy_flags` (per DEC-021 closed set).
- Calls `audit.write(event, policy.audit_path)` AFTER constructing the request, BEFORE returning. Any audit-write exception aborts (no `LLMRequest` returned).
- Validation command passes.

**Done when:** all three modes produce correct `LLMRequest` shapes verified against `FakeAdapter`; `test_build_llm_request_default_policy_zero_warehouse_calls` passes (default-mode regression DEC-012(c)).

**Files:** `src/signalforge/safety/request.py` (new), `tests/safety/test_request.py` (new), `tests/safety/test_default_mode_regression.py` (new — clusters DEC-012's three regression tests).

**Depends on:** US-007, US-008, US-009.

**TDD:** Yes. Test cases first:
- `test_build_llm_request_schema_only_zero_warehouse_calls` *(DEC-012(c))*
- `test_build_llm_request_aggregate_only_calls_column_stats_per_non_redacted_column`
- `test_build_llm_request_sample_redacts_values_to_redacted_constant`
- `test_build_llm_request_sample_redacts_names_in_rows_to_hashed`
- `test_build_llm_request_schema_uses_hashed_names_for_redacted`
- `test_build_llm_request_audit_emitted_exactly_once`
- `test_build_llm_request_audit_carries_signalforge_version`
- `test_build_llm_request_audit_carries_policy_hash`
- `test_build_llm_request_audit_carries_schema_version_1`
- `test_build_llm_request_audit_policy_flags_sample_mode_enabled`
- `test_build_llm_request_audit_policy_flags_redaction_disabled`
- `test_build_llm_request_audit_write_failure_raises_audit_write_error_no_request_returned` *(DEC-011)*
- `test_build_llm_request_returned_request_is_transitively_immutable` *(DEC-022)*

---

### US-011 — Public API + drift detector + AST audit-completeness check

**Description:** Wire `signalforge/safety/__init__.py` with the documented public surface; add the `tests/safety/test_drift_detector.py` (DEC-026) and `tests/safety/test_public_api.py` (which includes the AST-scan for direct `LLMRequest(...)` construction outside `request.py`).

**Traces to:** DEC-001 (re-export discipline), DEC-020(a) (audit-completeness AST scan), DEC-026 (drift detector).

**Acceptance criteria:**
- `signalforge/safety/__init__.py` re-exports: `SamplingMode`, `SafetyPolicy`, `LLMRequest`, `RedactionRecord`, `AuditEvent`, `load_safety_config`, `build_llm_request`, `aggregate_columns`, `redact_rows`, `SafetyError` + 9 subclasses.
- `__all__` matches the documented surface; private helpers (`_classify_column`, `_compute_policy_hash`, `_resolve_redact_patterns`, `_path_safety`, etc.) reachable via dotted import only.
- `tests/safety/test_drift_detector.py` defines `StrictAuditEvent(extra="forbid")` mirroring production fields; validates `tests/fixtures/safety/audit_events_sample.jsonl` against it.
- `tests/safety/test_public_api.py`:
  - `test_documented_surface_importable_from_package_root` — every name in the README's documented surface imports from `signalforge.safety`.
  - `test_private_helpers_not_in_dir` — `_classify_column`, `_compute_policy_hash` not in `dir(signalforge.safety)`.
  - `test_llm_request_construction_only_in_request_module` — AST-scans `src/signalforge/safety/*.py` (excluding `request.py`) for `Call(func=Name(id="LLMRequest"))`; asserts zero matches.
- Validation command passes.

**Done when:** `from signalforge.safety import SamplingMode, ...` works for every documented name; the AST scan catches a planted violation in a test fixture (negative test).

**Files:** `src/signalforge/safety/__init__.py` (full re-exports; replaces US-001's placeholder), `tests/safety/test_public_api.py` (new), `tests/safety/test_drift_detector.py` (new).

**Depends on:** US-003, US-004, US-005, US-006, US-007, US-008, US-009, US-010.

**TDD:** N/A (declarative public API).

---

### US-012 — `docs/safety-ops.md` + README "Data safety" section

**Description:** Author the operational reference matching the precedent set by `docs/manifest-loader-ops.md` and `docs/warehouse-adapter-ops.md`; add a "Data safety" section to README between "Configuration" and "Roadmap".

**Traces to:** DEC-019 (ops doc commitment), DEC-020(d) (audit log sensitivity), DEC-026(c) (rotation as user responsibility).

**Acceptance criteria:**
- `docs/safety-ops.md` sections (in order):
  1. **Default posture** — schema-only, the fail-closed model, the "explicit opt-in for sampling" framing.
  2. **Modes** — schema-only / aggregate-only / sample with concrete examples.
  3. **`signalforge.yml` reference** — top-level `safety:` namespace; mode; `redact: { extend, replace }`; `sample_size`; `audit_path`.
  4. **Redaction patterns** — case-insensitive `fnmatch` glob; built-ins; override semantics; the suspicious-column WARNING heuristic.
  5. **Per-column opt-out** — the four signals (column meta, model meta, tags, `meta.contains_pii`); precedence rules; case-insensitive `tags` matching; `contains_pii` truthy coercion.
  6. **Column-name redaction** — DEC-010's blake2b hash; how the audit log maps `(real, hashed)` back.
  7. **Audit JSONL schema** — every field with type and meaning; `audit_schema_version` reference; `policy_flags` closed set.
  8. **Audit log sensitivity** — the JSONL contains plaintext column names; treat at-rest as sensitive (Gitignore in `.signalforge/`).
  9. **Audit log rotation** — user responsibility (logrotate / archive); no built-in rotation in v0.1.
  10. **Debugging** — logger names, levels (`signalforge.safety` at INFO/WARNING/DEBUG); how to read a fail-closed `AuditWriteError`.
  11. **Typed-error reference** — table cross-linked to `signalforge.safety.errors` with each subclass's discriminating fields.
  12. **CLI integration note** — pointer to #9 ("`--mode` flag wires through `policy.with_mode()`"); explicit "no env-var override for mode" callout.
- `README.md` adds a "Data safety" section (3 paragraphs): the schema-only-default posture, link to `docs/safety-ops.md`, callout that `.signalforge/audit.jsonl` should be Gitignored.
- `.gitignore` adds `.signalforge/` (the audit-log directory).
- Validation command passes (no Python changes; ruff/pyright unaffected).

**Done when:** `docs/safety-ops.md` is committed; README cross-link works; `.gitignore` includes `.signalforge/`.

**Files:** `docs/safety-ops.md` (new), `README.md` (modified), `.gitignore` (modified).

**Depends on:** US-001 through US-011 (doc references the implemented behaviour).

**TDD:** N/A (docs only).

---

### US-013 — Quality Gate

**Description:** Run code-reviewer four times across the full changeset; address each pass's real bugs. Run CodeRabbit if available. Validation command must pass after all fixes.

**Traces to:** all DECs.

**Acceptance criteria:**
- 4 code-reviewer passes; each pass's blockers / concerns fixed before the next.
- CodeRabbit review if MCP/CLI available; non-blocking suggestions logged.
- `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` passes from a clean checkout.
- No `MagicMock` in any new test (grep enforces).
- No raw `yaml.load` (grep enforces).
- No f-string interpolation of user-controlled strings in any logger call within `src/signalforge/safety/` (grep `_LOGGER.\w+\(f"` and audit any hits).
- The three default-mode regression tests (DEC-012) pass.
- The drift detector (US-011) passes against the committed fixture.
- The AST audit-completeness scan (US-011) catches a planted violation.

**Done when:** all four passes' findings are addressed; validation is clean; no anti-pattern greps return hits.

**Files:** any file the reviewer flags.

**Depends on:** US-001 through US-012.

**TDD:** N/A (review pass).

---

### US-014 — Patterns & Memory

**Description:** Update `.claude/rules/`, `docs/`, and beads memory with new patterns established by this ticket.

**Traces to:** all DECs.

**Acceptance criteria:**
- New `.claude/rules/safety-layer.md` distilling the load-bearing rules: fail-closed audit semantics, column-name redaction via stable hash, audit-event reproducibility fields (signalforge_version + policy_hash + audit_schema_version), `extra="forbid"` on config-shaped models vs. `extra="ignore"` on read-back, the four opt-out signals + precedence, ANSI-safe lazy-format logger, AST audit-completeness scan, drift-detector pattern.
- `bd remember` entries for: (a) "schema-only mode redacts column names too — names alone leak PII"; (b) "audit writes are fail-closed — any write failure aborts the LLM call"; (c) "config-shaped Pydantic models use extra=forbid; read-back models use extra=ignore + drift detector".
- `CLAUDE.md` "Public API surface (v0.1)" section updated with `signalforge.safety` entries (`SamplingMode`, `SafetyPolicy`, `LLMRequest`, `load_safety_config`, `build_llm_request`, `SafetyError` hierarchy).
- `CLAUDE.md` "Repository status" bullet added for issue #4.
- `MEMORY.md` index entry pointing at `.claude/rules/safety-layer.md` (if convention applies — verify against existing memory layout).
- Validation command passes.

**Done when:** future-you opening this repo cold sees the safety-layer rules immediately on rule-discovery.

**Files:** `.claude/rules/safety-layer.md` (new), `CLAUDE.md` (modified), `bd remember` invocations.

**Depends on:** US-013.

**TDD:** N/A (documentation / rules).

## Beads Manifest

*(Phase 7 — populated on devolve.)*

## Refinement Log

*(Phase 3 — populated after Architecture Review.)*

## Detailed Breakdown

*(Phase 4 — populated after Refinement.)*

## Beads Manifest

*(Phase 7 — populated on devolve.)*
