# Prune layer (decision routing + fail-closed audit)

Apply to every module under `signalforge.prune` and to any new code that compiles a candidate test to SQL, runs it against the warehouse, or writes a prune-decision audit record.

Enforces "signal over volume": a candidate test that always passes on warehouse samples must be dropped, not shipped. A test we cannot evaluate must be kept — pruning drops only with positive evidence.

## Conservative drop-reason taxonomy

`DropReason` is a `Literal[...]` of exactly five values:

- `"always-passes"` — test ran, returned zero failing rows on a representative sample, scope was sufficient. Drop.
- `"failed-on-known-clean-data"` — test ran against a `trusted_models` opt-in target and returned non-zero failing rows. The model is known clean; the test is wrong. Drop.
- `"requires-future-data"` — test compiled to a sentinel (e.g. `relationships(to: <unknown>)` where the target model is absent from the manifest). Cannot evaluate; the operator revisits when the dependency lands. Dropped (no warehouse call; the structured reason carries the diagnostic).
- `"kept"` — test ran and returned non-zero failing rows on an untrusted model. Real signal. Kept.
- `"kept-without-evidence"` — could not positively evaluate (budget elapsed, SQL safety rejected the identifier, warehouse call raised, prune disabled, materialisation failed). Kept.

The decision matrix lives in `engine.py::_decide_from_test_result`. **Kept-without-evidence routes to `decision="kept"`, not `decision="dropped"`** — SignalForge ships tests it cannot evaluate; the LLM proposed them, and absent contradicting warehouse evidence the operator gets to make the call.

If you add a sixth `DropReason` literal, update production `DropReason` AND `StrictPruneDecision` AND the fixture at `tests/fixtures/prune/prune_event_v1.jsonl` AND the decision-matrix table in `docs/prune-ops.md` in the same change.

## Conservative-bias routing template

When a stage cannot positively evaluate one or more candidates, route to the existing `kept-without-evidence` literal — **never expand the enum**. The 5-value `DropReason` literal stays locked. The diagnostic travels in the `why` field; the source travels in the log signal.

Established sources, same routing, same audit invariant:

| Source | Scope | Log signal | `why` shape |
|--|--|--|--|
| Per-test warehouse error | one test | none extra | `f"<operation> failed: {type(exc).__name__}: {str(exc)[:200]}"` |
| Per-test SQL safety reject | one test | none extra | `"identifier rejected by SQL safety check"` |
| Total budget exhausted | every remaining | one stderr WARNING | `"total prune budget exceeded before evaluation"` |
| Pre-loop warehouse failure (materialisation) | every candidate | one stderr WARNING | `f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"` |
| Operator disable | every candidate | one stderr INFO | `"prune disabled in signalforge.yml"` |

All sources preserve the fail-closed audit invariant — one `PruneEvent` per candidate via `_write_audit_or_abort` even when the run did no warehouse work. The WARNING-vs-INFO split is the diagnostic question: "did the world get in our way (WARNING)" vs. "did the operator choose this (INFO)."

When a new source lands (`--no-prune` flag, `--max-cost` budget, circuit-breaker), copy this template verbatim. Don't invent a sixth `DropReason`.

## Fail-closed audit

`signalforge.prune.audit.write_prune_event` is a fail-closed JSONL writer. Identical contract to safety / draft:

1. **Propagation IS the defence.** Open with `O_APPEND | O_CREAT | 0o600`, single write (looped on short returns), `os.fsync`, close. No try/except internally — caller (`prune_tests`) wraps as `PruneAuditWriteError(cause=...)`.
2. **Size cap before any file open.** `_PRUNE_AUDIT_RECORD_LIMIT_BYTES = 4000`; oversize raises `PruneAuditRecordTooLargeError(size, limit)` BEFORE `os.open`. Propagates as-is.
3. **Per-decision write happens after each test, not in a final batch.** A run that crashes mid-iteration leaves one durable record per evaluated test. Buffering "to amortise fsync" defeats the audit guarantee.

## Symlink-hardened audit path

`signalforge.warehouse._path_safety.canonicalise_path` is the project's standard symlink/containment gate; route the writer's audit path through it at entry. Raise `PruneAuditWriteError` on containment failure. Don't trust default paths because they're "ours" — the reader's three traps from `manifest-readers.md` apply to writers too.

## Identifier shape validation at the compile seam

LLM-supplied `test.column` / `test.field` strings land in compiled SQL as quoted identifiers. The drafter's anchor-contract validator checks set-membership against `Manifest.columns` but does NOT enforce regex shape — a manifest column named `foo bar` or `users\`; DROP TABLE` passes the anchor check.

`signalforge.prune.compiler` calls `signalforge.warehouse._sql_safety.validate_identifier` on every identifier before quoting (`accepted_values.values` go through `escape_bq_string_literal`). Failures return `_InvalidIdentifier` which routes to `kept-without-evidence` (`why="identifier rejected by SQL safety check"`). Defence in depth.

## Compiler is dialect-driven, not BigQuery-specific

`_compile_test(test, table_ref, dialect: Dialect, manifest)` reads everything warehouse-specific from the `Dialect` value object returned by `WarehouseAdapter.dialect()` — it NEVER branches on `dialect.name`. An AST import-guard (`tests/prune/test_compiler_import_guard.py`) asserts no `snowflake`/`google.cloud` import under `signalforge/prune/`. **No `from google.cloud import bigquery` anywhere in `signalforge/prune/`.** The `relationships` parent join uses `TableRef.from_model` for the same reason.

The compiler consumes these declarative `Dialect` SQL-fragment fields:

- `quote_char` — identifier quote (BigQuery `` ` ``, Snowflake/Postgres `"`).
- `identifier_case` — **load-bearing.** `_fold_identifier`/`_quote` fold every identifier (columns AND each qualified-name component) per `"upper"`/`"lower"`/`"preserve"` BEFORE quoting. Snowflake folds to UPPER (conventional unquoted-DDL stores `CUSTOMER_ID`, so emitting `"customer_id"` would fail); BigQuery `"preserve"` is a no-op. Folding runs on an already-`validate_identifier`'d ASCII token, so it cannot introduce a quote-breaking char.
- `quote_qualified_per_component` — `True` quotes each component separately (`"DB"."SCH"."T"`); `False` wraps the whole dotted path in one quote pair (`` `p.d.t` ``). A single quoted string spanning dots reads as one identifier literally named `db.schema.table`.
- `sample_row_hash_expr` — row-hash expression for `MOD(<expr>, <bucket>) < 1`. BigQuery `ABS(FARM_FINGERPRINT(TO_JSON_STRING(t)))`; Snowflake `ABS(HASH(*))`.
- `timestamp_literal_template` / `date_literal_template` — `str.format(value=…)` templates for partition-filter literals. BigQuery `TIMESTAMP('{value}')` / `DATE('{value}')`; Snowflake `'{value}'::TIMESTAMP` / `'{value}'::DATE`. Only the `datetime`/`date` branches format; the `str` branch routes through `escape_bq_string_literal` (no `.format`).
- `sample_cte_alias` — the sample-CTE identifier (`WITH <alias> AS (...) ... FROM <alias>`). BigQuery bare `sample`; **Snowflake quoted `"sample"`** because `SAMPLE` is a Snowflake reserved keyword and an unquoted `WITH sample AS` is a syntax error there.

`supports_qualify` stays **declared-but-unconsumed forward-compat metadata**: `unique` keeps the dialect-portable `GROUP BY … HAVING COUNT(*) > 1`; a `QUALIFY` rewrite would change semantics (returns failing rows vs the duplicated key) and isn't Snowflake-specific. Don't add a QUALIFY codepath without a separate decision.

**Adding a new vendor dialect:** extend `Dialect` with a declarative field and read it in the compiler — never `if dialect.name == …`. Keep BigQuery-shaped defaults on new fields so existing construction sites + the 11 BigQuery snapshot fixtures stay byte-identical (the regression gate). Validate emitted SQL through a real parser (`sqlglot`) or executor (`fakesnow`/live), gated behind a maintainer-only marker — **snapshot equality certifies shape, not validity** (a snapshot can pin invalid SQL byte-for-byte; the `sqlglot` parse-guard caught the `sample` reserved-word bug).

NULL-exclusion pattern matches dbt-core verbatim: `unique` adds `WHERE col IS NOT NULL`; `accepted_values` adds `WHERE col IS NOT NULL AND col NOT IN (...)`; `relationships` adds child-side `WHERE child.col IS NOT NULL`. Snapshot fixtures pin the exact SQL bytes.

## Total-budget semantics

When `total_budget_seconds` is exceeded, the orchestrator:

1. Best-effort cancels the in-flight test (`query_job.cancel()` on BigQuery; ABC-level no-op fallback).
2. Marks every remaining un-started test as `kept-without-evidence` per the routing template above.
3. Emits a final WARNING with the count of un-started tests (lazy-format JSON).

No partial-evaluation results — a test running when the budget tripped is `kept-without-evidence`, not `kept` (failing-rows count is unknown). The single-threaded sequential model makes this enforceable.

`signalforge.prune.engine` declares `_sleep = time.sleep` at module scope for test-time reassignment (mirrors `llm-drafter.md`; don't monkey-patch globally).

## AST scan extension per new audit type

`tests/test_audit_completeness.py` permits `Call(func=Name(id="PruneEvent"))` only in `src/signalforge/prune/audit.py`. A sanity check that ≥1 construction exists in the blessed module guards against rename-without-update. Every new audit-event type gets its own scan; if a new module legitimately needs to construct a gated event-type, update the exclusion list AND document the new write seam. Don't suppress the test.

## ANSI-safe lazy-format JSON logger + grep gate

Same rule as the other layers. The grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm,draft,prune,grade,diff,cli}` and rejects any `_LOGGER\.\w+\(f"` hit.

## Custom `__repr__` on result-shaped models

Pydantic v2's default `__repr__` emits every field. `PruneResult.__repr__` shows only `model_unique_id`, `kept_count`, `dropped_count`, `elapsed_ms`. `PruneDecision.__repr__` shows only `test.test_type`, `test.column` (or `"<model-level>"`), `decision`, `reason`. Compiled SQL and sample failures stay accessible via field access; they don't slip out via casual debug-print.

User-supplied strings in error messages render via `repr()` (`_format_value` helper) — same rule. A model unique_id containing `\x1b[31m` quoted via `repr()` shows as `'\\x1b[31m...'`; raw interpolation would inject.

## Drift detectors are mandatory for read-back models

Every `extra="ignore"` production model — `PruneResult`, `PruneDecision`, `PruneEvent` — pairs with a `Strict<Model>(extra="forbid")` detector in `tests/prune/test_drift_detector.py`, validated against committed fixtures (`tests/fixtures/prune/prune_event_v1.jsonl` for the audit type). Adding a field to production without updating the strict mirror OR the fixture breaks the test loudly.

`extra=` placement convention from `safety-layer.md` applies: config-shaped (`PruneConfig`, `_PruneConfigFile`) → `extra="forbid"`; read-back (`PruneResult`, `PruneDecision`, `PruneEvent`) → `extra="ignore"`.

## API alignment with adjacent stages

`load_prune_config(project_dir, path=None) -> PruneConfig` matches `load_safety_config` / `load_draft_config`. Default `audit_path` resolves relative to `project_dir` (`<project_dir>/.signalforge/prune.jsonl`). Orchestrator entry: `prune_tests(model, adapter, candidates, manifest, *, config=None, audit_path=None)` — keyword-only optionals; model-and-adapter front-paired; mirrors `draft_schema`. Match this precedent for any future stage entry.

## `signalforge.yml` top-level namespace: `prune:`

The prune-stage block is `{ prune: { enabled, scope, sample_size, sample_strategy, test_timeout_seconds, total_budget_seconds, capture_failure_rows, trusted_models, partition_filter, min_kept_rate_warn } }`. `PruneConfig` uses `extra="forbid"`; the wrapping `_PruneConfigFile` uses `extra="ignore"` at the top level. Mirrors the same pattern across all pipeline-stage configs.

## `trusted_models` validated at orchestrator entry, not at config load

`PruneConfig.trusted_models: tuple[str, ...]` is validated at `prune_tests(...)` entry, not config load (manifest isn't loaded yet). Every entry must appear in `manifest.models` or `PruneTrustedModelNotFoundError` raises BEFORE any warehouse call. Silent no-op (typo'd id doesn't match anything) would leak "failed-on-known-clean-data" tests through as `kept` — exactly the failure mode this fails-loud against.

## 5-surface parity for v0.x → v0.(x+1) graduations

When graduating a reserved surface (in this file or any rule file's "reserved" / "schema-version surfaces" block) from forward-compat-only to behaviour-active, update **five surfaces in the same commit** — the non-CLI analogue of `cli-layer.md`'s 5-surface flag-parity rule:

1. **Rule file** — promote from "reserved" to "active" wording, retaining the historical DEC pointer.
2. **Ops doc** — `docs/<stage>-ops.md`. The contract surface external tooling keys on.
3. **CLAUDE.md public-API surface** — top-level orientation.
4. **Test** — pin the active behaviour, not just the reserved type signature.
5. **DEC in `plans/super/<n>-<topic>.md`** — the ADR-style record of why the graduation happened.

Surfaces 2 and 3 are the ones most often forgotten because they sit furthest from the code.

## Config fields and invariants

### Materialised sample

- **`PruneConfig.sample_strategy: Literal["oneshot", "materialised"] = "materialised"`.** `extra="forbid"` field; `materialised` is the default. Non-BQ adapters opt out via `prune.sample_strategy: oneshot`.
- **Two `WarehouseError` subclasses, both CLI tier 3.** `MaterialisationFailedError` wraps SDK/network/quota failures via `cause` kwarg. `MaterialisationNotSupportedError` is the ABC default-impl raise for non-BQ adapters. Both ship `default_remediation`; the no-support remediation is locked verbatim: `"Set 'prune.sample_strategy: oneshot' in signalforge.yml to fall back to per-test sampling, or wait for v0.3 multi-warehouse materialisation support."`
- **`_SESSION._sf_sample_<run_id>` is the audit signal in `compiled_sql`.** `run_id = blake2b(table.qualified_name + signalforge_version + str(n) + canonical_json(partition_filter), digest_size=8).hexdigest()` (16 hex chars, NUL-separator). Same input → same `run_id` → byte-equal `compiled_sql`. Returned `TableRef` carries `project=None, dataset="_SESSION", name="_sf_sample_<run_id>"` — `project=None` is load-bearing because BigQuery rejects the three-part form even inside the owning session.
- **`ttl_seconds=3600` is OUR-side cleanup-WARNING hint, NOT a BQ knob.** BigQuery enforces session max lifetime (~24h) server-side; the param drives WARNING text only.
- **`prune_tests` owns the `with adapter:` block.** Callers MUST pass an adapter that has not been entered — double `__enter__` is undefined. Without `__exit__`, materialised sessions rely on BQ's ~24h server-side timeout instead of explicit `BQ.ABORT_SESSION()`.
- **Total-budget includes materialisation.** No separate `materialisation_timeout_seconds` knob. Budget exhaustion mid-materialisation uses the existing `"total prune budget exceeded before evaluation"` `why` text.

### Operator-chosen prune disable

- **`PruneConfig.enabled: bool = True`.** `extra="forbid"` field; default preserves all prior behaviour. Operators opt in to the short-circuit via `prune.enabled: false`.
- **Short-circuit position.** `prune_tests` branches AFTER audit-path symlink-hardening + `config_hash` computation, BEFORE `_validate_trusted_models` / `TableRef.from_model` / `with adapter:`. Audit-path stays upstream so the disabled path benefits from the symlink defence; trusted-models/manifest-shape gates are bypassed (operator who disabled prune shouldn't need a valid `trusted_models`).
- **`why` text is locked verbatim:** `"prune disabled in signalforge.yml"`. Pinned by a stability test in `tests/prune/test_engine.py`.
- **CLI INFO emission at prune-stage entry.** `cmd_generate` emits one `_LOGGER.info` line AFTER override block and BEFORE `prune_tests` invocation. INFO (not WARNING) because the operator explicitly opted in. **`--quiet` DOES suppress this INFO** (unlike the cleanup-failure WARNING from `warehouse-adapters.md`, which must always surface).

### Kept-rate WARNING

- **`PruneConfig.min_kept_rate_warn: float = 0.0`.** `extra="forbid"` field; default preserves the silent posture (fires only when every candidate dropped). Range-bound `[0.0, 1.0]` via `field_validator`. Operators raise (e.g. `0.10`) to catch "fewer than 10% kept"; `1.0` always warns. The WARNING is informational — the run still returns a `PruneResult`; no exit-code path.
- **Single helper called at every `prune_tests` return site.** `_maybe_emit_kept_rate_warning` fires at all three return sites (disabled short-circuit, materialisation-failed branch, main happy path) so a future return-site addition can't silently drop the signal. Skips emission when `total == 0` (degenerate; would also `ZeroDivisionError`).
- **Doc framing — Architectural Commitment #1.** `docs/prune-ops.md` § Expected drop rates documents "a high drop rate is the working state, not the failure state" with reference numbers from the Austin bikeshare fixture. Without this prose, the load-bearing prune-and-drop behaviour reads as a defect on first contact.

### Empty-candidate short-circuit

`prune_tests` returns an empty `PruneResult` (zero decisions) BEFORE `_validate_trusted_models` / `TableRef.from_model` / `with adapter:` when `_iter_candidate_tests(candidates)` yields nothing. Without it, the default `materialised` + `sample` path would issue a real `CREATE TEMP TABLE ... AS SELECT` to sample for ZERO tests — warehouse cost for no signal. Reachable from `prune-existing` when every test in an external `schema.yml` is skip-recorded by the ingest layer. Placement mirrors the disabled short-circuit's no-warehouse-contact posture (after audit-path hardening + `config_hash`, before any warehouse call); the fail-closed audit invariant holds trivially (zero candidates → zero `PruneEvent`s). Guarded by `if not pairs:` so non-empty candidates are byte-identical. Pinned by `tests/prune/test_engine.py::test_prune_tests_empty_candidate_skips_warehouse_on_materialised_sample`.

### Normalised hash recipe

Every reproducibility hash in the audit/sidecar corpus uses `blake2b(canonical_json.encode("utf-8"), digest_size=8).hexdigest()` over canonical JSON. New writers reach for this recipe directly — don't introduce a new family or revive `SHA-256`. `blake2b-8` matches `SHA-256[:16]`'s collision profile for this use case (it answers "did two runs use the same canonicalised config?", not a security-grade integrity check).

- **`PruneEvent.audit_schema_version: Literal[2] = 2`** in production; the `prune.jsonl` fixture and drift detector track it in lockstep. Consumers correlating `config_hash` across audit JSONLs must gate on `audit_schema_version >= 2`.

### Snowflake compiler dialect

The compiler emits valid Snowflake SQL purely from `SNOWFLAKE_DIALECT` — see § "Compiler is dialect-driven" for the field list. Three load-bearing points:

- **`identifier_case` is load-bearing, not declared-only.** Folding to UPPER then quoting (`"CUSTOMER_ID"`) is what makes Snowflake SQL resolve against conventional unquoted-DDL tables. **Residual:** a table genuinely created with quoted-lowercase DDL breaks under this default — acceptable; the conventional majority is the right default.
- **`HASH()` reproducibility caveat.** BigQuery's `FARM_FINGERPRINT` is cross-time stable; Snowflake's `HASH()` is deterministic only *within a Snowflake release*. Sufficient for within-run prune determinism; documented in `docs/prune-ops.md`.
- **Validation tiers.** Byte-exact Snowflake snapshot fixtures (`tests/fixtures/prune/compiled_sql/snowflake/`) are the authoritative shape gate. A gated `@pytest.mark.snowflake` suite (`tests/prune/test_compiler_fakesnow.py`, run `uv run pytest -m snowflake --no-cov`) executes the four built-ins through `fakesnow` (rule-semantic assertions, never `HASH()` value-equality) AND parses every fixture through `sqlglot`'s Snowflake dialect. Keep a parser/executor in the loop for any new dialect — the sqlglot parse-guard caught the `sample` reserved-word bug.

## Reference

`plans/super/{6,22,35,51,55,121}-*.md` — design DECs. `src/signalforge/prune/` — implementation. `docs/prune-ops.md` — operational reference. Key tests: `tests/prune/test_drift_detector.py` (schema-drift gate), `tests/prune/test_compiler_import_guard.py` (SDK-import confinement), `tests/prune/test_compiler_fakesnow.py` (gated fakesnow/sqlglot validation), `tests/test_audit_completeness.py` (AST scans), `tests/llm/test_logger_grep_gate.py` (logger gate), `tests/fixtures/prune/prune_event_v1.jsonl` (audit fixture).
