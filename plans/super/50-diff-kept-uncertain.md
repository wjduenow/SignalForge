# Issue #50 — diff: visually distinguish `kept-without-evidence` from `kept`

## Meta

- **Ticket:** [#50](https://github.com/wjduenow/SignalForge/issues/50)
- **Branch:** `50-diff-kept-uncertain` (off `dev`)
- **Phase:** implementation complete
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.2 (closes a load-bearing visibility gap in Architectural Commitment #5 — explainable diffs)
- **Labels:** `enhancement`, `cli` (per the GitHub issue)

---

## Ticket summary

Pre-#50 the diff renderer collapsed `prune_result.reason == "kept"` and
`prune_result.reason == "kept-without-evidence"` into a single
`tier="kept"` (green) row. The only signal distinguishing the two was
the prose `why` field. This defeated the conservative-bias commitment
(`.claude/rules/prune-engine.md` DEC-006 / DEC-011) — a reviewer
scanning the kept column saw "we shipped this without evidence" in the
same visual bucket as "we shipped this because it caught a real failing
row." Issue #50 introduces a distinct `kept-uncertain` tier (Option B
from the ticket body) so the conservative-bias signal survives to the
operator.

## Decisions

### DEC-001 — Four-value `Tier` literal; `kept-uncertain` is the new value

`Tier = Literal["kept", "kept-uncertain", "dropped", "flagged"]`. The
prior three values are unchanged. `kept-uncertain` projects exactly
the prune signal `decision.decision == "kept" AND decision.reason ==
"kept-without-evidence"`. The 5-value prune-side `DropReason` literal
(`always-passes`, `requires-future-data`, `failed-on-known-clean-data`,
`kept`, `kept-without-evidence`) is **unchanged** — the diff layer
projects the existing prune signal to a new visible tier; it does not
expand the prune taxonomy.

Why a fourth tier (vs. Option A's `(uncertain)` badge or Option C's
summary-line count):

- A badge (Option A) keeps the green colour and depends on font /
  terminal rendering of parentheticals — easy to miss at a glance.
- A summary-line count (Option C) is operator-visible but does not
  surface *which* rows are uncertain on first scan.
- A tier literal (Option B) gives every renderer a distinct visual /
  structural signal: cyan colour in ANSI, distinct tier-cell text in
  Markdown / JSON. This is the change that matches the design intent.

### DEC-002 — `audit_schema_version` bumps 1 → 2 (hard break, no shim)

`DiffReport.audit_schema_version: Literal[2] = 2` (was `Literal[1] = 1`).
The bump is intentionally a hard break for external sidecar consumers:

- The sidecar is `O_TRUNC` write-only (`.claude/rules/diff-renderer.md`
  DEC-009). Prior runs' bytes are not preserved on disk, so there is no
  migration step — the next `signalforge generate` invocation
  overwrites with the v0.2 shape.
- The CLI does not re-read existing `.signalforge/diff.json` sidecars
  internally; the only readers are external (CI parsers, the v0.3
  GitHub Action). Those consumers gate on `audit_schema_version >= 2`
  to consume the four-tier taxonomy.
- A compat shim that loaded v1 sidecars as `Literal[1 | 2]` would defeat
  the gate's purpose: an external consumer reading a v1 sidecar would
  silently get `kept_uncertain_count` missing and the four-tier
  classifier not present.

`schema_version` stays at `1` — that's reserved for sidecar-JSON-shape
breaks (field rename / removal). Adding a new literal value to a
closed set is forward-compat at the wire format; the `audit_schema_version`
bump is the explicit out-of-band signal for *semantic* contract
changes.

### DEC-003 — Cyan for kept-uncertain in ANSI; avoid palette collision

`_CYAN = "\x1b[36m"` is the new ANSI colour. The existing palette is
green (`kept`), red (`dropped`), yellow (`flagged`), bold + dim for
header / placeholders. The ticket body suggested yellow or cyan; yellow
collides with `flagged`. Cyan is the dbt-cli "neutral / info" colour
and renders identically across xterm, iTerm2, Windows Terminal, and
GitHub Actions runners — no 256-colour or 24-bit fallback needed.

The new colour code flows through the existing `_color(...)` helper,
so the DEC-021 colour-precedence chain
(`respect_no_color_env=False` > `force_color=True/False` >
`FORCE_COLOR` > `NO_COLOR` > `isatty()`) is inherited automatically.
The unconditional DEC-007 ANSI strip on user-content fields also flows
unchanged — the CSI regex `\x1b\[[0-?]*[ -/]*[@-~]` covers `\x1b[36m`
identically to `\x1b[33m`.

`_COL_TIER` widened 8 → 14 to fit the literal `kept-uncertain`
(14 chars) without ellipsis. All 11 committed `.ansi` snapshot
fixtures regenerated to absorb the column-width ripple; this is a
non-functional cosmetic change that's bundled with the tier addition
because narrowing the cell would defeat the whole point (the operator
must see the literal `kept-uncertain` text, not `kept-un…`).

### DEC-004 — CLI batch summary stays at 3 counts (defer to v0.3)

The CLI's multi-model batch summary line currently aggregates
`kept_count + dropped_count + flagged_count` across models
(`signalforge.cli._helpers.format_batch_summary`). Splitting that line
into 4 counts (`kept / kept-uncertain / dropped / flagged`) is
Option-C-adjacent — operator-facing summary surface — and is out of
scope for issue #50. The deferred follow-up:

- Open a v0.3 issue to add `kept_uncertain_count` to
  `_SingleModelOutcome` and the batch summary header.
- For v0.2 / issue #50, the CLI surface stays at 3 counts; the new
  `kept_uncertain_count` is visible only on the per-model
  `DiffReport` and its rendered header. Operators inspecting batch
  output for kept-uncertain rows read the sidecar JSON or the
  per-model rendered diff, not the aggregated summary.

This deferral is documented in `docs/cli-ops.md` § Sidecar caveat
(existing section on last-writer-wins) as a known gap, not a regression.

---

## Origin-dominates-grading invariant (load-bearing)

The classifier `_tier_for_kept(decision, score, passed)` in
`signalforge.diff.engine` checks `decision.reason ==
"kept-without-evidence"` **before** the grading-aggregate dispatch. The
contract: a kept-without-evidence row stays `tier="kept-uncertain"`
regardless of grading attachment — never collapses to `flagged` even
when an attached `GradingResult` also fails the rubric.

The rationale: a test we couldn't positively evaluate cannot
meaningfully fail a grading criterion. Collapsing to `flagged` would
report "this test failed grading" when the truth is "we don't know
whether this test would have caught anything." That's a category
error.

Pinned by `tests/diff/test_engine.py::test_kept_uncertain_never_collapses_to_flagged`.

## `why` cascade carve-out (load-bearing)

The pre-#50 cascade for kept rows is
`rationale → first non-empty evidence → decision.why` (issue #41 DEC-022
of `.claude/rules/diff-renderer.md`). For kept-uncertain rows the cascade
is **bypassed**: `decision.why` is surfaced directly. The prune-emitted
message ("total prune budget exceeded before evaluation" / "sample
materialisation failed: …" / "identifier rejected by SQL safety check")
is the load-bearing operator signal; a drafter-supplied rationale or
a grader-supplied evidence describes a test we couldn't evaluate and
would mislead the reviewer.

The truncation pass (`_truncate_why`) still applies — the per-row cap
`max_why_chars` is honoured on the kept-uncertain branch too.

Pinned by `tests/diff/test_engine.py::test_kept_uncertain_why_uses_decision_why_not_rationale`.

---

## Surfaces touched

Per `.claude/rules/prune-engine.md` § "5-surface parity for v0.x → v0.(x+1)
graduations" (the rule generalising `cli-layer.md`'s 5-surface flag-parity
to non-CLI graduations), the following five surfaces updated in lockstep:

1. **Rule file** — `.claude/rules/diff-renderer.md` § "Tier classification"
   (expanded to four tiers; added origin-dominates and `why`-cascade-bypass
   invariants; cyan colour + `_COL_TIER` widening documented).
2. **Ops doc** — `docs/diff-ops.md`:
   - Sidecar JSON example bumped to `audit_schema_version: 2` + kept-uncertain
     row + `kept_uncertain_count`.
   - Decision matrix added a fourth row.
   - Public API surface block updated (`DiffReport.audit_schema_version`,
     `kept_uncertain_count`, `Tier` literal).
   - INFO log payload example carries `kept_uncertain`.
3. **CLAUDE.md** — issue #8 bullet's tier list and public API surface
   row (`Tier`, `DiffReport`).
4. **Test surface**:
   - `tests/diff/test_models.py` — strict mirror + positive tier test +
     `_sample_report` carries `kept_uncertain_count`.
   - `tests/diff/test_drift_detector.py` — strict mirror + four-tier
     seen-tiers assertion.
   - `tests/diff/test_engine.py` — four new behavioural tests:
     - `test_kept_without_evidence_routes_to_kept_uncertain_tier`
     - `test_kept_uncertain_never_collapses_to_flagged`
     - `test_kept_uncertain_why_uses_decision_why_not_rationale`
     - `test_kept_uncertain_count_in_info_log`
   - `tests/diff/test_renderers.py` — four new renderer tests:
     - `test_ansi_renderer_kept_uncertain_uses_cyan_colour`
     - `test_markdown_renderer_kept_uncertain_renders_distinct_row`
     - `test_kept_uncertain_with_hostile_why_is_ansi_stripped`
     - `test_json_renderer_serialises_kept_uncertain_tier_literally`
   - `tests/diff/_snapshot_inputs.py` — `_full_with_grade_report` now
     includes a `kept-uncertain` row (drift detector relies on the
     report-level fixture covering every tier).
   - `tests/fixtures/diff/diff_report_v1.json` — refreshed with the
     kept-uncertain row + `kept_uncertain_count: 1` + `audit_schema_version: 2`.
   - `tests/fixtures/diff/*.{ansi,md,json}` — all 11 snapshot fixtures
     regenerated via `bash tests/fixtures/diff/regenerate.sh` (column
     widening + new row in the canonical case).
   - `tests/fixtures/e2e_helpers/happy/.signalforge/diff.json` — bumped
     to `audit_schema_version: 2` + `kept_uncertain_count: 0` so the
     existing e2e-helper test still parses.
5. **DEC file** — this plan.

## Surfaces NOT touched (intentional)

- **`signalforge.prune` layer.** `DropReason` stays at 5 values;
  `kept-without-evidence` is the existing signal that the diff layer
  now projects to a distinct tier. The prune-engine rule file is
  unchanged.
- **CLI batch summary** (`format_batch_summary` /
  `_SingleModelOutcome.kept_count`). Per DEC-004 deferred to v0.3.
- **`schema_version`.** Stays at `1`; the sidecar JSON wire shape is
  forward-compat (adding a field + a new tier value is not a
  structural break).

---

## Validation

Ran `ruff check . && ruff format --check . && pyright && pytest` from
the project root:

- **ruff check** — all checks passed.
- **ruff format --check** — clean (post-format).
- **pyright** — 0 errors, 0 warnings, 0 informations.
- **pytest** — 1665 passed, 19 deselected (6 pre-existing
  symlink-loop failures unrelated to #50, confirmed by stashing the
  branch's changes and rerunning).

Pre-existing failures excluded from the validation pass:

- `tests/_common/test_path_safety.py::test_input_symlink_loop_raises`
- `tests/_common/test_path_safety.py::test_project_dir_symlink_loop_raises`
- `tests/diff/test_sidecar.py::test_write_sidecar_rejects_symlink_loop`
- `tests/grade/test_audit.py::test_write_grade_event_rejects_symlink_loop`
- `tests/manifest/test_loader.py::test_symlink_loop_in_default_path_is_rejected`
- `tests/manifest/test_loader.py::test_symlink_loop_in_explicit_manifest_path_is_rejected`

All six fail on plain `dev` (Python 3.13 + WSL filesystem behaviour
where the OS resolves symlink loops at `mkdir` time instead of letting
the project's `canonicalise_path` helper detect them). Out of scope
for issue #50.

---

## References

- Issue body: `https://github.com/wjduenow/SignalForge/issues/50`
- Rule file (updated): `.claude/rules/diff-renderer.md` § "Tier classification"
- Ops doc (updated): `docs/diff-ops.md` § "Decision matrix" + § "Sidecar JSON schema"
- Public-API surface (updated): `CLAUDE.md` lines 16 + 32
- Implementation:
  - `src/signalforge/diff/models.py` — `Tier`, `DiffReport.kept_uncertain_count`, `audit_schema_version: Literal[2] = 2`.
  - `src/signalforge/diff/engine.py::_tier_for_kept` — classifier; `_entry_for_test` — `why` cascade bypass; count aggregation + INFO log payload.
  - `src/signalforge/diff/_renderers.py` — `_CYAN`, `_COL_TIER = 14`, header / table / Markdown summary extensions.
- Cross-stage signal source: `.claude/rules/prune-engine.md` § "Conservative drop-reason taxonomy" and § "Three sources of `kept-without-evidence`" — the prune layer's signal that the diff layer now projects to a visible tier.
