# Manifest loader — operations guide

Operational reference for users of `signalforge.manifest`. Companion to
[`tests/fixtures/README.md`](../tests/fixtures/README.md) and the design
record in [`plans/super/2-manifest-loader.md`](../plans/super/2-manifest-loader.md).

## Memory profile

The loader's soft-size warning uses a 3× expansion ratio between on-disk
manifest bytes and resident Python memory; size your CI runner accordingly.

| Manifest size on disk | Approx. resident memory |
| --------------------- | ----------------------- |
| small (~50 KB)        | ~5 MB                   |
| medium (~5 MB)        | ~50–150 MB              |
| large (~30+ MB)       | ~300–500+ MB            |

## Soft size warning (DEC-008)

The loader exposes `MAX_MANIFEST_BYTES = 200 * 1024 * 1024` at module scope.
If `os.path.getsize(manifest_path)` exceeds it, `load()` emits a single
`UserWarning` (not an exception) and proceeds — v0.1 has no hard ceiling.
Tests that need to exercise the threshold can monkeypatch:

```python
import signalforge.manifest.loader as loader_mod
loader_mod.MAX_MANIFEST_BYTES = 1024  # force the warning on tiny fixtures
```

The warning text includes the 3× rule of thumb so users can plan capacity.

## Multi-version fixture regeneration (DEC-009 / DEC-012)

Cross-link: [`tests/fixtures/README.md`](../tests/fixtures/README.md) holds
the canonical recipe, including the per-schema-version `uvx dbt-core==X.Y.x`
incantation for v9 / v10 / v11.

- v12 can be regenerated with the in-dev-group `dbt-core>=1.8` install — no
  ephemeral `uvx` needed; `uv sync --dev` is sufficient.
- v9 / v10 / v11 use ephemeral `uvx` installs of dbt-core 1.5.x / 1.6.x /
  1.7.x; the older lines need `--python 3.11` because they import the
  removed `distutils` module.
- `bash tests/fixtures/regenerate.sh` drives the full matrix and strips
  non-deterministic metadata fields via `jq` so PR diffs don't churn.

## Supported schema versions

| Manifest schema | dbt-core lines       | Notes                          |
| --------------- | -------------------- | ------------------------------ |
| v9              | 1.5.x                | regen via `uvx`                |
| v10             | 1.6.x                | regen via `uvx`                |
| v11             | 1.7.x                | regen via `uvx`                |
| v12             | 1.8 / 1.9 / 1.10 / 1.11 | regen via in-`[dev]` install |

Schema **v20** (Fusion engine) is tracked as future work and currently
raises `UnsupportedManifestVersionError`.

## Column metadata: schema files are the prerequisite for column-level tests

SignalForge drafts **column-level** tests (`not_null`, `unique`,
`accepted_values`, per-column `custom_sql`, …) from the columns dbt
records on each model — i.e. `model.columns` in the manifest. dbt
populates that dict from your **schema `.yml` files** (the `models:` →
`columns:` blocks). The manifest is the source of truth for *what
columns exist*; the `catalog.json` overlay (next section) only fills in
the *type* of a column that is already declared — it never invents a
column.

**Consequence — a model with no schema `.yml` has zero columns.** `dbt
parse` on a model that declares no `columns:` yields an empty
`model.columns`, so:

- the drafter has nothing to anchor column-level tests to, and
- the ingest anchor check (`signalforge.ingest.anchor`) rejects any
  candidate test that references a column absent from `model.columns` —
  so even a hallucinated column test is dropped.

What a schema-less model still produces is limited to **model-level**
variants (e.g. `row_count_between`, `row_count_anomaly_by_period`) — a
small fraction of the coverage a column-described model yields. If a
`signalforge generate` run scores far fewer artifacts than you expected,
first check that the target model actually declares its columns:

```bash
python -c "import json,sys; m=json.load(open('target/manifest.json')); \
n=m['nodes']['model.<project>.<model>']; print(len(n['columns']), 'columns')"
```

### Generating schema files

To unlock column-level drafting, give each model a schema `.yml` with a
`columns:` list. Two common routes:

| Route | Command / tool | Result |
| ----- | -------------- | ------ |
| Scaffold from the warehouse | [`dbt-codegen`](https://github.com/dbt-labs/dbt-codegen) `generate_model_yaml` — `dbt run-operation generate_model_yaml --args '{"model_names": ["my_model"]}'` | prints a ready-to-commit `models: … columns:` block (the model must be built so the macro can read its columns from the warehouse) |
| By hand | author `_<dir>__models.yml` beside the model | full control over column descriptions, which also feed the drafter's prompt |

Then re-run `dbt parse` so the new columns land in `manifest.json`.

**Recommended follow-up: `dbt docs generate`.** Once the columns exist,
running `dbt docs generate` writes a sibling `catalog.json` that
SignalForge auto-merges to fill each column's real warehouse
`data_type` (next section). Schema files give SignalForge the
*columns*; `dbt docs generate` gives it the *types* — the two are
complementary, and `dbt docs generate` is **not** a substitute for
schema files (it cannot add a column the manifest doesn't already have).

## Column types from `catalog.json` (issue #159)

`signalforge.manifest.load(project_dir)` automatically merges column
types from a sibling `target/catalog.json` (next to `target/manifest.json`)
into `Column.data_type` on the in-memory `Manifest`. **No CLI flag, no
config knob — pure sibling auto-discovery.** Run `dbt docs generate`
in your dbt project to produce `catalog.json` alongside the existing
`manifest.json` and the LLM drafter will see real warehouse column
types in its prompt instead of `UNKNOWN` placeholders.

### What it does

| dbt build step  | `manifest.json` | `catalog.json` | `Column.data_type` |
| --------------- | --------------- | -------------- | ------------------ |
| `dbt parse`     | ✓               | absent         | `None` (renders as `UNKNOWN` in the drafter prompt) |
| `dbt docs generate` (after `dbt parse`) | ✓ | ✓ | real warehouse type (e.g. `"INT64"`, `"STRING"`, `"TIMESTAMP"`) |

The drafter's prompt — cached manifest summary AND dynamic data-section
schema — both render the populated type. Type-aware drafts reduce the
incidence of type-incoherent `custom_sql` business-rule tests (e.g. an
`INT64 <> STRING` comparison the warehouse will reject); see
[`docs/draft-ops.md` § Type-coherence defence](draft-ops.md#type-coherence-defence-issue-159)
for the parser-side belt-and-braces check.

### Failure modes (all silent except the path-safety gate)

- `catalog.json` absent → no merge; `data_type` fields stay `None`.
- `catalog.json` unreadable (permission denied) or malformed JSON → no
  merge; `data_type` fields stay `None`. **No log, no warning, no
  exception.** The manifest loader is stage-0 deterministic; emitting
  noise for a stale `catalog.json` is wrong UX.
- `catalog.json` declares a column NOT in `manifest.json` → ignored
  (manifest is the source of truth for "what columns exist").
- `manifest.json` has a column NOT in `catalog.json` → that column's
  `data_type` stays `None`.
- Column name casing differs between manifest and catalog (Snowflake
  uppercases identifiers; BigQuery preserves case; Postgres lowercases)
  → case-insensitive match via `lower(col_name)`; the merge works
  across all three warehouses without configuration.

**The one exception — path-containment violation.** If the resolved
`catalog.json` path escapes the project tree (e.g. a symlink that
resolves to `/etc/passwd`), the loader raises `PathContainmentError`
from `signalforge._common.path_safety` — same symlink-hardened gate as
`manifest.json` itself. This is a security boundary, not a stale-input
condition, so it deliberately fails loud rather than silently skipping.
A legitimate `catalog.json` will never trip this.

### Refreshing catalog.json

`catalog.json` is generated by `dbt docs generate`. If your warehouse
schema changes, re-run that command — SignalForge picks up the new
types on the next `manifest.load()` call. There is no in-memory cache
to invalidate; each `load()` rebuilds from disk.

For a regen of the test fixtures in this repo,
[`tests/fixtures/regenerate.sh`](../tests/fixtures/regenerate.sh) is
the maintainer-only driver.

## Error class quick reference

Public API: `from signalforge.manifest import errors`.

- **`ManifestNotFoundError`** — `load()` was given a path that does not
  exist or is not a regular file.
- **`UnsupportedManifestVersionError`** — `metadata.dbt_schema_version`
  resolves to a schema outside v9–v12 (e.g. v8 or v20/Fusion).
- **`ModelNotFoundError`** — `Manifest.get_model(unique_id)` was called
  with a unique_id absent from `nodes` and `disabled`.
- **`ModelDisabledError`** — `get_model()` matched a node, but it lives
  in the `disabled` dict; callers must opt in to disabled nodes
  explicitly.
- **`ModelPathOutsideProjectError`** — the resolver detected a model
  whose `original_file_path` (after symlink resolution) escapes the
  project root.
- **`ModelMissingSqlError`** — a model node has `raw_code: ""` or no
  resolvable SQL on disk.
