# Warehouse adapters

Established by issue #3 (BigQuery adapter). Apply to every adapter under `signalforge.warehouse.adapters` and to any new warehouse subpackage module.

## Subpackage layout: ABC + factory, concretes in `adapters/`

```
src/signalforge/warehouse/
  __init__.py            # re-exports the public surface
  base.py                # WarehouseAdapter ABC + from_profile factory
  errors.py              # WarehouseError hierarchy
  models.py              # Dialect, TableRef, PartitionFilter, ColumnStats, TestResult
  profiles.py            # load_profile, DbtProfileTarget
  _sql_safety.py         # identifier regex + cheap SQL sanity checks
  _path_safety.py        # symlink-hardened path canonicalisation (warehouse copy)
  _test_result_repr.py   # deterministic compact_repr for sample failures
  adapters/
    bigquery.py          # the only concrete adapter in v0.1
    _client.py           # pyright-noise shim around google.cloud.bigquery
```

The ABC is warehouse-agnostic. v0.2 Snowflake/Postgres slot under `adapters/` without restructuring (DEC-001). `_`-prefixed helpers stay reachable via dotted import but are absent from the package namespace.

## ABC + lazy-import factory (DEC-019)

```python
class WarehouseAdapter(abc.ABC):
    ...
    @classmethod
    def from_profile(cls, profile: DbtProfileTarget) -> WarehouseAdapter:
        from signalforge.warehouse.errors import UnsupportedProfileTypeError
        if profile.type == "bigquery":
            from signalforge.warehouse.adapters.bigquery import BigQueryAdapter  # lazy
            return BigQueryAdapter(...)
        raise UnsupportedProfileTypeError(profile_type=profile.type)
```

The concrete-adapter import lives **inside** the factory so callers who never invoke `from_profile` (tests injecting a fake client; v0.2 callers that pin Snowflake) don't pay the import cost or pull in `google-cloud-bigquery`.

Direct concrete instantiation (`BigQueryAdapter(project=..., location=...)`) stays supported for tests and explicit-config use — `from_profile` is the single dispatch point for the CLI / prune layer, not the only entry point.

## `_client.py` contains every `# pyright: ignore`

`google-cloud-bigquery` has gaps in its type stubs that provoke pyright noise. Confine **every** `# pyright: ignore[...]` and `# type: ignore[...]` for the BigQuery SDK to `adapters/_client.py`. The rest of the warehouse subpackage imports the shim's typed surface and stays pyright-clean.

The shim exposes a `_BQClientProtocol` duck-typed at exactly the surface the adapter consumes (`project`, `query`, `get_table`, `list_rows`). Both `bigquery.Client` and `tests/warehouse/_fake.py::FakeBigQueryClient` satisfy the protocol — the adapter calls the same method signatures regardless of which client was injected.

When v0.2 adds Snowflake/Postgres adapters, each gets its own `_client.py` shim under `adapters/` for the same reason. Don't pool SDK ignores into a generic util module.

## Test fakes use an `expect_*` helper API (DEC-002 / DEC-028)

Hand-rolled fakes only — `pytest-bigquery-mock` is unmaintained and `MagicMock`-style fakes auto-pass everything (violates `testing-signal.md`).

```python
fake = FakeBigQueryClient(project="p")
ref = TableRef(project="p", dataset="d", name="t")
fake.expect_query(matching=r"^SELECT COUNT", returns=[{"failures": 0}])
fake.expect_get_table(ref=ref, returns=FakeTable(num_rows=1_000_000, schema=[]))
fake.expect_list_rows(ref=ref, returns=[{"a": 1}])
adapter = BigQueryAdapter(client=fake, project="p")
adapter.run_test_sql(...)
fake.assert_all_expectations_met()
```

Each call consumes one matching expectation; calls outside the canned set raise `AssertionError("unexpected query: ...")`. Silent mismatches surface loudly. The fake lives under `tests/warehouse/_fake.py` — never import it from production code.

## Deterministic sampling, fail-loud sizing (DEC-006 / DEC-024)

`sample_rows(table, n, *, partition_filter=None)` uses the deterministic hash-mod pattern, **not** `TABLESAMPLE`:

```sql
SELECT * FROM `<quoted>` WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1 LIMIT n
```

Bucket size is derived from `Table.num_rows` so the expected sample is ~3–5× `n` before the LIMIT. Why deterministic: Architectural Commitment #5 (explainable diffs) requires same input → same prune decision; `TABLESAMPLE SYSTEM` is non-deterministic and doesn't work on views/MVs/wildcard tables.

Fail loud rather than silently scan TBs:

- `num_rows` missing on the `Table` resource → raise `UnknownTableSizeError`.
- `num_rows >= 100M` and no `partition_filter` → raise `SamplingRequiresPartitionFilterError`.

`partition_filter` is a typed `PartitionFilter` ADT (DEC-014), not a raw string. The adapter renders it via `_render_partition_filter` per-dialect; raw-string filter input is a SQL-injection seam and a cross-warehouse leak.

## Identifier validation at construction time (DEC-013)

Every public-API string field that becomes part of a SQL string runs through `_sql_safety.validate_identifier` at construction time. That covers `TableRef.{dataset, name}`, `PartitionFilter.column`, and the `column` parameter on `column_stats(table, column)`. The regex is strict:

```python
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
```

GCP **project IDs** use a different, hyphen-permissive grammar — route those through `validate_project_id`, not the strict identifier regex. Don't fold the two regexes back together.

`run_test_sql(sql)` does NOT parse SQL. It runs cheap rejects (`;`, `--`, unbalanced parens) inside `_sql_safety` and documents the contract: callers must supply a single SELECT statement returning rows. Full SQL parsing is overkill — the LLM drafter is the practical caller and we control its prompt.

## `QueryJobConfig` originates in `_default_job_config` (DEC-015)

Every query the adapter issues (`sample_rows`, `column_stats`, `run_test_sql`) builds its config through one private helper:

```python
def _default_job_config(self, *, stage: str) -> QueryJobConfig:
    # use_query_cache=False           — Architectural Commitment #5: reproducibility
    # maximum_bytes_billed=<limit>    — DEC-005: 100 MB default cap, per-call override-down only
    # labels={signalforge_stage, signalforge_version}  — v0.2 INFORMATION_SCHEMA cost attribution
```

`use_query_cache` is **not** user-overridable in v0.1. Same input → same prune decision is load-bearing for explainable diffs; cache hits would silently break that. Per-call `cost_limit_bytes` overrides the limit *downward only*; a profile-level `maximum_bytes_billed` caps both.

## Error hierarchy: typed + remediation, mirroring manifest layer

Every distinct failure mode is its own subclass of `WarehouseError`. Each carries a class-level `default_remediation`; the base `__str__` renders both message and `↳ Remediation:` line. The prune/CLI layers pattern-match on type rather than sniffing message text.

User-supplied strings render via `repr()` (`_format_value` helper) in every error message. A crafted dataset name like `foo'\nINFO: spoofed log line` cannot pollute log viewers — `repr()` quotes the string, escapes control characters, and makes whitespace visible. This is DEC-022 and applies to every typed exception in the layer.

`__repr__` on the adapter shows only `project` and `location` — never the underlying client, credentials object, or any token. Same DEC.

## Path safety: duplicated, not extracted (US-014 decision)

`signalforge.warehouse._path_safety.canonicalise_path` is a near-clone of `signalforge.manifest.loader._canonicalise_path`. The two copies stay decoupled:

- Different escape exceptions (`ProfileNotFoundError` vs `ModelPathOutsideProjectError`) keep each layer's catch surface homogeneous — every "we couldn't load the file" condition in one layer raises one typed error.
- A shared utility module would force every caller to translate the generic exception back to a layer-specific one, which is more code than the duplication.

US-014 evaluated extraction; the decision is to keep the duplication explicit. Apply the three traps from `manifest-readers.md` (resolve symlinks before containment check; catch `RuntimeError` on cycles; gate the *default* path through the same helper) when adding a third copy in a new reader.

## No logging in stage-0 modules; one-line warnings only at adapter boundary

Reader/parser modules emit no routine logs. The only exceptions are soft-threshold WARNINGs that signal the user is on a path likely to be slow or expensive — `profiles.py` emits one when `profiles.yml` exceeds the soft size cap (DEC-023); `_sql_safety.py` and `_path_safety.py` emit nothing. Observability beyond those soft warnings lives at the adapter boundary, where stage labels are known.

The adapter emits sparing `WARNING`-level signal when behaviour deviates from the deterministic happy path:

- `column_stats` queued batch exceeds the warning threshold (DEC-023) — signal that batching is doing real work.
- `sample_rows` num_rows >= threshold without a partition filter — signal before raising loud.

`INFO` and `DEBUG` are reserved for the prune/grade stages where signal-vs-volume tradeoffs surface. Don't add adapter-level INFO logging in v0.1.

## Reference

`plans/super/3-bigquery-adapter.md` — DEC-001 … DEC-028. `src/signalforge/warehouse/` — current implementation. `tests/warehouse/_fake.py` — `FakeBigQueryClient` + `expect_*` API. `docs/warehouse-adapter-ops.md` — operational reference.
