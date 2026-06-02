# Safety layer (PII redaction + audit log)

Established by issue #4 (PII safety layer). Apply to every module under `signalforge.safety` and to any new code that constructs an `LLMRequest`, redacts data heading to an LLM, or writes an audit record.

The safety layer sits between the warehouse adapter (#3) and the LLM-drafting pipeline (#5). It enforces SignalForge's deployment-blocker safety posture: schema-only by default, sample is explicit opt-in, every LLM call leaves a durable audit receipt.

## Fail-closed audit semantics (DEC-011)

Any exception inside `audit.write` propagates as `AuditWriteError` from `build_llm_request`. The function never returns an `LLMRequest` whose audit record didn't durably hit disk.

- `audit.write` opens with `O_APPEND | O_CREAT | 0o600`, writes one JSONL line **per chunk** (see § "Audit chunking (issue #185)" below — a logical event is 1 line when it fits ≤4000 B, otherwise N ≥ 2 chunks correlated by `audit_id`), calls `os.fsync` after each chunk, closes. Catches **no** exceptions internally — `OSError` / `PermissionError` / `IOError` / encoding failures all propagate.
- Size cap (`_AUDIT_RECORD_LIMIT_BYTES = 4000`) is checked **before** any file open for EVERY chunk; if any chunk would over-cap, the writer raises `AuditRecordTooLargeError` and leaves no on-disk artefact. The 4000 B cap stays load-bearing for `PIPE_BUF` atomic-append; reach to wide-table models is extended by symbol-table compression + chunking, not by raising the cap.
- `build_llm_request` calls `audit.write` AFTER constructing the request but BEFORE returning it. If audit fails, the partial request is dropped.

An unaudited LLM call is, by definition, PII leaving the warehouse without a receipt — exactly the failure mode this layer exists to prevent. The propagation IS the defence.

**Primary-work fail-closed vs. cleanup-boundary fail-soft.** This pattern propagates because the audit write is part of the unit of work — the unit either fully succeeds or fully aborts. Contrast with `warehouse-adapters.md` DEC-013/DEC-014, where cleanup fires AFTER the user's work has succeeded and blocking on cleanup failure would punish the operator for housekeeping they can't fix mid-flight; cleanup paths swallow + emit an operator-actionable WARNING instead. Don't conflate the two when introducing a new boundary in v0.3.

## Column NAMES leak PII too — redact them with stable hashes (DEC-010)

A column named `customer_ssn` or `john_smith_email` leaks PII via the name itself, even when no values are sampled. The layer redacts NAMES in `schema-only` and `aggregate-only` modes, not just values in `sample` mode.

The hash is `f"col_{blake2b(name.encode(), digest_size=4).hexdigest()}"` — 8 hex chars, deterministic per name, computed once and recorded in `RedactionRecord.hashed_name`. The (real → hashed) mapping is persisted to the audit JSONL so a reviewer can map back; the LLM only ever sees the hash.

When adding a new mode or surface, make sure the LLM-bound payload uses hashed names for every column where `RedactionRecord.redacted is True`.

## AuditEvent reproducibility fields (DEC-014)

Every `AuditEvent` carries three fields that look minor but are load-bearing:

- `signalforge_version: str` — read from `signalforge.__version__` at write time.
- `policy_hash: str` — 16-hex `blake2b(digest_size=8)` of the resolved `SafetyPolicy.model_dump_json` (sorted keys, canonical form via `_compute_policy_hash`). Migrated from `SHA-256[:16]` by issue #55 so the audit corpus reads one recipe across every writer (`safety.jsonl` / `llm_responses.jsonl` / `prune.jsonl` / `grade.jsonl` / `diff.json` all use `blake2b-8` over canonical JSON).
- `audit_schema_version: int` — frozen at the writer's `_AUDIT_SCHEMA_VERSION` constant; currently `4` (bumped 1→2 by #54 for `draft_skip_*` reasons, 2→3 by #55 for the `policy_hash` recipe change, 3→4 by #185 for the v4 symbol-table redaction shape + chunk-correlation triple). Typed `int` (not `Literal`) so older audit JSONLs still round-trip across version bumps — audit replay is a real requirement. **Note:** the #185 change dropped v3 read-back compatibility deliberately (library is pre-1.0, no corpus to migrate); the field remains `int` so future bumps stay round-trippable but the v3 `redactions: tuple[RedactionRecord, ...]` shape is gone from `AuditEvent` and the strict drift mirror.

The drift-detector test pairs production `AuditEvent` (`extra="ignore"`) with a one-off `StrictAuditEvent` (`extra="forbid"`) validated against the committed JSONL fixture. Adding a field to production without updating the strict model OR the fixture breaks the test loudly. As of #185 the fixture has three lines exercising all three legal v4 record shapes (non-chunked / chunk header / chunk continuation) and the strict mirror carries the same `@model_validator(mode="after")` shape rules that production enforces (see § "Audit chunking (issue #185)" below).

## Canonical timestamp shape across writers (issue #56)

Every audit/sidecar Pydantic model with a `timestamp: datetime` field renders through `signalforge._common.timestamp.iso8601_z` via `@field_serializer("timestamp")` — `YYYY-MM-DDTHH:MM:SS.ffffffZ`, microsecond precision, literal `Z` suffix, no `+00:00` form. The helper refuses naive datetimes (raises `ValueError`) and normalises non-UTC tz-aware inputs via `astimezone(UTC)` first.

Five surfaces ship with the serializer wired: `safety.AuditEvent`, `draft.LLMResponseEvent`, `prune.PruneEvent`, `grade.GradeEvent`, `grade.GradingReport`. Cross-writer byte-equal parity is pinned by `tests/_common/test_timestamp.py::test_cross_writer_timestamp_byte_parity`.

When a v0.3 writer ships a sixth audit-event type, do NOT lean on Pydantic's default datetime serialization — it emits `+00:00` instead of `Z` and the audit corpus reads two shapes. Wire the serializer at the model: `timestamp: datetime` + `@field_serializer("timestamp")` calling `iso8601_z`, and extend the cross-writer parity test to a sixth assertion in lockstep.

## Config-shaped models use `extra="forbid"`; read-back models use `extra="ignore"` (DEC-015)

The default in `manifest-readers.md` is `extra="ignore"` for forward-compat. The safety layer **deliberately overrides** for config files:

- `SafetyPolicy`, `_SafetyPolicyContent`, `_SafetyConfigFile` → `extra="forbid"`. A typo like `redacts:` (vs `redact:`) in a user-authored YAML file MUST fail loud — silent no-op is exactly the failure mode this ticket exists to prevent.
- `AuditEvent`, `RedactionRecord`, `LLMRequest` → `extra="ignore"`. Read back from JSONL files / consumed by downstream stages where forward-compat matters.

Pair every `extra="ignore"` reader-shaped model with a one-off `extra="forbid"` drift detector.

## Draft-skip vs. PII opt-out — semantics differ (issue #54)

Issue #54 added two new `RedactionReason` literals (`draft_skip_column_meta`, `draft_skip_model_meta`) driven by `meta.signalforge.skip_draft: true` at column or model level. They carry **different semantics** than the seven PII reasons:

- **PII reason** = "send a hashed placeholder in place of the real column name."
- **Draft-skip reason** = "omit the column entirely from the LLM payload" — not in `LLMRequest.columns_sent` / `.schema` / `.aggregates` / `.sampled_rows`. The `RedactionRecord` still rides on the `AuditEvent` so the operator-chosen omission is durably recorded.

Two contracts operationalise this:

1. **`DRAFT_SKIP_REASONS` frozenset** in `signalforge.safety.models` is the canonical "which reasons trigger omit-entirely" gate. `build_llm_request` reads it to compute `skipped_columns`. Adding a future omit-entirely reason MUST extend both `RedactionReason` AND `DRAFT_SKIP_REASONS` in lockstep — otherwise consumers silently leak the new reason's column into the prompt.
2. **Skip checks run BEFORE PII checks in `_classify_column`.** A column with both `skip_draft: true` and `tags: [pii]` routes to the skip reason; column-level skip beats model-level so the audit reason names the most-specific source.

**Strict `is True` check, not truthy.** `meta.signalforge.skip_draft` only fires on explicit Python `True` — `"true"` / `"yes"` / `1` are ignored. Mirrors `meta.signalforge.sample is False`: config noise must not silently engage a security-adjacent behaviour.

`audit_schema_version` bumped 1 → 2 in lockstep (the set of allowed `RedactionReason` literals widened). Field stays `int` for replay across versions.

## The four opt-out signals + precedence (DEC-003)

`_classify_column` returns `RedactionRecord | None` based on (in precedence order, first match wins, column-level always beats model-level when both fire):

1. Column-level `meta.signalforge.sample == False` → `column_meta_optout`
2. Column-level `tags: ["pii"]` (case-insensitive) → `tag_pii_column`
3. Column-level `meta.contains_pii` truthy → `meta_contains_pii_column`
4. Model-level `meta.signalforge.sample == False` → `model_meta_optout`
5. Model-level `tags: ["pii"]` → `tag_pii_model`
6. Model-level `meta.contains_pii` truthy → `meta_contains_pii_model`
7. Pattern match against `policy.redact_patterns` (case-insensitive `fnmatch`) → `pattern_match`

Every coercion path emits a DEBUG log when it normalises (e.g., `tags: [PII]` → lowercase). The seven reasons are a `Literal[...]` so audit-log consumers can pattern-match exhaustively. Don't add an eighth without updating both production `RedactionReason` and the drift detector.

## ANSI-safe lazy-format logger (DEC-022)

Same rule as the other layers (`llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017 / `grade-layer.md` DEC-029 / `diff-renderer.md` DEC-019). The grep gate at `tests/llm/test_logger_grep_gate.py` scans 6 dirs as of #9 (extends to `safety/` here) and rejects any `_LOGGER\.\w+\(f"` hit. Never f-string-interpolate user-controlled strings; JSON-encode in lazy-format `%s`.

## AST audit-completeness scan (DEC-020(a))

`tests/safety/test_public_api.py::test_llm_request_construction_only_in_request_module` scans every `.py` under `src/signalforge/safety/` (excluding `request.py`) for `Call(func=Name(id="LLMRequest"))` and rejects any hits. Construct `LLMRequest` only via `build_llm_request` — the docstring on `LLMRequest` says so, the AST scan enforces it.

If you add a new module that genuinely needs to construct an `LLMRequest` (e.g., a deserialiser for resumption), update the exclusion list AND document the audit-write seam. Don't suppress the test.

## Pydantic field-name shadow of `BaseModel` — scope the suppression, don't rename (issue #93)

`LLMRequest.schema: tuple[tuple[str, str], ...]` deliberately shadows Pydantic v1's deprecated `BaseModel.schema()` method. The field name is part of the audit-log contract (DEC-014); renaming would break `safety.jsonl` consumers, and a Pydantic `Field(alias="schema")` would change the Python attribute name out from under every internal caller of `request.schema`.

Pydantic emits a `UserWarning` at class-creation time for this kind of shadow. The fix is `warnings.catch_warnings()` around the class definition with a targeted `filterwarnings("ignore", message=r'Field name "schema".*shadows.*', category=UserWarning)`. Three rules:

1. **Scope to the class definition, not the module / process.** A bare `warnings.filterwarnings(...)` at module top-level permanently mutates the global filter list and silences unrelated future warnings. `catch_warnings()` is a context manager that restores the prior filter state on exit.
2. **Pin the message regex.** A category-only filter (`category=UserWarning`) is too broad. The `message=r'...'` anchor catches the specific Pydantic shadow warning and nothing else.
3. **Keep the existing `pyright: ignore[reportIncompatibleMethodOverride]` on the field.** Pyright's structural-override complaint is a separate signal from Pydantic's runtime UserWarning; both need their own suppression.

Regression test: `tests/safety/test_models.py::test_importing_safety_models_emits_no_userwarning` shells out a subprocess with `-W error::UserWarning` and asserts exit 0. Subprocess (not in-process `importlib.reload`) because the parent process imports `signalforge.safety.models` via the test collector before any in-test code runs.

When a future Pydantic field-name shadow surfaces (an audit-log contract is the same kind of "renaming would break consumers" pin), match this shape verbatim — don't reach for a global filter, and don't rename the field.

## Audit chunking (issue #185)

Wide-table dbt models (~66+ columns) produce audit events whose serialised byte size exceeds the 4000 B `PIPE_BUF` cap even after the v4 symbol-table compression (DEC-014 history). The cap stays — it's load-bearing for atomic concurrent appends. Instead, `audit.write` chunks the event across multiple ≤4000 B JSONL lines correlated by a deterministic `audit_id`.

### v4 redaction shape (issue #185)

The v3 `AuditEvent.redactions: tuple[RedactionRecord, ...]` field is GONE. Two new fields replace it:

- `redactions_by_reason: dict[RedactionReason, tuple[str, ...]] | None` — keyed by reason; values are sorted tuples of hashed names for deterministic snapshot stability.
- `column_name_map: dict[str, str] | None` — every hashed name appearing in any `redactions_by_reason` value has a `{hashed_name: real_column_name}` entry, so a reviewer can map back.

`RedactionRecord` the class still exists in `signalforge.safety.models` — it's used internally by `build_llm_request` during column classification. The fold from `tuple[RedactionRecord, ...]` to the two-dict shape happens at AuditEvent construction time. The custom `AuditEvent.__repr__` omits `column_name_map` per DEC-022 — the real column names are potentially PII-bearing.

### Layout

A chunked event is N ≥ 2 JSONL lines. The Pydantic `@model_validator(mode="after")` on `AuditEvent` enforces the three legal shapes:

- **Non-chunked** (`audit_id=None, chunk_index=None, chunk_count=None`): all metadata required; `redactions_by_reason` + `column_name_map` carry the full payload.
- **Chunk header** (`audit_id=<16-hex>, chunk_index=0, chunk_count=N≥2`): all metadata required; `redactions_by_reason={}` and `column_name_map={}` are empty.
- **Chunk continuation** (`audit_id` matches header, `chunk_index ∈ {1, …, N-1}`, `chunk_count=N`): metadata fields are `None`; `redactions_by_reason` and `column_name_map` carry slices.

`audit_id = blake2b(model_unique_id + "\x00" + timestamp.isoformat() + "\x00" + signalforge_version, digest_size=8).hexdigest()` — deterministic across runs of the same event.

### Writer contract

`audit.write` pre-computes all chunks in memory, then verifies EACH chunk ≤ `_AUDIT_RECORD_LIMIT_BYTES` BEFORE `os.open`. If any chunk would over-cap (pathological — e.g., a single column name > 4000 chars), raises `AuditRecordTooLargeError` with no on-disk artefact (fail-closed preserved). Otherwise: `mkdir -p` → `os.open(O_APPEND | O_CREAT | O_WRONLY, 0o600)` → single `try / finally` (close-only) → for-loop emitting each chunk + per-chunk `os.fsync`. Header writes FIRST so a mid-write crash leaves a parseable "header + < N chunks" partial that the reader can detect.

Scan 8 (`_FAIL_CLOSED_WRITER_MODULES`) is preserved — the chunk loop lives inside ONE `Try` block; individual `os.write` and `os.fsync` calls remain unguarded. When extending this pattern to a future stage's fail-closed multi-line writer, mirror exactly: pre-compute all chunks, size-check every chunk pre-open, single `Try` wrapping the per-chunk write+fsync loop.

### Reader contract — `read_audit_events(path)`

Returns `Iterator[AuditEvent]`. Non-chunked rows pass through unchanged. Chunked rows accumulate by `audit_id`; when chunk count is reached, the helper merges all chunks' `redactions_by_reason` (key-wise concat, then re-sort the tuples for determinism) + `column_name_map` (dict update), takes metadata from the header, **clears the chunk-correlation triple to `None`** so the reassembled event round-trips through the non-chunked validator branch, and yields the result. End-of-stream incomplete groups surface a `WARNING` via the standard ANSI-safe lazy-format JSON logger and are skipped — never raise (read-back path is fail-soft, distinct from the fail-closed write path).

The reader rejects adversarial / corrupt chunks where `chunk_index < 0` or `chunk_index >= chunk_count` with a one-line `audit chunk out-of-range` WARNING and skips the offending chunk. Without this guard the accumulator's length-only completion check could silently drop a genuine missing chunk on reassembly (the writer never produces out-of-range indices; the threat model is file corruption or external tampering). Pre-#185 v3 records (`redactions: tuple[RedactionRecord, ...]` shape) raise `pydantic.ValidationError` from `AuditEvent.model_validate` rather than skip — the drop-v3 design choice per DEC-005; documented in the reader's docstring.

### Empirical ceilings (operator-visible)

Measured against the shipped `_chunk_event` implementation, NOT the plan's optimistic redactions-only estimates:

- **Single-line ceiling:** ~65 columns. `columns_sent` (the full column-name tuple) ships in the header chunk, eating most of the 4000 B budget — the single-line case has no `audit_id`/`chunk_index`/`chunk_count` overhead but `columns_sent` alone scales as ~22 B per column.
- **Chunkable range:** ~66 → ~243 columns. Within this range the writer succeeds via chunking; a 170-col model produces ~3 chunks.
- **Hard ceiling:** ~243 columns. Beyond that, the header chunk alone overflows the cap (because `columns_sent` itself becomes oversize, independent of `redactions_by_reason`) and `AuditRecordTooLargeError` is raised. Operator workaround: `meta.signalforge.skip_draft: true` on noise columns to bring the model under the ceiling — this also drops the column from `columns_sent`, which is the binding constraint.

The plan's design-time single-line estimate at 170 columns (3,865 B) measured `redactions_by_reason` alone; including `columns_sent` + metadata, the real ceiling is much lower. If a future ticket reshapes the header to omit or slice `columns_sent` across continuation chunks (reassembled at read time from `column_name_map`), the ceilings move up substantially — out of scope for #185 because reshaping touches every downstream consumer of the audit corpus.

### Anti-pattern: `safety.mode: aggregate-only`

`aggregate-only` mode does NOT shrink `redactions_by_reason`. All three sampling modes (`schema-only`, `aggregate-only`, `sample`) build the same redaction set in `build_llm_request` — `aggregate-only` only changes what's sent to the LLM, not what's recorded in the audit. Don't suggest it as a workaround for the wide-table cap. The `AuditRecordTooLargeError` remediation text explicitly closes this misleading hint (locked verbatim by `tests/safety/test_errors.py`).

### `AuditRecordTooLargeError` parametric remediation

Signature: `__init__(self, size: int, limit: int, column_count: int | None = None, *, remediation: str | None = None)`. `column_count` is the exact redacted-column count derived from `len(event.column_name_map)` at the writer's raise site — every entry in any `redactions_by_reason` value list also appears in `column_name_map` by construction in `request.py`, so the map size is the single source of truth (early QG draft double-counted both surfaces and was corrected before merge).

Default remediation is a three-sentence operator script: (1) names the column count + bytes over cap when `column_count is not None`; (2) suggests `meta.signalforge.skip_draft: true` on noise columns and clarifies it's distinct from PII opt-out; (3) explicitly closes the `safety.mode: aggregate-only` anti-pattern (the mode does NOT shrink the redactions surface) and points at the `columns_sent` roadmap in `docs/safety-ops.md`. Locked verbatim — pinned by `tests/safety/test_errors.py::test_audit_record_too_large_default_remediation_locked` (parametrized `None` + `170`).

Stays CLI tier 3 in `_EXCEPTION_TO_EXIT_CODE` — error semantics post-#185 are "compression + chunking attempted, recovery exhausted" rather than "input shape problem," which aligns with the external-dependency tier.

## Fail-closed writer shape — Scan 8 covers all five writers (issue #38)

`tests/test_audit_completeness.py::test_fail_closed_writers_have_no_except_around_write_fsync` and `test_fail_closed_writers_use_short_write_loop` are the eighth AST scan in the project. They walk every fail-closed writer module — `signalforge.{safety,draft,prune,grade}.audit` and `signalforge.diff._sidecar` — and assert: (a) no `except` handler may wrap a `Try` whose body issues `os.write` / `os.fsync` (only `try / finally` around `os.close(fd)` is permitted); (b) every writer function uses a `while` loop around `os.write` so short writes (`EINTR`, pathological short returns) don't produce partial JSONL records.

The typed-error wrap (`AuditWriteError`, `LLMResponseAuditWriteError`, `PruneAuditWriteError`, `GradeAuditWriteError`, `DiffSidecarWriteError`) lives at the orchestrator boundary, not inside the writer. `AuditRecordTooLargeError` (and its layer-specific cousins) raises from inside the writer — pre-open, so no on-disk artefact — and propagates as-is.

When a v0.3 stage ships a sixth fail-closed writer, extend `_FAIL_CLOSED_WRITER_MODULES` in the scan. The writer module must mirror the prune/grade/diff template verbatim: serialise → size-check → `mkdir -p` → `os.open(O_APPEND | O_CREAT | O_WRONLY, 0o600)` → short-write `while` loop → `os.fsync` → close. The orchestrator owns the typed wrap.

## Pydantic v2 `with_mode` re-runs validators (DEC-018)

`SafetyPolicy.with_mode(mode)` does NOT use `model_copy(update=...)` — that path silently skips `@model_validator(mode="after")`, which would silently enable sample mode without emitting the DEC-021 WARNING.

Use `model_validate({**self.model_dump(), "mode": mode})` so every validator re-runs. `model_copy` is the wrong tool any time a validator side-effect is part of the contract — apply the same rule to any future "factory that produces a mutated copy" helpers.

## `signalforge.yml` top-level namespace: `safety:` (DEC-025)

The config file's top level is `{ safety: { ... } }`. Other top-level keys (`llm:`, `prune:`, `grade:`, ...) are reserved for future stages and silently ignored by the safety loader. Each stage validates its own subtree independently. `SafetyPolicy` uses `extra="forbid"`; the wrapping `_SafetyConfigFile` uses `extra="ignore"` at the top level. Mirrors the same pattern across all five pipeline-stage configs.

## Reference

`plans/super/4-pii-safety.md` — DEC-001 … DEC-026. `plans/super/185-wide-table-audit.md` — DEC-001 … DEC-008 (v4 symbol-table compression + chunking; drop-v3 pre-1.0 simplification). `src/signalforge/safety/` — current implementation. `tests/safety/_fake_adapter.py` — `FakeAdapter` + `expect_*` API. `docs/safety-ops.md` — operational reference. `tests/fixtures/safety/manifest_with_pii_meta.json` — fixture exercising all four opt-out signals. `tests/safety/test_wide_table.py` — wide-table integration tests pinning the chunking ceilings.
