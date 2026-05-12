# Prune layer (decision routing + fail-closed audit)

Established by issue #6 (test prune engine). Apply to every module under `signalforge.prune` and to any new code that compiles a candidate test to SQL, runs it against the warehouse, or writes a prune-decision audit record.

The prune layer sits between the LLM-drafting pipeline (#5) and the quality grader (#7). It enforces SignalForge's load-bearing "signal over volume" commitment: a candidate test that always passes on warehouse samples must be dropped, not shipped. Equally, a candidate test we cannot evaluate must be kept — pruning is allowed to drop only with positive evidence.

## Conservative drop-reason taxonomy (DEC-006, DEC-011)

`DropReason` is a `Literal[...]` of exactly five values:

- `"always-passes"` — test ran, returned zero failing rows on a representative sample, scope was sufficient. Drop.
- `"failed-on-known-clean-data"` — test ran against a `trusted_models` opt-in target and returned non-zero failing rows. The model is known clean; the test is wrong. Drop.
- `"requires-future-data"` — test compiled to a sentinel (e.g. `relationships(to: <unknown>)` where the target model is absent from the manifest). Cannot evaluate; the operator revisits when the dependency lands. Dropped (no warehouse call issued; the structured drop reason carries the diagnostic that surfaces in the diff).
- `"kept"` — test ran and returned non-zero failing rows on an untrusted model. Real signal. Kept.
- `"kept-without-evidence"` — total budget elapsed before this test ran, the test SQL was rejected by `validate_identifier`, the warehouse call raised, or any other "cannot positively evaluate" outcome. Kept.

The decision matrix lives in `engine.py::_decide_from_test_result` (the routing helper the orchestrator dispatches to once a `TestResult` lands). The load-bearing invariant: **kept-without-evidence routes to `decision="kept"`, not `decision="dropped"`.** SignalForge ships tests it cannot evaluate; the LLM proposed them, and absent contradicting warehouse evidence the operator gets to make the call. Dropping a test silently because we couldn't reach the warehouse is exactly the failure mode this commitment exists to prevent.

If you add a sixth `DropReason` literal in v0.2 (e.g. `"sample-too-small"`), update production `DropReason` AND `StrictPruneDecision` (the drift detector) AND the fixture at `tests/fixtures/prune/prune_event_v1.jsonl` AND the decision-matrix table in `docs/prune-ops.md` in the same change.

**Conservative-bias routing across `WarehouseError` subclasses.** When an orchestrator-entry warehouse exception fires — any `WarehouseError` subclass, including ones added by future adapters (`SnowflakeTransactionError`, `PostgresConnectionPoolDrainedError`, `DatabricksClusterUnavailableError`) — the WHOLE candidate set routes to `kept-without-evidence`, NOT just the test that happened to be running when the exception surfaced. The 5-value `DropReason` literal stays locked across these expansions; the diagnostic travels in the `why` field as `f"<operation> failed: {type(exc).__name__}: {str(exc)[:200]}"`. The orchestrator emits a single stderr WARNING at the head of the failure path so the operator gets a one-line out-of-band signal that the run was degraded — otherwise the only signal is N identical `why` fields buried in the diff. Generalises DEC-009 of #22 (materialisation failure) for any new pre-loop warehouse step that might land in v0.3 (cluster spin-up, schema introspection, transaction begin); apply the same routing without inventing a sixth `DropReason`.

**Three sources of `kept-without-evidence`; same routing, same audit invariant.** By v0.2 the rule has three established paths that route every candidate (or every remaining candidate) to `kept-without-evidence`:

1. **"We couldn't evaluate it" — per-test warehouse error.** A single `adapter.run_test_sql` call raises `WarehouseError`; only that test routes (`engine.py::_decide_kept_without_evidence_warehouse_error`). DEC-006 of #6.
2. **"We couldn't evaluate ANY of them" — budget exhaustion or pre-loop warehouse failure.** Total budget tripped (DEC-011 of #6) or `materialise_sample` raised (DEC-009 of #22) → every remaining (or every) candidate routes; one stderr WARNING fires at the head of the path. The 5-value `DropReason` stays locked; the typed-class prefix in `why` distinguishes the cause.
3. **"We chose not to evaluate ANY of them" — operator disable.** `PruneConfig.enabled=false` short-circuits the orchestrator (#35) → every candidate routes; one stderr INFO fires (not WARNING — operator explicitly opted in via config, so the signal is confirmation, not alarm).

All three share the load-bearing contract: route to the existing `kept-without-evidence` literal (don't expand the enum), preserve the fail-closed audit invariant (one `PruneEvent` per candidate via `_write_audit_or_abort`, even when the run did no warehouse work), emit a single sibling-stage log line at the short-circuit entry (typed-class prefix in the JSON payload distinguishes the cause). The split between WARNING and INFO is the diagnostic question: "did the operator choose this, or did the world get in our way?" — choose-this is INFO; got-in-our-way is WARNING.

When a v0.3 stage introduces a fourth source (e.g., a `--no-prune` CLI flag, a `--max-cost` budget gate, a circuit-breaker that opens after N consecutive prune-runs hit warehouse errors), apply the same three-part contract verbatim. The conservative-bias routing template from #22 (materialisation failure) and #35 (operator disable) makes this a paved path — reach for it before inventing a sixth `DropReason`. Cross-reference `plans/super/22-temp-table-sample.md` (DEC-009) and `plans/super/35-prune-enabled-doc-reframe.md` (DEC-001/DEC-007) in the new section so the precedent is explicit at the point a future maintainer reads it.

## Fail-closed audit (DEC-016, mirrors safety/draft)

`signalforge.prune.audit.write_prune_event` is the project's third fail-closed JSONL writer (after `signalforge.safety.audit.write` and `signalforge.draft.audit.write_response_event`). The contract is identical to those — load-bearing rules:

1. **Propagation IS the defence.** Open with `O_APPEND | O_CREAT | 0o600`, write one JSONL line, `os.fsync`, close. Catches **no** exceptions internally — `OSError`, `PermissionError`, encoding failures all propagate. The caller (`prune_tests`) wraps as `PruneAuditWriteError(cause=...)`. **Don't** add try/except inside the writer; the propagation is the contract.

2. **Size cap before any file open.** `_PRUNE_AUDIT_RECORD_LIMIT_BYTES = 4000` is checked before the `os.open`, so an oversize record leaves no on-disk artefact. Raises `PruneAuditRecordTooLargeError(size, limit)` which the orchestrator propagates as-is rather than re-wrapping.

3. **Per-decision write happens after each test, not in a final batch.** A `prune_tests` run that crashes mid-iteration leaves the prune.jsonl with one durable record per evaluated test up to the failure point. Buffering writes "to amortise fsync" defeats the audit guarantee.

An unaudited prune decision is, by definition, a `kept`/`dropped` verdict whose justification didn't durably hit disk — exactly the failure mode this layer exists to prevent. The fail-closed pattern is now established in three layers; treat it as the project default for any future audit-write seam.

## Symlink-hardened audit path (DEC-016, post-QG fix)

`signalforge.warehouse._path_safety.canonicalise_path` is the project's standard symlink/containment gate. Apply it to every user-supplied or default-derived path that the writer opens.

The original prune-audit implementation opened `<project>/.signalforge/prune.jsonl` directly without canonicalising. The QG review caught it: a symlinked `.signalforge/prune.jsonl -> /etc/passwd` (admittedly hostile, but the reader's path safety enforces this gate uniformly elsewhere) would have written outside the project tree. The fix routes the audit path through `canonicalise_path` at writer entry, raising `PruneAuditWriteError` on containment failure.

When introducing a fourth audit-write seam — grader (#7), diff renderer (#8), or beyond — apply `canonicalise_path` at writer entry. Don't trust the default path because it's "ours"; the reader's three traps from `manifest-readers.md` apply equally to writers.

## Identifier shape validation at the compile seam (DEC-024 + post-QG fix)

LLM-supplied `test.column` / `test.field` strings land in compiled SQL as backtick-quoted identifiers. The drafter's anchor-contract validator (`llm-drafter.md` DEC-003) checks set-membership against `Manifest.columns` but does NOT enforce regex shape — a manifest column named `foo bar` (whitespace) or `users\`; DROP TABLE` (backtick injection) passes the anchor check.

The compile seam is the layer's last line of defence. `signalforge.prune.compiler` calls `signalforge.warehouse._sql_safety.validate_identifier` on every identifier (`test.column`, `test.field`; `accepted_values.values` go through `escape_bq_string_literal` per DEC-024) **before** quoting. Failures return a sentinel `_InvalidIdentifier` which the engine routes to `kept-without-evidence` (`why="identifier rejected by SQL safety check"`).

Defence in depth — same principle as the warehouse adapter's DEC-013 (every public-API identifier validated at construction time). Don't skip the compile-seam validation on the assumption that the manifest is trusted; the manifest reader uses `extra="ignore"` for forward-compat and was not designed to enforce identifier shape on user-authored YAML.

## Compiler is dialect-driven, not BigQuery-specific (DEC-025)

`_compile_test(test, table_ref, dialect: Dialect, manifest)` reads `dialect.quote_char`, `dialect.identifier_case`, and `dialect.supports_qualify` from the `Dialect` value object returned by `WarehouseAdapter.dialect()`. **No `from google.cloud import bigquery` anywhere in `signalforge/prune/`.** v0.2 Snowflake/Postgres adapters return their own `Dialect` and the compiler routes accordingly without modification.

Mirrors the architectural commitment from `warehouse-adapters.md`: keep BigQuery-isms behind the adapter seam from day one. The `relationships` parent join uses `TableRef.from_model` for the same reason (DEC-026) — the table-reference shape is dialect-neutral.

The NULL-exclusion pattern in compiled SQL matches dbt-core verbatim (DEC-023): `unique` adds `WHERE col IS NOT NULL`; `accepted_values` adds `WHERE col IS NOT NULL AND col NOT IN (...)`; `relationships` adds child-side `WHERE child.col IS NOT NULL`. Snapshot fixtures pin the exact SQL bytes; divergence from dbt-core verdicts is a regression even if it appears semantically equivalent.

## Total-budget semantics (DEC-011)

When `total_budget_seconds` is exceeded, the orchestrator:

1. Best-effort cancels the in-flight test (`query_job.cancel()` on BigQuery; ABC-level no-op fallback for adapters that don't expose cancel).
2. Marks every remaining un-started test as `kept-without-evidence` with `why="total prune budget exceeded before evaluation"`.
3. Emits a final WARNING with the count of un-started tests (`json.dumps`-formatted).

No partial-evaluation results — a test that was running when the budget tripped is `kept-without-evidence`, not `kept` (the failing-rows count is unknown). The single-threaded sequential model (DEC-028) makes this enforceable; v0.2 concurrency will need a different cancellation contract.

`signalforge.prune.engine` declares `_sleep = time.sleep` at module scope per DEC-019 so the budget-watchdog tests reassign it to a deterministic stand-in. Mirrors `llm-drafter.md` DEC-004; same rationale (don't monkey-patch `time.sleep` globally).

## Single AST scan extension per new audit type (DEC-018)

`tests/test_audit_completeness.py` was the unified AST-scan suite as of issue #5; issue #6 extends it with a fifth scan: `Call(func=Name(id="PruneEvent"))` is permitted only in `src/signalforge/prune/audit.py`. The sanity check that *at least one* construction exists in the blessed module guards against accidental rename-without-update.

The pattern: whenever a new audit-event type is added — whether for prune, grade (#7), diff (#8), or beyond — extend `tests/test_audit_completeness.py` with a sixth/seventh/etc. AST scan that gates the type's construction to one module. Mirrors `safety.AuditEvent` and `draft.LLMResponseEvent` precedent.

If a new module genuinely needs to construct a gated event-type (e.g., a deserialiser for resumption), update the scan's exclusion list AND document the audit-write seam. **Don't suppress the test** — the entire point of the AST scan is to catch unaudited audit-event constructions, and a one-line exclusion entry is cheaper than the hour you'll spend debugging a missing JSONL line.

## ANSI-safe lazy-format JSON logger + grep gate (DEC-017)

Same rule as `safety-layer.md` DEC-022 and `llm-drafter.md` DEC-011 — never f-string-interpolate user-controlled strings into a `_LOGGER` call:

```python
_LOGGER.warning("budget exceeded: %s", json.dumps({"unstarted_count": n, "model": model_unique_id}))
```

**Never** `_LOGGER.warning(f"budget exceeded: {n} tests for {model_unique_id}")`. A model unique_id containing ANSI escapes (`\x1b[31m...`) would inject into log viewers. JSON encoding handles this; f-string interpolation does not.

The grep gate at `tests/llm/test_logger_grep_gate.py` scans `src/signalforge/{llm,draft,prune}` and rejects any `_LOGGER\.\w+\(f"` hit. Extend the scan to a fourth directory when the grader (#7) ships, rather than copy-pasting a per-layer gate; the single test is the source of truth.

## Custom `__repr__` on result-shaped models (DEC-022, post-QG fix)

Pydantic v2's default `__repr__` emits every field. `PruneResult` carries the full decision tuple, each `PruneDecision` carries `compiled_sql` and `sample_failures` — an accidental `_LOGGER.warning("result: %s", result)` in a future caller would dump megabytes of SQL and (potentially) sample-row contents to the log.

`PruneResult.__repr__` shows only `model_unique_id`, `kept_count`, `dropped_count`, `elapsed_ms`. `PruneDecision.__repr__` shows only `test.test_type`, `test.column` (or sentinel `"<model-level>"`), `decision`, `reason`. Compiled SQL and sample failures stay accessible via field access; they just don't slip out the casual debug-print path.

Apply to any future result-shaped model whose fields include user-content payloads. The pattern is "minimal `__repr__`; rich access via fields" — don't override `__str__` (Pydantic uses it for serialisation).

User-supplied strings in error messages render via `repr()` (`_format_value` helper) — same DEC-022 rule applies to `PruneError` subclasses. A model unique_id containing `\x1b[31m` quoted via `repr()` shows as `'\\x1b[31m...'` in error output; raw interpolation would inject.

## Drift detectors are mandatory for read-back models (DEC-010)

Every `extra="ignore"` production model — `PruneResult`, `PruneDecision`, `PruneEvent` — pairs with a `Strict<Model>(extra="forbid")` detector in `tests/prune/test_drift_detector.py`, validated against a committed JSON/JSONL fixture (`tests/fixtures/prune/prune_event_v1.jsonl` for the audit type). Adding a field to production without updating the strict mirror OR the fixture breaks the test loudly.

The QG review for #6 caught one near-miss where a production field landed without updating the strict mirror — the drift detector flagged it before it merged. Bake the rule: production `extra="ignore"` change = strict-model change = fixture refresh, in the same commit.

The `extra=` placement convention from `safety-layer.md` DEC-015 applies verbatim:

- `PruneConfig`, `_PruneConfigFile` → `extra="forbid"` (config-shaped; typos like `scop:` must fail loud).
- `PruneResult`, `PruneDecision`, `PruneEvent` → `extra="ignore"` (read-back shapes; forward-compat matters).

## API alignment with adjacent stages (post-QG fix)

`load_prune_config(project_dir, path=None) -> PruneConfig` matches the signature of `load_safety_config` and `load_draft_config`. The default `audit_path` resolves relative to `project_dir` (`<project_dir>/.signalforge/prune.jsonl` per DEC-016). Same for the orchestrator entry: `prune_tests(model, adapter, candidates, manifest, *, config=None, audit_path=None)` — keyword-only optionals, model-and-adapter front-paired, mirroring `draft_schema(model, adapter, policy, manifest, *, config)` from `llm-drafter.md`.

The CLI (#9) and any future orchestrator wants one calling convention; new stage configs and orchestrators must match the precedent. If you find yourself writing `load_grade_config(*, project_root)` or `grade_artifacts(adapter, model, ...)` (adapter-first), you've broken the alignment. Match the existing seam.

## `signalforge.yml` top-level namespace: `prune:` (DEC-020)

The prune-stage block is `{ prune: { enabled, scope, sample_size, sample_strategy, test_timeout_seconds, total_budget_seconds, capture_failure_rows, trusted_models, partition_filter } }`. Sibling top-level keys (`safety:`, `llm:`, future `grade:`/`diff:`) are reserved for other stages and silently ignored by the prune loader.

`PruneConfig` uses `extra="forbid"` (config-shaped; typos fail loud). The wrapping `_PruneConfigFile` uses `extra="ignore"` at the top level so unknown sibling stages don't break the loader. Mirrors `llm-drafter.md` DEC-027 / `safety-layer.md` DEC-025 verbatim.

When introducing a new pipeline-stage config, claim its own top-level key. Don't pile under `prune:` — each stage's behaviour-knob block stays separate, and the v2-config migration cost grows linearly with the number of stages whose config got merged.

## `trusted_models` is validated at orchestrator entry, not at config load (DEC-008)

`PruneConfig.trusted_models: tuple[str, ...]` is a tuple of `unique_id` strings. It is **not** validated at `load_prune_config(...)` time — the manifest isn't loaded yet, so we can't check membership. Validation happens at `prune_tests(...)` entry: every entry must appear in `manifest.models`; otherwise raises `PruneTrustedModelNotFoundError(unique_id=...)` **before any warehouse call**.

A typo like `trusted_models: ["model.proj.cusotmers"]` (vs. `customers`) MUST fail loud — silent no-op (the model isn't trusted because it doesn't match anything) is exactly the failure mode that leaks "failed-on-known-clean-data" tests through as `kept`. Apply the same "validate-against-manifest at orchestrator entry" pattern to any future config field that references manifest entities by `unique_id`.

## v0.2 reservations / additions (issue #22 — temp-table-materialised sample)

Issue #22 lands the materialised-sample optimisation as the v0.2 default. The additions below extend (don't replace) every rule in this file; the v0.1 oneshot path remains the fallback for non-BigQuery adapters.

**5-surface parity for v0.x → v0.(x+1) graduations.** When graduating a reserved surface in this section (or any rule file's "v0.2 reservations" / "v0.3 reservations" block) from forward-compat-only to behaviour-active, update **five surfaces in the same commit** — the non-CLI analogue of `cli-layer.md`'s 5-surface flag-parity rule (help / docstring / ops doc / test name / DEC):

1. **Rule file** — this block (or the equivalent in another rule file) — promote from "reserved" to "active" wording, retaining the historical DEC pointer.
2. **Ops doc** — `docs/prune-ops.md` § "Configuration" / § "Decision matrix" / § "Cost model" (or the corresponding stage's ops doc). The contract surface external CI parsers and downstream tooling key on.
3. **CLAUDE.md public-API surface** — the bullet under "Public API surface (v0.1 + v0.2 additions)" so a maintainer reading top-level orientation sees the active surface, not the reserved one.
4. **Test** — promote a forward-compat smoke test (or write a fresh one) that pins the active behaviour, not just the reserved type signature.
5. **DEC in `plans/super/<n>-<topic>.md`** — the ADR-style record of why the graduation happened. Don't leave the plan stale relative to shipped behaviour.

Surfaces 2 and 3 are the ones most often forgotten because they sit furthest from the code. Codify the parity check into every graduation PR's review checklist. Same lesson as `cli-layer.md` pass-3 / pass-4; the surface count is the same; the mapping is "rule file ↔ argparse help" / "ops doc ↔ ops doc" / "CLAUDE.md ↔ handler docstring" / "test ↔ test" / "DEC ↔ DEC".

- **`PruneConfig.sample_strategy: Literal["oneshot", "materialised"] = "materialised"` (DEC-007 of #22).** New `extra="forbid"` field on `PruneConfig`; `materialised` is the v0.2 default. Operators on non-BigQuery adapters opt out via `prune.sample_strategy: oneshot`. v0.1 YAML files without the field load with the `materialised` default and the orchestrator's conservative-bias routing handles non-BQ adapters gracefully. Same `extra="forbid"` placement convention as the rest of `PruneConfig` — typos like `sample_stratagy:` (or US `materialized`) fail loud at config load.

- **Two new typed errors, both `WarehouseError` subclasses, both → CLI tier 3 (DEC-008 of #22).** `MaterialisationFailedError(WarehouseError)` wraps any SDK / network / quota failure during `BigQueryAdapter.materialise_sample` via the `cause` kwarg pattern (mirrors `LLMResponseAuditWriteError`); `MaterialisationNotSupportedError(WarehouseError)` is the ABC default-impl raise for non-BQ adapters. Both registered in `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` at tier 3 (external-dep / fail-closed audit-write durability — see `cli-layer.md` four-tier taxonomy); the 7th AST scan in `tests/test_audit_completeness.py` catches a missed registration. Both ship `default_remediation`; the no-support remediation reads `"Set 'prune.sample_strategy: oneshot' in signalforge.yml to fall back to per-test sampling, or wait for v0.3 multi-warehouse materialisation support."` (DEC-006 of #22 — locked verbatim and tested for stability).

- **`_SESSION._sf_sample_<run_id>` is the audit signal in `compiled_sql` (DEC-001 of #22).** Every `PruneEvent.compiled_sql` produced under `sample_strategy=materialised` references `FROM \`_SESSION._sf_sample_<16-hex>\`` (two-part `_SESSION._sf_sample_<run_id>` — no `<project>.` prefix; `BigQueryAdapter.materialise_sample` returns `TableRef(project=None, dataset="_SESSION", ...)` because BigQuery rejects the three-part `<project>._SESSION.<name>` form even inside the owning session). The 16-hex `run_id` is `blake2b(table.qualified_name + signalforge_version + str(n) + canonical_json(partition_filter), digest_size=8).hexdigest()` (inputs joined with NUL separator; 16 hex chars). Same input → same `run_id` → byte-equal `compiled_sql` across runs → `compiled_sql_hash` reproducibility invariant (DEC-005 of #6) preserved. Oneshot runs continue to reference the source table directly; the `_SESSION` prefix is the durable signal that distinguishes the two modes in the audit JSONL. Don't paraphrase the table-name pattern — `_SESSION._sf_sample_<run_id>` is what the join in `INFORMATION_SCHEMA.JOBS_BY_PROJECT` keys on.

- **DEC-009 conservative-bias routing across every `WarehouseError` subclass + orchestrator-level WARNING.** Any exception thrown by `adapter.materialise_sample(...)` (`MaterialisationFailedError`, `MaterialisationNotSupportedError`, `UnknownTableSizeError`, `SamplingRequiresPartitionFilterError`, or any other `WarehouseError` subclass) is caught at orchestrator entry. Every candidate test routes to `kept-without-evidence` with `why=f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"` (DEC-005 shape; ~250 bytes per record under the 4000-byte JSONL audit cap). One `PruneEvent` per candidate is written to the audit JSONL — fail-closed audit preserved (DEC-016 of #6). The orchestrator ALSO emits a single stderr WARNING at the head of the failure path (BEFORE the per-decision audit writes) so the operator gets a one-line out-of-band signal that the run was degraded — otherwise the only signal is N identical `why` fields buried in the diff. Lazy-format JSON shape (passes the grep gate): `_LOGGER.warning("materialisation failed; routing all tests to kept-without-evidence: %s", json.dumps({"model_unique_id": ..., "candidate_count": N, "error_class": type(exc).__name__, "error_message": str(exc)[:200]}))`. Mirrors the v0.1 budget-exceeded WARNING pattern (DEC-011 of #6).

- **DEC-010 — total-budget includes materialisation; no separate `materialisation_timeout_seconds` knob in v0.2.** `PruneConfig.total_budget_seconds` ticks from orchestrator entry through the materialisation query and into the per-test loop. Budget exhaustion mid-materialisation marks every remaining test `kept-without-evidence` with the existing `why="total prune budget exceeded before evaluation"` (DEC-011 of #6 unchanged). Don't add a new per-stage timeout knob — the single budget is the contract; v0.3 may add per-stage knobs when the batch runner ships.

- **`prune_tests` owns the `with adapter:` block (DEC-013 of #22).** The orchestrator invokes the adapter as a context manager itself so `BigQueryAdapter.__exit__` fires and the session-cleanup code in DEC-013 / DEC-014 runs. **Callers MUST pass an adapter that has not already been entered** — double `__enter__` is undefined behaviour. Library callers (notebooks, scripts) should NOT wrap the `prune_tests` call in their own `with adapter:`; the engine owns the context. Without `__exit__` running, materialised sessions rely on BigQuery's server-side timeout (~24h) for cleanup — the explicit `BQ.ABORT_SESSION()` call never fires and the temp tables linger. The `prune_tests` docstring documents the contract; the test suite pins it (`test_prune_tests_uses_adapter_as_context_manager`).

## v0.2 additions (issue #35 — operator-chosen prune disable)

Issue #35 lands `PruneConfig.enabled` as the operator-chosen escape hatch for "draft a run without warehouse contact." It does not replace any v0.1 behaviour; the default (`enabled=True`) preserves every rule in this file. Reaches Architectural Commitment #4 (OSS-first / "I can try this safely") at the first-run UX surface — operators who get an unexpected BQ bill on a `safety.mode: schema-only` run reasonably distrust the tool; this knob plus the docs reframe (US-004 of #35) closes the gap.

- **`PruneConfig.enabled: bool = True` (DEC-005 of #35).** New `extra="forbid"` field on `PruneConfig`; default `True` preserves all v0.1 behaviour. Same `extra="forbid"` placement convention as the rest of `PruneConfig` — typos like `enabld:` fail loud at config load. Operators on any adapter (BQ in v0.1; Snowflake / Postgres in v0.3+) opt in to the short-circuit via `prune.enabled: false`.

- **Short-circuit position (DEC-002 of #35).** `prune_tests` branches on `not resolved_config.enabled` AFTER audit-path symlink-hardening + `config_hash` computation, BEFORE `_validate_trusted_models` / `TableRef.from_model` / `adapter.dialect()` / `with adapter:`. The audit-path gate stays upstream so the disabled path still benefits from the symlink-harden defence. The trusted-models / manifest-shape gates are intentionally bypassed — an operator who disabled prune shouldn't need to keep a valid `trusted_models` list, and a manifest-shape problem irrelevant to non-running SQL must not block them. No warehouse calls, no LLM calls, no context-manager entry.

- **Conservative-bias routing on the disabled path (DEC-001 / DEC-007 of #35).** Every candidate routes to `kept-without-evidence` (`decision="kept"`, `reason="kept-without-evidence"`) — NOT a sixth `DropReason` literal. The 5-value taxonomy (`always-passes`, `requires-future-data`, `failed-on-known-clean-data`, `kept`, `kept-without-evidence`) stays locked. The distinguishing signal travels in the `why` field: `"prune disabled in signalforge.yml"` (DEC-003 of #35 — locked verbatim; pinned by a stability test in `tests/prune/test_engine.py`). One `PruneEvent` per candidate is written via `_write_audit_or_abort` so the fail-closed audit invariant (DEC-016 of #6) is preserved — the diff renderer's `(run_id, test_anchor)` join stays uncorrupted even when prune did no work. Mirrors the materialisation-failed branch (DEC-009 of #22) structurally; the only differences are the `why` text and the lack of a triggering exception.

- **CLI INFO emission at prune-stage entry (DEC-004 of #35).** `cmd_generate` emits exactly one `_LOGGER.info(...)` line when `prune_config.enabled is False`, AFTER the `--scope` / `--sample-strategy` override block and BEFORE `prune_tests` is invoked. Lazy-format JSON per the layer-wide grep gate (`tests/llm/test_logger_grep_gate.py`): `_LOGGER.info("prune disabled in signalforge.yml; routing all candidates to kept-without-evidence: %s", json.dumps({"model_unique_id": ..., "candidate_count": N}))`. INFO (not WARNING) because the operator explicitly opted in via config — surfacing a WARNING on every run would be nagging, not signal; the trade-off is documented in the README + `docs/safety-ops.md` + `docs/prune-ops.md` so the INFO is a confirmation, not a discovery. **`--quiet` DOES suppress this INFO** — unlike the cleanup-failure WARNING from `warehouse-adapters.md` (which must always surface because it's operator-actionable), this INFO is purely confirmatory.

When a future v0.3 surface ships an analogous "operator-chosen skip" branch (e.g., `--no-grade`, `--no-draft`), apply the same three-part contract verbatim: route to the existing "no evidence" literal (don't expand the enum), preserve the fail-closed audit invariant, emit a single sibling-stage INFO at the short-circuit entry. The conservative-bias routing template from the materialisation-failed precedent (#22) and the operator-disable precedent (#35) makes this a paved path.

## v0.2 additions (issue #51 — kept-rate WARNING + expected-drop-rate docs)

Issue #51 lands `PruneConfig.min_kept_rate_warn` as a soft, end-of-run signal for "did the prune work as intended?" plus a `docs/prune-ops.md` § Expected drop rates section. It does not replace any v0.1 or v0.2 behaviour; the default (`0.0`) only fires the WARNING when every candidate was dropped — the "did we lose the whole LLM draft?" signal. Reaches Architectural Commitment #1 (signal over volume) at the operator-trust surface — a first-run user seeing "11 dropped, 1 kept" reasonably distrusts the differentiator without prose framing; the doc + WARNING close the gap.

- **`PruneConfig.min_kept_rate_warn: float = 0.0` (issue #51).** New `extra="forbid"` field on `PruneConfig`; default `0.0` preserves the silent posture for every existing operator config (no WARNING unless every candidate dropped). Range-bound to `[0.0, 1.0]` via `field_validator` mirroring the `_positive` / `_non_negative` pattern on adjacent numeric knobs — values outside the unit interval fail loud at config load. Operators raise the threshold (e.g., `0.10`) to catch "fewer than 10% kept" on typical staging models; `1.0` means "always warn" (kept_rate <= 1.0 always). The WARNING is informational — the run still returns a `PruneResult` and exits cleanly; there is no exit-code path or typed error.

- **WARNING emission at every `prune_tests` return site (issue #51).** `_maybe_emit_kept_rate_warning` is the single helper called at all three return sites in `prune_tests`: the disabled short-circuit (#35), the materialisation-failed branch (#22), and the main happy path. Lazy-format JSON shape (passes the layer-wide DEC-017 logger gate): `_LOGGER.warning("prune kept rate at or below configured threshold: %s", json.dumps({"model_unique_id": ..., "total_tests": N, "kept": K, "dropped": N-K, "kept_rate": <float>, "min_kept_rate_warn": <float>}))`. The helper skips emission when `total == 0` — an empty candidate set is its own degenerate signal (the drafter produced nothing) and is not the failure mode this warning is here to catch; the skip also avoids `ZeroDivisionError`. On the disabled / materialisation-failed paths every decision is `kept-without-evidence` so `kept_rate == 1.0` and the WARNING never fires unless an operator explicitly sets `min_kept_rate_warn=1.0`.

- **Doc framing — Architectural Commitment #1 at the trust surface (issue #51).** `docs/prune-ops.md` § Expected drop rates documents the working state ("a high drop rate is the working state, not the failure state") with cited reference numbers from the Austin bikeshare staging fixture (8 candidates, 5 dropped = 62.5%, per-test-type breakdown). The section explicitly names the failure mode the WARNING catches — "a run that drops everything is the failure mode to watch for" — and links to the threshold knob. `README.md` § Expected output cross-links to the ops doc. Without this prose, the differentiator's *load-bearing* prune-and-drop behaviour reads as a defect on first contact; the doc-plus-WARNING pair is the calibration mechanism.

When a future v0.3 surface adds a new run-shape signal (drift-detection counts, grade-quality summary, multi-model batch aggregates), apply the same three-part contract: (1) operator-configurable threshold field on the relevant `<Stage>Config`, with `field_validator` range-bound, (2) single helper-function emission at every return site (one source of truth, no per-branch duplication), (3) doc framing in the relevant `docs/<stage>-ops.md` that names the failure mode the signal catches. Default the field to a value that preserves the silent posture for existing operators.

## v0.2 additions (issue #55 — normalise hash recipe across writers)

Issue #55 normalised every reproducibility hash in the audit / sidecar corpus to `blake2b(digest_size=8)` over canonical JSON (the recipe already used by `signalforge.draft`, `signalforge.grade`, `signalforge.diff`). The pre-#55 outliers were `signalforge.prune.audit._compute_config_hash` (`SHA-256[:16]`) and `signalforge.safety.policy._compute_policy_hash` (`SHA-256[:16]`); both migrated in lockstep so a reviewer correlating `safety.jsonl` / `llm_responses.jsonl` / `prune.jsonl` / `grade.jsonl` / `diff.json` reads one recipe.

- **`_PRUNE_AUDIT_SCHEMA_VERSION` bumped 1 → 2 (issue #55).** `PruneEvent.audit_schema_version: Literal[2] = 2` in production; the v0.1 `prune.jsonl` fixture and drift detector refreshed in lockstep. Consumers correlating `config_hash` across audit JSONLs must gate on `audit_schema_version >= 2` to skip records produced by the pre-migration writer. Sibling safety bump 2 → 3 documented in `safety-layer.md` § AuditEvent reproducibility fields.

- **Why one recipe.** `blake2b-8` is a 16-hex-char digest with the same collision profile as `SHA-256[:16]` for this use case (the surface area is "did two runs use the same canonicalised config?"; not a security-grade integrity check). The choice is consistency, not strength — mixed hash families across the audit corpus mean a reviewer correlating across writers needs to know which family applies where, which is exactly the kind of operator-trust friction Architectural Commitment #5 ("explainable diffs") exists to eliminate.

When v0.3 adds a new writer (CLI run-history audit, multi-model batch checkpointer, etc.) that needs a reproducibility hash, reach for `blake2b(canonical_json.encode("utf-8"), digest_size=8).hexdigest()` directly. Don't introduce a new family or revive `SHA-256` — the normalisation is now load-bearing for cross-writer correlation.

## Reference

`plans/super/6-prune-engine.md` — DEC-001 … DEC-028. `plans/super/22-temp-table-sample.md` — DEC-001 … DEC-014 (v0.2 materialised-sample additions). `src/signalforge/prune/` — current implementation. `docs/prune-ops.md` — operational reference. `tests/prune/test_drift_detector.py` — schema-drift gate. `tests/test_audit_completeness.py` — AST-scan suite (7 scans as of #9). `tests/llm/test_logger_grep_gate.py` — lazy-format logger gate (6 dirs as of #9). `tests/fixtures/prune/prune_event_v1.jsonl` — committed audit fixture for the drift detector.
