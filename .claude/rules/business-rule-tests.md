# Custom business-rule tests (`custom_sql` — the 5th dbt test type)

Established by issue #116. Apply to any code touching the `custom_sql` candidate-test variant — drafting it, resolving its Jinja, compiling/pruning it, grading it, diffing it, writing it to disk, or ingesting it from `tests/*.sql`.

`custom_sql` is the first test type that is a **full singular-test SELECT** (returns failing rows) rather than a parameterless/parameterised dbt schema test. It encodes Architectural Commitment #1 for business rules an operator can't express with the four built-ins: SignalForge drafts them (from `meta.signalforge.business_rules` or LLM inference), then **prunes and grades** them like everything else — an always-pass business rule is dropped, not shipped.

## The variant (DEC-002)

`signalforge.draft.models.CandidateTestCustomSQL` — `type: Literal["custom_sql"]`, `sql: str` (non-empty validated), `column: str | None` (None ⇒ model-level), `rationale: str | None`. Frozen, `extra="ignore"`, a member of the `CandidateTest` discriminated union (`Field(discriminator="type")`). Paired `StrictCandidateTestCustomSQL` drift mirror + a `candidate_schema_v1.json` row.

**`column=None` is the model-level case** and must be special-cased AHEAD of any `test.column not in model_columns` check — several dispatch sites (`draft.parser` anchor validator, `_common.artifact_id`, the prune compiler) carry an explicit `custom_sql` arm for exactly this reason. When you add a 6th test type, audit every exhaustive `isinstance`/`match` over the union (compiler `_compile_test`, `model_test_args_hash`, `diff._emitter._render_test`, the parser) — a missing arm is a latent crash, not a type error, because the union is open at runtime.

## Two input paths, both in the dynamic prompt block (DEC-001)

`meta.signalforge.business_rules` (NL `str` or `list[str]`, column- and model-level) is read with the **safety-layer `meta.get("signalforge")` dict-guard pattern** (strict `isinstance(dict)`; a scalar/list under the key is config noise, dropped — never fail loud). Rules render into the drafter's **dynamic (non-cached) block**, NOT the cached system prompt — so they don't perturb the prompt-cache golden. When no rules are declared, the inferred-fallback prompt still permits `custom_sql`. `_PROMPT_VERSION` rotated when the `custom_sql` catalogue entry landed in the cached system prompt; the cache-stability snapshot moved in lockstep (`llm-drafter.md`). `exclude_tests` can name `"custom_sql"` (it's in `VALID_TEST_TYPES`).

## Bounded Jinja resolution — NO Jinja engine (DEC-004, DEC-005)

`signalforge.manifest.resolve_template_refs(sql, model, manifest) -> str` substitutes `{{ this }}` (→ `model.resolve_this()`), `{{ ref('m') }}` / `{{ ref('pkg','m') }}` / version forms, and `{{ source('s','t') }}` to qualified names via the manifest source registry + `resolve_ref`/`resolve_source` (also #116). Control-flow Jinja (`{% … %}`, `{{ var() }}`, `{{ env_var() }}`, macros) and any residual `{{ }}` after substitution are **rejected loudly** (`UnsupportedJinjaError`/`TemplateResolutionError`, in the existing `manifest/errors.py` — do NOT add a new `errors.py`, scan-7 asserts exactly 11). The resolver lives in the **manifest layer** (stage-0, no logging) so both prune and ingest consume it without a cross-stage import.

## Prune-compile: resolve → safety-check → wrap, dialect-driven (DEC-003/006/008/009)

`prune.compiler._compile_custom_sql` ordering is load-bearing and security-relevant: **resolve Jinja first, then `validate_test_sql` on the RESOLVED sql, then wrap.** The adapter re-validates before running (belt-and-braces). No `count(*)` pre-wrap in the compiler — it returns the failing-rows SELECT; the adapter owns `SELECT COUNT(*) AS failures FROM (<sql>) AS t` (matching the built-ins; pre-wrapping double-counts). Dialect-driven via `Dialect.quote_char`; **no `google.cloud` import under `prune/`.**

- **Single-table** (no `JOIN` after literal-stripping) → sample-CTE wrap.
- **Multi-table** → full-scan, bounded only by `maximum_bytes_billed` (sampling a join is semantically wrong; DEC-006). The bytes cap is the sole guardrail — documented operator tuning point.
- **Conservative-bias routing — never raise out of the compiler, never add a 6th `DropReason`.** Safety-reject / unsupported-Jinja / ambiguous-ref → `_InvalidIdentifier` → `kept-without-evidence`; unresolvable `ref()`/`source()` (manifest-absent target) → `_RequiresFutureData` → `requires-future-data` (the relationships-missing-target precedent). The engine matrix (`_decide_from_test_result`) is test-type-agnostic and routes `custom_sql` generically; over-bytes-cap is just a per-test `WarehouseError` → `kept-without-evidence`.

### Materialised-sample substitution — the #116 QG bug, READ THIS (US-019 / QG fix)

A new test type that builds its own `FROM` clause (rather than from the passed `table_ref`) will **silently bypass the materialised temp-table sample**. Under the default `sample_strategy="materialised"` + `scope="sample"`, the engine materialises a temp table and calls the compiler with `table_ref = <temp table>`, `scope="full"`, `partition_filter=None`. The four built-ins FROM `table_ref`, so they read the temp table for free. `custom_sql` resolves `{{ this }}` to the model's **source** table, so it MUST explicitly rewrite the model's own qualified name → the quoted `table_ref` when `table_ref.qualified_name != model.resolve_this().qualified_name` (all occurrences). Missing this = full-scanning production under the cost-saving strategy. Pinned by a test asserting the compiled SQL references `_SESSION._sf_sample_*` and never the source table. **Any future warehouse-substituting strategy (or new self-FROM test type) must carry the same substitution + test.**

## On-disk artifact: proposed `.sql` files (DEC-010/011)

Singular tests are standalone `tests/*.sql` files, not `schema.yml` blocks. `DiffReport.proposed_test_files: tuple[ProposedTestFile, …]` carries them (each `path` + `sql`); `audit_schema_version` bumped 2→3. The diff emitter SKIPS `custom_sql` in the YAML emitter and routes it to `proposed_test_files`; renderers show new-file hunks with the unconditional ANSI-strip + dynamically-sized markdown fence on the SQL body (the SQL is user/LLM content). Filenames come from `diff._test_file_writer.anchor_to_filename` (slugs every component to `[A-Za-z0-9_]` — cannot escape `tests/`). Writing is the project's **6th fail-closed writer** (`_FAIL_CLOSED_WRITER_MODULES`, scan 8): size-check → symlink-hardened canonicalise → `O_WRONLY|O_CREAT|O_TRUNC|0o600` → short-write loop → fsync, no `except` around write/fsync. `DiffTestFileWriteError`/`DiffTestFileRecordTooLargeError` are tier-3 in the exit-code table.

## CLI: `generate --write --force` + read-only `prune-existing` (DEC-010/013)

`generate --write` writes proposed `.sql` via `write_test_file`. The `-- signalforge:generated <hash>` header marker is the ownership signal: a NEW file is written; a marked file is overwritten ONLY with `--force`; an **unmarked (hand-authored) file is NEVER overwritten, even with `--force`** (refuse + WARNING). `prune-existing` stays read-only (#105) and gains `--tests-dir` (default `<project>/tests`): it ingests existing `tests/*.sql` via `ingest.read_test_files(..., existing=<schema candidate>)`, dedupes by `(model, "custom_sql", sql_hash)`, and prunes them alongside schema.yml tests in one run. No `--write` there.

## Ingest: stage-0 reader, closed SkipReason (DEC-013)

`ingest.read_test_files` enumerates `*.sql`, resolves refs to associate each test to the model it references (unrelated files are simply not included — NOT skip-recorded), and records unsupported-Jinja files as `SkippedTest(reason="malformed-supported-test")`. **`SkipReason` stays the closed 3-value Literal** — a new disposition reuses an existing reason, never a 4th. No logging, no audit writer (stage-0; `ingest-layer.md`).

## Testing the `custom_sql` paths

Engineer determinism by **rule semantics**, not LLM SQL bytes: a tautology over the model's own rows is mathematically always-pass → dropped; an engineered violation is guaranteed failing-rows → kept. Multi-table tests need a real two-distinct-ref JOIN (a `{{ this }} JOIN {{ this }}` self-join may collapse to the single-table classifier) AND an assertion on the dispatched SQL shape (no `WITH sample`) — asserting only the routed decision lets a sampling regression pass (the #116 QG gap). The e2e fixture injects `meta.signalforge.business_rules` into the per-run `tmp_path` manifest copy so it stays decoupled from the `init-demo` byte-parity gate.

## Reference

`plans/super/116-business-rule-tests.md` — DEC-001 … DEC-015 + the QG fixes. See-Also: `llm-drafter.md` (prompt + cache), `prune-engine.md` (DropReason lock, conservative-bias routing, materialised sample), `diff-renderer.md` (proposed artifacts, fail-closed writer, audit_schema_version), `ingest-layer.md` (stage-0 reader, closed SkipReason), `cli-layer.md` (exit-code taxonomy, 5-surface parity), `manifest-readers.md` (resolver + source registry, drift detectors), `warehouse-adapters.md` (`_sql_safety`, `maximum_bytes_billed`).
