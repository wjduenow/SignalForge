# Python build conventions

Established by issue #1 scaffolding (DEC-002, DEC-011). Apply to every new Python package in this repo.

## Build backend: Hatchling

```toml
[build-system]
requires = ["hatchling>=1.18"]
build-backend = "hatchling.build"
```

## src layout, not flat

Package source lives under `src/<package>/`. Tests live under `tests/`. No `tests/__init__.py` (pytest's rootdir handles src-layout discovery; an empty init masks import errors).

## Dynamic version sourced from `__init__.py`

```toml
[project]
dynamic = ["version"]

[tool.hatch.version]
path = "src/<package>/__init__.py"
```

The package's `__init__.py` declares `__version__ = "..."` (PEP 440). Hatchling reads it via regex.

## Wheel target packages — non-negotiable

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/<package>"]
```

Do NOT rely on Hatchling auto-discovery for src layout. The wheel will silently produce an empty package if you forget this. Always declare explicitly. (DEC-011, learned the slow way.)

## Editable install (zsh-safe)

```bash
pip install -e ".[dev]"
```

Quote the extras — `[dev]` is a glob in zsh and fails with `no matches found` unquoted.

## Python version: advertised floor matches the tested floor (issue #46)

`pyproject.toml` declares `requires-python = ">=3.11"`; `[tool.pyright].pythonVersion` is `"3.11"`; `.github/workflows/ci.yml` runs on `python-version: "3.11"`. **All three agree** — what we advertise is what we type-check is what we test.

The original `>=3.10` floor was an aspirational support promise: the package could install on 3.10, but no CI job and no pyright pass exercised the 3.10 path. Three concrete divergence sources where 3.10-only code can pass review without being caught:

- `match`-statement exhaustiveness varies subtly between 3.10 and 3.11.
- PEP 604 union-type stringification semantics differ.
- PEP 695 type-parameter syntax (3.12+) is easy to slip in once PEP 604 is used.

Picked the cheaper of the two options from issue #46: narrow the floor to 3.11 rather than widen CI / pyright to a 3.10 matrix. v0.1 users who need 3.10 support can pin to a 3.10-compatible patch release; v0.2 will revisit if a real user reports.

When CI widens to a Python matrix (likely v0.3, in lockstep with `ci-supply-chain.md` DEC-003 graduation), bump `pyright.pythonVersion` to the floor of the matrix AND keep `requires-python` aligned with that floor. The three values stay in lockstep; drift between them is exactly the bug this DEC closes.

## Reference

`plans/super/1-project-scaffolding.md` — DEC-002, DEC-004, DEC-011, DEC-014. Issue #46 — Python version reconciliation.
