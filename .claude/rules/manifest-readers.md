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

## User-facing string-grammar selectors (issue #37)

When a CLI flag accepts a string grammar that the manifest layer parses (e.g. dbt-style `tag:<name>`, `path:<glob>`, comma-separated unions), the parser + matcher live in a dedicated module under `signalforge.manifest.` rather than in CLI code. Issue #37's `signalforge.manifest.select` ships the pattern:

- **`parse_<grammar>(expr: str) -> tuple[<Atom>, ...]`** — splits + classifies + validates. Returns a tuple of typed value objects. Raises a domain-specific error (`SelectorParseError(ManifestError)`) on malformed input. The CLI layer wraps this error as its own tier-2 typed exception (`CliSelectorParseError`) at the dispatcher boundary; the manifest error itself stays neutral so library callers can consume it directly.
- **`select_<entity>(manifest, expr) -> tuple[<Entity>, ...]`** — parses + matches. Dedupes by primary key (`unique_id`) and sorts deterministically (ASCII-codepoint, NOT locale-aware, so test ordering is stable across environments). Returns an empty tuple on zero match — the CLI layer raises its own `CliSelectorNoMatchError` at orchestrator entry, not the manifest layer.
- **Atom value objects** (e.g. `TagAtom`, `PathAtom`, `BareAtom`) — Pydantic v2 frozen with `extra="forbid"` (deliberately stricter than the `extra="ignore"` default for read-back models; selector atoms are user input where typos must fail loud). Use a discriminated union with `Literal["..."]` `kind` field + `Field(discriminator="kind")` on the union alias.

Three load-bearing conventions:

1. **Whitespace handling is part of the grammar contract.** Document explicitly whether the parser strips whitespace around atoms (`tag:foo , path:bar` → `(TagAtom("foo"), PathAtom("bar"))`) AND whether it preserves whitespace INSIDE payloads (`tag:my tag` → tag named `"my tag"`, exact-match against `Model.tags`). Issue #37 strips around-the-comma whitespace but preserves payload whitespace verbatim — the latter is a deliberate choice (matchers are exact-match, no normalisation), but it means an operator typo like `tag:my tag` matches nothing rather than failing loud. Document this trade-off in the parser's docstring.
2. **Multi-expression union is set-OR, deduped by primary key.** Overlapping atoms (e.g. `tag:staging,path:models/staging/*` both matching the same model) yield ONE entry in the result, not duplicates. The sort order is the only thing tests can pin deterministically.
3. **Bare-value disambiguation.** When the grammar has a "bare" atom that routes to one of multiple targets (issue #37: `model.` prefix → `unique_id` lookup; else `original_file_path` lookup), the disambiguator is a simple prefix check, NOT a regex. Typos like `tags:staging` (one extra char) silently route to the bare branch and produce a zero-match error rather than a grammar error. Acceptable for v0.2; v0.3 could add a typo-detection pass.

The drift-detector pattern (one-off `extra="forbid"` strict model paired with a fixture) does NOT apply to selector atoms — atoms are user-input typed (already `extra="forbid"`), not read-back-from-disk types where forward-compat matters. The standard pattern is mandatory only for `extra="ignore"` reader-shaped models.

When v0.3 adds a sibling grammar (e.g. `--filter <expr>` for column-level filters, `--exclude <expr>` for set subtraction), copy this module shape: parser + matcher + atom-union + domain error in the manifest layer; CLI-tier-2 wrapper in the CLI layer; `extra="forbid"` everywhere on the user-input typed surface.

## Reference

`plans/super/2-manifest-loader.md` — DEC-001, DEC-007, DEC-008, DEC-013, DEC-014, DEC-017. `plans/super/37-multi-model-select.md` — DEC-001, DEC-012, DEC-016 (selector grammar additions). `src/signalforge/manifest/loader.py` — current implementation of all three traps. `src/signalforge/manifest/select.py` — issue-#37 selector module (parse_selector / select_models / SelectorAtom).
