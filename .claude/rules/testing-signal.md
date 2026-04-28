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

## Drift detection via one-off `extra="forbid"` model

If a parser uses `extra="ignore"` in production (forward-compat), pair it with a test that constructs a one-off `StrictModel(BaseModel)` with `extra="forbid"` and validates a known-current fixture against it. Adding a key to the fixture without updating the model breaks the test loudly. Reference: `tests/manifest/test_models.py::test_drift_detector_extra_forbid`.

## Reference

`plans/super/1-project-scaffolding.md` — DEC-010. `plans/super/2-manifest-loader.md` — DEC-005, DEC-009, DEC-012, DEC-017. `tests/test_smoke.py`, `tests/manifest/`, `tests/fixtures/regenerate.sh` — current implementations.
