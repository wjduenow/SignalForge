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

## Reference

`plans/super/1-project-scaffolding.md` — DEC-010. `tests/test_smoke.py` — current implementation. The `strict_markers = true` ini setting was discovered during US-003 of issue #1.
