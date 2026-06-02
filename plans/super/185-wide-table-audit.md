# #185 — Wide-table audit-record size cap

> **Meta**
> - **Ticket:** [#185](https://github.com/wjduenow/SignalForge/issues/185) — `safety: AuditRecordTooLargeError blocks generate on wide-table models (170+ columns)`
> - **Branch:** `feature/185-wide-table-audit` (off `origin/dev`)
> - **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/185-wide-table-audit`
> - **Phase:** devolved
> - **Plan PR:** [#192](https://github.com/wjduenow/SignalForge/pull/192) (draft, targets `dev`)
> - **Plan started:** 2026-06-02
> - **Tracks follow-on of:** #179 (empirical retest, PR #182)

## Background

[#179 retest](docs/research/179-test-primitive-expansion-retest.md) ran `signalforge generate` against the `intuit_airflow` substrate (Airflow + dbt 1.8 + Snowflake) and hit a hard failure on `raw/datashare_googlead.sql` (170 columns):

```
datashare_googlead   row_count_between   generate-failed
ERROR: Audit record size 4519 exceeds atomic-append limit 4000.
  ↳ Remediation: Audit records must stay under 4000 bytes for atomic concurrent appends;
    reduce columns_sent or redactions count.
```

`safety.audit.write_audit_event` correctly raised `AuditRecordTooLargeError` BEFORE any file open, per the fail-closed contract documented in `safety-layer.md` DEC-011 — the failure mode is itself correct. But the operator cannot make `generate` succeed without restructuring their model, and the remediation text ("reduce columns_sent or redactions count") is not actionable: the data layer is the data layer.

The retest writeup frames this as **a real product limitation**, not an edge case — operators with wide-table dbt projects (CDC unions, BI rollups) will routinely hit it.

## Why the cap exists (load-bearing)

`safety-layer.md` DEC-011: `_AUDIT_RECORD_LIMIT_BYTES = 4000` is checked **before** any file open. The cap is load-bearing for atomic concurrent appends — the OS-level `PIPE_BUF` floor (4096 on Linux) guarantees a single-syscall `write` of ≤4096 bytes is atomic across concurrent JSONL writers. Raising the cap above `PIPE_BUF` would break the atomicity contract the safety layer exists to enforce.

The 96-byte margin (4096 − 4000) accommodates the trailing newline + small per-record overhead so the write stays under `PIPE_BUF` even at the cap.

## Scope

**In scope:**
- Make `signalforge generate` succeed on wide-table models (≥170 columns) without restructuring the model or raising the per-line size cap.
- Preserve every load-bearing invariant in `safety-layer.md`: fail-closed audit (propagation IS the defence), size cap before any file open, `O_APPEND | O_CREAT | 0o600`, single short-write loop, fsync, no `except` around write/fsync, atomic concurrent appends under `PIPE_BUF`.
- Update the operator-facing remediation text to be actionable.
- Update drift detector + committed audit fixture + ops doc + the 8th AST scan's `_FAIL_CLOSED_WRITER_MODULES` if any new writer ships.

**Out of scope:**
- Multi-process audit writers (the cap is currently sufficient for the single-process invariant; multi-process is a v0.3+ concern).
- Cross-stage audit-record cap changes (`draft`/`prune`/`grade`/`diff` writers also use 4000 bytes; same rationale; no change in this ticket).
- A general audit-replay tool (only test code reads `safety.jsonl` today).

## Architectural commitments to preserve

(From `CLAUDE.md` and `.claude/rules/safety-layer.md`.)

1. **Fail-closed audit.** An unaudited LLM call is PII leaving the warehouse without a receipt; propagation of any write failure IS the defence. Catches **no** exceptions inside the writer.
2. **Size cap before any file open.** Oversize raises `AuditRecordTooLargeError` BEFORE `os.open`. No partial / orphaned on-disk artifact ever.
3. **Atomic single-line append (`PIPE_BUF` floor).** Every JSONL record stays ≤4096 bytes including newline — single `os.write` (looped on short returns) is atomic across concurrent writers.
4. **Forward-compat read-back.** `AuditEvent` / `RedactionRecord` use `extra="ignore"`; paired with a one-off `extra="forbid"` drift detector + committed fixture. `audit_schema_version: int` (not `Literal`) preserves version round-trip.
5. **Explainable diffs.** Every redacted column still has a durable receipt; the (real → hashed) mapping survives whatever restructure we pick so a reviewer can map back.

## Discovery findings (Phase 1)

### The failure shape

`AuditEvent` carries `redactions: tuple[RedactionRecord, ...]` (`src/signalforge/safety/models.py:106-143`). Each `RedactionRecord` carries `column_name`, `hashed_name`, `redacted: bool`, `reason: RedactionReason` (closed Literal of 9 values).

On a 170-column model, `redactions` is the payload bloat — the writeup measured 4519 bytes for one record. The other `AuditEvent` fields (timestamp, model_unique_id, mode, columns_sent, row_count, signalforge_version, policy_hash, audit_schema_version, policy_flags) are bounded.

**Per-record byte budget (rough):**
- `AuditEvent` headers/identity: ~250 bytes (timestamp, model_unique_id, mode, version fields, policy_hash, policy_flags).
- `columns_sent` (tuple of strings): ~10–25 bytes per column × N columns.
- `redactions` (tuple of `RedactionRecord`): ~80–120 bytes per record × redacted-column count.

On 170 columns with most/all redacted under `schema-only`, `redactions` alone is ~14–20 KB worth of JSON. The 4000-byte cap will never accommodate per-column records on wide tables in their current shape.

### Three solution paths (issue text)

| Option | Mechanic | audit_schema_version bump | Effort | Notes |
|---|---|---|---|---|
| **1: chunk** | Split a wide event into a header line + ≥1 follow-up lines, each ≤4000 B. Per-line `run_id` correlates. | 3 → 4 | High (chunked reader; per-chunk correlation; harder drift detector) | Survives arbitrarily wide tables; multi-line ordering becomes a contract surface. |
| **2: compress** | Symbol-table form: `{reason_id: [col_hash, …]}` instead of `[{col, reason}, …]`. | 3 → 4 | Medium (new shape; one-pass build/decode; same reader surface) | Fits ~170-col schema-only cleanly; may still over-cap on hyper-wide (1000+ col) tables. |
| **3: raise cap** | Bump `_AUDIT_RECORD_LIMIT_BYTES` > 4000. | — | Low | **Blocked** — breaks `PIPE_BUF` atomic-append. Rejected by `safety-layer.md` DEC-011. |

The issue text's recommended path: **Option 2 first, fall back to Option 1 if 2 turns out insufficient.**

### Existing surfaces that move in lockstep

From the convention sweep (full report in session notes). The big buckets:

- **Production models & constants:** `safety/models.py` (`AuditEvent`, `RedactionRecord`, `_AUDIT_SCHEMA_VERSION`); `safety/request.py` (build seam); `safety/audit.py` (writer); `safety/errors.py` (`AuditRecordTooLargeError.default_remediation`).
- **Drift detector:** `tests/safety/test_drift_detector.py` (`StrictAuditEvent`, `StrictRedactionRecord`); `tests/fixtures/safety/audit_events_sample.jsonl`.
- **AST scans:** `tests/test_audit_completeness.py` Scan 2 (`AuditEvent` construction confinement); Scan 8 (fail-closed writer shape; six writer modules currently listed).
- **CLI exit codes:** `tests/cli/test_exit_codes.py` / `signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` — `AuditRecordTooLargeError` stays tier 3.
- **Docs:** `docs/safety-ops.md` (audit shape section); `CHANGELOG.md` Unreleased § Changed.
- **Tests:** `tests/safety/test_audit.py` (oversize gate); `tests/safety/test_request.py` (propagation); a new wide-table integration test (170-col synthetic manifest) to pin the fix.

### What does NOT need to change

- The 4000-byte cap (load-bearing under DEC-011).
- The fail-closed writer shape (DEC-011, scan 8).
- The AST construction confinement (DEC-020(a), scan 2).
- Cross-stage audit writers (`draft`/`prune`/`grade`/`diff`) — separate corpora, separate sizes; this ticket scopes to safety only.
- The CLI tier-3 exit code mapping for `AuditRecordTooLargeError`.

### Key file refs

- `src/signalforge/safety/models.py:87-104` — `RedactionRecord` (4 fields, frozen, extra="ignore").
- `src/signalforge/safety/models.py:106-143` — `AuditEvent` (9 fields, `audit_schema_version: int = 3`).
- `src/signalforge/safety/audit.py:59` — `_AUDIT_RECORD_LIMIT_BYTES: Final[int] = 4000`.
- `src/signalforge/safety/audit.py:62-165` — `write` (serialize → size-check → mkdir → O_APPEND open → write-loop → fsync → close, no except).
- `src/signalforge/safety/errors.py:191-214` — `AuditRecordTooLargeError`.
- `src/signalforge/safety/request.py:167-231` — `build_llm_request` (classifies columns once, dispatches per-mode, constructs AuditEvent with same `redactions` tuple in all modes).
- `src/signalforge/safety/policy.py:250-271` — `_compute_policy_hash` (canonical-JSON blake2b-8).
- `tests/safety/test_audit.py:162-183` — oversize gate tests.
- `tests/test_audit_completeness.py:367-413` — Scan 2 (AuditEvent confinement).
- `docs/research/179-test-primitive-expansion-retest.md:214,257-268,330-346` — failure narrative + remediation discussion.

### Mode interaction (aggregate-only)

Important nuance from the codebase scout: `safety.mode: aggregate-only` does NOT shrink `redactions` — all three modes (`schema-only`, `aggregate-only`, `sample`) build the same `redactions` tuple. The issue text suggested `aggregate-only` as a workaround; it isn't one for this failure mode. (See `src/signalforge/safety/request.py:167-195`.) The remediation text needs to reflect that.

## Scoping decisions (Phase 1 close)

| # | Question | Decision | Rationale |
|---|---|---|---|
| DEC-001 | Solution shape | **Both in one ticket** — compress redactions in-line AND chunk-when-still-oversize | Maximum reach in one ship; chunking is the safety net for the hyper-wide (1000+ col) tables compression alone won't cover; user explicitly accepts the bigger blast radius. |
| DEC-002 | Compression form | **Deferred to Phase 2 architecture review** | Pick after sizing candidate forms against a real 170-col fixture; symbol-table-by-reason vs single-field-omit-when-all-same is a measurable comparison, not a guess. |
| DEC-003 | Wide-table integration test fixture | **Two fixtures: boundary + wide** | Boundary fixture pins the exact regression line where the uncompressed audit crosses 4000 B; 200-col fixture pins the realistic operator-scale case. |
| DEC-004 | Remediation text for still-cap-hit case | **Combine column count + follow-up issue pointer** | Names the column count that pushed over the cap, suggests `meta.signalforge.skip_draft: true` on noise columns, AND points at the tracked chunking follow-up issue. Actionable for the operator + visible roadmap. |

## Architecture review (Phase 2 — complete)

Four parallel reviewers (data-model / fail-closed-writer / testing+UX / multi-surface-parity). Summary table:

| Area | Reviewer | Rating | Headline finding |
|---|---|---|---|
| Compression form (Option 2 shape) | data-model | **pass** | Symbol-table-by-reason (`{reason: [hashed_name, …]}`) compresses 75% on 170-col → 3,865 B; retains the (real → hashed) map via a sibling `column_name_map` dict. |
| Chunking shape (Option 1 layout) | data-model | **pass** | Header line (full event minus redactions, `chunk_index=0`, `chunk_count=N`) + N–1 chunk lines (correlation key only, redaction slice). |
| Schema-version round-trip | data-model | **pass** | `extra="ignore"` + `audit_schema_version: int` handles v3↔v4 either direction. |
| Drift detector v3/v4 coexistence | testing+UX | **blocker** | Strategy must be locked: one combined `StrictAuditEvent` with optional fields + post-validator vs. parallel `StrictAuditEventV3` + `StrictAuditEventV4`. |
| Atomic concurrent appends | fail-closed-writer | **pass with note** | Single-process invariant unchanged; chunks-from-same-event stay together (sequential in one thread); cross-event interleaving is acceptable + correlatable via `audit_id`. |
| Mid-write crash behaviour | fail-closed-writer | **pass** | **Write header FIRST**, chunks after — partial = "header + < N chunks", reader surfaces a WARNING. (Reviewer 2 proposed `is_final: bool` instead; resolved below.) |
| Size-check-before-open | fail-closed-writer | **pass** (with discipline) | Compute ALL chunks in-memory and verify each ≤4000 B BEFORE `os.open`. No partial on-disk artefact if any chunk over-cap. |
| `AuditRecordTooLargeError` semantics | fail-closed-writer | **pass** | Stay tier 3; one class, parametric remediation (carries `column_count`); no rename. |
| fsync semantics under chunking | fail-closed-writer | **pass** | Per-chunk fsync (each line independently durable); Scan 8 unchanged (one `Try` wraps the loop, individual `os.write`/`os.fsync` calls remain unguarded). |
| 8th AST scan (`_FAIL_CLOSED_WRITER_MODULES`) | multi-surface | **pass** | Currently 6 modules; rule file text says "covers all five writers (issue #38)" — *informational drift, not a contract bug*. Same `safety/audit.py` module gains chunking logic; module count unchanged unless we extract chunking into a sibling `_chunked_audit.py` (we won't — keep in `audit.py`). |
| Operator UX (stderr message + remediation) | testing+UX | **concern** | Message + remediation shape sound (names column count, suggests `skip_draft`, points at follow-up issue). Phase 3 must lock the **exact text** + verify `column_count` is passed from `audit.write` call site to the error constructor. |
| Boundary fixture engineering | testing+UX | **concern** | Synthetic in-test builder preferred over committed JSON; need to settle whether the boundary is "exact 4000 B in v3 uncompressed shape" or "exact 4000 B in v4 compressed shape." |
| Wide-table happy path | testing+UX | **pass** | 170-col → single-line v4 compressed; 500-col → multi-line chunked; both with reassembly roundtrip. |
| Pathological case (4000-char column name) | testing+UX | **pass** | One-line defensive test; the only path that fires `AuditRecordTooLargeError` post-#185. |
| Snapshot stability | testing+UX | **pass** | Single-process serial deterministic; no sort needed for unit tests; sort helper for any future concurrent test. |
| Multi-surface parity (~30 surfaces) | multi-surface | **pass** | Comprehensive checklist captured; loaded as the Phase 4 detailing input. |

Full reviewer output captured in session notes; condensed conclusions above.

### Blockers / open questions for Phase 3

| ID | Open question | Why it blocks detailing |
|---|---|---|
| OQ-1 | Drift detector strategy: single combined strict model vs parallel v3 + v4 strict models | Determines fixture shape, number of test files, and whether the v3 fixture stays unchanged. |
| OQ-2 | Compression form: symbol-table-by-reason vs single-field-omit-when-all-same vs other | Determines exact byte-budget math; reviewer A's measurement says symbol-table fits, but Phase 3 should sanity-check against a real 170-col build before locking. |
| OQ-3 | Chunk crash-detection: header-first + reader-warns-on-partial vs `is_final: bool` flag on every line | Two equivalent-strength options; affects header shape and reader code. |
| OQ-4 | Exact remediation-text wording for the (now pathological-only) `AuditRecordTooLargeError` | Operator-facing string; lock in Phase 3 so it doesn't churn during implementation. |
| OQ-5 | Boundary fixture: v3-shape boundary (engineered to catch regressions on the old path) vs v4-shape boundary (engineered against the new path) | Phase 3 must decide which boundary the test pins. |
| OQ-6 | Do we ship a config knob (`safety.audit_chunking_enabled: bool`) or chunking always-on? | Default-off would keep v3 audits byte-identical for existing operators; default-on means everyone gets v4 immediately. |

## Refinement (Phase 3 — complete)

| # | Question | Decision | Rationale |
|---|---|---|---|
| DEC-005 | Drift detector strategy for v3 ↔ v4 coexistence | **Drop v3 entirely** | User call: library is pre-1.0 and not yet adopted; the cost of carrying v3 compat (parallel strict mirrors, fixture-shape sprawl, field-aliasing) buys us nothing today. Single `StrictAuditEvent` validates v4 only; production `AuditEvent` carries the v4 shape only; old `.signalforge/safety.jsonl` files from prior runs round-trip via `extra="ignore"` (extra fields silently dropped; new fields absent → optional default `None`) but aren't tested against the strict mirror. `_AUDIT_SCHEMA_VERSION` still bumps 3 → 4 as a forward-compat marker for future readers. |
| DEC-006 | Compression form + chunk crash-detection | **Symbol-table-by-reason + header-first chunking** | Compress: `redactions_by_reason: dict[RedactionReason, tuple[str, ...]]` (keyed by reason; values = hashed names) + `column_name_map: dict[str, str]` sibling (hashed → real, preserves the reviewer mapback). Measured: 170-col → 3,865 B (135 B headroom under 4000 B cap). Chunk: header line (full event minus redactions, `chunk_index=0`, `chunk_count=N`) written FIRST + N–1 chunk lines (`audit_id` correlation + slice of `redactions_by_reason` + slice of `column_name_map`). Partial-on-disk shape = "header present, < N chunks" — operator-visible signal of incompleteness; reader surfaces a WARNING and skips. Crash-safer than `is_final` flag (no "all chunks but header missing" failure shape). |
| DEC-007 | `AuditRecordTooLargeError` remediation text | **Three-sentence operator script** | Stderr text names: (1) the column count + N bytes over the cap **after** compression + chunking were attempted; (2) actionable workaround (`meta.signalforge.skip_draft: true` on noise columns to drop them from the audit entirely — distinct from PII opt-out, per safety-layer.md DEC-003); (3) explicit "do NOT use `safety.mode: aggregate-only` — it does not shrink the redactions tuple" (closes the issue text's misleading suggestion); (4) follow-up issue pointer for any future deeper restructure. Concrete wording locked in Phase 4 detailing. |
| DEC-008 | Boundary fixture target + chunking opt-in knob | **v4-shape boundary, chunking always-on, no config knob** | Boundary fixture is a synthetic in-test builder that finds the exact column count where the v4-compressed shape first crosses 4000 B (i.e., where chunking activates). No `safety.audit_chunking_enabled: bool` — chunking is part of the contract, not an opt-in. Keeps `SafetyPolicy` shape unchanged; no new drift-mirror field; no docs/CLI/skill churn for a knob nobody needs default-off. |

### Cross-cutting clarifications from refinement

- **`audit_schema_version: int` stays an `int`, not a `Literal[4]`.** Per safety-layer.md DEC-014, the field is `int` so older audit JSONLs round-trip; this stays true even though we no longer write v3 records.
- **Dropping v3 means dropping `RedactionRecord` from `AuditEvent.redactions` field.** `RedactionRecord` the class can stay (it's still useful as an internal value object on the build path) — but the `AuditEvent` model no longer carries `redactions: tuple[RedactionRecord, ...]`. Replace with `redactions_by_reason` + `column_name_map`.
- **Skill-parity surface (`SKILL.md`) likely unchanged.** Chunking is internal to the audit-write path; the operator-facing CLI surface (subcommands, flags, demo commands) is unchanged. Verify in Phase 4 detailing by reading SKILL.md against the parity gate.

## Detailing (Phase 4 — complete)

### Pre-detailing design lock — one model vs two

`AuditEvent` evolves to carry **both** header rows and chunk-continuation rows in a **single** Pydantic class (Option A from architecture review). The header carries all existing metadata + the chunk-correlation triple `(audit_id, chunk_index=0, chunk_count=N)` + the **complete** `redactions_by_reason` + `column_name_map` when N=1, OR empty maps when N≥2. Continuation rows omit metadata fields (mark them `| None = None`) and carry only `(audit_id, chunk_index≥1, chunk_count, redactions_by_reason_slice, column_name_map_slice)`. A `@model_validator(mode="after")` enforces the shape rules:

- **Non-chunked** (`audit_id is None` AND `chunk_index is None` AND `chunk_count is None`): all metadata required; both `redactions_by_reason` and `column_name_map` present (may be empty).
- **Chunk header** (`audit_id` set, `chunk_index == 0`, `chunk_count ≥ 2`): all metadata required; `redactions_by_reason` and `column_name_map` are empty dicts.
- **Chunk continuation** (`audit_id` set, `chunk_index ≥ 1`, `chunk_count ≥ 2`, `chunk_index < chunk_count`): metadata fields are `None`; `redactions_by_reason` and `column_name_map` carry the slice.

Drift mirror: one `StrictAuditEvent(extra="forbid")` with the same validator. Single fixture file; mix of non-chunked + chunked (header + continuation) lines.

### Story breakdown

Natural ordering: models → build seam → write/read path → errors → drift mirror → tests → docs. Each story is right-sized for one Ralph context window.

| # | Story | Depends on |
|---|---|---|
| US-001 | `safety.models`: drop v3 `redactions`; add v4 fields + `@model_validator` shape rules | — |
| US-002 | `safety.request`: bump `_AUDIT_SCHEMA_VERSION` to 4; build v4-shape `AuditEvent` (symbol-table compression at construction) | US-001 |
| US-003 | `safety.audit`: pre-open chunker + multi-line writer + per-chunk fsync + reader helper | US-001 |
| US-004 | `safety.errors`: parametric `AuditRecordTooLargeError` (carries `column_count`; three-sentence remediation) | US-001 |
| US-005 | Drift detector + fixture regen (v4-only) | US-001, US-002, US-003 |
| US-006 | Wide-table integration tests (boundary + 170-col + 500-col + pathological) | US-002, US-003, US-005 |
| US-007 | Extend concurrent-write test for chunked events | US-003 |
| US-008 | Docs: `docs/safety-ops.md` v4 schema + CHANGELOG `[Unreleased]` | US-001 … US-005 |
| US-009 | Rule update: `.claude/rules/safety-layer.md` DEC-011 + DEC-014 history + new chunking subsection (**orchestrator-only edit** — workers can't Write `.claude/`) | US-008 |
| US-010 | **Quality Gate** — code-review 4 passes + CodeRabbit + validation green | US-001 … US-009 |
| US-011 | **Patterns & Memory** — memory updates + lessons | US-010 |

### Stories — detail

---

#### US-001 — `safety.models`: drop v3 redactions, add v4 fields, shape validator

**Traces to:** DEC-005, DEC-006

**Description:** Drop the `AuditEvent.redactions: tuple[RedactionRecord, ...]` field. Add four v4 fields: `redactions_by_reason: dict[RedactionReason, tuple[str, ...]] | None = None`, `column_name_map: dict[str, str] | None = None`, `audit_id: str | None = None`, `chunk_index: int | None = None`, `chunk_count: int | None = None`. Mark the existing metadata fields (`model_unique_id`, `mode`, `columns_sent`, `row_count`, `signalforge_version`, `policy_hash`, `policy_flags`, `timestamp`) `| None = None` to allow chunk-continuation rows. Add `@model_validator(mode="after")` enforcing the three shape rules (non-chunked / chunk-header / chunk-continuation). `RedactionRecord` class stays (still used on the build path inside `request.py` before folding to the symbol-table form).

**Acceptance criteria:**
- `AuditEvent` model lints + type-checks under pyright 3.11
- `redactions: tuple[RedactionRecord, ...]` field is REMOVED from `AuditEvent`
- New fields present with the exact types above
- `@model_validator` rejects: chunked + missing audit_id; chunk_index ≥ chunk_count; chunk-header with non-empty `redactions_by_reason`; chunk-continuation with non-None metadata; non-chunked with audit_id set
- Custom `__repr__` excludes `column_name_map` (potentially PII-bearing real names per safety-layer.md DEC-022 precedent — show only `audit_id`, counts, version)
- `uv run pytest tests/safety/test_models.py` passes
- Canonical validation (`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`) passes for the safety subpackage

**Done when:** Tests pass, type-check clean, validator covers all three shape branches.

**Files:** `src/signalforge/safety/models.py`, `tests/safety/test_models.py`

**TDD test cases (write first):**
- `test_audit_event_v4_non_chunked_shape_validates`
- `test_audit_event_v4_chunk_header_with_empty_redactions_validates`
- `test_audit_event_v4_chunk_continuation_with_none_metadata_validates`
- `test_audit_event_chunked_missing_audit_id_raises`
- `test_audit_event_chunk_index_at_or_above_count_raises`
- `test_audit_event_non_chunked_with_audit_id_raises`
- `test_audit_event_chunk_header_with_nonempty_redactions_raises`
- `test_audit_event_chunk_continuation_with_metadata_raises`
- `test_audit_event_repr_omits_column_name_map`
- `test_audit_event_v3_redactions_field_removed` (assert `redactions` not in `AuditEvent.model_fields`)

---

#### US-002 — `safety.request`: build v4 audit event (symbol-table compression)

**Traces to:** DEC-005, DEC-006

**Description:** Bump `_AUDIT_SCHEMA_VERSION` from 3 to 4. In `build_llm_request`, after column classification builds the `RedactionRecord` list, fold it into `redactions_by_reason` (`dict[RedactionReason, tuple[hashed_name, ...]]`) + `column_name_map` (`dict[hashed_name, real_column_name]`). Construct `AuditEvent` with those + `audit_id=None, chunk_index=None, chunk_count=None` (non-chunked path; the writer decides whether to chunk based on serialised size). The internal classification still produces `RedactionRecord` objects — only the `AuditEvent` shape changes.

**Acceptance criteria:**
- `_AUDIT_SCHEMA_VERSION` is `4`
- `build_llm_request` no longer constructs an `AuditEvent` with `redactions: tuple[RedactionRecord, ...]`
- `redactions_by_reason` keys are valid `RedactionReason` literals; values are sorted tuples of hashed names (deterministic ordering for snapshot stability)
- `column_name_map` keys = every hashed name appearing in `redactions_by_reason` values; values = the corresponding real column name
- Existing `tests/safety/test_request.py` updated to assert against the v4 shape; previously v3-shaped assertions migrated
- The 8th AST scan (fail-closed writer) still passes (no writer-shape change here)
- The 2nd AST scan (AuditEvent construction confined to `safety.request`) still passes
- Canonical validation passes

**Done when:** `uv run pytest tests/safety/test_request.py` + `tests/test_audit_completeness.py` pass.

**Files:** `src/signalforge/safety/request.py`, `tests/safety/test_request.py`

---

#### US-003 — `safety.audit`: chunker + multi-line writer + reader helper

**Traces to:** DEC-006

**Description:** Add `_chunk_event(event: AuditEvent, limit: int = _AUDIT_RECORD_LIMIT_BYTES) -> tuple[bytes, ...]` — serialise the event; if it fits in one line, return `(line_bytes,)`. Otherwise emit: (1) a header serialisation with empty `redactions_by_reason`/`column_name_map` + `audit_id` (deterministic: `blake2b(model_unique_id + timestamp.isoformat() + signalforge_version, digest_size=8).hexdigest()`), `chunk_index=0`, `chunk_count=N`; (2) N–1 continuation serialisations, each carrying a greedy-fit slice of `redactions_by_reason` + `column_name_map`. Greedy fill: walk reasons in deterministic order; if a single reason's hashed_name list alone exceeds chunk capacity, split it across chunks.

Update `write(event, path)`: serialise → chunk → verify EVERY chunk ≤ limit BEFORE `os.open` (any chunk over → `AuditRecordTooLargeError`, no file artifact); `mkdir -p`; `os.open(O_APPEND | O_CREAT | O_WRONLY, 0o600)`; **single** `Try / finally` (close-only); inside the try, `for chunk in chunks: short_write_loop(chunk); os.fsync(fd)`. Per-chunk fsync.

Add `read_audit_events(path: Path) -> Iterator[AuditEvent]` — accumulate chunks by `audit_id`; emit reassembled `AuditEvent` when `chunk_index` count == `chunk_count`; surface a WARNING via the standard ANSI-safe lazy-format JSON logger when end-of-stream leaves incomplete groups; non-chunked rows pass through unchanged.

**Acceptance criteria:**
- `_chunk_event` always returns `tuple[bytes, ...]` of length ≥ 1; every element ≤ `_AUDIT_RECORD_LIMIT_BYTES`
- Pre-open size-check rejects any single-chunk-over-cap with `AuditRecordTooLargeError` (carries `column_count`); no on-disk artefact on raise
- Per-chunk `os.fsync` is called
- Scan 8 (`_FAIL_CLOSED_WRITER_MODULES`) still passes — the `Try` block wraps the chunk loop; individual `os.write` / `os.fsync` calls remain unguarded
- `read_audit_events` reassembles a chunked record byte-for-byte equal to the source `AuditEvent` (model_dump round-trip)
- `read_audit_events` surfaces a WARNING on partial groups; doesn't raise
- New tests pass: `tests/safety/test_audit.py` (oversize gate, fsync, chunking writer, reader reassembly, partial-group WARNING)
- Canonical validation passes

**Done when:** `uv run pytest tests/safety/test_audit.py tests/test_audit_completeness.py` passes.

**Files:** `src/signalforge/safety/audit.py`, `tests/safety/test_audit.py`

**TDD test cases (write first):**
- `test_chunk_event_small_record_returns_single_chunk`
- `test_chunk_event_wide_record_splits_into_multiple_chunks_each_under_cap`
- `test_chunk_event_deterministic_audit_id`
- `test_write_pre_open_size_check_rejects_pathological_chunk_no_artifact`
- `test_write_per_chunk_fsync_called_n_times`
- `test_write_header_first_then_continuations_in_order`
- `test_read_audit_events_reassembles_chunked_record`
- `test_read_audit_events_passes_non_chunked_through`
- `test_read_audit_events_warns_on_partial_group`

---

#### US-004 — `safety.errors`: parametric `AuditRecordTooLargeError`

**Traces to:** DEC-007

**Description:** Extend `AuditRecordTooLargeError.__init__(self, size: int, limit: int, column_count: int | None = None, *, remediation: str | None = None)`. When `remediation is None`, build the three-sentence operator script naming the column count (when provided), `meta.signalforge.skip_draft: true` workaround, an explicit "do NOT use `safety.mode: aggregate-only` — does not shrink the redactions tuple" line (closes the issue text's misleading hint), and a follow-up issue pointer. Wire the call site in `audit.py` to pass `column_count=len(event.column_name_map or {}) + sum(len(v) for v in (event.redactions_by_reason or {}).values())` (best-available estimate). Stay tier 3 in `_EXCEPTION_TO_EXIT_CODE` (no mapping change). Lock the exact remediation text in a stability test.

**Acceptance criteria:**
- `AuditRecordTooLargeError.column_count` attribute populated when passed
- Default remediation contains the four required elements (column count when known, skip_draft hint, aggregate-only-NOT-a-workaround clarification, follow-up issue pointer)
- The 7th AST scan (every typed exception in `_EXCEPTION_TO_EXIT_CODE`) still passes
- Stability test pins the verbatim remediation text
- `tests/cli/test_exit_codes.py` still passes (tier 3 mapping intact)
- Canonical validation passes

**Done when:** Stability test passes; existing tests updated.

**Files:** `src/signalforge/safety/errors.py`, `tests/safety/test_errors.py`, `tests/safety/test_audit.py` (remediation-text propagation)

---

#### US-005 — drift detector + fixture regen (v4-only)

**Traces to:** DEC-005

**Description:** Update `StrictAuditEvent` in `tests/safety/test_drift_detector.py` to mirror the v4 shape (Optional metadata, v4 redactions fields, chunk-correlation triple, same `@model_validator`). Regenerate `tests/fixtures/safety/audit_events_sample.jsonl` with three lines: (1) a small non-chunked v4 record; (2) a chunked header (chunk_index=0, chunk_count=2, empty redactions_by_reason); (3) a chunked continuation (chunk_index=1, chunk_count=2, slice of redactions_by_reason). Update `regenerate.sh` to produce the new shape. v3 fixture lines are REPLACED, not appended (per DEC-005, drop v3 entirely).

**Acceptance criteria:**
- `StrictAuditEvent` has `extra="forbid"` and matches `AuditEvent`'s v4 field set + validator
- Fixture has exactly 3 lines (1 non-chunked + 2 forming a single chunked event)
- Every line validates against `StrictAuditEvent`
- `tests/safety/test_drift_detector.py` passes
- `regenerate.sh` is documented + runnable
- Canonical validation passes

**Done when:** Drift detector test green against the new fixture.

**Files:** `tests/safety/test_drift_detector.py`, `tests/fixtures/safety/audit_events_sample.jsonl`, `tests/fixtures/safety/regenerate.sh`

---

#### US-006 — wide-table integration tests

**Traces to:** DEC-003, DEC-006, DEC-008

**Description:** New test module `tests/safety/test_wide_table.py`. Synthetic `_make_wide_model(col_count: int) -> Model` helper in test scope. Four tests:

1. **Boundary fixture test.** Linspace search 1 → 500 columns; find the smallest `col_count` where the v4-compressed serialised `AuditEvent` first crosses 4000 B. Assert that at `col_count − 1` the writer emits a single line; at `col_count` the writer emits ≥ 2 chunked lines. Pin the boundary value as a constant (`_V4_CHUNK_BOUNDARY_COL_COUNT`) so a regression in compression efficiency fails loud.
2. **170-col happy path.** `build_llm_request` against a 170-col synthetic model under `schema-only` + pattern-match-all; verify `audit.jsonl` has 1 line; `read_audit_events` returns the original event; `column_name_map` covers every redacted column.
3. **500-col chunked path.** Same construction at 500 columns; verify ≥ 2 lines; each ≤ 4000 B; all chunks share an `audit_id`; reassembled event = original `AuditEvent` byte-for-byte after `model_dump_json(sort_keys=True)`.
4. **Pathological column-name test.** Construct a `RedactionRecord` whose hashed name + reason together would push a single chunk over the cap (column name = 4000-char string is the canonical shape). Assert `AuditRecordTooLargeError` with `column_count` populated; no on-disk artefact.

**Acceptance criteria:**
- All 4 tests pass
- Boundary constant is a deterministic integer (no flakiness across runs)
- 500-col reassembly is byte-identical after sort-keys normalisation
- Pathological test confirms no `audit.jsonl` file created
- Canonical validation passes

**Done when:** `uv run pytest tests/safety/test_wide_table.py` passes.

**Files:** `tests/safety/test_wide_table.py` (new)

---

#### US-007 — concurrent-write under chunking

**Traces to:** DEC-006

**Description:** Extend `tests/safety/test_audit.py::test_audit_write_concurrent_threads_no_interleave` (or add a sibling test). 10 threads × 50 events each; events alternate between small-non-chunked and wide-chunked (170-col synthetic). Verify every event is recoverable via `read_audit_events`; for each chunked event, all chunks correlate by the same `audit_id`; every line ≤ 4000 B (`PIPE_BUF` invariant); no JSON-parse errors; no torn lines.

**Acceptance criteria:**
- Concurrent test passes
- Every reassembled event matches its source by `model_unique_id` + `audit_id`
- No interleaving WITHIN a single event's chunk set (per-thread serial writes preserve order)
- Cross-event interleaving is acceptable + handled by the reader's audit_id grouping
- Canonical validation passes

**Done when:** Extended concurrent test passes 100 consecutive runs locally.

**Files:** `tests/safety/test_audit.py`

---

#### US-008 — docs + CHANGELOG

**Traces to:** DEC-005 … DEC-008

**Description:** Update `docs/safety-ops.md` with a new § "Audit JSONL schema (v4)" documenting: the four new fields, the chunk correlation contract, the reader reassembly behaviour, the no-`safety.mode: aggregate-only` warning. Update `CHANGELOG.md` `[Unreleased]`:
- `### Added` — symbol-table compression of audit redactions; chunked audit-event support for wide-table models (#185).
- `### Changed` — `audit_schema_version` bumped 3 → 4; `AuditEvent.redactions` field replaced by `redactions_by_reason` + `column_name_map`; `AuditRecordTooLargeError` remediation text now actionable. Library is pre-1.0 and was not yet adopted; backward-compat read of v3 records is NOT supported.
- `### Removed` — `AuditEvent.redactions: tuple[RedactionRecord, ...]` field.

**Acceptance criteria:**
- `uv run mkdocs build` succeeds (non-strict)
- `docs/safety-ops.md` covers the four DEC-006 shape rules
- CHANGELOG entries follow the project's `Added` / `Changed` / `Removed` convention
- No mkdocs warnings on the new section
- Canonical validation passes

**Done when:** Docs build clean; CHANGELOG bullets read correctly.

**Files:** `docs/safety-ops.md`, `CHANGELOG.md`

---

#### US-009 — rule update: `.claude/rules/safety-layer.md`

**Traces to:** DEC-005, DEC-006

**Description:** **Orchestrator-only edit** (workers can't Write `.claude/` in worktrees — see memory `ralph-worker-claude-dir-perms`). Updates:
- DEC-011 § now describes compression + chunking as the wide-table solution (cap remains 4000 B for PIPE_BUF atomicity; reach is extended by compression + chunking).
- DEC-014 § history line: `1 → 2 → 3 → 4` with the #185 rationale.
- New § "Audit chunking" describing the header + continuation layout, the reader-warns-on-partial contract, the per-chunk fsync, and the orchestrator-only `audit_id` derivation.
- Cross-reference the issue and the plan.

**Acceptance criteria:**
- Rule file reads coherently
- All cross-references resolve
- File passes existing internal-link / markdown-shape checks
- Canonical validation passes

**Done when:** Rule file updated; PR review approves the rule prose.

**Files:** `.claude/rules/safety-layer.md`

---

#### US-010 — Quality Gate

**Traces to:** Standard /super-plan template

**Description:** Run the code reviewer 4 times across the full changeset (correctness / conventions+parity / tests+coverage / docs+UX angles), fixing all real bugs found each pass. Run CodeRabbit. Re-run canonical validation. Per memory `qg-diverse-reviewer-angles-catch-cross-surface-drift`, set distinct angles to catch cross-surface drift via triangulation.

**Acceptance criteria:**
- All 4 review passes complete; all real-bug findings fixed
- CodeRabbit review processed
- `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` passes
- Coverage gate passes (`--cov-fail-under`)
- No open blockers

**Done when:** All gates green.

**Files:** any

---

#### US-011 — Patterns & Memory

**Traces to:** Standard /super-plan template

**Description:** Record durable lessons from #185 as memory pointers + rule updates:
- The "drop pre-1.0 backward compat to simplify" pattern (when it's appropriate; what it costs).
- The symbol-table-by-reason compression form as a reusable shape for tuple-of-records audit payloads.
- The "header first, reader warns on partial" chunked-write crash-detection pattern (alternative to `is_final` flag).
- Any rule-file drift surfaced during the QG passes.

**Acceptance criteria:**
- Memory updates committed under `/home/wesd/.claude/projects/-home-wesd-Projects-SignalForge/memory/`
- `MEMORY.md` index updated with one-line pointers
- Any new rule file conventions consolidated into the relevant `.claude/rules/*.md` (orchestrator-only edit)
- Canonical validation passes

**Done when:** Memory + rule updates committed.

**Files:** memory dir, possibly `.claude/rules/*.md`

---

### Rules compliance check (gate before publish)

Cross-referenced each story against the Phase 1 Convention Checker output:

| Rule constraint | Covered by | Notes |
|---|---|---|
| safety-layer.md DEC-011 (cap before file open, no except around write/fsync) | US-003 | Pre-open chunk size-check; single `Try / finally` (close-only) |
| safety-layer.md DEC-014 (audit_schema_version `int`, not `Literal`) | US-001, US-002 | Bumped to 4; type stays `int` |
| safety-layer.md DEC-015 (extra="ignore" production + paired extra="forbid" drift mirror) | US-001, US-005 | Production model + strict mirror updated together |
| safety-layer.md DEC-020(a) (AST scan: AuditEvent construction in `safety.request` only) | US-002 | Scan unchanged; construction site stays in `request.py` |
| safety-layer.md DEC-022 (custom `__repr__` redaction) | US-001 | `__repr__` omits `column_name_map` (potential PII via real names) |
| safety-layer.md DEC-023 (fail-closed writer shape, Scan 8) | US-003 | Per-chunk fsync inside single `Try` |
| testing-signal.md drift detector + planted-violation | US-005 | StrictAuditEvent mirror + fixture |
| testing-signal.md AST scan three bypass patterns | US-002, US-003 | Existing scans cover; no new gated class added |
| cli-layer.md DEC-008 exit-code taxonomy | US-004 | `AuditRecordTooLargeError` stays tier 3 |
| cli-layer.md format_error_to_stderr | US-004 | New remediation flows through existing sink |
| skill-parity.md (CLI surface unchanged) | — | Verified: no subcommand/flag/demo change |
| docs-publishing.md (mkdocs build gate) | US-008 | Both `docs-build` + `docs` jobs covered |
| python-build.md (no packaging change) | — | No new package data; wheel unchanged |
| ci-supply-chain.md (no CI workflow change) | — | Same matrix, same SHA-pinned actions |

No rule violations found; ready for publish.

## Publish + Devolve (Phases 5–7 — complete)

Plan committed to `feature/185-wide-table-audit` and pushed; draft PR [#192](https://github.com/wjduenow/SignalForge/pull/192) opened against `dev`. User approved + requested devolve in the same turn. Beads created:

### Beads manifest

- **Epic:** `bd_1-scaffolding-2i3` — #185: Wide-table audit-record size cap
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/185-wide-table-audit`
- **Branch:** `feature/185-wide-table-audit`
- **Plan PR:** #192

| Bead ID | Story | Depends on |
|---|---|---|
| `bd_1-scaffolding-2i3.1` | US-001 — safety.models drop v3 + add v4 + shape validator | — |
| `bd_1-scaffolding-2i3.2` | US-002 — safety.request build v4 AuditEvent (symbol-table) | US-001 |
| `bd_1-scaffolding-2i3.3` | US-003 — safety.audit chunker + multi-line writer + reader | US-001 |
| `bd_1-scaffolding-2i3.4` | US-004 — safety.errors parametric `AuditRecordTooLargeError` | US-001 |
| `bd_1-scaffolding-2i3.5` | US-005 — drift detector + fixture regen (v4-only) | US-001, US-002, US-003 |
| `bd_1-scaffolding-2i3.6` | US-006 — wide-table integration tests (4 tests) | US-002, US-003, US-005 |
| `bd_1-scaffolding-2i3.7` | US-007 — extend concurrent-write test for chunked events | US-003 |
| `bd_1-scaffolding-2i3.8` | US-008 — docs safety-ops.md v4 schema + CHANGELOG | US-001 … US-005 |
| `bd_1-scaffolding-2i3.9` | US-009 — rule update `.claude/rules/safety-layer.md` (orchestrator-only) | US-008 |
| `bd_1-scaffolding-2i3.10` | US-010 — Quality Gate (4-angle code review + CodeRabbit) | US-001 … US-009 |
| `bd_1-scaffolding-2i3.11` | US-011 — Patterns & Memory | US-010 |

**Ready (`bd ready`):** `bd_1-scaffolding-2i3.1` (US-001).

### Orchestrator-only beads

- **US-009** (`bd_1-scaffolding-2i3.9`) edits `.claude/rules/safety-layer.md` — Ralph workers cannot Write to `.claude/` in worktrees (memory `ralph-worker-claude-dir-perms`). The orchestrator must edit this directly during the run, then close the bead.

