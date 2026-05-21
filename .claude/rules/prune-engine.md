# Prune layer (decision routing + fail-closed audit)

Established by issue #6 (test prune engine). Apply to every module under `signalforge.prune` and to any new code that compiles a candidate test to SQL, runs it against the warehouse, or writes a prune-decision audit record.

The prune layer sits between the LLM-drafting pipeline (#5) and the quality grader (#7). It enforces SignalForge's load-bearing "signal over volume" commitment: a candidate test that always passes on warehouse samples must be dropped, not shipped. Equally, a candidate test we cannot evaluate must be kept — pruning is allowed to drop only with positive evidence.

## Conservative drop-reason taxonomy (DEC-006, DEC-011)

`DropReason` is a `Literal[...]` of exactly five values:

- `"always-passes"` — test ran, returned zero failing rows on a representative sample, scope was sufficient. Drop.
- `"failed-on-known-clean-data"` — test ran against a `trusted_models` opt-in target and returned non-zero failing rows. The model is known clean; the test is wrong. Drop.
- `"requires-future-data"` — test compiled to a sentinel (e.g. `relationships(to: <unknown>)` where the target model is absent from the manifest). Cannot evaluate; the operator revisits when the dependency lands. Dropped (no warehouse call; the structured reason carries the diagnostic).
- `"kept"` — test ran and returned non-zero failing rows on an untrusted model. Real signal. Kept.
- `"kept-without-evidence"` — could not positively evaluate (budget elapsed, SQL safety rejected the identifier, warehouse call raised, prune disabled, materialisation failed). Kept.

The decision matrix lives in `engine.py::_decide_from_test_result`. **Kept-without-evidence routes to `decision="kept"`, not `decision="dropped"`** — SignalForge ships tests it cannot evaluate; the LLM proposed them, and absent contradicting warehouse evidence the operator gets to make the call.

If you add a sixth `DropReason` literal in v0.2, update production `DropReason` AND `StrictPruneDecision` AND the fixture at `tests/fixtures/prune/prune_event_v1.jsonl` AND the decision-matrix table in `docs/prune-ops.md` in the same change.

## Conservative-bias routing template (generalised across #22 / #35 / #51 / #55)

When a stage cannot positively evaluate one or more candidates, route to the existing `kept-without-evidence` literal — **never expand the enum**. The 5-value `DropReason` literal stays locked. The diagnostic travels in the `why` field; the source travels in the log signal.

Four established sources, same routing, same audit invariant:

| Source | Scope | Log signal | `why` shape |
|--|--|--|--|
| Per-test warehouse error (DEC-006 of #6) | one test | none extra | `f"<operation> failed: {type(exc).__name__}: {str(exc)[:200]}"` |
| Per-test SQL safety reject (DEC-024) | one test | none extra | `"identifier rejected by SQL safety check"` |
| Total budget exhausted (DEC-011 of #6) | every remaining | one stderr WARNING | `"total prune budget exceeded before evaluation"` |
| Pre-loop warehouse failure (DEC-009 of #22 — materialisation) | every candidate | one stderr WARNING | `f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"` |
| Operator disable (#35) | every candidate | one stderr INFO | `"prune disabled in signalforge.yml"` |

All sources preserve the fail-closed audit invariant — one `PruneEvent` per candidate via `_write_audit_or_abort` even when the run did no warehouse work. The WARNING-vs-INFO split is the diagnostic question: "did the world get in our way (WARNING)" vs. "did the operator choose this (INFO)."

When a v0.3 source lands (`--no-prune` flag, `--max-cost` budget, circuit-breaker), copy this template verbatim. Don't invent a sixth `DropReason`.

## Fail-closed audit (DEC-016, mirrors safety/draft)

`signalforge.prune.audit.write_prune_event` is the project's third fail-closed JSONL writer. Identical contract to safety / draft:

1. **Propagation IS the defence.** Open with `O_APPEND | O_CREAT | 0o600`, single write (looped on short returns), `os.fsync`, close. No try/except internally — caller (`prune_tests`) wraps as `PruneAuditWriteError(cause=...)`.
2. **Size cap before any file open.** `_PRUNE_AUDIT_RECORD_LIMIT_BYTES = 4000`; oversize raises `PruneAuditRecordTooLargeError(size, limit)` BEFORE `os.open`. Propagates as-is.
3. **Per-decision write happens after each test, not in a final batch.** A run that crashes mid-iteration leaves one durable record per evaluated test. Buffering "to amortise fsync" defeats the audit guarantee.

## Symlink-hardened audit path (post-QG fix)

`signalforge.warehouse._path_safety.canonicalise_path` is the project's standard symlink/containment gate; route the writer's audit path through it at entry. Raise `PruneAuditWriteError` on containment failure. Don't trust default paths because they're "ours" — the reader's three traps from `manifest-readers.md` apply to writers too.

## Identifier shape validation at the compile seam (DEC-024)

LLM-supplied `test.column` / `test.field` strings land in compiled SQL as backtick-quoted identifiers. The drafter's anchor-contract validator checks set-membership against `Manifest.columns` but does NOT enforce regex shape — a manifest column named `foo bar` or `users\`; DROP TABLE` passes the anchor check.

`signalforge.prune.compiler` calls `signalforge.warehouse._sql_safety.validate_identifier` on every identifier before quoting (`accepted_values.values` go through `escape_bq_string_literal`). Failures return `_InvalidIdentifier` which routes to `kept-without-evidence` (`why="identifier rejected by SQL safety check"`). Defence in depth — same principle as the warehouse adapter's DEC-013.

## Compiler is dialect-driven, not BigQuery-specific (DEC-025)

`_compile_test(test, table_ref, dialect: Dialect, manifest)` reads `dialect.quote_char`, `dialect.identifier_case`, and `dialect.supports_qualify` from the `Dialect` value object returned by `WarehouseAdapter.dialect()`. **No `from google.cloud import bigquery` anywhere in `signalforge/prune/`.** v0.2 Snowflake/Postgres adapters return their own `Dialect`. The `relationships` parent join uses `TableRef.from_model` for the same reason (DEC-026).

NULL-exclusion pattern matches dbt-core verbatim (DEC-023): `unique` adds `WHERE col IS NOT NULL`; `accepted_values` adds `WHERE col IS NOT NULL AND col NOT IN (...)`; `relationships` adds child-side `WHERE child.col IS NOT NULL`. Snapshot fixtures pin the exact SQL bytes.

## Total-budget semantics (DEC-011)

When `total_budget_seconds` is exceeded, the orchestrator:

1. Best-effort cancels the in-flight test (`query_job.cancel()` on BigQuery; ABC-level no-op fallback).
2. Marks every remaining un-started test as `kept-without-evidence` per the routing template above.
3. Emits a final WARNING with the count of un-started tests (lazy-format JSON).

No partial-evaluation results — a test running when the budget tripped is `kept-without-evidence`, not `kept` (failing-rows count is unknown). The single-threaded sequential model (DEC-028) makes this enforceable.

`signalforge.prune.engine` declares `_sleep = time.sleep` at module scope per DEC-019 for test-time reassignment (mirrors `llm-drafter.md` DEC-004; don't monkey-patch globally).

## AST scan extension per new audit type (DEC-018)

Issue #6 extended `tests/test_audit_completeness.py` with a fifth scan: `Call(func=Name(id="PruneEvent"))` is permitted only in `src/signalforge/prune/audit.py`. Sanity check that ≥1 construction exists in the blessed module guards against rename-without-update. Every new audit-event type gets its own scan; if a new module legitimately needs to construct a gated event-type, update the exclusion list AND document the new write seam. Don't suppress the test.

## ANSI-safe lazy-format JSON logger + grep gate (DEC-017)

Same rule as the other layers (`safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011). The grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm,draft,prune,grade,diff,cli}` (6 dirs as of #9) and rejects any `_LOGGER\.\w+\(f"` hit.

## Custom `__repr__` on result-shaped models (DEC-022)

Pydantic v2's default `__repr__` emits every field. `PruneResult.__repr__` shows only `model_unique_id`, `kept_count`, `dropped_count`, `elapsed_ms`. `PruneDecision.__repr__` shows only `test.test_type`, `test.column` (or `"<model-level>"`), `decision`, `reason`. Compiled SQL and sample failures stay accessible via field access; they don't slip out via casual debug-print.

User-supplied strings in error messages render via `repr()` (`_format_value` helper) — same rule. A model unique_id containing `\x1b[31m` quoted via `repr()` shows as `'\\x1b[31m...'`; raw interpolation would inject.

## Drift detectors are mandatory for read-back models (DEC-010)

Every `extra="ignore"` production model — `PruneResult`, `PruneDecision`, `PruneEvent` — pairs with a `Strict<Model>(extra="forbid")` detector in `tests/prune/test_drift_detector.py`, validated against committed fixtures (`tests/fixtures/prune/prune_event_v1.jsonl` for the audit type). Adding a field to production without updating the strict mirror OR the fixture breaks the test loudly.

`extra=` placement convention from `safety-layer.md` DEC-015 applies: config-shaped (`PruneConfig`, `_PruneConfigFile`) → `extra="forbid"`; read-back (`PruneResult`, `PruneDecision`, `PruneEvent`) → `extra="ignore"`.

## API alignment with adjacent stages (post-QG fix)

`load_prune_config(project_dir, path=None) -> PruneConfig` matches `load_safety_config` / `load_draft_config`. Default `audit_path` resolves relative to `project_dir` (`<project_dir>/.signalforge/prune.jsonl`). Orchestrator entry: `prune_tests(model, adapter, candidates, manifest, *, config=None, audit_path=None)` — keyword-only optionals; model-and-adapter front-paired; mirrors `draft_schema` from `llm-drafter.md`. Match this precedent for any future stage entry.

## `signalforge.yml` top-level namespace: `prune:` (DEC-020)

The prune-stage block is `{ prune: { enabled, scope, sample_size, sample_strategy, test_timeout_seconds, total_budget_seconds, capture_failure_rows, trusted_models, partition_filter, min_kept_rate_warn } }`. `PruneConfig` uses `extra="forbid"`; the wrapping `_PruneConfigFile` uses `extra="ignore"` at the top level. Mirrors `llm-drafter.md` DEC-027 / `safety-layer.md` DEC-025 verbatim.

## `trusted_models` validated at orchestrator entry, not at config load (DEC-008)

`PruneConfig.trusted_models: tuple[str, ...]` is validated at `prune_tests(...)` entry, not config load (manifest isn't loaded yet). Every entry must appear in `manifest.models` or `PruneTrustedModelNotFoundError` raises BEFORE any warehouse call. Silent no-op (typo'd id doesn't match anything) would leak "failed-on-known-clean-data" tests through as `kept` — exactly the failure mode this fails-loud against.

## 5-surface parity for v0.x → v0.(x+1) graduations

When graduating a reserved surface (in this file or any rule file's "reserved" / "schema-version surfaces" block) from forward-compat-only to behaviour-active, update **five surfaces in the same commit** — the non-CLI analogue of `cli-layer.md`'s 5-surface flag-parity rule:

1. **Rule file** — promote from "reserved" to "active" wording, retaining the historical DEC pointer.
2. **Ops doc** — `docs/<stage>-ops.md`. The contract surface external tooling keys on.
3. **CLAUDE.md public-API surface** — top-level orientation.
4. **Test** — pin the active behaviour, not just the reserved type signature.
5. **DEC in `plans/super/<n>-<topic>.md`** — the ADR-style record of why the graduation happened.

Surfaces 2 and 3 are the ones most often forgotten because they sit furthest from the code.

## v0.2 additions

### Materialised sample (issue #22)

- **`PruneConfig.sample_strategy: Literal["oneshot", "materialised"] = "materialised"`** (DEC-007 of #22). New `extra="forbid"` field; `materialised` is the v0.2 default. Non-BQ adapters opt out via `prune.sample_strategy: oneshot`.
- **Two new `WarehouseError` subclasses, both CLI tier 3** (DEC-008 of #22). `MaterialisationFailedError` wraps SDK/network/quota failures via `cause` kwarg. `MaterialisationNotSupportedError` is the ABC default-impl raise for non-BQ adapters. Both ship `default_remediation`; the no-support remediation is locked verbatim (DEC-006 of #22): `"Set 'prune.sample_strategy: oneshot' in signalforge.yml to fall back to per-test sampling, or wait for v0.3 multi-warehouse materialisation support."`
- **`_SESSION._sf_sample_<run_id>` is the audit signal in `compiled_sql`** (DEC-001 of #22). `run_id = blake2b(table.qualified_name + signalforge_version + str(n) + canonical_json(partition_filter), digest_size=8).hexdigest()` (16 hex chars, NUL-separator). Same input → same `run_id` → byte-equal `compiled_sql` across runs. Returned `TableRef` carries `project=None, dataset="_SESSION", name="_sf_sample_<run_id>"` — `project=None` is load-bearing because BigQuery rejects the three-part form even inside the owning session.
- **`ttl_seconds=3600` is OUR-side cleanup-WARNING hint, NOT a BQ knob** (DEC-013 of #22). BigQuery sessions have server-enforced max lifetime (~24h); the param drives the WARNING text only.
- **`prune_tests` owns the `with adapter:` block** (DEC-013 of #22). Callers MUST pass an adapter that has not been entered — double `__enter__` is undefined. Without `__exit__`, materialised sessions rely on BQ's ~24h server-side timeout instead of explicit `BQ.ABORT_SESSION()`.
- **Total-budget includes materialisation** (DEC-010 of #22). No separate `materialisation_timeout_seconds` knob in v0.2. Budget exhaustion mid-materialisation uses the existing `"total prune budget exceeded before evaluation"` `why` text.

### Operator-chosen prune disable (issue #35)

- **`PruneConfig.enabled: bool = True`** (DEC-005 of #35). New `extra="forbid"` field; default preserves all v0.1 behaviour. Operators opt in to the short-circuit via `prune.enabled: false`.
- **Short-circuit position** (DEC-002 of #35). `prune_tests` branches AFTER audit-path symlink-hardening + `config_hash` computation, BEFORE `_validate_trusted_models` / `TableRef.from_model` / `with adapter:`. Audit-path stays upstream so the disabled path benefits from the symlink defence; trusted-models/manifest-shape gates are bypassed (operator who disabled prune shouldn't need a valid `trusted_models`).
- **`why` text is locked verbatim** (DEC-003 of #35): `"prune disabled in signalforge.yml"`. Pinned by a stability test in `tests/prune/test_engine.py`.
- **CLI INFO emission at prune-stage entry** (DEC-004 of #35). `cmd_generate` emits one `_LOGGER.info` line AFTER override block and BEFORE `prune_tests` invocation. INFO (not WARNING) because the operator explicitly opted in — surfacing a WARNING would nag. **`--quiet` DOES suppress this INFO** (unlike the cleanup-failure WARNING from `warehouse-adapters.md`, which must always surface).

### Kept-rate WARNING (issue #51)

- **`PruneConfig.min_kept_rate_warn: float = 0.0`** (issue #51). New `extra="forbid"` field; default preserves the silent posture (fires only when every candidate dropped). Range-bound `[0.0, 1.0]` via `field_validator`. Operators raise (e.g. `0.10`) to catch "fewer than 10% kept"; `1.0` always warns. The WARNING is informational — the run still returns a `PruneResult`; no exit-code path.
- **Single helper called at every `prune_tests` return site.** `_maybe_emit_kept_rate_warning` fires at all three return sites (disabled short-circuit, materialisation-failed branch, main happy path) so a future return-site addition can't silently drop the signal. Skips emission when `total == 0` (degenerate; would also `ZeroDivisionError`).
- **Doc framing — Architectural Commitment #1.** `docs/prune-ops.md` § Expected drop rates documents "a high drop rate is the working state, not the failure state" with reference numbers from the Austin bikeshare fixture. Without this prose, the load-bearing prune-and-drop behaviour reads as a defect on first contact.

### Empty-candidate short-circuit (issue #105)

`prune_tests` returns an empty `PruneResult` (zero decisions) BEFORE `_validate_trusted_models` / `TableRef.from_model` / `with adapter:` when `_iter_candidate_tests(candidates)` yields nothing. Without it, the default `materialised` + `sample` path would issue a real `CREATE TEMP TABLE ... AS SELECT` (`adapter.materialise_sample`) to sample for ZERO tests — warehouse cost for no signal. The all-empty case is reachable from issue #105's `prune-existing` when every test in an external `schema.yml` is skip-recorded by the ingest layer. Placement mirrors the disabled short-circuit's no-warehouse-contact posture (after audit-path hardening + `config_hash`, before any warehouse call); the fail-closed audit invariant holds trivially (zero candidates → zero `PruneEvent`s). Guarded by `if not pairs:` so non-empty candidates are byte-identical. Pinned by `tests/prune/test_engine.py::test_prune_tests_empty_candidate_skips_warehouse_on_materialised_sample` (fake adapter with no queued expectations → any warehouse call raises).

### Normalised hash recipe (issue #55)

Issue #55 migrated every reproducibility hash in the audit/sidecar corpus to `blake2b(digest_size=8)` over canonical JSON (the recipe already used by draft / grade / diff). Pre-#55 outliers were `signalforge.prune.audit._compute_config_hash` and `signalforge.safety.policy._compute_policy_hash` (both `SHA-256[:16]`); both migrated in lockstep.

- **`_PRUNE_AUDIT_SCHEMA_VERSION` bumped 1 → 2.** `PruneEvent.audit_schema_version: Literal[2] = 2` in production; v0.1 `prune.jsonl` fixture and drift detector refreshed in lockstep. Consumers correlating `config_hash` across audit JSONLs must gate on `audit_schema_version >= 2`.
- **One recipe across the corpus.** `blake2b-8` has the same collision profile as `SHA-256[:16]` for this use case (this is "did two runs use the same canonicalised config?", not a security-grade integrity check). The choice is consistency, not strength. New writers in v0.3 reach for `blake2b(canonical_json.encode("utf-8"), digest_size=8).hexdigest()` directly — don't introduce a new family or revive `SHA-256`.

## Reference

`plans/super/6-prune-engine.md` — DEC-001 … DEC-028. `plans/super/22-temp-table-sample.md` — v0.2 materialised-sample additions. `plans/super/35-prune-enabled-doc-reframe.md` — operator-disable additions. `plans/super/51-kept-rate-warn-doc.md` — kept-rate WARNING + drop-rate doc. `plans/super/55-normalise-hash-recipe.md` — hash recipe normalisation. `src/signalforge/prune/` — current implementation. `docs/prune-ops.md` — operational reference. `tests/prune/test_drift_detector.py` — schema-drift gate. `tests/test_audit_completeness.py` — AST-scan suite. `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate. `tests/fixtures/prune/prune_event_v1.jsonl` — committed audit fixture.
