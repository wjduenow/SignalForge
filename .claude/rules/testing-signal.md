# Testing: signal over volume

Established by issue #1 scaffolding (DEC-010). Apply to every test in this repo.

This rule encodes SignalForge's first architectural commitment (signal over volume) from `CLAUDE.md` into the test suite: a test that always passes is worse than no test, because it consumes review attention without catching anything.

## No `assert True`-shaped tests

Every test must be capable of failing if its target is broken. The smoke test (`tests/test_smoke.py`) is the floor: it imports the package, asserts `__version__` is set, and asserts a PEP 440 shape. Each of those would fail on a real regression.

If you find yourself writing a test that can't fail, delete it instead of shipping it.

## Strict markers — both settings required (pytest 9 quirk)

```toml
[tool.pytest.ini_options]
addopts = "-ra --strict-markers"
strict_markers = true
```

`addopts = "--strict-markers"` alone is **not enough on pytest 9.x** — it sets `option.strict_markers=True`, but `_pytest/mark/structures.py` reads `getini("strict_markers")` instead. Without both, an unknown `@pytest.mark.foo` warns but does NOT error at collection time.

To verify locally: temporarily decorate a test with `@pytest.mark.does_not_exist`, run pytest, observe an *error* (not just a warning). Revert.

## src layout discovery

Do NOT create `tests/__init__.py`. Pytest's rootdir handles discovery for src layouts; an empty `__init__.py` masks import errors and makes failures harder to read.

## Fixture regeneration via ephemeral `uvx`

When a fixture's correctness depends on an external tool's output (dbt's `manifest.json`, etc.), commit the *generated* artefact and document a regeneration script that runs the tool via `uvx` (or `pipx run`) at a pinned version:

```bash
uvx --python 3.11 --from "dbt-duckdb==X.Y.*" --with "dbt-core==X.Y.*" dbt parse
```

Pin the tool version in dev-deps for the *latest* schema only — older versions are summoned ephemerally. Strip non-deterministic fields (`generated_at`, `invocation_id`, `user_id`, etc.) with `jq` before committing so the JSON is reproducible. Reference: `tests/fixtures/regenerate.sh` (issue #2).

## Seeded determinism over snapshot normalisation

When a derived identifier needs to land in a snapshot fixture (compiled SQL, generated artefact id, run-scoped temp-table name), prefer **seeded determinism** — `blake2b(stable_inputs, digest_size=N).hex()` over the inputs that should produce the same output across runs — over post-hoc regex normalisation in the test. The seeded path makes raw bytes stable across runs without a normalisation layer; the normalisation path becomes a maintenance burden the moment the regex shape evolves (e.g., the LLM drafter's `prompt_version` would have needed regex updates every time the system prompt changed if it weren't already a content hash). Mirrors DEC-001 of #22 (16-hex `_sf_sample_<run_id>` derived from `(model.unique_id, signalforge_version, sample_size, partition_filter)`) and the LLM drafter's `prompt_version` (DEC of #5). Reach for the hash recipe before reaching for `re.sub` in tests.

## Drift detection via one-off `extra="forbid"` model

If a parser uses `extra="ignore"` in production (forward-compat), pair it with a test that constructs a one-off `StrictModel(BaseModel)` with `extra="forbid"` and validates a known-current fixture against it. Adding a key to the fixture without updating the model breaks the test loudly. Reference: `tests/manifest/test_models.py::test_drift_detector_extra_forbid`.

## Coverage measurement

Established by issue #27 (DEC-001, DEC-004, DEC-009). Coverage instrumentation runs both locally and in CI via `--cov*` flags in `pyproject.toml` `addopts`.

### `--cov-fail-under` runs locally too

The `--cov-fail-under=<N>` gate lives in `addopts`, so every local `pytest` invocation enforces the coverage floor. This is deliberate — the canonical validation command (`ruff check . && ruff format --check . && pyright && pytest`) catches coverage regressions before push, not just in CI.

### Two-run baseline procedure (DEC-001)

When bumping the threshold:

1. `pytest --cov=signalforge --cov-report=term` — record total %.
2. Repeat — record second total %.
3. If `|run_1 - run_2| > 1`, investigate (likely a non-deterministic test).
4. Pick `floor(min(run_1, run_2, 80))`. The cap of 80 matches clauditor's precedent and prevents aspirational thresholds that cause churn.

Revisit when actual coverage exceeds `<N> + 5` for two consecutive `dev` builds.

### Known gap: excluded markers (DEC-004)

Coverage measures only the default pytest set. Tests gated behind `bigquery`, `anthropic`, `cli_subprocess`, and `e2e` markers are excluded by addopts (`-m 'not bigquery and not anthropic and not cli_subprocess and not e2e'`). Those code paths are exercised via fakes in unit tests; the real-network paths are not instrumented.

Because `--cov-fail-under` is in `addopts`, marker-specific runs (`pytest -m cli_subprocess`, `pytest -m bigquery`, `pytest -m e2e`) will fail the coverage gate. Use `--no-cov` for these runs:

```bash
pytest -m cli_subprocess --no-cov
SF_RUN_BQ=1 pytest -m bigquery --no-cov
SF_RUN_BQ=1 GOOGLE_CLOUD_PROJECT=<billing-project> ANTHROPIC_API_KEY=sk-... pytest -m e2e --no-cov
```

## End-to-end gated tests (issue #10)

Established by issue #10 (e2e smoke test against `bigquery-public-data`). Apply to any new test that exercises the full pipeline against a real warehouse + a real LLM provider.

### Belt-and-suspenders gating: marker + runtime `pytest.skipif`

A gated end-to-end test carries TWO independent gates:

1. **`@pytest.mark.<gate>` on the test function** — registered in `pyproject.toml` `[tool.pytest.ini_options].markers` AND added to the default `addopts` exclusion list (`-m 'not <gate>'`). Default `pytest` invocations DESELECT (do not collect) the test entirely. Mirrors the `bigquery` / `anthropic` / `cli_subprocess` precedent.
2. **A `_skip_reason()` helper called inside the test** — returns the missing-env-var message; the test calls `pytest.skip(reason)` when set. Surfaces a clear runtime skip when a maintainer runs `pytest -m <gate>` but forgets one of the env vars.

Both gates are required. The marker prevents accidental collection in CI; the runtime skip turns a missing-env-var run into an obvious skip-with-reason rather than a cryptic real-network failure. Mirrors `tests/warehouse/test_bigquery_integration.py:1-17` precedent.

### Three-env-var gate for full-stack e2e

When the test exercises BOTH the warehouse adapter AND the LLM seam (issue #10's case), require THREE env vars:

- `SF_RUN_BQ=1` (or equivalent for non-BQ adapters) — opt-in to real warehouse calls.
- `ANTHROPIC_API_KEY` — opt-in to real LLM calls.
- `GOOGLE_CLOUD_PROJECT` — the billing project (BigQuery won't bill `bigquery-public-data` to itself; ADC's default project may not be set).

Each missing var → a distinct skip reason naming the missing var. Maintainers debug missing-env-var skips quickly when the message says exactly which one.

### `tmp_path` fixture isolation for tests producing on-disk artefacts

If the test produces audit JSONLs or sidecar JSON under `<project_dir>/.signalforge/`, the test MUST copy the committed fixture into pytest's `tmp_path` before invoking the CLI:

```python
def test_e2e(tmp_path):
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)  # shutil.copytree
    main(["generate", "models/staging/<name>.sql", "--project-dir", str(project_dir)])
    # audits land in tmp_path/.signalforge/, not the committed fixture
```

Mirrors `tests/cli/_factories.py::make_fake_dbt_project` and `tests/cli/_e2e_helpers.py::copy_fixture_to_tmp`. Without this, running the test in place pollutes the committed `tests/fixtures/<name>/.signalforge/` directory across runs (DEC-008 of `plans/super/10-e2e-bigquery-smoke.md`).

### Engineered determinism for LLM-driven assertions

When an assertion depends on what the LLM drafts (which is non-deterministic across runs), engineer the test INPUT so the assertion is mathematically guaranteed.

Issue #10's load-bearing example: the AC required at least one `drop_reason="always-passes"` decision, which depends on the LLM drafting a `not_null` test on a column that's actually never null. The fixture's staging SQL ships an engineered literal column (`'austin' AS region`) and a `COALESCE`'d column (`COALESCE(start_time, TIMESTAMP '...') AS start_time_safe`). The LLM reliably proposes `not_null` on every column; `not_null` on a literal is mathematically guaranteed to always-pass; the prune engine deterministically drops it.

The pattern: identify the LLM-driven assertion → identify the input shape that makes it deterministic → engineer the fixture to produce that shape. Apply to any future end-to-end assertion that depends on LLM output.

### Hand-crafted manifest seed when workers can't run live tooling

When a fixture depends on `dbt parse` against a live warehouse (or any tool requiring credentials Ralph workers don't have), ship:

1. A **regen script** (sibling of `tests/fixtures/regenerate.sh`) that documents the maintainer-only command for full reproduction.
2. A **hand-crafted minimal seed** of the generated artefact, validated by an in-process loads test (no env vars). Workers produce the seed; maintainers run the regen script later for full parity.

Issue #10's `tests/fixtures/dbt_project_austin/{regenerate.sh, target/manifest.json}` is the precedent. The seed manifest contains exactly the model + source the test exercises; the regen script overwrites with the live `dbt parse` output. The committed seed must satisfy `signalforge.manifest.load(fixture_dir)` — test it via a loads-only test that ships in the same commit (DEC-004 of issue #10).

### Multi-surface drift on user-facing model arguments

A user-facing CLI flag's value can drift across surfaces (README example, test argv, plan example, ops-doc example). When a value's correctness depends on the resolver's parsing rules (e.g., `Manifest.get_model` accepts unique_id and file path but NOT bare model names — bare names route to the file-path branch and fail), pin the **canonical form** in ONE place and reference it everywhere.

Issue #10's gotcha: `signalforge generate stg_bikeshare_trips` (bare name) failed with `ModelNotFoundError`; only `signalforge generate models/staging/stg_bikeshare_trips.sql` (file path) and `signalforge generate model.<project>.stg_bikeshare_trips` (unique_id) work. The bare-name form was caught only by Pass 4 of the Quality Gate code review — no test in the unit suite exercises the CLI's full model-arg path against the real `Manifest.get_model`. Mitigation: pre-merge code review explicitly verifies CLI examples by running them locally; the orchestrator should not trust prose alone for resolver-arg shapes.

## Reference

`plans/super/1-project-scaffolding.md` — DEC-010. `plans/super/2-manifest-loader.md` — DEC-005, DEC-009, DEC-012, DEC-017. `plans/super/27-codecov-coverage.md` — DEC-001, DEC-004, DEC-009. `plans/super/10-e2e-bigquery-smoke.md` — DEC-001, DEC-002, DEC-004, DEC-008, DEC-010, DEC-022 (end-to-end gated tests section). `tests/test_smoke.py`, `tests/manifest/`, `tests/fixtures/regenerate.sh`, `tests/fixtures/dbt_project_austin/regenerate.sh`, `tests/cli/_e2e_helpers.py`, `tests/cli/test_e2e_bigquery_smoke.py` — current implementations.
