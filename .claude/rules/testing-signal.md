# Testing: signal over volume

Established by issue #1 scaffolding (DEC-010). Apply to every test in this repo.

This rule encodes SignalForge's first architectural commitment (signal over volume) from `CLAUDE.md` into the test suite: a test that always passes is worse than no test, because it consumes review attention without catching anything.

## No `assert True`-shaped tests

Every test must be capable of failing if its target is broken. The smoke test (`tests/test_smoke.py`) is the floor: imports the package, asserts `__version__` is set, asserts PEP 440 shape. Each would fail on a real regression.

If you find yourself writing a test that can't fail, delete it instead of shipping it.

## Strict markers — both settings required (pytest 9 quirk)

```toml
[tool.pytest.ini_options]
addopts = "-ra --strict-markers"
strict_markers = true
```

`addopts = "--strict-markers"` alone is **not enough on pytest 9.x** — it sets `option.strict_markers=True`, but `_pytest/mark/structures.py` reads `getini("strict_markers")` instead. Without both, an unknown `@pytest.mark.foo` warns but does NOT error at collection time.

## src layout discovery

Do NOT create `tests/__init__.py`. Pytest's rootdir handles discovery for src layouts; an empty `__init__.py` masks import errors.

## Fixture regeneration via ephemeral `uvx`

When a fixture's correctness depends on an external tool's output (dbt's `manifest.json`, etc.), commit the *generated* artefact and document a regeneration script that runs the tool via `uvx` at a pinned version:

```bash
uvx --python 3.11 --from "dbt-duckdb==X.Y.*" --with "dbt-core==X.Y.*" dbt parse
```

Pin the tool version in dev-deps for the *latest* schema only — older versions are summoned ephemerally. Strip non-deterministic fields (`generated_at`, `invocation_id`, etc.) with `jq` before committing.

## Seeded determinism over snapshot normalisation

When a derived identifier needs to land in a snapshot fixture, prefer **seeded determinism** — `blake2b(stable_inputs, digest_size=N).hex()` over inputs that should produce the same output across runs — over post-hoc regex normalisation. The seeded path makes raw bytes stable; the normalisation path becomes a maintenance burden the moment the regex shape evolves. Reach for the hash recipe before reaching for `re.sub` in tests.

## Drift detection via one-off `extra="forbid"` model

If a parser uses `extra="ignore"` in production (forward-compat), pair it with a test that constructs a one-off `StrictModel(BaseModel)` with `extra="forbid"` and validates a known-current fixture against it. Adding a key to the fixture without updating the model breaks the test loudly.

## AST single-construction-seam scans must catch all three bypass patterns (issue #40)

Several rule files mandate that a "fail-closed audit event" class is constructed in exactly one module (`AuditEvent` in `safety.request`, `LLMResponseEvent` in `draft.audit`, `PruneEvent` in `prune.audit`, `GradeEvent` in `grade.audit`). The scan visitor MUST catch three bypass patterns:

1. **Bare** — `Target(...)` after `from <module> import Target`. Caught by `Call(func=Name(id=Target))`.
2. **Import-alias** — `from <module> import Target as Alias; Alias(...)`. Caught by tracking aliases introduced via `ast.ImportFrom` whose `alias.name == target`, then matching `Call(func=Name(id=<alias>))`.
3. **Module-attribute** — `from <pkg> import <module>; module.Target(...)`. Caught by matching `Call(func=Attribute(attr=Target))` regardless of `<obj>` — the gated class names are unique enough that an attribute access with the same name is overwhelmingly likely to be the gated class.

A bare-name-only visitor is trivially bypassable and provides **false confidence**. `tests/test_audit_completeness.py::_QualifiedNameCallFinder` is the canonical implementation; reuse for any new gated-construction scan. `getattr(module, "Target")(...)` is acceptable to leave unprotected — too dynamic for AST gating, and any reviewer reading `getattr` should already be on alert.

Each new scan also needs a planted-violation regression test exercising all three patterns.

## Source-scan gates: AST over per-line regex (issue #45)

Source-scanning gates that enforce "no X in module Y" (the `_LOGGER` lazy-format gate; future similar tests) MUST be AST-based, never per-line regex. The historic logger gate used `re.search` per line and was trivially bypassable by splitting across lines:

```python
_LOGGER.info(
    f"resolved project_dir: {project_dir}"  # NOT caught by per-line regex
)
```

Any time a gate scans source for a pattern that can legally span multiple lines in Python, reach for `ast.parse` + `ast.NodeVisitor`. The parser normalises every quote style, prefix permutation, and whitespace arrangement into the same node type. `_LoggerFStringVisitor` and `_file_has_logging_or_logger_node` in `tests/llm/test_logger_grep_gate.py` are the precedent.

Three load-bearing details:

1. **Walk positional args AND keyword arg values, recursively.** A `_LOGGER.warning("stuff", extra={"x": f"bad {y}"})` hides an f-string two layers deep. The visitor must `ast.walk(arg)` over every positional AND `kw.value`. Surface-level `isinstance` checks miss it.
2. **Substring scans false-positive on docstrings / comments mentioning the gated token.** A rule citation like `"per manifest-readers.md, no _LOGGER allowed"` would trip `if "_LOGGER" in source:`. The AST walk over `Name(id='_LOGGER')` doesn't see string literals (those parse as `Constant`). Plant a docstring containing the gated token; assert NO match.
3. **"No logging at all" stage-0 enforcement must cover every form of indirection.** `import logging`, `import logging as X`, `from logging import getLogger`, `logging.getLogger(__name__)`, bare `_LOGGER` references are all separate AST node types. Cover each. A check that only matches the literal token `_LOGGER` lets `from logging import getLogger; LOG = getLogger(__name__)` slip through.

Planted-violation self-checks are mandatory — without them, a refactor that broke the visitor would silently disable the gate at the precise moment a real violation needed catching.

## Coverage measurement

Established by issue #27 (DEC-001, DEC-004, DEC-009). Coverage instrumentation runs both locally and in CI via `--cov*` flags in `pyproject.toml` `addopts`.

### `--cov-fail-under` runs locally too

The `--cov-fail-under=<N>` gate lives in `addopts`, so every local `pytest` invocation enforces the coverage floor. The canonical validation command catches regressions before push, not just in CI.

### Two-run baseline procedure (DEC-001)

When bumping the threshold:

1. `pytest --cov=signalforge --cov-report=term` — record total %.
2. Repeat — record second total %.
3. If `|run_1 - run_2| > 1`, investigate (likely a non-deterministic test).
4. Pick `floor(min(run_1, run_2, 80))`. The cap of 80 matches clauditor's precedent and prevents aspirational thresholds that cause churn.

Revisit when actual coverage exceeds `<N> + 5` for two consecutive `dev` builds.

### Known gap: excluded markers (DEC-004)

Coverage measures only the default pytest set. Tests gated behind `bigquery`, `anthropic`, `cli_subprocess`, `e2e`, and `wheel_smoke` markers are excluded by addopts. Those code paths are exercised via fakes in unit tests; the real-network / wheel-build paths are not instrumented.

Marker-specific runs must use `--no-cov` because `--cov-fail-under` would fail runs that exercise only a fraction of the codebase:

```bash
pytest -m cli_subprocess --no-cov
pytest -m wheel_smoke --no-cov
SF_RUN_BQ=1 pytest -m bigquery --no-cov
SF_RUN_BQ=1 GOOGLE_CLOUD_PROJECT=<billing-project> ANTHROPIC_API_KEY=sk-... pytest -m e2e --no-cov
```

For a one-shot **pre-release** measurement of how much coverage the gated paths contribute (run all gated markers under `--cov` in a single invocation), see `CONTRIBUTING.md` § "Pre-release coverage audit". A maintainer running that before each release catches coverage regressions in the gated paths that the default badge number cannot surface.

The gated markers carry **no Python-version pin** — they run on whatever interpreter `uv run` resolves, and CI runs none of them (all are deselected by `addopts`). For maintainer runs that exercise the wheel-build (`wheel_smoke`) or console-script (`cli_subprocess`) paths, target the **matrix ceiling** (currently 3.13, issue #96) via `uv run --python 3.13 pytest -m <marker> --no-cov` so packaging/entry-point behaviour is checked on the newest supported interpreter. The live-service markers (`bigquery` / `anthropic` / `e2e`) are interpreter-invariant — run them on a single version; don't multiply paid API calls across the matrix.

The `wheel_smoke` marker (issue #47) verifies wheel-build packaging without coupling it to default CI — the test shells out `python -m build --wheel` and asserts the canonical demo file set appears under `signalforge/_demo/`. See `python-build.md` § "Shipping package data".

## End-to-end gated tests (issue #10)

Apply to any new test that exercises the full pipeline against a real warehouse + a real LLM provider.

### Belt-and-suspenders gating: marker + runtime `pytest.skipif`

A gated end-to-end test carries TWO independent gates:

1. **`@pytest.mark.<gate>` on the test function** — registered in `pyproject.toml` `[tool.pytest.ini_options].markers` AND added to the default `addopts` exclusion list. Default `pytest` invocations DESELECT the test entirely.
2. **A `_skip_reason()` helper called inside the test** — returns the missing-env-var message; the test calls `pytest.skip(reason)` when set. Surfaces a clear runtime skip when a maintainer runs `pytest -m <gate>` but forgets one of the env vars.

The marker prevents accidental collection in CI; the runtime skip turns a missing-env-var run into an obvious skip-with-reason.

### Three-env-var gate for full-stack e2e

When the test exercises BOTH the warehouse adapter AND the LLM seam, require THREE env vars: `SF_RUN_BQ=1`, `ANTHROPIC_API_KEY`, `GOOGLE_CLOUD_PROJECT` (the billing project — BigQuery won't bill `bigquery-public-data` to itself). Each missing var → a distinct skip reason naming the var.

### `tmp_path` fixture isolation for tests producing on-disk artefacts

If the test produces audit JSONLs or sidecar JSON under `<project_dir>/.signalforge/`, copy the committed fixture into `tmp_path` before invoking the CLI:

```python
def test_e2e(tmp_path):
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)
    main(["generate", "models/staging/<name>.sql", "--project-dir", str(project_dir)])
```

Without this, running the test in place pollutes the committed fixture across runs (DEC-008 of issue #10).

### Engineered determinism for LLM-driven assertions

When an assertion depends on what the LLM drafts (non-deterministic across runs), engineer the INPUT so the assertion is mathematically guaranteed. The AC for both warehouse e2e smokes requires at least one `reason="always-passes"` drop; the LLM reliably proposes `not_null` on every column, and `not_null` over a column with zero NULL rows on the sample is mathematically guaranteed to always-pass.

**WHERE the always-pass column must live depends on whether the model is materialised (issue #124 QG lesson — read this before copying the trick).** The prune engine runs the candidate test against the model's **relation** (`TableRef.from_model`), NOT against the model's `raw_code`. Two fixture shapes, two correct sources of the always-pass:

- **Source-as-model alias trick (no `dbt run`; the v0.1/v0.2 e2e path).** The model's `alias` is overridden so its relation resolves directly to a real, pre-existing **source** table (BigQuery `bikeshare_trips`; Snowflake `TPCH_SF1.CUSTOMER`). Under `oneshot` (or any strategy that samples the source), prune queries that **source table** — so every declared column MUST exist on the source, and the always-pass must come from a **natural NOT NULL real source column** (BigQuery `trip_id`/`start_time`; Snowflake's `c_custkey` primary key). An engineered literal/`COALESCE` column (`'austin' AS region`) lives only in the never-executed `raw_code`; declaring it as a model column makes the drafted `not_null` compile to an "invalid identifier" against the source → `kept-without-evidence`, never `always-passes`, and the AC can never hold live. (This exact mistake shipped in #124's first seed and was caught only at the Quality Gate — the seed declared renamed/engineered columns that don't exist on `TPCH_SF1.CUSTOMER`.)
- **Materialised model (a real `dbt run` built the relation).** Only here does an engineered literal/`COALESCE` column physically exist on the queried relation, so `'literal' AS region` / `COALESCE(...) AS x` is a valid always-pass source.

The austin BigQuery fixture (`tests/fixtures/dbt_project_austin`) and the TPCH Snowflake seed (`tests/fixtures/snowflake`) are both the source-as-model shape and both rely on **natural NOT NULL columns** — match that when adding a third warehouse's e2e. Do NOT copy a literal-column trick onto a source-as-model fixture.

### Hand-crafted manifest seed when workers can't run live tooling

When a fixture depends on `dbt parse` against a live warehouse (or any tool requiring credentials Ralph workers don't have), ship: (1) a regen script documenting the maintainer-only full-reproduction command; (2) a hand-crafted minimal seed, validated by an in-process loads test (no env vars). The seed must satisfy `signalforge.manifest.load(fixture_dir)` — test via a loads-only test that ships in the same commit (DEC-004 of #10).

### Multi-surface drift on user-facing model arguments

A user-facing CLI flag's value can drift across surfaces (README, test argv, plan, ops doc). When correctness depends on resolver parsing rules (e.g. `Manifest.get_model` accepts unique_id and file path but NOT bare model names — bare names route to the file-path branch and fail), pin the canonical form in ONE place and reference it everywhere.

Issue #10's gotcha: `signalforge generate stg_bikeshare_trips` (bare name) failed with `ModelNotFoundError`; only the file path or unique_id forms work. Caught only by Pass 4 of Quality Gate review — no unit test exercises the CLI's full model-arg path against the real `Manifest.get_model`. Mitigation: pre-merge review explicitly verifies CLI examples by running them locally.

### Per-test provider overlay via `apply_provider_override` (#155 US-004 / DEC-012)

When an e2e test needs to swap the LLM provider config (e.g. `tests/cli/test_e2e_openai_smoke.py` runs `signalforge generate` against the same Austin bikeshare fixture as the baseline BQ smoke, but with `grade.provider: openai` instead of the Anthropic default), the canonical helper is `tests.cli._e2e_helpers.apply_provider_override(project_dir, *, grade_provider=None, grade_model=None, grade_max_output_tokens=None) -> None`. Reads `<project_dir>/signalforge.yml`, overlays the `grade:` block deltas, writes back. Non-destructive: unset knobs left alone. Raises `FileNotFoundError` if the fixture's `signalforge.yml` is missing. This is the seam #155 US-005/US-006/US-007 (the three e2e provider variants) all flow through.

Two load-bearing rules: (1) **per-test overlay, not a `GradeConfig` default bump.** Lowering `GradeConfig.max_output_tokens` default would over-budget Anthropic/OpenAI calls; the per-test overlay scopes the Gemini-specific 2048 floor to the test that needs it (DEC-009 of #155). (2) **Drafter stays Anthropic Sonnet across all three e2e providers per DEC-011** — fixture stability requires only the grader varies. Tests that overlay `grade_provider` MUST still gate on `ANTHROPIC_API_KEY` (drafter) AND the provider-specific key (grader); a 3-env-var skip gate is insufficient when the drafter remains Anthropic — five is the contract (drafter API key + grader API key + their respective `SF_RUN_*` opt-ins + `GOOGLE_CLOUD_PROJECT` for the BigQuery leg). #155 QG Pass 4 caught the openai-smoke 3-vs-5 drift exactly because the docstring contract said "three" while the BQ smoke + Gemini sibling already used the five-var pattern.

The same belt-and-suspenders gating still applies: marker + runtime `_skip_reason()` + `tmp_path` isolation. The overlay is applied AFTER `copy_fixture_to_tmp` so the committed fixture is never modified.

## Reference

`plans/super/1-project-scaffolding.md` — DEC-010. `plans/super/2-manifest-loader.md` — DEC-005, DEC-009, DEC-012, DEC-017. `plans/super/27-codecov-coverage.md` — DEC-001, DEC-004, DEC-009. `plans/super/10-e2e-bigquery-smoke.md` — DEC-001, DEC-002, DEC-004, DEC-008, DEC-010, DEC-022. `tests/test_smoke.py`, `tests/manifest/`, `tests/fixtures/regenerate.sh`, `tests/cli/_e2e_helpers.py`, `tests/cli/test_e2e_bigquery_smoke.py`.
