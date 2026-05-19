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

## Shipping package data (non-`.py` files) — explicit `include` directive (issue #47)

Hatchling's default `packages` declaration ships `.py` files under the named tree. Non-`.py` data files (YAML, JSON, SQL, dotfiles like `.gitignore`) are **not** auto-discovered — they need an explicit `include` entry alongside `packages`:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/signalforge"]
include = ["src/signalforge/_demo"]  # ship the bundled demo tree
```

Defence-in-depth: even when the current Hatchling behaviour appears to ship sibling data files under `packages`, the `include` directive is a contract surface that survives Hatchling version drift. Issue #47's empirical investigation found that current Hatchling versions sometimes do auto-include but the behaviour is not contractually documented — the `include` directive makes it explicit and gated by a CI test (see below).

**Maintainer-only `wheel_smoke` gate.** Issue #47 (DEC-003) lands a `@pytest.mark.wheel_smoke` test that shells out `python -m build --wheel` (with `build` shipped via `[dependency-groups].dev`, the primary path always resolves under `uv run`; falls back to `uvx --from build pyproject-build` when `build` is unavailable), opens the artifact via `zipfile.ZipFile`, and asserts the canonical file set appears under the expected wheel path. Registration:

```toml
[tool.pytest.ini_options]
markers = [
  "wheel_smoke: maintainer-only; builds the wheel via python -m build and inspects the artifact (run with --no-cov)",
]
addopts = "... -m 'not bigquery and not anthropic and not cli_subprocess and not e2e and not wheel_smoke'"
```

Maintainer runs `uv run pytest -m wheel_smoke --no-cov` before declaring a packaging-touching PR ready. The `--no-cov` is required because `--cov-fail-under` in default `addopts` fails marker-specific runs that exercise only a fraction of the codebase (mirrors `uv run pytest -m bigquery --no-cov` and `uv run pytest -m cli_subprocess --no-cov` precedents from `testing-signal.md`).

**Belt-and-braces verification step.** Before merging any PR that touches `[tool.hatch.build.targets.wheel]`, run `uv build && unzip -l dist/*.whl | grep <expected-path>` locally and inspect the file list. The wheel_smoke marker catches absence; the manual `unzip -l` catches surprising additions (e.g. cache directories, hidden build artefacts).

**Dotfile inclusion is fragile.** Hatchling's glob behaviour on dotfiles (`.gitignore`, `.env.example`) varies by version. The wheel_smoke test must explicitly assert dotfile presence alongside regular files — a regression that drops only the dotfile would otherwise slip through. Fallback if Hatchling silently strips a dotfile: ship as a non-dot name (e.g. `gitignore.demo`) and rewrite to the dot-name at copy time in the consuming code (issue #47 DEC-006 documents the fallback; not needed in v0.1 because current Hatchling preserves dotfiles under `include`).

## uv-managed dev environment (uv migration)

The canonical dev install is `uv sync --dev`. `uv.lock` is committed; the lockfile carries the exact resolved versions every dev / CI run uses.

- `[dependency-groups].dev` in `pyproject.toml` is the uv-native (PEP 735) source of truth for dev deps. CI runs `uv sync --dev` then `uv run ruff check .` / `uv run pyright` / `uv run pytest`.
- `[project.optional-dependencies].dev` is kept in sync with `[dependency-groups].dev` for `pip install -e ".[dev]"` back-compat. The two lists deliberately mirror each other; `build` is the only extra entry that lives in `[dependency-groups].dev` only (powers the `wheel_smoke` marker under `uv run`).
- The setup-uv GitHub Action is SHA-pinned (`astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b  # v8.1.0`) per `ci-supply-chain.md`.

The old "quote the `".[dev]"` — `[dev]` is a glob in zsh" gotcha drops away — `uv sync --dev` takes no glob-fragile argument.

## Python version: advertised floor matches the tested floor (issue #46, uv migration)

`pyproject.toml` declares `requires-python = ">=3.11"`; `[tool.pyright].pythonVersion` is `"3.11"`; `.github/workflows/ci.yml` runs a `python-version: ["3.11", "3.12"]` matrix. **All three agree on the floor** — what we advertise (`>=3.11`) is what we type-check (`3.11`) is what we test as the *floor of the matrix* (`3.11`). The 3.12 iteration runs pytest only; pyright is gated on `matrix.python-version == '3.11'` so the type-check pins to the advertised floor.

The original `>=3.10` floor was an aspirational support promise: the package could install on 3.10, but no CI job and no pyright pass exercised the 3.10 path. Three concrete divergence sources where 3.10-only code can pass review without being caught:

- `match`-statement exhaustiveness varies subtly between 3.10 and 3.11.
- PEP 604 union-type stringification semantics differ.
- PEP 695 type-parameter syntax (3.12+) is easy to slip in once PEP 604 is used.

Picked the cheaper of the two options from issue #46: narrow the floor to 3.11 rather than widen CI / pyright to a 3.10 matrix. v0.1 users who need 3.10 support can pin to a 3.10-compatible patch release.

The uv migration widened the CI matrix to 3.11 / 3.12 (uv's interpreter management makes a multi-version matrix essentially free). The intended ceiling is 3.13, but Python 3.13 changed `Path.resolve()` to raise `OSError(errno.ELOOP)` instead of `RuntimeError` on cyclic symlinks; six tests under the path-safety / sidecar / audit / manifest-loader surfaces rely on the 3.11/3.12 shape. A follow-up issue tracks updating the catch sites in `signalforge/_common/path_safety.py` and `signalforge/manifest/loader.py` so 3.13 can return to the matrix.

The floor stays at 3.11; if a future ticket bumps the floor (e.g. for a stdlib feature only present in 3.12+), `requires-python` AND `pyright.pythonVersion` AND the matrix floor move in lockstep. Drift between them is exactly the bug this DEC closes.

## Reference

`plans/super/1-project-scaffolding.md` — DEC-002, DEC-004, DEC-011, DEC-014. Issue #46 — Python version reconciliation. `plans/super/47-init-demo.md` — DEC-002 (Hatch `include` for non-`.py` package data), DEC-003 (`wheel_smoke` maintainer-gate pattern), DEC-006 (dotfile inclusion fallback).
