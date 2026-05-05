# Issue #27 — CI: Codecov coverage reporting (mirror clauditor)

## Meta

- **Ticket:** [#27](https://github.com/wjduenow/SignalForge/issues/27) — `enhancement`
- **Branch:** `feature/27-codecov-coverage` (off `dev`)
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/27-codecov-coverage`
- **Phase:** devolved — approved 2026-05-04; beads epic + 6 tasks created with dependencies
- **PR:** [#28](https://github.com/wjduenow/SignalForge/pull/28) (draft)
- **Sessions:** 1 (started 2026-05-04)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1 (post-CLI infra polish — first ticket after the v0.1 feature surface closed at #9)

---

## Discovery

### Ticket summary (verbatim from GitHub issue #27)

> **Goal:** Wire up Codecov coverage reporting to CI so PRs surface a per-PR coverage delta and the README shows a coverage badge. Mirrors the setup clauditor ships in `.github/workflows/ci.yml` lines 54–61.
>
> **Why:** Track regression in coverage across the layered pipeline (manifest / warehouse / safety / llm / draft / prune / grade / diff / cli — 9 subpackages as of #9). Today the test count (1381 as of #9) is the only proxy for coverage. The QG passes for #9 each found a real defect that better coverage signal would have flagged earlier.
>
> **Acceptance criteria** (paraphrased):
> 1. `pytest-cov>=5.0` added to `[dev]` in `pyproject.toml`.
> 2. Pytest addopts include `--cov=signalforge --cov-report=xml --cov-report=term-missing --cov-fail-under=<N>` where `<N>` is decided after a baseline run (clauditor uses 80; pick lower of 80 or `floor(actual_coverage_now)`).
> 3. `.github/workflows/ci.yml` runs coverage-instrumented pytest and uploads `coverage.xml` to Codecov via `codecov/codecov-action` — pinned to a commit SHA per `ci-supply-chain.md` DEC-003. `fail_ci_if_error: false`.
> 4. `CODECOV_TOKEN` referenced as `${{ secrets.CODECOV_TOKEN }}`. Operator action documented for adding it.
> 5. README gains a Codecov badge near the top (clauditor shape).
> 6. Local `pytest` keeps working without Codecov network access — `--cov-fail-under` runs locally too.
> 7. Short `docs/ops/codecov.md` (or `CONTRIBUTING.md` section) explains badge, per-PR comment, threshold bumps.
>
> **Notes:** Single-Python CI (3.11). No `codecov.yml` config in v0.1.

### Codebase findings (Subagent B — file:line cited)

**`.github/workflows/ci.yml`** (38 lines, single `lint-test` job):
- `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1` (line 21)
- `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0` (line 23) — Python 3.11, pip cache keyed on `pyproject.toml`
- Steps: `pip install -e ".[dev]"` → `ruff check .` → `ruff format --check .` → `pyright` → `pytest` (lines 28–37)
- Workflow-level `permissions: { contents: read }` (line 9-10), `concurrency` block (lines 12-14)
- **No coverage / codecov references anywhere in the file.**

**`pyproject.toml`** (current state):
- `[project.optional-dependencies]` line 21: `dev = ["ruff", "pyright", "pytest", "dbt-core>=1.8,<2", "types-PyYAML>=6,<7"]` — no `pytest-cov`.
- `[tool.pytest.ini_options]` line 57: `addopts = "-ra --strict-markers --import-mode=importlib -m 'not bigquery and not anthropic and not cli_subprocess'"`
- `strict_markers = true` (line 59) — already present (testing-signal.md DEC-010 already satisfied).
- Three default-excluded markers: `bigquery`, `anthropic`, `cli_subprocess`.

**`README.md`** lines 1–15: no badges anywhere. First content line is the project description; no `[![...]()]` block at the top. Issue #27 is the first ticket to add badges.

**`CONTRIBUTING.md`** (83 lines): canonical validation command at line ~40 is `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`. Test-marker docs at ~50 cover the three default exclusions; ~60 documents the `cli_subprocess` opt-in pattern; ~70 documents BigQuery integration gating (`SF_RUN_BQ=1`).

**`docs/`** flat layout — no `docs/ops/` subdir. Eight `*-ops.md` files at the root: `cli-ops.md`, `diff-ops.md`, `draft-ops.md`, `grade-ops.md`, `manifest-loader-ops.md`, `prune-ops.md`, `safety-ops.md`, `warehouse-adapter-ops.md`. Convention: one ops doc per pipeline layer. **The ticket's suggested `docs/ops/codecov.md` would create a new sub-tree**; the convention-matching name is `docs/codecov-ops.md` or a section in `CONTRIBUTING.md`.

**Coverage references in the repo:** zero `pytest-cov` / `--cov` / `codecov` hits in any config or workflow. Only mentions of "coverage" are in source comments (`src/signalforge/llm/client.py`) and test design notes (`tests/diff/`) — none related to test coverage.

**Test count:** 96 test files; ticket cites 1381 collected tests as of #9. (Could not collect from a fresh worktree without install — taken as given.)

**Workflow files:** Only `.github/workflows/ci.yml` exists. No release / codeql / dependabot workflows.

### Clauditor reference (verbatim — the "mirror" target)

`/home/wesd/Projects/clauditor/.github/workflows/ci.yml` lines 42–61:
```yaml
test:
  steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v4
      with:
        python-version: ${{ matrix.python-version }}
    - run: uv sync --dev
    - run: uv run pytest --cov=clauditor --cov-report=xml
    - uses: codecov/codecov-action@v5
      if: matrix.python-version == '3.13'
      with:
        files: coverage.xml
        fail_ci_if_error: false
      env:
        CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}
```

`/home/wesd/Projects/clauditor/pyproject.toml`:
- Line 90 (addopts): `addopts = "--import-mode=importlib --cov-fail-under=80"`
- Line 100 (dep): `"pytest-cov>=5.0"` in `[dependency-groups].dev`

**Key divergences SignalForge will not mirror:**
- clauditor uses `uv` + matrix Python (3.11/3.12/3.13); SignalForge uses `pip` + single 3.11. Codecov upload is gated on the highest matrix version in clauditor — SignalForge has only one job, so no gating clause needed.
- clauditor's `--cov-report=xml` only; SignalForge ticket asks for both `xml` and `term-missing` (so local runs see uncovered lines without re-running).
- clauditor pins `codecov-action@v5` (tag); SignalForge's `ci-supply-chain.md` DEC-003 mandates SHA pinning. v5 latest stable SHA needs lookup at implementation time.

### Convention findings (Subagent C — rules audit)

Read every `.claude/rules/*.md`. The constraints that bind this ticket:

1. **`ci-supply-chain.md` DEC-003 — pinned action SHAs.** `codecov/codecov-action` MUST be pinned to a 40-char commit SHA with a trailing `# vX.Y.Z` comment. Implementation-time `gh api repos/codecov/codecov-action/git/refs/tags/v5 --jq '.object.sha'` (dereference once more if annotated).
2. **`ci-supply-chain.md` DEC-003 — single Python.** No matrix; the upload step has no `if:` gate.
3. **`ci-supply-chain.md` — `permissions: { contents: read }`** at workflow level already in place; the codecov step does not require write scopes.
4. **`ci-supply-chain.md` — concurrency** already in place; no change.
5. **`testing-signal.md` DEC-010 — strict markers.** `addopts = "--strict-markers"` AND `strict_markers = true` both required (pytest 9.x quirk). Already in place; coverage flags do not interact.
6. **`testing-signal.md` — no `assert True` tests.** Coverage measurement does not weaken this; coverage is a *second* signal.
7. **`testing-signal.md` — fixture regeneration via ephemeral `uvx`.** Not affected.
8. **`python-build.md` — `pyproject.toml` shape.** `pytest-cov>=5.0` lands in `[project.optional-dependencies].dev` (matches existing `dev` extras shape; SignalForge does not use PEP 735 `[dependency-groups]` like clauditor — preserve the existing convention).
9. **`python-build.md` — zsh-safe install.** No change to `pip install -e ".[dev]"`.
10. **`CLAUDE.md` — canonical validation command.** `ruff check . && ruff format --check . && pyright && pytest` runs locally and in CI. Adding `--cov-fail-under` to `addopts` means **the canonical command now also gates coverage locally** — that's the ticket's intent.
11. **No `workflow-project.md`** exists. No project-specific super-plan extensions.
12. **No new AST scan / grep gate / fail-closed writer** introduced — this ticket is config-only and does not touch source code under `src/signalforge/`. The 6-dir grep gate (cli-layer.md DEC-019) does not need extension.
13. **No new YAML namespace.** `signalforge.yml` is unaffected; coverage config lives in `pyproject.toml`, not the project config.
14. **README badge convention:** none exists. Adding the first badge means setting precedent. Clauditor's badge shape: `[![codecov](https://codecov.io/gh/wjduenow/SignalForge/branch/dev/graph/badge.svg)](https://codecov.io/gh/wjduenow/SignalForge)`. Branch is `dev` (not `main`) because SignalForge develops on `dev` and `main` is the released line — Codecov should track the actively-developed branch.

### Scope summary

**What this ticket changes:**
- `pyproject.toml`: +1 dep in `[dev]`, +4 flags in pytest `addopts`.
- `.github/workflows/ci.yml`: +1 step (codecov upload), +1 secrets reference. The existing pytest step gains `--cov` flags via the addopts change (no workflow edit needed for that — local and CI both pick it up from `pyproject.toml`).
- `README.md`: +1 badge near the top (this is the first badge — sets precedent for a CI-status badge in v0.2+).
- `CONTRIBUTING.md` or `docs/codecov-ops.md`: documentation for the operator action (add `CODECOV_TOKEN`) and for interpreting coverage signal.

**What this ticket does NOT change:**
- No source code under `src/signalforge/`.
- No tests under `tests/` (existing tests run unchanged; only their *measurement* changes).
- No new YAML config namespace.
- No `codecov.yml` config file (the ticket explicitly defers it; clauditor doesn't ship one).

This is a config-only ticket — the smallest scope of any v0.1 ticket so far. Story count expected: 3-5 implementation stories + Quality Gate + Patterns & Memory.

---

## Scoping questions (open — to resolve before Phase 2)

### SQ-01 — Coverage threshold (`<N>`) sourcing strategy

Acceptance criterion 2 says: "decided after running coverage once on `dev` and picking a non-aspirational baseline (clauditor uses 80; pick the lower of 80 or `floor(actual_coverage_now)` so the gate doesn't immediately go red)."

Three ways to get there:

- **A — Two-step within this ticket.** US-001 lands `pytest-cov` dep + the addopts MINUS `--cov-fail-under`, runs CI to capture baseline from the `term-missing` output, US-002 adds `--cov-fail-under=floor(baseline)` once measured. One PR, two commits.
- **B — Single-step with conservative pick.** Pick `--cov-fail-under=70` up-front (well below 80 and below any plausible `floor(actual)`). One commit. Adjust upward in a follow-up after the first PR's coverage report lands.
- **C — Measure locally before merging.** Plan author runs `pytest --cov` in the worktree, reads the percentage, and writes the `floor(actual_coverage_now)` value into the addopts on the first commit. One commit. Requires the worktree to have a working install (which it does after `pip install -e ".[dev]"`).

Recommendation: **C**. Single commit, faithful to the ticket's "non-aspirational baseline", no second round-trip. Risks: a flaky test run could over-state coverage; mitigated by running twice. The ticket's own wording suggests this is the intent — clauditor picked 80 once, didn't iterate.

### SQ-02 — Documentation file location

Acceptance criterion 7: "short `docs/ops/codecov.md` (or a section in `CONTRIBUTING.md`)".

- **A — `docs/codecov-ops.md`.** Matches existing convention (eight peer files: `cli-ops.md`, `diff-ops.md`, etc.). Layer-ops naming. Does NOT match the ticket's exact suggestion (`docs/ops/codecov.md`).
- **B — `docs/ops/codecov.md`.** Matches the ticket suggestion verbatim. Creates a new `docs/ops/` subdir; future ops docs would either move there or proliferate at the wrong level.
- **C — Section in `CONTRIBUTING.md`.** No new file. The CONTRIBUTING.md is already 83 lines covering branching, validation, fixtures, markers — adding a "Coverage" section is consistent with that structure.

Recommendation: **A**. The ticket's `docs/ops/codecov.md` was a paraphrase; the convention is flat `docs/<layer>-ops.md` and "codecov" is the layer here (CI/observability). C is a backup if we want to keep the CONTRIBUTING.md as the single contributor entry point.

### SQ-03 — `codecov.yml` config file in v0.1?

The ticket says: *"A `codecov.yml` config file is optional in v0.1; clauditor doesn't ship one. Skip unless needed for tuning."*

- **A — Skip.** Take the default Codecov status check + per-PR comment behaviour. Add when (if) a real tuning need surfaces.
- **B — Ship a minimal one** with `coverage.status.project.default.target: auto threshold: 1%` (don't fail on tiny regressions during stabilisation).

Recommendation: **A**. Matches the ticket; matches clauditor; less surface to maintain. Codecov defaults are reasonable.

### SQ-04 — Coverage scope: default test set only, or include `cli_subprocess`?

`addopts` excludes `bigquery`, `anthropic`, `cli_subprocess`. After this change, the same defaults still apply — coverage is measured against the ~1300 default tests, and the ~80 BigQuery-integration / Anthropic-real-API / cli_subprocess paths are not exercised.

Concrete impact: the `signalforge.warehouse.adapters.bigquery` module's real-network code paths (the `_get_table` / `client.query` lines) are exercised only by `FakeBigQueryClient` in unit tests. Coverage metrics will reflect that, which is a true-but-incomplete signal.

- **A — Status quo.** Coverage measures only the default test set. Maintainers run `pytest -m bigquery` / `-m cli_subprocess` separately for completeness — no coverage upload in those paths.
- **B — Add a separate CI job.** A `coverage-cli-subprocess` job runs `pytest -m cli_subprocess --cov-append`, the two `coverage.xml` get merged before upload. Doubles CI minutes.
- **C — Defer to v0.2.** Note in the docs that current coverage excludes gated markers; revisit if the gap becomes load-bearing.

Recommendation: **A + C** — current ticket measures default-set only and notes the limitation in `docs/codecov-ops.md`. v0.2 can add a gated coverage merge if a real defect ever surfaces because of the gap.

### SQ-05 — README badge: which branch and which placement?

Two sub-decisions:

- Branch: SignalForge develops on `dev` and `main` is the released line (CLAUDE.md). Codecov badges typically point at the actively-developed branch so PR coverage is meaningful. Default: `branch/dev`.
- Placement: README has zero badges currently. Convention-setting decision. Two reasonable shapes:
  - **A — Single Codecov badge at the top**, before the project description.
  - **B — Badge row** including a placeholder for a future CI-status badge (just Codecov for now; v0.2 can add).

Recommendation: branch `dev`; placement A (just the Codecov badge for now — adding a row before the row exists is over-engineering).

### SQ-06 — `codecov/codecov-action` SHA pinning approach

`ci-supply-chain.md` DEC-003 mandates 40-char SHA + `# vX.Y.Z` comment. The implementer needs to:

- Look up the SHA at implementation time via `gh api repos/codecov/codecov-action/git/refs/tags/v5 --jq '.object.sha'` (dereference annotated tags via `git/tags/<sha>` if needed).
- Use `v5` (latest major) — clauditor uses `v5`, the action's docs recommend v5 for current setups.

This is implementation-detail, not a planning decision. Noted here so the implementer doesn't forget the dereference step.

---

## Architecture review (Phase 2)

Six review areas; three concerns; zero blockers. Inline review (config-only ticket — six parallel subagents would all return n/a or pass).

| Area | Rating | Finding |
|------|--------|---------|
| Security | concern | AR-C1 — fork-PR token access (resolved DEC-007) |
| Performance | pass | ~10–15% pytest wall-time overhead from coverage instrumentation. Acceptable. |
| Data model | n/a | No schema / migration / Pydantic model. |
| API design | n/a | No public-surface change. |
| Observability | concern | AR-C2 — badge branch + Codecov cache (resolved DEC-008) |
| Testing strategy | concern | AR-C3 — local-measurement reliability for SQ-01=C (resolved DEC-001) |

Untouched by this ticket (sanity-confirmed): the five fail-closed audit writers (`safety.jsonl`, `llm_response.jsonl`, `prune.jsonl`, `grade.jsonl`, `diff.json`); the six AST audit-completeness scans; the six-dir grep gate; every drift detector; the eight error hierarchies; the `signalforge.yml` namespace map.

---

## Decisions (DEC-)

### DEC-001 — Coverage threshold via two-run local measurement

**Decision:** SQ-01=C. Plan author runs `pytest --cov=signalforge --cov-report=term` twice in the worktree, picks `--cov-fail-under=floor(min(both_runs, 80))`, writes the value into `addopts` on the first commit. Single PR, single threshold.

**Rationale:** Faithful to the ticket's "non-aspirational baseline" wording. The `min(both_runs, ...)` gives margin against test-flake noise; the `min(..., 80)` caps aspiration to clauditor's precedent. Documented as the canonical procedure in `docs/codecov-ops.md` so the next threshold bump (a v0.2 task) follows the same shape.

### DEC-002 — Documentation at `docs/codecov-ops.md`

**Decision:** SQ-02=A. New file `docs/codecov-ops.md` matching the established flat `docs/<layer>-ops.md` convention (eight peers: `cli-ops.md`, `diff-ops.md`, `draft-ops.md`, `grade-ops.md`, `manifest-loader-ops.md`, `prune-ops.md`, `safety-ops.md`, `warehouse-adapter-ops.md`). `CONTRIBUTING.md` gains a one-line pointer.

**Rationale:** The ticket's `docs/ops/codecov.md` was a paraphrase, not the project's actual convention. A `docs/ops/` subdir would orphan the eight existing ops docs. Codecov is the "CI/coverage layer" in the project taxonomy, so `codecov-ops.md` slots in cleanly.

### DEC-003 — Skip `codecov.yml` config file in v0.1

**Decision:** SQ-03=A. Take Codecov's default project + patch status checks and default per-PR comment. No `codecov.yml` shipped.

**Rationale:** Matches the ticket and clauditor. Less surface area; tuning lands when a real signal/noise complaint surfaces in v0.2+. Reserved file path: `codecov.yml` at repo root if v0.2 needs it.

### DEC-004 — Coverage scope: default test set only; document the gap

**Decision:** SQ-04=A+C. Coverage measures the default pytest set (excludes `bigquery`, `anthropic`, `cli_subprocess` per existing addopts). Real-network adapter paths and the console-script wheel smoke are NOT covered. `docs/codecov-ops.md` documents the gap explicitly. v0.2 may add a gated coverage merge if a defect surfaces because of the gap.

**Rationale:** Doubling CI minutes (option B) for ~80 gated tests buys little in v0.1 — those paths have separate maintainer-run gates (`pytest -m bigquery`, `pytest -m cli_subprocess`) and the tests that ARE covered exercise the same code via fakes (`FakeBigQueryClient`, `FakeAnthropicClient`, in-process `main(argv)` smoke). Honest signal-with-disclosure beats inflated coverage.

### DEC-005 — README badge: single Codecov badge at top, branch `dev`

**Decision:** SQ-05=A. Badge lands as the first line of `README.md` (before the description), shape `[![codecov](https://codecov.io/gh/wjduenow/SignalForge/branch/dev/graph/badge.svg)](https://codecov.io/gh/wjduenow/SignalForge)`.

**Rationale:** README has zero badges today; this sets the precedent. Branch=`dev` because SignalForge develops on `dev` and `main` is the released line (CLAUDE.md). v0.2's CI-status badge can join later.

### DEC-006 — `codecov/codecov-action@<sha>` pinning at implementation time

**Decision:** Pin `codecov/codecov-action` to a 40-char commit SHA with trailing `# v5.X.Y` comment, per `ci-supply-chain.md` DEC-003. SHA resolved at implementation time via:

```bash
gh api repos/codecov/codecov-action/git/refs/tags/v5 --jq '.object.sha'
# If annotated tag: dereference once more via repos/.../git/tags/<sha>
```

Use `v5` major (current stable; matches clauditor; action's own docs recommend v5).

**Rationale:** Hard `ci-supply-chain.md` rule. Implementation note, not a planning choice — recorded so the implementer doesn't forget the dereference step.

### DEC-007 — Codecov step runs unconditionally; fork-PR upload-failure noise documented

**Decision:** AR-C1=(a). Codecov step has no `if:` guard; runs on every CI invocation. `fail_ci_if_error: false` is mandated by the ticket and prevents CI failure when the upload itself fails. Fork-PR runs will silently fail the upload (no `CODECOV_TOKEN` exposure on fork-PR `pull_request` events) — documented in `docs/codecov-ops.md` as expected behavior.

**Rationale:** Matches clauditor's posture. Adding the `head.repo.full_name == github.repository` guard is YAML for a problem we're not feeling yet (no fork PR traffic in v0.1). v0.2 can revisit if the upload-fail logs become noise.

### DEC-008 — Badge tracks `dev`; doc explains `main` will lag

**Decision:** Badge URL hardcodes `branch/dev`. `docs/codecov-ops.md` explains that the badge reflects coverage on `dev` (the development line) and will not update on `main` until v0.1 ships. Operators browsing `main`'s README won't think coverage is broken.

**Rationale:** Resolves AR-C2. The decision is identical to DEC-005's branch choice; this DEC pins the documentation half.

### DEC-009 — Pytest addopts gain four cov flags

**Decision:** Append to existing `addopts` (NOT replace): `--cov=signalforge --cov-report=xml --cov-report=term-missing --cov-fail-under=<N>` where `<N>` is set per DEC-001. Result:

```toml
addopts = "-ra --strict-markers --import-mode=importlib -m 'not bigquery and not anthropic and not cli_subprocess' --cov=signalforge --cov-report=xml --cov-report=term-missing --cov-fail-under=<N>"
```

**Rationale:** `xml` for the CI upload artifact (`coverage.xml`); `term-missing` so local runs and CI logs both surface uncovered line numbers without re-running. The four-flag set is exactly what the ticket calls for.

### DEC-010 — `pytest-cov>=5.0` in `[project.optional-dependencies].dev`; preserve classic shape

**Decision:** Append `"pytest-cov>=5.0"` to the existing `dev = [...]` array in `[project.optional-dependencies]`. Do **not** migrate to PEP 735 `[dependency-groups]` (clauditor's shape).

**Rationale:** SignalForge's existing convention is `[project.optional-dependencies]` (`python-build.md` is silent on which shape is canonical, but the established pattern across all v0.1 work is the classic shape). Migrating to `[dependency-groups]` is a separate decision and not in scope for this ticket.

### DEC-011 — `CONTRIBUTING.md` gets a one-line pointer; full operator action lives in `docs/codecov-ops.md`

**Decision:** `CONTRIBUTING.md` gains a one-line "**Coverage:** see `docs/codecov-ops.md`" pointer in the validation/CI area. The full operator action (add `CODECOV_TOKEN` at codecov.io → Settings → Repository Upload Token) lives in the ops doc, not duplicated in CONTRIBUTING.md.

**Rationale:** CONTRIBUTING.md is the contributor-onboarding entry point (branching, validation, fixtures, markers); the ops doc is the layer-specific reference. Keeping the operator action in one place avoids drift.

---

## Detailed breakdown (Phase 4)

Six stories. Pipeline ordering is "config-then-CI-then-docs" — the right architectural order for this ticket because:
- US-001 (pyproject.toml) produces the local gate AND `coverage.xml` that US-002 uploads.
- US-002 (ci.yml) consumes `coverage.xml`.
- US-003 (README badge) is independent.
- US-004 (docs) is independent.

US-005/006 are the standard Quality Gate + Patterns & Memory tail per `/super-plan`.

No TDD sections — this ticket is pure CI/config plumbing; there's no business logic to write tests against. The canonical validation command (`pytest`) IS the test for US-001. CI itself (the first PR run) is the test for US-002.

### US-001 — `pyproject.toml`: add `pytest-cov`, add cov flags, measure baseline

**Description:** Add `pytest-cov>=5.0` to `[project.optional-dependencies].dev`. Run baseline measurement (procedure below). Append four `--cov*` flags to `addopts` with the measured threshold.

**Traces to:** DEC-001, DEC-009, DEC-010.

**Acceptance criteria:**
- `pyproject.toml` line 21 ends with `... "types-PyYAML>=6,<7", "pytest-cov>=5.0"]` (preserve classic `[project.optional-dependencies]` shape; do NOT migrate to `[dependency-groups]`).
- `pyproject.toml` `addopts` (line 57) appends `--cov=signalforge --cov-report=xml --cov-report=term-missing --cov-fail-under=<N>` where `<N>` is `floor(min(run_1_pct, run_2_pct, 80))` from the baseline procedure below.
- Two-run baseline procedure executed by the implementer:
  1. From the worktree: `pip install -e ".[dev]"` (pulls in `pytest-cov`).
  2. `pytest --cov=signalforge --cov-report=term` — record total coverage % as `run_1_pct`.
  3. Repeat: `pytest --cov=signalforge --cov-report=term` — record as `run_2_pct`.
  4. If `|run_1_pct - run_2_pct| > 1`, investigate divergence before proceeding (likely indicates a non-deterministic test). Otherwise pick `<N> = floor(min(run_1_pct, run_2_pct, 80))`.
  5. Record both percentages and the picked `<N>` in the plan doc's "Implementation log" section (added at the end of US-001).
- Local validation passes: `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` exits 0 with the new `--cov-fail-under=<N>` gate active.
- `coverage.xml` is generated alongside the test run (verify with `ls coverage.xml`).
- `coverage.xml` is gitignored (add a one-line `.gitignore` entry if not already covered by a glob).

**Done when:** Local `pytest` exits 0 with the chosen `--cov-fail-under` gate; `coverage.xml` present; baseline percentages logged in the plan doc.

**Files:**
- `pyproject.toml` — edit `[project.optional-dependencies].dev`, edit `[tool.pytest.ini_options].addopts`.
- `.gitignore` — add `coverage.xml` and `.coverage` if not already covered.
- `plans/super/27-codecov-coverage.md` — append measured baseline to "Implementation log" section.

**Depends on:** none.

### US-002 — `ci.yml`: add SHA-pinned `codecov/codecov-action` upload step

**Description:** Append a `Upload coverage to Codecov` step after the existing `Pytest` step. SHA-pinned per `ci-supply-chain.md`. No matrix gate (single Python). `fail_ci_if_error: false`. Token from secrets.

**Traces to:** DEC-006, DEC-007.

**Acceptance criteria:**
- Step lands AFTER the `Pytest` step in `.github/workflows/ci.yml` (the Pytest step itself is unchanged — `addopts` from `pyproject.toml` already triggers `--cov-report=xml` so `coverage.xml` is produced).
- Action pinned: `uses: codecov/codecov-action@<40-char-sha>  # v5.X.Y` with the exact SHA resolved at implementation time via `gh api repos/codecov/codecov-action/git/refs/tags/v5 --jq '.object.sha'` (dereference once more via `git/tags/<sha>` if the tag is annotated). The trailing `# v5.X.Y` comment matches the version the SHA points at.
- `with: { files: coverage.xml, fail_ci_if_error: false }`.
- `env: { CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }} }`.
- No `if:` guard — runs unconditionally per DEC-007.
- Workflow-level `permissions: { contents: read }` and `concurrency` block remain unchanged.

**Done when:** Workflow YAML is valid (`gh workflow view` parses; or `act` dry-run if available); the new step is committed.

**Files:**
- `.github/workflows/ci.yml` — append one step after line 37.

**Depends on:** US-001 (the `--cov-report=xml` flag in addopts is what produces `coverage.xml`).

### US-003 — `README.md`: add Codecov badge at top, branch `dev`

**Description:** Insert a single Codecov badge on a new first line of `README.md`, before the project title. Set the project's badge precedent.

**Traces to:** DEC-005, DEC-008.

**Acceptance criteria:**
- README's line 1 (or 1-2 with a blank line) is the badge: `[![codecov](https://codecov.io/gh/wjduenow/SignalForge/branch/dev/graph/badge.svg)](https://codecov.io/gh/wjduenow/SignalForge)`.
- Branch in the URL is `dev` (NOT `main`) per DEC-005.
- Existing `# SignalForge` heading and the `> LLM-drafted dbt schema.yml...` blockquote remain in their current order, immediately after the badge.

**Done when:** Rendered README on the PR's GitHub view shows the badge at the top.

**Files:**
- `README.md` — prepend two lines (badge + blank line) before line 1.

**Depends on:** none (independent of US-001/US-002).

### US-004 — `docs/codecov-ops.md`: operator documentation + CONTRIBUTING pointer

**Description:** Create the operator-facing reference matching the eight existing `docs/<layer>-ops.md` files. Add a one-line pointer in `CONTRIBUTING.md`.

**Traces to:** DEC-002, DEC-007 (fork-PR gotcha), DEC-008 (badge tracks `dev`), DEC-001 (baseline procedure), DEC-004 (gap re. excluded markers), DEC-011 (CONTRIBUTING pointer).

**Acceptance criteria:**

`docs/codecov-ops.md` covers, in order:

1. **Operator setup** — how to add `CODECOV_TOKEN` at codecov.io → Settings → Repository Upload Token; how to add it to GitHub repo secrets.
2. **Reading the badge** — what the percentage means; that it tracks `dev` (NOT `main`) per DEC-005/DEC-008; that `main`'s README will show a stale-or-unknown badge until v0.1 ships and the merge happens.
3. **Reading the per-PR comment** — Codecov posts a delta comment automatically; how to interpret +/- coverage delta; what the file-level annotations mean.
4. **Bumping the threshold** — the two-run baseline procedure from DEC-001, applied as the canonical method for any future bump. Explicit bump cadence: revisit when actual coverage exceeds `<N> + 5` for two consecutive `dev` builds.
5. **Known gap: excluded markers** — coverage measures the default pytest set; `bigquery` / `anthropic` / `cli_subprocess` paths are NOT instrumented. Cite `pyproject.toml` line 57's `-m 'not bigquery and not anthropic and not cli_subprocess'`. Reservation: v0.2 may add a gated coverage-append job per DEC-004.
6. **Fork-PR upload failures** — fork-PR `pull_request` events don't get `secrets.CODECOV_TOKEN`; the upload silently fails; `fail_ci_if_error: false` prevents CI failure; the failure log is expected per DEC-007.
7. **Local-only `--cov-fail-under` gating** — the gate runs locally too (it's in `addopts`). Devs running `pytest` see coverage failures the same way CI does. Cite `CLAUDE.md`'s canonical validation command.

`CONTRIBUTING.md` gains exactly one new line in the validation/CI area: `**Coverage:** see [`docs/codecov-ops.md`](docs/codecov-ops.md).` (or equivalent inline pointer that doesn't duplicate the operator action).

**Done when:** New ops doc renders cleanly on GitHub; CONTRIBUTING.md pointer resolves to the new file.

**Files:**
- `docs/codecov-ops.md` — new file (~80–120 lines).
- `CONTRIBUTING.md` — one-line addition near the existing validation section.

**Depends on:** none (independent; can land in any order relative to US-001/2/3, but is most useful AFTER the others so the doc references shipped behavior).

### US-005 — Quality Gate

**Description:** Standard `/super-plan` quality-gate pass. Four code-review passes via Agent (subagent_type=code-reviewer or general-purpose with explicit review prompt), fixing real bugs found each pass. CodeRabbit review if available. Final canonical-validation re-run.

**Traces to:** all DEC-### implicitly.

**Acceptance criteria:**
- Four code-review passes completed; each pass's output appended to plan doc as `## QG Pass N`.
- Real bugs fixed in lockstep with discovery (do NOT batch fixes across passes).
- CodeRabbit review run if available (`gh pr review` or `coderabbit.ai` integration).
- Canonical command passes: `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.
- `pytest -m cli_subprocess` passes (the gated wheel-smoke marker — verifies the `signalforge` console script still works after the addopts change; addopts default-excludes the marker so the in-default-suite coverage baseline isn't affected, but maintainer-run before merge per `cli-layer.md` DEC-018).

**Done when:** All four passes complete with no real bugs remaining; canonical + cli_subprocess validation passes.

**Files:** any introduced by US-001..004 (review surface only, not new files unless a real bug demands one).

**Depends on:** US-001, US-002, US-003, US-004.

### US-006 — Patterns & Memory

**Description:** Distill the patterns from this ticket into `.claude/rules/`, `docs/`, or memory. Always last; depends on Quality Gate.

**Traces to:** DEC-001 through DEC-011.

**Acceptance criteria:**
- Decide whether the patterns warrant a new rule file or extension to an existing one. Likely candidates:
  - **`ci-supply-chain.md`** — extend with a "Codecov upload" subsection covering: SHA-pin the action; `fail_ci_if_error: false` posture; fork-PR token-access caveat (DEC-007); single-Python-no-matrix posture for the upload step (DEC-006).
  - **`testing-signal.md`** — extend with a "Coverage measurement" subsection covering: `--cov-fail-under` in addopts means the gate runs locally too (DEC-009); the two-run baseline procedure (DEC-001); the known gap re. excluded markers (DEC-004).
  - **No new rule file** likely needed — coverage is config layer, not a new pipeline stage.
- Memory updates if any general lessons emerged (e.g., "fork-PR token access is a public-OSS gotcha" — likely worth a `feedback` or `project` memory).
- Update `CLAUDE.md` "Repository status" section if the project status warrants it (e.g., note that #27 added Codecov coverage reporting — but this can also wait for `/closeout`).

**Done when:** Rule extensions / docs / memory updates committed; PR description updated to reference the new patterns.

**Files:**
- `.claude/rules/ci-supply-chain.md` (likely extension).
- `.claude/rules/testing-signal.md` (likely extension).
- Memory files under `~/.claude/projects/-home-wesd-Projects-SignalForge/memory/` if new lessons emerged.

**Depends on:** US-005 (Quality Gate must complete first).

---

## Implementation log

(Filled during US-001 baseline measurement and Quality Gate passes.)

### US-001 baseline measurement

(To be filled by implementer.)

| Run | Command | Coverage % |
|-----|---------|------------|
| 1   | `pytest --cov=signalforge --cov-report=term` | 95% |
| 2   | `pytest --cov=signalforge --cov-report=term` | 95% |
| Picked `<N>` | `floor(min(95, 95, 80))` | **80** |

---

## Beads manifest

- **Epic:** `SignalForge-8qq` — 27: Codecov coverage reporting
- **Tasks:**
  - `SignalForge-8qq.1` — US-001: pyproject.toml: add pytest-cov, cov flags, measure baseline
  - `SignalForge-8qq.2` — US-002: ci.yml: add SHA-pinned codecov/codecov-action upload step (depends on .1)
  - `SignalForge-8qq.3` — US-003: README.md: Codecov badge at top, branch dev
  - `SignalForge-8qq.4` — US-004: docs/codecov-ops.md + CONTRIBUTING.md pointer
  - `SignalForge-8qq.5` — US-005: Quality Gate: code review x4 + canonical validation (depends on .1-.4)
  - `SignalForge-8qq.6` — US-006: Patterns & Memory: update ci-supply-chain.md + testing-signal.md (depends on .5)
- **Devolved:** 2026-05-04

