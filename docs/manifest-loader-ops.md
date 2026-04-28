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

- v12 can be regenerated with the in-`[dev]` `dbt-core>=1.8` install — no
  `uvx` needed; `pip install -e ".[dev]"` is sufficient.
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
