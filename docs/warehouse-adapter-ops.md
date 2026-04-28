# Warehouse adapter — operations guide

Operational reference for users of `signalforge.warehouse`. Companion to
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md) and the design
record in [`plans/super/3-bigquery-adapter.md`](../plans/super/3-bigquery-adapter.md).

v0.1 ships the BigQuery adapter only. Snowflake and Postgres are tracked
for v0.2; the public ABC (`WarehouseAdapter`) and the `from_profile`
factory are warehouse-agnostic so adding a sibling adapter is purely
additive.

## Quick start

One-time, on a fresh machine:

```bash
gcloud auth application-default login
```

Application Default Credentials (ADC) is the only supported auth method
in v0.1 (see [§2 dbt profile resolution](#dbt-profile-resolution)).

Then, from Python:

```python
from pathlib import Path
from signalforge.warehouse import (
    WarehouseAdapter,
    TableRef,
    load_profile,
)

profile = load_profile(Path("my_dbt_project"))
with WarehouseAdapter.from_profile(profile) as adapter:
    sample = adapter.sample_rows(
        TableRef(project="my-gcp-project", dataset="analytics", name="dim_users"),
        n=100,
    )
```

`WarehouseAdapter.from_profile` dispatches on `profile.type`. v0.1 only
supports `profile.type == "bigquery"`; anything else raises
`UnsupportedProfileTypeError` with a remediation pointing at the v0.2
roadmap entry.

## dbt profile resolution

`load_profile(project_dir, target=None)` resolves a `profiles.yml` in
this order (DEC-009):

1. `$DBT_PROFILES_DIR/profiles.yml` — user-trusted; honoured first when
   the env var is set.
2. `<project_dir>/profiles.yml` — symlink-hardened via the same path
   gate the manifest loader uses (`canonicalise_path`); a symlink that
   escapes the project tree raises `ProfileNotFoundError` rather than
   silently falling through to the home-dir path.
3. `~/.dbt/profiles.yml` — user-trusted.

`ProfileNotFoundError` lists every path searched in its remediation, so
"why didn't you find my profile?" answers itself from the exception
message.

**Active-target resolution.** Within the resolved profile, the active
output is selected as: explicit `target=` argument → the profile's own
`target:` field → `ProfileTargetNotFoundError`. `ProfileTargetNotFoundError`
inherits from `ProfileNotFoundError`, so a single `except` clause covers
both "no profile" and "wrong target" if the caller does not need to
distinguish them.

**Auth-method support (v0.1, DEC-017).** Only `method: oauth` (or unset,
which means "let dbt-bigquery default to ADC") is accepted. Every other
documented dbt-bigquery method raises `UnsupportedAuthMethodError` from
the Pydantic field validator, with the remediation pointing at
`gcloud auth application-default login`:

| `method` value                  | v0.1 behaviour                  |
| ------------------------------- | ------------------------------- |
| `oauth` / unset                 | accepted; uses ADC              |
| `service-account`               | `UnsupportedAuthMethodError`    |
| `service-account-json`          | `UnsupportedAuthMethodError`    |
| `oauth-secrets`                 | `UnsupportedAuthMethodError`    |
| `impersonate-service-account`   | `UnsupportedAuthMethodError`    |

Service-account methods land in v0.2; the v0.1 surface is intentionally
narrow so the auth path has one well-tested branch.

`DbtProfileTarget` is a strict (`extra="forbid"`) Pydantic v2 model.
Unknown profile keys raise `ValidationError`; this is a deliberate
divergence from the manifest reader's `extra="ignore"` posture (DEC-017),
because silently dropping an auth-config key could mean SignalForge
falls back to ADC when the user thought they had configured something
else. Forward-compat against new dbt-bigquery fields is the drift-detector
test's responsibility (`tests/warehouse/test_profiles.py`).

## Cost defaults

The BigQuery adapter is opinionated about cost on every query.

- **`maximum_bytes_billed = 100 MB` by default** (DEC-005). `BigQueryAdapter`
  takes a `max_bytes_billed=` kwarg; the dbt profile's
  `maximum_bytes_billed` field flows through `load_profile` and
  `from_profile` and overrides the default. Queries that exceed the cap
  raise `BytesBilledExceededError`, which carries `job_id`, `bytes_billed`,
  and `limit` so users can cross-link to the BigQuery job history.
- **`use_query_cache=False` on every query** (DEC-015). Architectural
  Commitment #5 — explainable diffs — requires that the same input
  produce the same prune decision; cached results break that contract.
  v0.2 may re-enable caching behind an explicit opt-in; in v0.1 it is
  unconditionally off.
- **BigQuery job labels are auto-set** on every query:
  - `signalforge_stage` — the pipeline stage that issued the query
    (e.g. `column_stats`, `sample_rows`, `run_test_sql`).
  - `signalforge_version` — the package version (with `.` rewritten to
    `_` to satisfy BigQuery's label-character constraint).

  Both are filterable in `INFORMATION_SCHEMA.JOBS_BY_PROJECT` for v0.2
  cost analysis, e.g.:

  ```sql
  SELECT job_id, total_bytes_billed
  FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE labels.signalforge_stage = 'sample_rows'
  ```

## Sampling strategy

`adapter.sample_rows(table, n, partition_filter=None)` returns up to
`n` rows from `table`, deterministically.

**Default: hash-mod (DEC-006).** Every call wraps the table in:

```sql
SELECT * FROM <quoted> AS t
WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1
LIMIT n
```

`bucket` is sized from `Table.num_rows` so the expected sample size lands
near `n`. The hash-mod approach is deterministic across runs, works on
views, materialised views, wildcard tables, and CTEs, and behaves
correctly when `TABLESAMPLE` does not.

**The TABLESAMPLE cost-asterisk.** `TABLESAMPLE SYSTEM` is documented in
BigQuery as the canonical sampling primitive, but it does **not**
proportionally reduce bytes billed on un-clustered tables — it scans the
whole table and then drops blocks. It is only cost-effective on
clustered tables or in conjunction with a partition filter. Hash-mod
has the same cost story (it scans everything too) without the
determinism downside, so it is the v0.1 default. v0.2 will add an
opt-in TABLESAMPLE strategy for clustered tables where the bytes-billed
math works out.

**`PartitionFilter` (DEC-014).** Scope a sample to a specific partition
to actually reduce bytes billed:

```python
from datetime import date
from signalforge.warehouse import PartitionFilter

adapter.sample_rows(
    table,
    n=100,
    partition_filter=PartitionFilter(
        column="event_date",
        op=">=",
        value=date(2024, 1, 1),
    ),
)
```

Each adapter renders its own SQL for `PartitionFilter`. The typed
`column`/`op`/`value` triple — `op` is a `Literal["=", ">", ">=", "<",
"<=", "!="]` and `column` is identifier-validated at construction time —
removes a SQL-injection seam and prevents cross-warehouse SQL leaks
(DEC-018).

**Fail-loud thresholds (DEC-024).** Sampling refuses to silently
over-spend in two cases:

| Condition                                              | Exception                                |
| ------------------------------------------------------ | ---------------------------------------- |
| `Table.num_rows` is `None`/`0` and no partition filter | `UnknownTableSizeError`                  |
| `Table.num_rows >= 100_000_000` and no partition filter | `SamplingRequiresPartitionFilterError`   |

Both carry the offending `table` (and `num_rows`, where known) and a
remediation that names the fix. Fail-loud is preferred over a guessed
bucket size because the worst case — a terabyte-scale unscoped scan —
is silent on the user's side and very loud on the bill.

## `column_stats` access pattern

`adapter.column_stats(table, column)` returns a `ColumnStats` object
with `count`, `distinct`, `nulls`, `min`, `max`, and `data_type`. To
keep query count down, calls are **batched per table** (DEC-025): the
adapter accumulates references inside the active `with adapter:` block
and flushes them as a single SQL query the first time any field of any
returned `ColumnStats` is read.

This means `column_stats` MUST be called inside a `with adapter:` block
— calling it outside one raises `RuntimeError`. The recommended pattern
is to collect references first, then read fields:

```python
with WarehouseAdapter.from_profile(profile) as adapter:
    refs = {col: adapter.column_stats(table, col) for col in ["a", "b", "c"]}
    for col, stats in refs.items():
        print(col, stats.count, stats.distinct, stats.nulls)
```

The first `stats.count` read flushes a single batched query covering
all three columns; subsequent reads on the same table are served from
the result cached on the references.

**Complex types (DEC-016).** For BigQuery types where ordering is not
meaningful — `GEOGRAPHY`, `JSON`, `BYTES`, `ARRAY<...>`, `STRUCT<...>`,
`RANGE<...>` — `min` and `max` are `None`. `count`, `distinct`, and
`nulls` are populated for every type. The prune layer keys decisions on
`data_type` (the raw BigQuery type string) without re-reading the
catalog.

## Integration tests (maintainer-only)

The default `pytest` invocation skips warehouse-touching tests via
`addopts = -m 'not bigquery'` (DEC-021). To run them locally:

```bash
SF_RUN_BQ=1 pytest -m bigquery
```

The `SF_RUN_BQ=1` env-var gate lives on top of the marker so even an
explicit `-m bigquery` will not fire BigQuery requests in CI by accident.

Fixtures use `bigquery-public-data.samples.shakespeare`, which is free
under BigQuery's 1 TB/month query tier. There is no CI job for these
tests in v0.1; revisit when external contributors arrive and a billing
account on the project is available.

## Debugging

Enable `DEBUG`-level logs from anywhere in the adapter layer:

```python
import logging
logging.getLogger("signalforge.warehouse").setLevel(logging.DEBUG)
```

The adapter never logs full SQL or row data (DEC-027) — only batch-flush
events, metadata-cache misses, and the two soft-threshold warnings
(profile size, query bytes). Treat `DEBUG` output as a hint about *when*
the adapter went to the warehouse, not *what* it sent.

**Finding the BigQuery job ID for a failed query.** Every typed error
carries the contextual fields the BigQuery console needs:

- `BytesBilledExceededError` — `.job_id`, `.bytes_billed`, `.limit`
- `QuerySyntaxError` — `.detail` (the BigQuery error text verbatim)
- `TableNotFoundError` — `.table`
- `ColumnNotFoundError` — `.table`, `.column`

**Common errors and where they surface:**

| Error                                       | First thing to check                                                  |
| ------------------------------------------- | --------------------------------------------------------------------- |
| `WarehouseAuthError`                        | `gcloud auth application-default login` (ADC not set up).             |
| `BytesBilledExceededError`                  | Raise `max_bytes_billed=` on `BigQueryAdapter`, or supply a `PartitionFilter`. |
| `UnknownTableSizeError`                     | Supply a `PartitionFilter`, or call `adapter.refresh_table_metadata`. |
| `SamplingRequiresPartitionFilterError`      | Supply a `PartitionFilter` to scope the sample.                       |
| `InvalidIdentifierError`                    | Check `TableRef`/`PartitionFilter.column` against `[A-Za-z_][A-Za-z0-9_]*`. |

## Error reference

Public API: `from signalforge.warehouse import errors`. Every exception
subclasses `WarehouseError` and carries a `default_remediation` rendered
on a `↳ Remediation:` line by `__str__`.

| Class                                    | When raised                                                                                              | Carried fields                                       | Default remediation (abbreviated)                                                              |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `WarehouseError`                         | Base class; never raised directly.                                                                       | `message`, `remediation`                             | _(no remediation set — base class)_                                                             |
| `WarehouseAuthError`                     | Wraps `google.auth.exceptions.DefaultCredentialsError` / `RefreshError`.                                 | `message`                                            | Run `gcloud auth application-default login` to set up ADC.                                      |
| `UnsupportedProfileTypeError`            | dbt profile's `type` is not `"bigquery"`.                                                                | `profile_type`                                       | v0.1 supports `type: bigquery` only; Snowflake/Postgres tracked for v0.2.                        |
| `UnsupportedAuthMethodError`             | dbt profile's `method` is not `"oauth"` (or unset).                                                      | `method`                                             | v0.1 supports `method: oauth` (or unset) only; run `gcloud auth application-default login`.     |
| `ProfileNotFoundError`                   | None of the three search paths yielded a `profiles.yml` (or the project file is missing/malformed).      | `searched_paths`                                     | Create a `profiles.yml` at one of the searched paths, or set `DBT_PROFILES_DIR`.                |
| `ProfileTargetNotFoundError`             | The profile resolved but the requested `target` is missing. Inherits `ProfileNotFoundError`.             | `profile_name`, `target`, `searched_paths`           | Add the target to `profiles.yml`, or pass an explicit `target=` that exists in the profile.     |
| `ManifestProjectNotFoundError`           | `Model.database` is `None` so `TableRef.from_model` cannot construct a fully-qualified ref.              | `model_unique_id`                                    | Set `database:` for the model in dbt, or pass an explicit `project=`.                            |
| `ManifestSchemaNotFoundError`            | `Model.schema_` is `None` so `TableRef.from_model` cannot construct a fully-qualified ref.               | `model_unique_id`                                    | Set `schema:` for the model in dbt, or pass an explicit `dataset=`.                              |
| `InvalidIdentifierError`                 | A SQL identifier (project / dataset / table / column) failed the `[A-Za-z_][A-Za-z0-9_]*` regex.         | `field`, `value`                                     | Identifiers must match `[A-Za-z_][A-Za-z0-9_]*`.                                                |
| `BytesBilledExceededError`               | BigQuery rejected a query because `maximum_bytes_billed` was exceeded.                                   | `job_id`, `bytes_billed`, `limit`                    | Narrow the query (partition filter / smaller sample) or raise `max_bytes_billed`.                |
| `TableNotFoundError`                     | BigQuery 404 for the requested `TableRef`.                                                               | `table`                                              | Verify the `project.dataset.table` exists and credentials have read access.                      |
| `ColumnNotFoundError`                    | A column reference does not exist on the resolved table schema.                                          | `table`, `column`                                    | Verify the column name against the table schema (`INFORMATION_SCHEMA.COLUMNS`).                  |
| `QuerySyntaxError`                       | BigQuery rejected a query as malformed (separates from `BytesBilledExceededError` despite both being 400). | `detail`                                             | Inspect the BigQuery error detail and fix the SQL (or update the drafter prompt if recurring).   |
| `SamplingError`                          | Parent for sampling-time failures; never raised directly. Catch it to handle both subclasses uniformly.  | _(none)_                                             | Inspect the subclass remediation; fail-loud is preferred to silent over-spend.                   |
| `SamplingRequiresPartitionFilterError`   | `Table.num_rows >= 100_000_000` and no `PartitionFilter` was supplied.                                   | `table`, `num_rows`                                  | Pass a `PartitionFilter` to scope the sample.                                                    |
| `UnknownTableSizeError`                  | `Table.num_rows` is `None`/`0` and no `PartitionFilter` was supplied.                                    | `table`                                              | Provide `partition_filter`, or call `adapter.refresh_table_metadata` once `num_rows` is populated. |
