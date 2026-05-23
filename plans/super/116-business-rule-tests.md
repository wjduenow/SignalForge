# Super Plan — #116: Custom business-rule test generation

## Meta

- **Ticket:** #116 — feat: custom business-rule test generation (meta.business_rules + LLM-inferred singular SQL tests)
- **Branch:** `feature/116-business-rule-tests`
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/116-business-rule-tests`
- **Phase:** detailing (awaiting approval)
- **Sessions:** 1 (2026-05-22)
- **Research anchor:** `docs/research/dbt-tool-design-sketches.md` §8, Opportunity 2 v2 milestone

## Summary

Extend SignalForge's drafter beyond the four built-in dbt test types (`not_null`,
`unique`, `accepted_values`, `relationships`) to a fifth: **custom singular SQL
tests** encoding business rules. Sourced two ways — natural-language rules in
`meta.signalforge.business_rules` (primary) and LLM inference from compiled SQL +
column profile (fallback). The new candidate tests flow through the **existing**
prune → grade → diff pipeline, preserving Architectural Commitment #1 (signal over
volume — drop always-pass / uninformative business-rule tests) and #2 (evaluation
in the loop — grade them against the rubric).

### Locked at kickoff

- **Rule source:** both — `meta.signalforge.business_rules` primary, LLM-inferred fallback.
- **Test shape:** custom singular SQL test (`.sql` artifact class), not `dbt_utils.expression_is_true`.

## Discovery

### Code seams to extend (Codebase Scout findings)

| Seam | File | Current shape | Extension |
|---|---|---|---|
| CandidateTest union | `draft/models.py:36-148` | 4 frozen variants, `Field(discriminator="type")`, `extra="ignore"` | Add `CandidateTestCustomSQL` (`type: Literal["custom_sql"]`, `sql`, `column: str \| None`, `rationale`) + union member |
| Anchor-contract validator | `draft/parser.py:86-157` | per-type column-membership checks, `exclude_tests` gate | Add custom-SQL branch; model-level allows `column=None`; exempt from parent-column check |
| Drafter prompt | `draft/prompts.py:59-274` | `_TEST_CATALOGUE_LINES`, `_render_system_prompt`, `_PROMPT_VERSION`, `<MODEL_SQL>` envelope | Inject business-rule guidance + (if LLM-drafted) catalogue entry; rotate `_PROMPT_VERSION` |
| Prune compiler | `prune/compiler.py:273-597` | 4 per-type compilers, `Dialect.quote_char` dispatch, `validate_identifier`/`escape_bq_string_literal` | Add `_compile_custom_sql`; wrap+sample; route safety-rejects to sentinel |
| SQL safety | `warehouse/_sql_safety.py` | `validate_identifier` (strict regex), `escape_bq_string_literal`, `validate_test_sql` (cheap rejects) | Reuse; the load-bearing security surface for arbitrary expressions |
| Artifact id | `_common/artifact_id.py:55-173` | `model_test_args_hash` per-type dispatch; 6 dotted shapes | Add custom_sql hash branch (include `sql` in domain); formatter is generic, no change |
| Grade layer | `grade/*` | consumes `artifact_id` as opaque string | No change — new shapes flow through |
| Diff emitter | `diff/_emitter.py:128-231` | renders tests **into schema.yml** only | **NEW: singular tests are `.sql` files, not schema.yml** — needs a new emit path + `DiffReport` field |
| Manifest meta | `manifest/models.py:61,78` | `Column.meta` / `Config.meta` free dicts; safety reads `meta.signalforge.*` | Read `meta.signalforge.business_rules`; precedent exists |
| Ingest | `ingest/parser.py` | parses 4 schema.yml test types; skip-records the rest | Optional: read existing `tests/*.sql` for prune-existing (scope Q) |

**The architectural wrinkle:** singular dbt tests are standalone `.sql` files under
`tests/`, returning failing rows — *not* schema.yml blocks. The diff renderer today
only emits a proposed `schema.yml`. This is the largest new design surface.

### Rule constraints to satisfy (Convention Checker findings)

- **`DropReason` is locked at 5 values** (`prune-engine.md` DEC-006/011). Any "couldn't
  evaluate this business-rule test" outcome routes to `kept-without-evidence` — do **not**
  add a 6th. Follow the conservative-bias routing template (per-test `why`; one WARNING if all).
- **SQL safety at the compile seam** (`prune-engine.md` DEC-024): `validate_identifier` on
  every identifier before quoting; safety-rejects → `kept-without-evidence`
  (`why="identifier rejected by SQL safety check"`). No full SQL parser (DEC-013 of #3).
- **New `CandidateTestCustomSQL` is read-back** → `extra="ignore"` + a `Strict*` drift
  detector + committed fixture (`manifest-readers.md`, `testing-signal.md`).
- **New error classes** must register in `_EXCEPTION_TO_EXIT_CODE` with a tier; the 7th AST
  scan (`test_every_typed_error_is_in_exit_code_mapping_table`) fails the build otherwise
  (`cli-layer.md` DEC-024). If a new `errors.py` stage is added, bump the scan-7 module count
  and add the base to `_EXCEPTION_MAPPING_EXCLUDED_BASES`.
- **No new audit-event class anticipated** → no new construction-seam AST scan. If the diff
  layer gains a second on-disk writer, extend `_FAIL_CLOSED_WRITER_MODULES` (`safety-layer.md`
  scan 8) and mirror the writer template verbatim.
- **Logger grep-gate**: lazy-format + `json.dumps()` for user strings across the 6 scanned dirs.
- **`_PROMPT_VERSION` rotation** on any drafter-prompt change; keep
  `tests/llm/test_prompt_cache_stability.py` green.
- **5-surface parity** for adding a new test type: rule file + ops doc + CLAUDE.md + test + DEC.
- **Engineered determinism** for any LLM-output assertion (`testing-signal.md`).

### Proposed scope (pre-refinement)

Draft → prune → grade → diff for a 5th test type, both input paths, with the new `.sql`
artifact handled end-to-end through write/diff. Exact in/out boundaries pending the scoping
answers below.

## Scoping answers (locked)

- **SQL granularity:** arbitrary failing-rows SELECT — full dbt singular-test contract,
  `{{ this }}` / `{{ ref() }}` / cross-model joins, best-effort sampling.
- **Ingest scope:** generate **and** prune-existing — extend ingest to read existing
  `tests/*.sql` and associate each to the model it references.
- **Input sequencing:** both paths (meta-driven + LLM-inferred) ship together.

## Architecture Review

Two deep reviews ran: Security/feasibility (arbitrary-SQL surface + Jinja resolution) and
Testing-strategy. Ratings:

| Area | Rating | Finding |
|---|---|---|
| SQL injection / statement-stacking | **concern** | `validate_test_sql` blocks top-level `;`, `--`, `/* */`, unbalanced parens (via `_strip_string_literals`). **Gap:** a `;` inside a backtick-quoted identifier isn't stripped. Bytes-cap bounds the blast radius; cheap to also scan inside backticks. |
| Runaway cost / scan | **concern** | `maximum_bytes_billed` (default 100 MB, profile-overridable) + `use_query_cache=False` is the only ceiling. Arbitrary SELECT exposes a far larger feasible scan than the 4 built-ins. Over-cap → query fails → route to `kept-without-evidence`. Operator must tune the cap. |
| Jinja-ref resolution | **pass** | Bounded regex substitution of `{{ this }}` + `{{ ref('x') }}` → qualified names is feasible with NO Jinja engine (mirrors ingest's existing `_unwrap_ref_or_source`). `Model` carries `database`/`schema_`/`alias`; `TableRef.from_model` builds the qualified name. Control-flow Jinja (`{% %}`, `{{ var() }}`) must be rejected loudly. `{{ source() }}` needs a manifest source registry not currently exposed (scope Q). |
| Prompt-injection | **pass** | `<MODEL_SQL>` envelope + `PromptEnvelopeBreachError` already guard `raw_code` before any LLM call. `business_rules` is unwrapped free text, but the LLM's emitted SQL still passes `validate_test_sql` + the bytes cap, so a hostile rule degrades to a no-op (kept-without-evidence), not an escape. |
| Sampling correctness | **concern** | Sampling only the driving table of a multi-table join is semantically wrong (false negatives). Need a cheap single-vs-multi-table detection + a routing decision (sample / full-scan / kept-without-evidence). Precedent: the `_RequiresFutureData` sentinel + conservative-bias routing template. |
| New `.sql` on-disk artifact (write + diff) | **concern** | The diff layer only emits `schema.yml` today. Writing singular tests as `tests/*.sql` files is a **new path-safety surface**: injection-safe filename builder + a fail-closed writer (extends `_FAIL_CLOSED_WRITER_MODULES`, scan 8) + overwrite policy. |
| CandidateTest variant / drift detector | **pass** | Add `CandidateTestCustomSQL` (`extra="ignore"`) + `StrictCandidateTestCustomSQL` mirror + fixture row. Proven pattern. |
| Prune compiler snapshot | **pass** (downgraded from agent's "blocker") | Unit tests control the `.sql` input, so the compiled envelope (`SELECT count(*) ... FROM (<sql>) t` + sample CTE) is byte-deterministic and snapshot-able. LLM non-determinism only touches e2e, which asserts kept/dropped **counts** via engineered-determinism fixtures, not SQL bytes. |
| Ingest `tests/*.sql` | **pass** | Stage-0 reader extension: enumerate `.sql`, regex-extract `ref()`, associate to the target model, dedupe by `(model, custom_sql, sql_hash)`. No audit writer, no logging (ingest-layer.md). |
| AST scans / logger gate | **pass** | New error classes auto-register-or-fail scan 7. No new audit-event class → no new construction-seam scan. Ingest stays silent (no logger-gate change); `.sql` writer lives in the diff/cli layer that already logs. |

**No blockers.** Five concerns carried into refinement as decisions.

## Refinement Log

### Decisions

- **DEC-001 — Two input paths, shipped together.** Read business rules from
  `meta.signalforge.business_rules` (column-level and model-level, mirroring the safety
  layer's `meta.signalforge.*` reads) as NL `str` or `list[str]`; when absent, the drafter
  infers candidate rules from compiled SQL + column profile. Both land in #116.

- **DEC-002 — New `CandidateTestCustomSQL` variant.** `type: Literal["custom_sql"]`,
  `sql: str`, `column: str | None` (None = model-level), `rationale: str | None`, frozen,
  `extra="ignore"`. Added to the `CandidateTest` discriminated union. Paired
  `StrictCandidateTestCustomSQL(extra="forbid")` drift mirror + a `candidate_schema` fixture row.

- **DEC-003 — Arbitrary failing-rows SELECT (dbt singular-test contract).** The artifact is a
  full SELECT returning failing rows, may join other models. Not constrained to a predicate.

- **DEC-004 — Bounded Jinja resolution, NO Jinja engine.** A shared resolver substitutes
  `{{ this }}`, `{{ ref('m') }}` (pkg/version forms → last positional name), and
  `{{ source('s','t') }}` → qualified table names (reuses ingest's `_unwrap_ref_or_source`
  precedent). Control-flow Jinja (`{% … %}`), `{{ var() }}`, `{{ env_var() }}`, and macro
  calls are **rejected loudly** with a new typed error. Unresolvable `ref`/`source` →
  rejected.

- **DEC-005 — Manifest source registry exposed.** Surface dbt `sources` (database/schema/
  identifier) from the manifest as resolvable relations so `{{ source() }}` resolves to a
  `TableRef`-able qualified name. New manifest-layer surface (US-001).

- **DEC-006 — Multi-table tests run full-scan within the bytes cap.** Single-table custom
  tests use the existing sample CTE; multi-table tests (detected by a cheap post-resolution
  scan for `JOIN` / multiple table refs) run **unsampled** against full data, bounded by
  `maximum_bytes_billed` (sampling a join is semantically wrong → false negatives). Over-cap /
  over-budget / warehouse-error → `kept-without-evidence`. Cross-model rules thus actually get
  pruned. Operator must tune the bytes cap; documented.

- **DEC-007 — Conservative-bias routing reused; `DropReason` stays 5-value.** No 6th literal.
  Locked `why` strings: `"test SQL rejected by safety check"`, `"unsupported Jinja in test SQL"`,
  `"business-rule test exceeded byte cap"`, plus the existing per-test/budget messages.

- **DEC-008 — SQL-safety hardening + reuse.** Extend `_strip_string_literals` to also strip
  backtick-quoted identifiers so a `;` hidden inside backticks is caught by `validate_test_sql`.
  The custom-SQL compiler runs `validate_test_sql` pre-flight; failures → `kept-without-evidence`.
  Runs under the read-only adapter + bytes cap (documented RO posture). No full SQL parser
  (DEC-013 of #3 preserved).

- **DEC-009 — Compiler envelope is deterministic.** `_compile_custom_sql` resolves Jinja, then
  wraps `SELECT count(*) AS failures FROM (<resolved_sql>) AS t`; single-table → sample-CTE wrap,
  multi-table → full-scan (+ partition filter when available). Unit fixtures control the input
  `sql`, so the compiled bytes are snapshot-stable; LLM non-determinism is confined to e2e
  (asserts kept/dropped counts via engineered-determinism fixtures).

- **DEC-010 — `.sql` files written on `generate --write`, `--force` to overwrite.** Kept
  singular tests are written to `tests/<model>__<descriptor>_<args_hash>.sql` via an
  injection-safe filename builder + a fail-closed writer (extends `_FAIL_CLOSED_WRITER_MODULES`,
  scan 8) carrying a `-- signalforge:generated <hash>` header marker. Refuse to overwrite an
  un-marked or existing file unless `--force`. `prune-existing` stays **read-only** (#105 DEC-003)
  — surfaces proposed removals in the diff + sidecar only.

- **DEC-011 — Diff carries the new artifact.** `DiffReport.proposed_test_files: tuple[(path, sql), …]`;
  renderers show them as new-file hunks; sidecar serialises them. Tier classification unchanged
  (kept / kept-uncertain / dropped / flagged); `why` cascade unchanged.

- **DEC-012 — `artifact_id` extends, formatter unchanged.** `model_test_args_hash` adds a
  `custom_sql` branch including `sql` in the hash domain → `test.column.<col>.custom_sql[.<hash>]`
  / `test.model.custom_sql[.<hash>]`. Cross-stage `is`-identity parity preserved.

- **DEC-013 — Ingest reads `tests/*.sql` for prune-existing.** Stage-0 reader extension:
  enumerate `.sql`, resolve `ref()`/`source()`/`this`, associate each test to the referenced
  model, dedupe against schema.yml tests by `(model, "custom_sql", sql_hash)`. Unsupported-Jinja
  `.sql` → `SkippedTest` (`SkipReason` stays the closed 3-value set: `malformed-supported-test`).
  No audit writer, no logging (ingest-layer.md).

- **DEC-014 — New typed errors registered.** Jinja/resolution + `.sql`-write errors land in
  the right `errors.py`, register in `_EXCEPTION_TO_EXIT_CODE` (tier 1 parse/load, tier 2
  input/overwrite-refusal, tier 3 warehouse/write-durability), auto-gated by scan 7.

- **DEC-015 — `_PROMPT_VERSION` rotates; cache-stability snapshot updated in lockstep.**
  `VALID_TEST_TYPES` gains `"custom_sql"` so `exclude_tests` can suppress it. business_rules
  render into the dynamic (non-cached) block.

## Detailed Breakdown

Architecture order: manifest → resolver → draft → safety/prune → diff → write → ingest → CLI → e2e.
Every story's AC includes the canonical validation command
(`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`).

- **US-001 — Manifest source registry + relation resolution.** Expose dbt `sources`
  (database/schema/identifier) as resolvable relations; add `Manifest` helpers to resolve a
  `ref(name)` and a `source(s,t)` to a qualified-name `TableRef`. *Traces:* DEC-005.
  *Files:* `manifest/models.py`, `manifest/loader.py`, `manifest/__init__.py`, drift fixture.
  *Depends:* none. *TDD:* ref/source/this resolution + ambiguous-ref fail-loud.

- **US-002 — Template-ref resolver (no Jinja engine).** `resolve_template_refs(sql, model, manifest) -> str`
  substituting `{{ this }}` / `{{ ref() }}` / `{{ source() }}`; reject `{% %}` / `{{ var() }}` /
  macros with a typed `…TemplateError`. *Traces:* DEC-004. *Files:* shared seam (`prune/` or
  `_common/`), `errors.py`, exit-code registration. *Depends:* US-001. *TDD:* each form, pkg/
  version refs, control-flow rejection, unresolvable-ref rejection.

- **US-003 — `CandidateTestCustomSQL` model + union + drift mirror.** *Traces:* DEC-002.
  *Files:* `draft/models.py`, `tests/draft/test_drift_detector.py`, `tests/fixtures/draft/*`.
  *Depends:* none.

- **US-004 — Anchor-contract + `exclude_tests` extension.** Validate custom_sql (non-empty sql,
  Jinja-allowlist, column membership when set; model-level allows `column=None`; exempt from
  parent-column check); add `"custom_sql"` to `VALID_TEST_TYPES`. *Traces:* DEC-002, DEC-015.
  *Files:* `draft/parser.py`, `draft/config.py`. *Depends:* US-003. *TDD:* valid/invalid cases,
  exclude-tests rejection, collect-all (no short-circuit).

- **US-005 — Drafter: business-rule reading + prompt.** Read `meta.signalforge.business_rules`
  (column + model); render guidance + catalogue entry; rotate `_PROMPT_VERSION`; update
  `tests/llm/test_prompt_cache_stability.py`. *Traces:* DEC-001, DEC-015. *Files:* `draft/prompts.py`,
  `draft/schema.py` (orchestrator), `safety`/meta read path. *Depends:* US-003. *TDD:* meta read,
  inferred fallback, prompt-version rotation.

- **US-006 — SQL-safety hardening.** Backtick-aware `_strip_string_literals`; confirm
  `validate_test_sql` catches `;` inside backticks. *Traces:* DEC-008. *Files:*
  `warehouse/_sql_safety.py`. *Depends:* none. *TDD:* backtick-`;`, comment-in-backtick, balanced parens.

- **US-007 — Prune compiler `_compile_custom_sql`.** Resolve Jinja → wrap count envelope;
  single-table sample-CTE vs multi-table full-scan detection; safety-reject sentinel; snapshot
  fixtures (`custom_sql.sql`, `custom_sql_sample.sql`, `custom_sql_fullscan.sql`). *Traces:*
  DEC-003, DEC-006, DEC-008, DEC-009. *Files:* `prune/compiler.py`, `tests/fixtures/prune/compiled_sql/*`.
  *Depends:* US-002, US-003, US-006. *TDD:* each shape + reject paths.

- **US-008 — Prune engine routing + audit.** Wire custom_sql outcomes to `_decide_from_test_result`;
  multi-table full-scan; over-cap/error → `kept-without-evidence` with locked `why`; refresh
  `prune_event_v1.jsonl` + drift mirror. *Traces:* DEC-006, DEC-007. *Files:* `prune/engine.py`,
  `tests/fixtures/prune/*`, `tests/prune/test_drift_detector.py`. *Depends:* US-007.

- **US-009 — `artifact_id` custom_sql branch.** *Traces:* DEC-012. *Files:* `_common/artifact_id.py`,
  parity tests. *Depends:* US-003.

- **US-010 — Diff: `proposed_test_files` + emitter + renderers + sidecar.** *Traces:* DEC-011.
  *Files:* `diff/models.py`, `diff/_emitter.py`, `diff/_renderers/*`, `diff/_sidecar.py`,
  drift fixtures. *Depends:* US-003, US-009. *TDD:* new-file hunk render, sidecar round-trip.

- **US-011 — Filename-safety builder + fail-closed `.sql` writer.** Injection-safe
  `anchor_to_filename`; fail-closed writer (O_TRUNC, short-write loop, fsync, no except around
  write) extending `_FAIL_CLOSED_WRITER_MODULES`; typed write errors. *Traces:* DEC-010, DEC-014.
  *Files:* new `cli/_sql_writer.py` (or `diff/_test_file_writer.py`), `errors.py`,
  `tests/test_audit_completeness.py` (scan 8 module list). *Depends:* none. *TDD:* path-escape
  rejection, perms 0o600, parent mkdir, no-except AST shape.

- **US-012 — CLI `generate --write` writes `.sql` + `--force`.** Header marker; refuse-overwrite
  without `--force`; exit-code registration; 5-surface parity test. *Traces:* DEC-010, DEC-014.
  *Files:* `cli/generate.py`, `cli/_helpers.py`, `docs/cli-ops.md`, parity test. *Depends:* US-010, US-011.

- **US-013 — Ingest `tests/*.sql` reader.** Enumerate, resolve refs, associate to model, dedupe;
  unsupported-Jinja → `SkippedTest`; fixtures under `tests/fixtures/ingest/custom_sql_files/`.
  *Traces:* DEC-013. *Files:* `ingest/reader.py`, `ingest/parser.py`, `ingest/__init__.py`,
  fixtures. *Depends:* US-002, US-003. *TDD:* ref-extraction, association, dedupe, skip-record.

- **US-014 — `prune-existing` wiring (read-only).** Merge schema.yml + `tests/*.sql` for the
  positional model; full-scan multi-table; diff + sidecar only (no `--write`); docs. *Traces:*
  DEC-010, DEC-013. *Files:* `cli/prune_existing.py`, `docs/cli-ops.md`. *Depends:* US-013, US-008.

- **US-015 — e2e gated test.** dbt fixture with `meta.signalforge.business_rules` + an engineered
  always-pass custom test (`WHERE 1=0`) and a finds-failures one (engineered violation); assert
  kept/dropped counts under the `e2e` marker. *Traces:* DEC-009. *Files:* `tests/cli/test_e2e_*.py`,
  `tests/fixtures/dbt_project_*`. *Depends:* US-012, US-014.

- **US-016 — Documentation (operator-facing).** Update the user-facing docs to reflect
  business-rule test generation end to end: `docs/draft-ops.md` (the new test type + how
  `meta.signalforge.business_rules` is authored, NL + list shapes, inferred fallback),
  `docs/prune-ops.md` (full-scan-vs-sample behaviour for multi-table tests, the bytes-cap
  tuning note + over-cap → kept-without-evidence, expected-drop-rate framing), `docs/diff-ops.md`
  (the new `proposed_test_files` artifact + `.sql` new-file hunks), `docs/ingest-ops.md` (reading
  `tests/*.sql`, ref/source/this resolution, unsupported-Jinja skip), `docs/cli-ops.md`
  (`generate --write` writing `.sql` + the `--force` flag, exit codes, stderr shapes; the
  `prune-existing` read-only behaviour with singular tests), `README.md` (Expected output /
  Trying-it-out walkthrough showing a business-rule test drafted → pruned → written), and the
  MkDocs `nav:` if any new doc page lands (per `docs-publishing.md`). Worked example of a
  `meta.signalforge.business_rules` rule → generated `.sql` → kept/dropped decision.
  *Traces:* DEC-001, DEC-006, DEC-010, DEC-011, DEC-013. *Files:* `docs/*-ops.md`, `README.md`,
  `mkdocs.yml` (if nav changes). *Depends:* US-015 (documents final shipped behaviour).

- **US-017 — Quality Gate.** Code reviewer ×4 (fix each pass) + CodeRabbit; full validation green;
  doc build green (`uv run --only-group docs mkdocs build`). *Depends:* all implementation stories + US-016.

- **US-018 — Patterns & Memory (priority 99).** New `.claude/rules/business-rule-tests.md` (or
  extend llm-drafter / prune-engine / diff / ingest rules); CLAUDE.md public-API surface; 5-surface
  parity confirmation for the new test type; record any non-obvious patterns in memory. (Operator-
  facing ops docs are owned by US-016; this story owns internal conventions + the rules corpus.)
  *Depends:* US-017.

### Rules-compliance gate (validated against `.claude/rules/`)

- `DropReason` stays 5-value (US-007/008) ✓ — conservative-bias routing, locked `why` strings.
- New read-back model paired with `Strict*` drift detector + fixture (US-003) ✓.
- New typed errors registered in `_EXCEPTION_TO_EXIT_CODE`; scan 7 green (US-002/011/014) ✓.
- Fail-closed `.sql` writer mirrors the sidecar template; scan-8 module list extended (US-011) ✓.
- `_PROMPT_VERSION` rotation + cache-stability snapshot (US-005) ✓.
- Logger grep-gate: ingest stays silent; `.sql` writer logs via lazy-format JSON (US-011/012) ✓.
- 5-surface parity for the new test type + the `--force` flag (US-012/017) ✓.
- Dialect-driven compile, no BigQuery-isms in `prune/` core (US-007) ✓.

## Beads Manifest

_(Phase 7)_
