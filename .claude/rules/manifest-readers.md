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

## Catalog.json sibling merge for `Column.data_type` (issue #159)

`signalforge.manifest.load(project_dir)` looks for a `catalog.json` sibling next to the resolved `manifest.json` (`<resolved_manifest>.parent / "catalog.json"`) and, when present, merges its column types into `Column.data_type` on the in-memory `Manifest`. `dbt parse` does NOT populate `data_type`; only `dbt docs generate` (which produces `catalog.json`) carries types. The merge closes that gap for any user who runs the full dbt build, so the drafter's prompt — which already renders `data_type` into the cached manifest summary (`llm-drafter.md` § "Cached-block scope") and the dynamic data section (`safety-layer.md` § PII redaction → `LLMRequest.schema`) — sees real warehouse types instead of `UNKNOWN`.

Five load-bearing rules — match this shape for any future "dbt-adjacent target/ file" the loader merges:

1. **Sibling path goes through the same symlink-hardened canonicalisation as the manifest itself.** Compute `catalog_path = resolved_manifest.parent / "catalog.json"` then route through `signalforge._common.path_safety.canonicalise_path(catalog_path, project_resolved)`. `PathContainmentError` propagates out of `load()`. The "default" sibling path is NOT exempt from the gate (DEC-007 of #2 generalised — "default paths must go through the same gate as overrides" applies to siblings too).
2. **Silent degradation on absent / malformed / unreadable catalog.** Catalog absent → no-op; `OSError` / `json.JSONDecodeError` on read → no-op; non-dict root / non-dict `nodes` / non-dict `columns` / non-dict per-column entry → no-op; missing or non-string `type` field → no-op. The loader is stage-0; emitting a log or raising for a stale catalog is the wrong UX (it punishes the user for forgetting `dbt docs generate`, which is not a SignalForge concern). The drafter sees `data_type=None` and renders "UNKNOWN" — same as today's no-catalog world.
3. **Case-insensitive column matching keyed on `lower(col_name)`.** dbt's `catalog.json` mirrors the warehouse's identifier convention: Snowflake uppercases (`USER_ID`), BigQuery preserves (`user_id`), Postgres lowercases (`user_id`). Manifest preserves whatever dbt parsed. Building the catalog index as `{lower(col_name): data_type}` and matching the manifest column by `lower(name)` is a strict superset across all three — a BigQuery match is exact under lower-fold; Snowflake's upper-fold matches manifest's lower; Postgres is identity. No `Manifest.metadata.adapter_type` branching needed.
4. **Phantom catalog columns are silently dropped; manifest columns absent from catalog stay `data_type=None`.** A catalog that declares a column not present on the manifest's `Model.columns` is NOT used to add a phantom column — manifest is the source of truth for "what columns exist." A manifest column with no matching catalog entry keeps `data_type=None` (renders as "UNKNOWN" in the drafter prompt). Neither shape is an error.
5. **Frozen-model overlay via `model_copy(update=...)`.** `Column`, `Model`, `Manifest` are all `frozen=True`. The overlay rebuilds bottom-up: `Column.model_copy(update={"data_type": cat_type})` → new `columns` dict → `Model.model_copy(update={"columns": new_columns})` → new `nodes` dict → `Manifest.model_copy(update={"nodes": new_nodes})`. Re-stash `_PROJECT_DIR_ATTR` on the new Manifest instance (resolver-index cache is intentionally dropped and rebuilt lazily). Skip the whole rebuild — return the original `manifest` — when `model_changed=False` for every model (no overlay applied → byte-equal output).

**No `_PROMPT_VERSION` rotation needed when catalog types start flowing in.** Per `llm-drafter.md` § "Cached-block scope" + DEC-009 of #159, `_PROMPT_VERSION` is a template-text hash (`blake2b(_SYSTEM_PROMPT + _MANIFEST_SUMMARY_TEMPLATE + _DATA_SECTION_TEMPLATES_JSON)`); per-project rendered-byte variation has always been allowed (different manifests → different output). Populating `data_type` from catalog.json is per-project content variation, not a template change. The cache-stability golden uses the `fct_orders` fixture (which already has populated types); it is unaffected.

**No drift detector required.** `Column.data_type: str | None = None` predates #159; the drift-detector pattern is mandatory only when a NEW field lands on a read-back model with `extra="ignore"`. The catalog overlay sets an existing field; no shape change.

When the operator wants types but does NOT run `dbt docs generate` (e.g. a dbt-parse-only CI step), the operator-facing answer is "run `dbt docs generate` once and commit `target/catalog.json` or add it to the SignalForge fixture set." The library never reaches into the warehouse adapter for types from the manifest layer; that path stays stage-0 deterministic.

## Reference

`plans/super/2-manifest-loader.md` — DEC-001, DEC-007, DEC-008, DEC-013, DEC-014, DEC-017. `plans/super/37-multi-model-select.md` — DEC-001, DEC-012, DEC-016 (selector grammar additions). `plans/super/159-drafter-column-types.md` — DEC-001, DEC-002, DEC-007, DEC-009, DEC-010 (catalog.json sibling merge). `src/signalforge/manifest/loader.py` — current implementation of all three traps + the `_apply_catalog_overlay` helper. `src/signalforge/manifest/select.py` — issue-#37 selector module (parse_selector / select_models / SelectorAtom).
