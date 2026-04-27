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

## Reference

`plans/super/1-project-scaffolding.md` — DEC-002, DEC-004, DEC-011, DEC-014.
