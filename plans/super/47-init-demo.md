# 47: `signalforge init-demo` — ship Austin demo to PyPI users

## Meta

- **Ticket:** [GH #47](https://github.com/wjduenow/SignalForge/issues/47)
- **Branch:** `feature/47-init-demo`
- **Worktree:** `../worktrees/SignalForge/47-init-demo`
- **Phase:** devolved
- **PR:** [#78](https://github.com/wjduenow/SignalForge/pull/78)
- **Epic:** `bd_1-scaffolding-t1o`
- **Sessions:**
  - 2026-05-11 — Phase 1 discovery (parallel research, scoping decisions locked)
  - 2026-05-11 — Phase 2 architecture review (3 blockers + 4 concerns surfaced)
  - 2026-05-11 — Phase 3 refinement (15 DECs locked), Phase 4 detailing (9 stories)
  - 2026-05-11 — Phase 5 published as draft PR #78
  - 2026-05-11 — Phase 6 approved, Phase 7 devolved to beads

## Beads manifest

- **Epic:** `bd_1-scaffolding-t1o` — "47: signalforge init-demo subcommand"
- **Tasks:**
  - `bd_1-scaffolding-t1o.1` — US-001 — Bootstrap `src/signalforge/_demo/` tree + parity test (no deps)
  - `bd_1-scaffolding-t1o.2` — US-002 — Wire wheel packaging + `wheel_smoke` maintainer gate (depends on .1)
  - `bd_1-scaffolding-t1o.3` — US-003 — Public `signalforge.demo.copy_demo` module (depends on .1)
  - `bd_1-scaffolding-t1o.4` — US-004 — CLI `init-demo` subcommand + typed CLI errors (depends on .3)
  - `bd_1-scaffolding-t1o.5` — US-005 — 5-surface parity test (depends on .4, .7)
  - `bd_1-scaffolding-t1o.6` — US-006 — Subprocess `--help` smoke (depends on .4)
  - `bd_1-scaffolding-t1o.7` — US-007 — Docs: README + `cli-ops.md` + `CLAUDE.md` (depends on .4)
  - `bd_1-scaffolding-t1o.8` — US-008 — Quality Gate (depends on .1..7)
  - `bd_1-scaffolding-t1o.9` — US-009 — Patterns & Memory (depends on .8)
- **Worktree:** `/home/wesd/Projects/worktrees/SignalForge/47-init-demo`
- **Branch:** `feature/47-init-demo`

## Ticket summary

Add `signalforge init-demo [<dest>]` subcommand that copies a packaged Austin dbt-bikeshare demo out of the installed wheel into `<dest>` (default `./signalforge-demo/`). Refuses non-empty `<dest>` unless `--force` is set. Prints a one-screen "next steps" message naming the env vars + commands a PyPI user needs to actually run the demo. Replaces the broken `cp -r tests/fixtures/...` snippet in README Quick Start (which assumes a clone but is presented under `pip install signalforge-dbt`).

**AC-1** `signalforge init-demo` works post-`pip install signalforge-dbt` with no repo files present.
**AC-2** Demo fixture lives in the wheel — verifiable via `unzip -l <wheel> | grep _demo`.
**AC-3** `--force` semantics: refuses non-empty `<dest>` unless set.
**AC-4** README Quick Start + `docs/cli-ops.md` § Subcommands both updated.
**AC-5** Subprocess smoke test extends to cover `signalforge init-demo --help`.

## Discovery

### Codebase findings (key seams)

- **CLI subcommand template** — `src/signalforge/cli/version.py` (40 lines) and `src/signalforge/cli/lint.py` (~240 lines) are the canonical shape. Each exports two public symbols: `add_parser(subparsers) -> None` and `cmd_<name>(args) -> int`. Registered from `src/signalforge/cli/__init__.py:79-81`. `init-demo` follows this verbatim — new module `src/signalforge/cli/init_demo.py`.
- **CLI error layer** — `src/signalforge/cli/errors.py` ships `CliError` / `CliPathError` / `CliInputError`. New typed errors for this ticket subclass one of those bases and land in `_EXCEPTION_TO_EXIT_CODE` (7th AST scan auto-gates).
- **Path canonicalisation** — `src/signalforge/cli/_helpers.py::canonicalise_user_path(raw, project_dir)` is the project-wide gate. `init-demo`'s `<dest>` is not strictly inside a project_dir (the operator runs it before they have a project), so the canonicalisation contract needs a small adaptation — see refinement.
- **No existing `importlib.resources` consumer** — SignalForge does not currently load any package data at runtime. Greps for `importlib.resources`, `pkg_resources`, and `__file__`-relative resource lookups returned no hits under `src/signalforge/`. This ticket adds the project's first.
- **Hatch wheel target** — `pyproject.toml:36-37`:
  ```toml
  [tool.hatch.build.targets.wheel]
  packages = ["src/signalforge"]
  ```
  No `MANIFEST.in`, no `shared-data`, no `force-include`, no `include-package-data`. To ship the demo, files MUST live under `src/signalforge/` (current `packages` declaration includes everything under that tree); files outside it are not packaged.
- **Austin fixture inventory** — `tests/fixtures/dbt_project_austin/` (8 files, ~22 KB content + 13 KB manifest = ~64 KB total on disk):
  - `dbt_project.yml` (670 B)
  - `signalforge.yml` (547 B)
  - `models/staging/sources.yml` (1.3 KB)
  - `models/staging/stg_bikeshare_trips.sql` (662 B)
  - `target/manifest.json` (13 KB) — locked dbt v1.8 manifest seed
  - `profiles.yml` (1.4 KB) — current copy has a "DO NOT use signalforge against this" header and a `bigquery-public-data` placeholder that is billing-broken; demo copy needs a rewrite with `<YOUR_GCP_PROJECT>` placeholder
  - `.gitignore` (761 B) — references issue #10 / DEC-021; can be slimmed for demo audience
  - `regenerate.sh` (4.3 KB) — maintainer-only; uses `uvx dbt parse`; MUST NOT ship in demo
- **README Quick Start snippet to replace** — `README.md:112-129` (`mkdir -p /tmp/sf-austin && cp -r tests/fixtures/dbt_project_austin/. /tmp/sf-austin/` heredoc through `signalforge generate ...`). Also `README.md:47` (intro mention of `tests/fixtures/dbt_project_austin/`) and `README.md:161` (`/tmp/sf-austin/.signalforge/` reference) for cross-surface consistency.
- **`docs/cli-ops.md` § Subcommands template** — Lines 55-271 contain three existing entries (`generate`, `lint`, `version`). `lint` (244-263) is the closest shape precedent for `init-demo` (short, two-paragraph blurb + flags list). The line `"The CLI exposes three subcommands"` (line 57) must bump to four.
- **CLAUDE.md public API surface** — Line 33 documents `signalforge.cli.main(...)` + `CliError` hierarchy. If `init-demo` exposes a new `signalforge.demo` module (e.g., a `copy_demo(dest: Path, *, force: bool) -> None` library entry point), it lands in that bullet. Defaults assume CLI-only — see refinement.
- **Subprocess smoke test** — `tests/cli/test_subprocess_smoke.py::test_signalforge_version_via_subprocess` is the precedent. Marker `@pytest.mark.cli_subprocess`, excluded by default `addopts`, run via `pytest -m cli_subprocess --no-cov`. The new test follows the same shape but invokes `signalforge init-demo --help`.

### Rules constraints (informing detailing)

1. **`cli-layer.md` DEC-009** — Flat subcommand layout: one new module `src/signalforge/cli/init_demo.py` exporting `add_parser` + `cmd_init_demo`. Registered in `__init__.py`. No nested dirs.
2. **`cli-layer.md` DEC-008 / DEC-019 / DEC-024 (7th AST scan)** — Every new concrete `*Error` subclass under `cli/errors.py` lands in `_EXCEPTION_TO_EXIT_CODE` at an explicit tier. Likely additions:
   - `CliInitDemoDestExistsError(CliInputError)` — tier 2 (non-empty dest, no `--force`); ticket-suggested behaviour reads as input-validation, not load-time-state-not-ready
   - `CliInitDemoFixtureMissingError(CliError)` — tier 1 (the wheel didn't ship `_demo/`; broken install)
   - Possibly `CliInitDemoCopyError(CliError)` — tier 1 (filesystem write failure)
3. **`cli-layer.md` DEC-016** — `cmd_init_demo` wraps the whole pipeline in one `try/except Exception`, returns `map_exception_to_exit_code(exc)`, never leaks a traceback. Floor-of-every-test assertion: `"Traceback" not in capsys.readouterr().err`.
4. **`cli-layer.md` DEC-017** — Stderr shape: `ERROR: <message>` plus optional `↳ Remediation:` footer. Multi-violation bullets only if a multi-error case lands (unlikely here).
5. **`cli-layer.md` DEC-019 (logger grep gate)** — AST-based gate scans `src/signalforge/cli/init_demo.py`. Any `_LOGGER` call uses lazy-format JSON (`_LOGGER.info("event: %s", json.dumps({...}))`); never f-string. Likely v0.1 init-demo has zero `_LOGGER` calls — stdout prints are the operator channel.
6. **`cli-layer.md` 5-surface parity** — argparse help string + handler docstring + `docs/cli-ops.md` § Subcommands entry + test name + DEC in this plan, all updated in the implementation PR.
7. **`cli-layer.md` DEC-027** — Path canonicalisation pattern. `init-demo`'s `<dest>` is NOT a project_dir assertion (no `dbt_project.yml` required); it's an output directory chosen by the operator. Adapt: resolve via `Path(dest).resolve()` for symlink safety, but don't require containment in a project tree. New typed error covers the "resolve failed" case.
8. **`python-build.md` DEC-011** — Wheel target packaging is non-negotiable: `[tool.hatch.build.targets.wheel] packages = ["src/signalforge"]` already covers everything under that tree, so placing `_demo/` files under `src/signalforge/_demo/` gets them shipped automatically. Hatch will NOT auto-discover files outside the declared `packages` — `tests/fixtures/...` is invisible to the wheel.
9. **`testing-signal.md` (no `assert True`)** — Every new test must be capable of failing on regression. In-process test calls `main(["init-demo", str(tmp_path)])`, asserts return == 0, asserts specific demo files exist at `tmp_path` (e.g., `(tmp_path / "models" / "staging" / "stg_bikeshare_trips.sql").is_file()`).
10. **`testing-signal.md` (subprocess-gated pattern)** — New subprocess test inherits the `@pytest.mark.cli_subprocess` marker; CI ignores it by default; maintainers run `pytest -m cli_subprocess --no-cov`. Single subprocess test is the source of truth for "the wheel actually exposes the script" — extend the existing test file rather than adding a new one.
11. **No new audit-event class, no new AST scan** — `init-demo` writes no JSONL audit, owns no fail-closed writer. The 7th AST scan auto-covers the new error classes. No 8th scan needed.
12. **CLAUDE.md "Public API surface"** — Only update if `init-demo` exposes a new public Python entry point (e.g., `signalforge.demo.copy_demo(...)`). If CLI-internal, no entry needed.

### Scoping decisions (locked Phase 1)

- **SD-1 — Two copies + parity test.** `src/signalforge/_demo/` is the shipped tree (lands in the wheel via existing `packages = ["src/signalforge"]`). `tests/fixtures/dbt_project_austin/` stays as the test fixture for issue #10's e2e smoke. A new parity test reads both trees and asserts byte-equality except for two documented rewrites: `profiles.yml` (shipped copy uses `env_var('GOOGLE_CLOUD_PROJECT')`; test copy keeps the maintainer-only header) and `.gitignore` (shipped copy slimmed for demo audience). `tests/fixtures/dbt_project_austin/regenerate.sh` is amended to update BOTH trees in lockstep.
- **SD-2 — `env_var('GOOGLE_CLOUD_PROJECT')` placeholder.** The shipped `profiles.yml` uses dbt's native `env_var(...)` lookup so an operator with `GOOGLE_CLOUD_PROJECT` set (already the README's recommendation) runs the demo with zero file edits. Aligns with `README.md:119-123` precedent.
- **SD-3 — In-process end-to-end + subprocess `--help`.** In-process test calls `main(["init-demo", str(tmp_path)])`, asserts return code 0, asserts representative files exist at `tmp_path` (covers AC-2 in default CI). Subprocess test invokes `signalforge init-demo --help` under `@pytest.mark.cli_subprocess` (covers `[project.scripts]` wiring per AC-5).
- **SD-4 — Ship locked `target/manifest.json`.** `_demo/target/manifest.json` ships the existing 13 KB dbt-v1.8-locked manifest seed so `signalforge generate --dry-run` works out of the box. Next-steps message references `dbt parse` for refresh; no extra friction on first run.
- **SD-5 — Public `signalforge.demo.copy_demo(...)`.** New public module `signalforge.demo` exposes `copy_demo(dest: Path, *, force: bool = False) -> None`. CLI calls into it. CLAUDE.md "Public API surface" gets a new bullet alongside `signalforge.cli.main(...)`. Library callers (notebooks, scripts) get a clean programmatic entry.
- **SD-6 — Tier 2 exit code for dest-exists-without-force.** `CliInitDemoDestExistsError(CliInputError)` → exit 2 (input validation; mirrors `ModelNotFoundError` precedent — operator supplied a path the CLI rejects pre-action). Lands in `_EXCEPTION_TO_EXIT_CODE`; 7th AST scan auto-gates.

---

## Architecture Review

### Security

| # | Area | Rating | Finding |
|---|------|--------|---------|
| S-1 | Path/symlink defence on `<dest>` | concern | `init-demo` has no `project_dir` context (operator is *creating* one), so the project's `canonicalise_path(input, project_dir)` containment helper doesn't apply directly. Need a standalone resolve + symlink-cycle guard. Open question: refuse `<dest>` that resolves outside `Path.home()` / contains marker files (`.git/`, `.bashrc`), or trust the operator? |
| S-2 | `--force` blast radius | **BLOCKER** | The ticket says "`--force` allows overwrite of non-empty dest" but doesn't specify semantics. Three options: (a) `rmtree(dest) && copy` — atomic, simple, catastrophic if `dest=~`; (b) merge-overwrite individual files — polluting `~` is recoverable; (c) refuse per-file clobber even with `--force` — safest, may leave a partial tree. Must pick one. |
| S-3 | `importlib.resources` extraction | pass | `importlib.resources.files("signalforge") / "_demo"` is safe by construction — the API rejects `../` escapes in resource names. Source tree is in our repo + gated by parity test. Use `importlib.resources.as_file(...)` context manager for the temp-extract pattern. |
| S-4 | Symlinks inside the shipped `_demo/` tree | concern | Source tree has no symlinks today, but `shutil.copytree(..., symlinks=False)` plus a parity-test assertion "_demo/ contains no symlinks" codifies the policy and gates future drift. |
| S-5 | TOCTOU between `dest.exists()` check and `copytree` | pass | Theoretical only; matches project-wide stance (loader / profiles layer doesn't defend either). Document in handler docstring. |
| S-6 | Next-steps message naming `ANTHROPIC_API_KEY` | pass | The message names env-var names, not values. Same content the README already prints. No new disclosure surface. |
| S-7 | dbt `env_var('GOOGLE_CLOUD_PROJECT')` in shipped `profiles.yml` | pass | Lookup-time substitution; not a template-injection vector. dbt-native pattern. |

### Packaging / installation

| # | Area | Rating | Finding |
|---|------|--------|---------|
| P-1 | Will `_demo/` data files actually ship in the wheel? | **BLOCKER** | The plan's earlier assumption that `packages = ["src/signalforge"]` auto-ships non-`.py` files was **wrong**. Hatchling's default `packages` glob only picks up `.py`. Verified empirically: a current `python -m build --wheel` produces a 324 KB wheel with zero data files. Must add an explicit `include` directive: `[tool.hatch.build.targets.wheel] include = ["src/signalforge/_demo"]` (alongside the existing `packages` line). Verify post-fix with `unzip -l dist/*.whl \| grep _demo/`. |
| P-2 | `importlib.resources` discoverability without `__init__.py` in `_demo/` | pass | Python 3.11+ `Traversable` resolves subdirs without requiring `__init__.py`. `_demo/` stays a plain data dir; don't add a marker. |
| P-3 | Editable install (`pip install -e .`) | pass | Hatchling editable reads directly from `src/signalforge/` on disk; `_demo/` is discoverable immediately, `include` directive is not consulted in editable mode. CI is unaffected. |
| P-4 | CI gate for AC-2 ("demo fixture lives in the wheel") | **BLOCKER** | Manual `unzip -l` at release-time is brittle. AC-2 needs a CI gate. Two options: (i) maintainer-only `pytest -m wheel_smoke` that runs `hatch build` and asserts `_demo/` files in the artifact; (ii) one-shot subprocess test that asserts `importlib.resources.files("signalforge") / "_demo"` has the expected file count when run against the installed editable build. Option (ii) is cheaper and runs in default CI; option (i) is the only one that catches an `include` typo. |
| P-5 | Wheel size: 64 KB demo on top of 324 KB current (~20%) | concern | Acceptable for a CLI tool. Document in CHANGELOG. |
| P-6 | Will Hatchling pick up `_demo/.gitignore`? | concern | Hatchling's `include` glob behaviour on dotfiles is not guaranteed. Verify with a built-wheel inspection; if `.gitignore` is missing, rename to `gitignore.demo` in the shipped tree and have `copy_demo` rewrite to `.gitignore` at copy time. The shipped name matters less than the on-disk name post-copy. |
| P-7 | `target/manifest.json` shipped under `_demo/target/` | pass | Hatchling does not treat `target/` specially. Ships like any other data file under `include`. |
| P-8 | 5-surface parity for `--force` | pass | Precedent at `tests/cli/test_5_surface_parity_select.py` (issue #37). Copy the shape into a new `tests/cli/test_5_surface_parity_init_demo.py`. |

### Testing strategy

| # | Area | Rating | Finding |
|---|------|--------|---------|
| T-1 | In-process end-to-end test | pass | Existing pattern: `main(["init-demo", str(tmp_path)])` → assert exit 0, assert representative files appeared. Gates AC-2 in default CI (no marker). |
| T-2 | Subprocess `--help` smoke | pass | Extend `tests/cli/test_subprocess_smoke.py` with a second test invoking `signalforge init-demo --help` (same `@pytest.mark.cli_subprocess` marker). |
| T-3 | Parity test between `src/signalforge/_demo/` and `tests/fixtures/dbt_project_austin/` | pass | New `tests/test_demo_fixture_parity.py` (or similar) reads both trees, asserts byte-equality except for two named files (`profiles.yml`, `.gitignore`). Closes the SD-1 drift hole. |
| T-4 | Wheel-build CI gate (P-4 above) | **BLOCKER** | See P-4. Picking option (i) or (ii) is the refinement decision. |

### Blockers to resolve in refinement

- **B-1 (S-2):** `--force` semantics — pick atomic replace, merge-overwrite, or refuse-per-file-clobber.
- **B-2 (P-1):** Confirm `include = ["src/signalforge/_demo"]` directive lands in `pyproject.toml`. (Implementation-level; no design alternative.)
- **B-3 (P-4 / T-4):** CI gate for AC-2 — wheel_smoke marker that runs `hatch build` and inspects the artifact, OR in-process resource-existence check.

### Concerns to address

- **C-1 (S-1):** Path-resolve strategy for `<dest>` — standalone `Path(dest).resolve()` + symlink-cycle catch; optional marker-file refusal (`.git/`, `.bashrc`).
- **C-2 (S-4):** `shutil.copytree(symlinks=False)` + parity-test assertion that `_demo/` ships no symlinks.
- **C-3 (P-6):** Dotfile inclusion — verify `.gitignore` in built wheel; fall back to `gitignore.demo` rename + copy-time rewrite if Hatchling drops it.
- **C-4 (P-5):** Document the +64 KB wheel size in CHANGELOG / next-steps.

---

## Refinement log (DECs)

- **DEC-001 (B-1) — `--force` = atomic replace.** `copy_demo` with `force=True` runs `shutil.rmtree(dest)` (after confirming dest is not `/`, not `Path.home()`, not the cwd) then `shutil.copytree(_demo_src, dest)`. Matches `cp -rf` user expectations and produces a verbatim demo tree. The catastrophic-`~` footgun is partially mitigated by the symlink-cycle guard in DEC-004 plus the runtime sanity check that `dest.resolve() not in (Path("/"), Path.home(), Path.cwd())` — refuse with `CliInitDemoUnsafeDestError(CliInputError)` (tier 2). Without `--force`, `dest.exists() and any(dest.iterdir())` raises `CliInitDemoDestExistsError`. Empty existing dirs proceed without `--force`.
- **DEC-002 (B-2) — Hatch wheel `include` directive.** `pyproject.toml` `[tool.hatch.build.targets.wheel]` grows an explicit `include = ["src/signalforge/_demo"]` line alongside the existing `packages = ["src/signalforge"]`. Verified empirically that without this the data files are silently dropped. Pure implementation; no design alternative.
- **DEC-003 (B-3 / T-4) — `@pytest.mark.wheel_smoke` maintainer gate.** New marker registered in `pyproject.toml` `[tool.pytest.ini_options].markers` and added to default `addopts` exclusion (`-m 'not bigquery and not anthropic and not cli_subprocess and not e2e and not wheel_smoke'`). Test at `tests/test_wheel_packaging.py::test_wheel_includes_demo_fixture` shells out `python -m build --wheel --outdir <tmp>`, opens the artifact via `zipfile.ZipFile`, asserts the canonical demo file set + `.gitignore` appear under `signalforge/_demo/`. Maintainers run `pytest -m wheel_smoke --no-cov` before declaring an init-demo PR ready. Mirrors `cli_subprocess` precedent (`testing-signal.md` § subprocess-gated smoke pattern).
- **DEC-004 (C-1) — Standalone path resolve, no containment boundary.** `<dest>` flows through `Path(dest).expanduser().resolve()` with `try/except RuntimeError` for symlink cycles → `CliPathError(cause=...)`. **No** marker-file refusal, **no** `Path.home()` containment. Matches `mkdir` / `cp` / `tar` UX (these don't refuse home-dir writes either). The `--force` blast-radius guard from DEC-001 catches the most catastrophic case (`signalforge init-demo --force ~`) explicitly. Note: `init-demo` does NOT route through `canonicalise_user_path(...)` — that helper enforces a `project_dir` containment boundary that doesn't apply here (operator is *creating* the project). Documented in handler docstring.
- **DEC-005 (C-2) — `shutil.copytree(symlinks=False)` + zero-symlinks parity assertion.** `copy_demo` follows symlinks (i.e. `symlinks=False` means "copy contents, not the link itself"). The parity test from SD-1 also asserts `_demo/` contains zero symlinks via `Path.rglob('*')` + `is_symlink()`. Codifies the "shipped demo is symlink-free" policy. Future drift breaks the parity test loudly.
- **DEC-006 (C-3) — Ship `.gitignore` as-is; wheel_smoke gates inclusion.** Keep the filename. The DEC-003 wheel_smoke test asserts `signalforge/_demo/.gitignore` appears in the built wheel. If Hatchling silently drops it under the directory glob, add an explicit `include = ["src/signalforge/_demo", "src/signalforge/_demo/.gitignore"]` extension — discovered at test-run time, not pre-emptively. Fallback (`gitignore.demo` rename + copy-time rewrite) is documented but not implemented in v0.1.
- **DEC-007 (C-4) — Document +64 KB wheel size in PR body.** No CHANGELOG file exists in v0.1; the PR description carries the size delta. Future v0.x ticket may introduce CHANGELOG; this DEC is a no-op until then.
- **DEC-008 (SD-1) — Parity test for `_demo/` ↔ `tests/fixtures/dbt_project_austin/`.** New `tests/test_demo_fixture_parity.py`: walks both trees, asserts byte-equality EXCEPT for two named files (`profiles.yml`, `.gitignore`). The two exceptions are documented in the test with a clear comment naming the rewrite (the shipped `profiles.yml` uses `env_var('GOOGLE_CLOUD_PROJECT')`; the test-fixture `profiles.yml` has the maintainer-only "DO NOT signalforge against this" header). Drift in any other file fails the test.
- **DEC-009 (SD-2) — Shipped `profiles.yml` uses `env_var('GOOGLE_CLOUD_PROJECT')`.** dbt-native lookup; operator with the env var set runs the demo with zero file edits. Aligns with `README.md:119-123` precedent.
- **DEC-010 (SD-3) — In-process e2e + subprocess `--help`.** `tests/cli/test_init_demo.py` covers in-process happy path + every error tier via `main([...])`. `tests/cli/test_subprocess_smoke.py` extends with one `signalforge init-demo --help` invocation under `@pytest.mark.cli_subprocess`.
- **DEC-011 (SD-4) — Ship locked `target/manifest.json`.** The existing 13 KB dbt-v1.8-locked manifest seed ships at `src/signalforge/_demo/target/manifest.json`. Next-steps message references `dbt parse` for refresh (no in-PR dbt invocation).
- **DEC-012 (SD-5) — Public `signalforge.demo.copy_demo(dest, *, force=False) -> None`.** New module `src/signalforge/demo.py`. CLI calls into it. `CLAUDE.md` "Public API surface" gets a new bullet alongside `signalforge.cli.main(...)`. Library callers (notebooks, scripts) get a programmatic entry point. The function raises `DemoDestExistsError`, `DemoDestUnsafeError`, `DemoFixtureMissingError` (lower-level typed errors); CLI wraps them into `Cli*Error` subclasses at the handler boundary so the CLI exit-code taxonomy stays homogeneous.
- **DEC-013 (SD-6) — Tier 2 exit code for dest-exists-without-force.** `CliInitDemoDestExistsError(CliInputError)` → exit 2. Mirrors `ModelNotFoundError` precedent. Lands in `_EXCEPTION_TO_EXIT_CODE`; 7th AST scan auto-gates.
- **DEC-014 — Next-steps message: plain text, stdout.** The post-copy message prints to stdout (operator channel), uses no ANSI colour codes, no markdown. Names `GOOGLE_CLOUD_PROJECT` and `ANTHROPIC_API_KEY` env vars + the exact commands to run (`signalforge lint`, `signalforge generate models/staging/stg_bikeshare_trips.sql --dry-run`). Single canonical wording lives in `signalforge.cli.init_demo._NEXT_STEPS_MESSAGE`. The message survives `--no-color` because it carries no colour codes.
- **DEC-015 — `regenerate.sh` updates BOTH trees in lockstep.** `tests/fixtures/dbt_project_austin/regenerate.sh` gains a final phase: after the live `dbt parse` lands the test-fixture manifest, the script `cp`s every file into `src/signalforge/_demo/`, then applies the demo-only rewrites (`profiles.yml` swap to `env_var(...)`; `.gitignore` slimmed). The parity test (DEC-008) gates future drift. Maintainers running the script keep both trees aligned by construction.

### Decision dependencies

- DEC-001 → DEC-013 (force semantics define the dest-exists error semantics).
- DEC-002 → DEC-003 (Hatch include directive is the surface DEC-003 verifies).
- DEC-008 → DEC-009 + DEC-015 (parity test gates the only two allowed deltas, regen script maintains both sides).
- DEC-012 → DEC-013 (CLI errors wrap the lower-level demo-module errors).

---

## Detailed Breakdown (stories)

Order follows SignalForge's typical layering: data → library → CLI surface → tests → docs → quality. Each story fits one Ralph context window. Every story's acceptance criteria includes the canonical validation command:

```bash
ruff check . && ruff format --check . && pyright && pytest
```

### US-001 — Bootstrap `src/signalforge/_demo/` tree + parity test

**Description:** Create the shipped demo tree as a copy of the test fixture with two named rewrites. Add a parity test that gates future drift. Amend the existing regen script to maintain both sides in lockstep.

**Traces to:** SD-1, DEC-008, DEC-009, DEC-015.

**Files:**
- NEW `src/signalforge/_demo/dbt_project.yml` — copy of `tests/fixtures/dbt_project_austin/dbt_project.yml`
- NEW `src/signalforge/_demo/signalforge.yml` — copy
- NEW `src/signalforge/_demo/profiles.yml` — copy with the project target rewritten to use `env_var('GOOGLE_CLOUD_PROJECT')` per DEC-009; strip the maintainer-only "DO NOT signalforge against this" header
- NEW `src/signalforge/_demo/.gitignore` — copy, slimmed of issue-#10 / DEC-021 internal references
- NEW `src/signalforge/_demo/models/staging/sources.yml` — copy
- NEW `src/signalforge/_demo/models/staging/stg_bikeshare_trips.sql` — copy
- NEW `src/signalforge/_demo/target/manifest.json` — copy
- NEW `tests/test_demo_fixture_parity.py` — walks both trees, asserts byte-equality EXCEPT for the two named files; asserts `_demo/` contains zero symlinks (`is_symlink()` over `rglob('*')`)
- MOD `tests/fixtures/dbt_project_austin/regenerate.sh` — final phase: copy each file into `src/signalforge/_demo/`, apply demo-only rewrites to `profiles.yml` and `.gitignore`

**TDD:**
- `test_demo_fixture_parity_holds_byte_for_byte_except_documented_files` — fails on uncommanded drift
- `test_demo_fixture_contains_no_symlinks` — codifies DEC-005
- `test_demo_profiles_yml_uses_env_var_macro` — pins DEC-009 specifically
- `test_test_fixture_profiles_yml_retains_maintainer_header` — confirms the rewrite is one-way

**Done when:** Both fixture trees exist; parity test passes; the regen script runs end-to-end (manual maintainer check, not gated in CI).

**Depends on:** none.

---

### US-002 — Wire wheel packaging + wheel_smoke maintainer gate

**Description:** Add the Hatch `include` directive so `_demo/` ships in the wheel. Register the `wheel_smoke` pytest marker. Write the maintainer-only test that builds the wheel and asserts demo files appear.

**Traces to:** DEC-002, DEC-003, DEC-006.

**Files:**
- MOD `pyproject.toml`:
  - `[tool.hatch.build.targets.wheel]` → add `include = ["src/signalforge/_demo"]` alongside existing `packages`
  - `[tool.pytest.ini_options].markers` → register `wheel_smoke`
  - `[tool.pytest.ini_options].addopts` → extend the existing marker-exclusion expression to `... and not wheel_smoke`
- NEW `tests/test_wheel_packaging.py`:
  - `@pytest.mark.wheel_smoke`
  - subprocess `python -m build --wheel --outdir <tmp>`
  - open the artifact via `zipfile.ZipFile`, list members
  - assert the canonical demo file set appears under `signalforge/_demo/` (8 files including `.gitignore` per DEC-006)
  - assert the test sets a 60-second timeout (mirrors `cli_subprocess` precedent)

**TDD:**
- `test_wheel_includes_all_demo_files` (under `@pytest.mark.wheel_smoke`)
- `test_wheel_includes_demo_gitignore_dotfile` — DEC-006 specifically

**Done when:** `pytest -m wheel_smoke --no-cov` passes; default `pytest` excludes the marker; `unzip -l dist/*.whl | grep _demo/` shows 8 files.

**Depends on:** US-001.

---

### US-003 — Public `signalforge.demo.copy_demo(...)` module

**Description:** New public library entry point that locates the bundled `_demo/` tree via `importlib.resources`, validates the destination, and copies the tree. Owns the lower-level typed-error surface that the CLI wraps.

**Traces to:** DEC-001, DEC-004, DEC-005, DEC-011, DEC-012.

**Files:**
- NEW `src/signalforge/demo.py`:
  - Module docstring explains the public contract.
  - `copy_demo(dest: Path | str, *, force: bool = False) -> Path` — returns the resolved dest path.
  - Path handling: `dest = Path(raw_dest).expanduser().resolve()` with `RuntimeError` catch → `DemoPathError(cause=...)`.
  - Sanity gate: refuse `dest in (Path("/"), Path.home(), Path.cwd())` with `force=True` → `DemoDestUnsafeError`.
  - Existence gate: `dest.exists() and any(dest.iterdir())` with `force=False` → `DemoDestExistsError`. Empty existing dirs proceed.
  - Force branch: `force=True` with non-empty `dest` → `shutil.rmtree(dest)` then `shutil.copytree(...)`.
  - Source lookup: `importlib.resources.files("signalforge").joinpath("_demo")`; wrap with `importlib.resources.as_file(...)` for the temp-extract pattern; raise `DemoFixtureMissingError` if the path doesn't traverse.
  - `shutil.copytree(src, dest, symlinks=False)` per DEC-005.
- NEW `src/signalforge/_demo_errors.py` (or inline in `demo.py`): `DemoError(Exception)` base + `DemoPathError`, `DemoDestExistsError`, `DemoDestUnsafeError`, `DemoFixtureMissingError`.

**TDD:**
- `test_copy_demo_to_empty_dir_copies_all_files`
- `test_copy_demo_to_nonexistent_dir_creates_and_copies`
- `test_copy_demo_to_nonempty_dir_without_force_raises_dest_exists_error`
- `test_copy_demo_to_nonempty_dir_with_force_replaces_atomically`
- `test_copy_demo_force_against_home_raises_dest_unsafe_error`
- `test_copy_demo_force_against_root_raises_dest_unsafe_error`
- `test_copy_demo_force_against_cwd_raises_dest_unsafe_error`
- `test_copy_demo_with_symlink_dest_resolves_target` — symlink dest is followed, not preserved
- `test_copy_demo_with_cyclic_symlink_dest_raises_demo_path_error`
- `test_copy_demo_returns_resolved_dest_path`
- `test_copy_demo_copies_target_manifest_json` — DEC-011 specifically
- `test_copy_demo_copies_dotfile_gitignore` — DEC-006 specifically
- `test_copy_demo_with_relative_dest_resolves_against_cwd`

**Done when:** All TDD tests pass; `pyright src/signalforge/demo.py` clean; module is importable as `from signalforge.demo import copy_demo`.

**Depends on:** US-001 (needs the source tree).

---

### US-004 — CLI subcommand `init-demo` + typed CLI errors

**Description:** Add the CLI subcommand following the `version.py` / `lint.py` template. Wrap `signalforge.demo`'s lower-level typed errors at the handler boundary into CLI-tier errors. Register all new errors in `_EXCEPTION_TO_EXIT_CODE`. Emit the next-steps message to stdout on success.

**Traces to:** DEC-001, DEC-012, DEC-013, DEC-014.

**Files:**
- NEW `src/signalforge/cli/init_demo.py`:
  - `add_parser(subparsers)` registers positional `dest` (optional, default `./signalforge-demo/`) + `--force`
  - `cmd_init_demo(args) -> int` wraps the pipeline in a single `try/except Exception`, calls `signalforge.demo.copy_demo(args.dest, force=args.force)`, prints `_NEXT_STEPS_MESSAGE.format(dest=...)` to stdout, returns 0
  - `_NEXT_STEPS_MESSAGE` constant — plain text, names `GOOGLE_CLOUD_PROJECT` + `ANTHROPIC_API_KEY` + the three commands per DEC-014
  - On any exception: `format_error_to_stderr(exc)`, `return map_exception_to_exit_code(exc)`
- MOD `src/signalforge/cli/__init__.py` — register the subcommand (mirror existing `lint`/`version` add_parser calls)
- MOD `src/signalforge/cli/errors.py`:
  - `CliInitDemoDestExistsError(CliInputError)` — tier 2, wraps `DemoDestExistsError`
  - `CliInitDemoDestUnsafeError(CliInputError)` — tier 2, wraps `DemoDestUnsafeError`
  - `CliInitDemoFixtureMissingError(CliError)` — tier 1 (broken install), wraps `DemoFixtureMissingError`
  - `CliInitDemoCopyError(CliError)` — tier 1, generic copy failure (`OSError` / `shutil` errors)
  - Each carries a `default_remediation`
- MOD `src/signalforge/cli/_helpers.py` (`_EXCEPTION_TO_EXIT_CODE`) — register all four new error classes (`CliPathError` already mapped; reused for DEC-004 symlink-cycle case)

**TDD:**
- `test_cmd_init_demo_to_fresh_path_returns_0_and_prints_next_steps`
- `test_cmd_init_demo_emits_next_steps_naming_env_vars` — DEC-014 specifically (asserts `"GOOGLE_CLOUD_PROJECT"` and `"ANTHROPIC_API_KEY"` in stdout)
- `test_cmd_init_demo_against_existing_nonempty_dir_returns_exit_2_with_remediation`
- `test_cmd_init_demo_force_against_existing_nonempty_dir_returns_0`
- `test_cmd_init_demo_force_against_home_returns_exit_2_dest_unsafe`
- `test_cmd_init_demo_never_leaks_traceback` — DEC-016 of `cli-layer.md` floor-of-every-CLI-test assertion
- `test_init_demo_help_lists_force_flag` — argparse help surface (precursor to 5-surface parity)
- `test_init_demo_help_lists_dest_positional`
- `test_init_demo_default_dest_is_signalforge_demo` — pins the default
- `test_cli_init_demo_dest_exists_error_in_exit_code_table` — 7th AST scan auto-covers, but pin explicit tier 2 assertion

**Done when:** `signalforge init-demo --help` works; in-process e2e tests pass; all four new errors land in the exit-code mapping table; 7th AST scan passes.

**Depends on:** US-003.

---

### US-005 — 5-surface parity test for `init-demo` + `--force`

**Description:** New parity test mirroring `tests/cli/test_5_surface_parity_select.py` (issue #37). Asserts the `init-demo` subcommand name and the `--force` flag appear with consistent semantics across argparse help, handler docstring, `docs/cli-ops.md`, and this plan's DEC list.

**Traces to:** `cli-layer.md` 5-surface parity rule; DEC-001.

**Files:**
- NEW `tests/cli/test_5_surface_parity_init_demo.py`:
  - Asserts `"--force"` and the destination-positional name appear in: argparse help output (via `main(["init-demo", "--help"])` capturing stdout), `signalforge.cli.init_demo.__doc__` / handler docstring, `docs/cli-ops.md` § Subcommands, `plans/super/47-init-demo.md` DEC list
  - Copy the shape from `test_5_surface_parity_select.py` verbatim where applicable

**TDD:** the test itself is the artefact; no separate unit tests.

**Done when:** `pytest tests/cli/test_5_surface_parity_init_demo.py` passes; the four surfaces are aligned in the same commit.

**Depends on:** US-004, US-007 (needs `docs/cli-ops.md` updated first OR the test is written to fail until US-007 lands; recommend gating the test with `@pytest.mark.xfail(strict=True, reason="enabled by US-007")` and removing the marker in US-007).

---

### US-006 — Subprocess smoke test for `init-demo --help`

**Description:** Extend the existing subprocess smoke test file with one `signalforge init-demo --help` invocation. Catches `[project.scripts]` regressions specifically against the new subcommand.

**Traces to:** DEC-010; ticket AC-5.

**Files:**
- MOD `tests/cli/test_subprocess_smoke.py`:
  - New test `test_signalforge_init_demo_help_via_subprocess` under `@pytest.mark.cli_subprocess`
  - `subprocess.run(["signalforge", "init-demo", "--help"], capture_output=True, text=True, timeout=10)`
  - assert returncode == 0; assert stdout contains `"init-demo"`; assert `"Traceback" not in result.stderr`

**TDD:** the test itself.

**Done when:** `pytest -m cli_subprocess --no-cov` passes (maintainer-only); the new test is excluded by default `addopts`.

**Depends on:** US-004.

---

### US-007 — Docs: README + `docs/cli-ops.md` + CLAUDE.md

**Description:** Update README Quick Start to use `signalforge init-demo` instead of the `cp -r` snippet. Add `init-demo` entry to `docs/cli-ops.md` § Subcommands following the `lint` precedent. Extend CLAUDE.md "Public API surface" with the new `signalforge.demo.copy_demo` symbol.

**Traces to:** AC-4; DEC-012; `cli-layer.md` 5-surface parity rule.

**Files:**
- MOD `README.md`:
  - Lines 47, 112-129, 161 — replace the `cp -r tests/fixtures/dbt_project_austin/...` snippet with `signalforge init-demo /tmp/sf-austin` (or similar); strip the inline heredoc for `profiles.yml` since the shipped one now uses `env_var('GOOGLE_CLOUD_PROJECT')`
- MOD `docs/cli-ops.md`:
  - Line 57: bump "three subcommands" → "four subcommands"
  - § Subcommands: new `### \`signalforge init-demo [<dest>]\`` entry between `generate` and `lint` (or after `version` — pick by alphabetical or feature-grouping convention; check existing order)
  - Document: positional `<dest>`, `--force` flag, error tiers, the next-steps message
- MOD `CLAUDE.md`:
  - Public API surface bullet for `signalforge.cli.main(...)` — extend to mention `signalforge.demo.copy_demo(dest: Path | str, *, force: bool = False) -> Path` and the `DemoError` hierarchy (`DemoError`, `DemoPathError`, `DemoDestExistsError`, `DemoDestUnsafeError`, `DemoFixtureMissingError`)
  - Repository-status block: add `#47 (init-demo)` entry summarising the ticket

**TDD:** none (docs); the 5-surface parity test (US-005) is the implicit gate.

**Done when:** the three doc surfaces describe `init-demo` consistently; the README Quick Start reads cleanly for a PyPI user who has never cloned the repo.

**Depends on:** US-004.

---

### US-008 — Quality Gate

**Description:** Run the code-reviewer agent four times across the full changeset, fixing all real bugs found each pass. Run CodeRabbit review if available. Project validation (`ruff check . && ruff format --check . && pyright && pytest`) must pass after all fixes. Additionally run the gated marker suites once locally: `pytest -m wheel_smoke --no-cov`, `pytest -m cli_subprocess --no-cov`.

**Traces to:** All DECs (verification pass).

**Done when:** Four review passes complete; no real bugs remaining; all gated suites pass.

**Depends on:** US-001, US-002, US-003, US-004, US-005, US-006, US-007 (everything).

---

### US-009 — Patterns & Memory

**Description:** Update `.claude/rules/` with new patterns learned from this ticket and refresh CLAUDE.md.

**Traces to:** Repo convention maintenance.

**Likely additions:**
- `.claude/rules/python-build.md` — new section on shipping non-`.py` package-data via Hatch's `include` directive (DEC-002), and the `wheel_smoke` maintainer-gate pattern (DEC-003). Document the empirical-verification step (`python -m build --wheel && unzip -l ...`).
- `.claude/rules/cli-layer.md` — extend the subcommand layout section with `init-demo` as a fourth precedent; note the new `signalforge.demo` library-surface pattern (CLI calls into a public lib module with its own typed error hierarchy, wrapped at the handler boundary).
- `.claude/rules/testing-signal.md` — extend the gated-marker section with `wheel_smoke` as a fourth marker (alongside `bigquery`, `anthropic`, `cli_subprocess`, `e2e`).
- `CLAUDE.md` — repo-status block already updated in US-007; verify nothing else needs sync.

**Done when:** Rules reflect the new patterns; CLAUDE.md is consistent.

**Depends on:** US-008.

---

## Rules compliance gate

Validated each story against the 12 rules-constraints from Phase 1:

| Rule | Story coverage |
|------|----------------|
| cli-layer.md DEC-009 flat layout | US-004 |
| cli-layer.md DEC-008/024 four-tier exits + 7th AST scan | US-004 |
| cli-layer.md DEC-016 no traceback | US-004 (TDD includes traceback assertion) |
| cli-layer.md DEC-017 stderr shape | US-004 (typed errors carry remediation) |
| cli-layer.md DEC-019 logger grep gate | US-003, US-004 (no `_LOGGER` calls; stdout prints) |
| cli-layer.md DEC-027 path canonicalisation | US-003 (standalone resolve per DEC-004) |
| cli-layer.md 5-surface parity | US-005 (dedicated test) |
| python-build.md DEC-011 wheel packaging | US-002 |
| testing-signal.md no `assert True` | All test stories (specific assertions) |
| testing-signal.md src layout | already in place; no change |
| testing-signal.md subprocess-gated smoke | US-006 |
| testing-signal.md coverage gate | US-002 adds wheel_smoke exclusion to `addopts`; default `pytest` still passes coverage floor |

---

