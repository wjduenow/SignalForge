# External-format readers (Pydantic v2)

Established by issue #2 (manifest loader). Apply to any module in this repo that parses an externally-defined JSON/YAML format (dbt's `manifest.json`, `catalog.json`, `run_results.json`, BigQuery `INFORMATION_SCHEMA` snapshots, etc.).

## Pydantic v2: frozen + extra=ignore in production

```python
from pydantic import BaseModel, ConfigDict

_BASE = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)
```

`frozen=True` makes models immutable post-construction. `extra="ignore"` survives upstream schema additions (e.g. dbt Fusion v20 fields) without code changes. `populate_by_name=True` lets callers use either the field name or the alias when constructing.

Do **not** use `extra="forbid"` in production. Reserve it for *test-only* `StrictModel(BaseModel)` subclasses constructed inline in drift-detector tests — those catch silent schema expansion before a live manifest does.

## Symlink-hardened path resolution

Any function that takes a user-supplied path and reads from disk MUST canonicalise via:

```python
def _canonicalise_path(input_path: Path | str, project_dir: Path) -> Path:
    try:
        project_resolved = project_dir.resolve(strict=True)
        if not Path(input_path).is_absolute():
            p = project_resolved / input_path
        else:
            p = Path(input_path)
        resolved = p.resolve(strict=False)
    except RuntimeError as exc:        # symlink loop
        raise ModelPathOutsideProjectError(...) from exc
    if not resolved.is_relative_to(project_resolved):
        raise ModelPathOutsideProjectError(...)
    return resolved
```

Three traps to remember:

1. `Path.relative_to()` does **not** follow symlinks. Use `.resolve()` first.
2. `Path.resolve()` raises `RuntimeError` on cycles regardless of `strict=`. Wrap.
3. The "default" path (e.g. `target/manifest.json`) must go through the same gate as a user-supplied override. Don't trust convention.

Issue #2's pass-2 review caught all three by accident; they're now baked into the loader and its regression tests.

## Errors carry remediation

Every typed exception in an external-format reader subclasses a module base (e.g. `ManifestError`) and accepts a `remediation: str` kwarg. `__str__` renders both the message and a `↳ Remediation:` line. This makes "explainable diffs" (CLAUDE.md commitment #5) load-bearing from the very first stage of the pipeline.

## No logging / metrics in stage-0 modules

Reader modules are deterministic JSON-to-typed-objects. They do not emit logs or metrics in v0.1. Observability lives in the stage that *consumes* the data (LLM drafting, prune, grade) — that's where signal-vs-volume tradeoffs surface. Adding logs here just generates noise.

## Reference

`plans/super/2-manifest-loader.md` — DEC-001, DEC-007, DEC-008, DEC-013, DEC-014, DEC-017. `src/signalforge/manifest/loader.py` — current implementation of all three traps.
