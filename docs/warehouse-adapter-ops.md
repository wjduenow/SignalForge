# Warehouse adapter — operations guide

Operational reference for users of `signalforge.warehouse`. Companion to
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md) and the design
record in [`plans/super/3-bigquery-adapter.md`](../plans/super/3-bigquery-adapter.md).

v0.1 ships the BigQuery adapter only. Snowflake and Postgres are tracked
for v0.2; the public ABC (`WarehouseAdapter`) and the `from_profile`
factory are warehouse-agnostic so adding a sibling adapter is purely
additive.

**Snowflake skeleton (v0.2, issue #119).** `from_profile` now dispatches
`type: snowflake` to a `SnowflakeAdapter` skeleton — `dialect()` returns the
Snowflake `Dialect` (`quote_char='"'`, `identifier_case='upper'`,
`supports_qualify=True`), but the sampling / profiling / test-running methods
raise `NotImplementedError` naming epic #118 until #122–#124 land. The
connector is an optional extra — install it with
`pip install "signalforge-dbt[snowflake]"` (or `uv pip install "signalforge-dbt[snowflake]"`);
the base install never pulls `snowflake-connector-python`.

**Snowflake profile parsing (v0.2, issue #120).** `load_profile` parses a
`type: snowflake` target into a `DbtProfileTarget` carrying `account`,
`user`, `role`, `warehouse`, `database`, `schema`, `threads`, and the auth
fields. Required keys are `account` / `user` / `warehouse` — a missing one
raises `IncompleteProfileError` listing every missing key. **Auth methods:**
v0.2 supports `password`, key-pair (`private_key_path` +
`private_key_passphrase`), and SSO (`authenticator: externalbrowser`); OAuth
(`authenticator: oauth` + `token`), inline `private_key`, and MFA
(`username_password_mfa`) are deferred and fail loud with a remediation
naming the supported set. **Identifier validation:** `warehouse`,
`database`, `schema`, and `role` are validated as strict SQL identifiers at
load time (they become SQL when #122 opens the connection). **Known
limitation:** the strict identifier grammar rejects Snowflake's legal `$` in
`warehouse`/`database`/`schema`/`role` names (e.g. `WH$PROD`) — a documented
v0.x deferral. The `account` locator uses a separate permissive grammar
(accepts org-account `myorg-account1` and region-suffixed `xy12345.us-east-1`
forms; rejects quotes, `;`, whitespace, control chars).

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

`WarehouseAdapter.from_profile` dispatches on `profile.type`.
`profile.type == "bigquery"` is fully implemented; `profile.type ==
"postgres"` (v0.2 stub, #53) and `profile.type == "snowflake"` (v0.2
skeleton, #119) dispatch to their adapters, whose warehouse-operation
methods raise `NotImplementedError` until the full implementations land.
Any other `profile.type` raises `UnsupportedProfileTypeError` with a
remediation pointing at the v0.2 roadmap entry.

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
  raise `BytesBilledExceededError`. The exception's `limit` field always
  carries the configured cap; `job_id` and `bytes_billed` are populated
  only when BigQuery's `BadRequest` exposes them (it usually doesn't on
  the pre-execution rejection path) and are otherwise `None`. The error
  message and remediation are sufficient to act on without those fields;
  v0.2 may revisit by surfacing the failed `QueryJob` so the IDs flow
  through.
- **`use_query_cache=False` on every query** (DEC-015). Architectural
  Commitment #5 — explainable diffs — requires that the same input
  produce the same prune decision; cached results break that contract.
  v0.2 may re-enable caching behind an explicit opt-in; in v0.1 it is
  unconditionally off.
- **Per-call `timeout_ms`** (DEC-013 of issue #6): pass an integer to
  `_default_job_config(stage="...", timeout_ms=...)` to set
  `QueryJobConfig.job_timeout_ms`; BigQuery cancels the job
  server-side at expiry. Bytes-scanned through the cancellation point
  still bill — set conservatively. Default `None` (no timeout).
  Reserved for v0.2 prune layer integration (issue #6 ships with
  `total_budget_seconds` enforcement only; v0.1 has no public
  `WarehouseAdapter.run_test_sql` kwarg for per-test timeouts).
- **BigQuery job labels are auto-set** on every query:
  - `signalforge_stage` — the pipeline stage that issued the query.
    Values are `warehouse_sample` (from `sample_rows`),
    `warehouse_sample_materialise` (from `materialise_sample`, v0.2 —
    see [Materialised sampling](#materialised-sampling-v02-issue-22)),
    `warehouse_stats` (from `column_stats`), `warehouse_test` (from
    `run_test_sql`), and `warehouse_session_abort` (from the
    `__exit__` cleanup `CALL BQ.ABORT_SESSION()` query — DEC-013 of
    #22).
  - `signalforge_version` — the package version (with `.` rewritten to
    `_` to satisfy BigQuery's label-character constraint).

  Both are filterable in `INFORMATION_SCHEMA.JOBS_BY_PROJECT` for v0.2
  cost analysis. Stage labels are `warehouse_sample`,
  `warehouse_sample_materialise` (v0.2), `warehouse_stats`,
  `warehouse_test`, and `warehouse_session_abort` (v0.2; one per
  pipeline stage that issues a query):

  ```sql
  SELECT job_id, total_bytes_billed
  FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE labels.signalforge_stage = 'warehouse_sample'
  ```

## Sampling strategy

`adapter.sample_rows(table, n, partition_filter=None)` returns up to
`n` rows from `table`, deterministically.

**Default: hash-mod (DEC-006).** Every call wraps the table in:

```sql
SELECT * FROM <quoted> AS t
WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1
ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(t))
LIMIT n
```

The trailing `ORDER BY` makes the `LIMIT` truncation deterministic when
the bucket filter retains more than `n` rows; without it BigQuery's
`LIMIT` picks an arbitrary subset and breaks the same-input →
same-output prune contract.

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
with `count`, `distinct`, `nulls`, `min`, `max`, and `data_type`. The
call MUST be inside a `with adapter:` block (DEC-025); calling it
outside one raises `RuntimeError`.

**Flush semantics in v0.1.** Inside an active `with adapter:` block,
calls to `adapter.column_stats(table, col)` accumulate per table. The
**first** call for a given table flushes every column queued for that
table in a single batched aggregate query, populating the cache;
subsequent `column_stats(table, ...)` calls for columns already in the
cache return without issuing another query. Columns queued *after* a
flush are batched into the next flush when the first uncached column is
requested.

The returned `ColumnStats` is a fully-populated typed value (no lazy
proxy). v0.2 may add a lazy proxy that defers the flush until a field is
read, but the v0.1 contract is "first call flushes the queued batch" —
predictable and easy to reason about at a `with`-block boundary.

Use the recommended pattern below to keep call sites cheap to refactor
when the lazy form lands:

```python
with WarehouseAdapter.from_profile(profile) as adapter:
    refs = {col: adapter.column_stats(table, col) for col in ["a", "b", "c"]}
    for col, stats in refs.items():
        print(col, stats.count, stats.distinct, stats.nulls)
```

In v0.1 this issues three queries (one per `column_stats` call); v0.2
will collapse all three into a single batched query at first field
read.

**Complex types (DEC-016).** For BigQuery types where ordering is not
meaningful — `GEOGRAPHY`, `JSON`, `BYTES`, `ARRAY<...>`, `STRUCT<...>`,
`RANGE<...>` — `min` and `max` are `None`. `count`, `distinct`, and
`nulls` are populated for every type. The prune layer keys decisions on
`data_type` (the raw BigQuery type string) without re-reading the
catalog.

## Materialised sampling (v0.2, issue #22)

The `WarehouseAdapter` ABC ships a `materialise_sample` method in v0.2
that pre-computes a deterministic sample into a session-scoped temp
table, so every candidate test's per-test query reads from the
narrow materialised sample rather than re-running the full-row hash
filter against the source table for every test (see
[`docs/prune-ops.md` § Cost model](prune-ops.md#cost-model-us-003-verification)
for the cost story).

ABC signature (`signalforge.warehouse.base`):

```python
def materialise_sample(
    self,
    table: TableRef,
    n: int,
    *,
    partition_filter: PartitionFilter | None = None,
    ttl_seconds: int = 3600,
) -> TableRef: ...
```

The default ABC implementation raises `MaterialisationNotSupportedError`
with a remediation pointing at `prune.sample_strategy: oneshot` in
`signalforge.yml`. The `BigQueryAdapter` overrides; non-BQ adapters
inherit the default until v0.3.

**BigQueryAdapter session-state pattern.** The first call to
`materialise_sample` runs `CREATE TEMP TABLE _sf_sample_<run_id> AS SELECT ...`
(the CTAS itself uses the bare `_sf_sample_<run_id>` name — no
`_SESSION.` prefix) with `QueryJobConfig(create_session=True, ...)`
against the `warehouse_sample_materialise` stage label. **BigQuery
assigns the session_id server-side**; the adapter captures it from
`job.session_info.session_id` and stores it on the adapter instance
as `self._active_session_id` for the duration of the prune run.
Subsequent `run_test_sql` calls automatically attach
`ConnectionProperty(key="session_id", value=self._active_session_id)`
so the per-test query resolves `_SESSION._sf_sample_<run_id>` against
the same session. The returned `TableRef` carries
`project=None, dataset="_SESSION", name="_sf_sample_<run_id>"` —
two-part `qualified_name` `_SESSION._sf_sample_<run_id>`. `project=None`
is load-bearing because BigQuery rejects the three-part
`<project>._SESSION.<name>` form even inside the owning session.

The `run_id` is OUR derivation —
`blake2b(table.qualified_name + signalforge_version + str(n) + canonical_json(partition_filter), digest_size=8).hexdigest()`
(inputs joined with NUL separator; 16 hex chars) — so the temp-table
name is deterministic across runs and the `compiled_sql_hash`
reproducibility invariant on `PruneEvent` (DEC-005 of issue #6) is
preserved.

**`ttl_seconds` is OUR-side hint, not a BQ knob.** BigQuery sessions
have a server-enforced max lifetime (~24h regardless of activity)
plus a BQ-default idle timeout. The `ttl_seconds=3600` parameter is
NOT passed to BigQuery — it's a hint to the cleanup-WARNING text
(the "auto-expire in Ns" line below). Don't go looking for a BQ SDK
call to set it; there isn't one in v0.2.

The session-state pattern mirrors `column_stats`'s batching state
(DEC-008 / DEC-025 of issue #3): adapter-instance state scoped to a
`with adapter:` block; cleanup driven by `__exit__`.

**v0.2 → v0.3 migration story for non-BQ adapters.** Snowflake and
Postgres adapters in v0.2 inherit the default `materialise_sample` →
`MaterialisationNotSupportedError` raise. Operators on those
warehouses opt in to the v0.1 oneshot path via
`prune.sample_strategy: oneshot` in `signalforge.yml`. Each
non-BigQuery adapter then ships its own session-equivalent in v0.3
(Snowflake: temporary tables tied to the session; Postgres:
`CREATE TEMP TABLE` inside a transaction). The ABC default-raise is
the v0.2 stop-gap, not a permanent surface.

> **Snowflake update (#122/#124):** Snowflake no longer inherits the
> `MaterialisationNotSupportedError` default — `materialise_sample` is
> implemented. However, neither sample strategy is functional against a
> *live* Snowflake yet (the `HASH(*)` sample SQL is rejected in
> `WHERE`/`ORDER BY`, and oneshot's row-count routes through a BigQuery-only
> seam). On Snowflake use **`prune.scope: full`**, NOT
> `prune.sample_strategy: oneshot`; see "Known limitations on live Snowflake"
> below.

## Query-bytes estimation (v0.2, issue #36)

The `WarehouseAdapter` ABC ships an `estimate_query_bytes` method in
v0.2 so the `signalforge generate --estimate` cost-preview flow can
estimate how many bytes a candidate query would process WITHOUT
actually scanning the source table. The BigQuery override uses
`QueryJobConfig(dry_run=True)` and reads `job.total_bytes_processed`
off the returned job; the Snowflake override (issue #130) runs
`EXPLAIN USING JSON` and parses `GlobalStats.bytesAssigned` from the
returned plan. Adapters without their own primitive inherit the ABC's
default `EstimateNotSupportedError` raise.

ABC signature (`signalforge.warehouse.base`):

```python
def estimate_query_bytes(self, sql: str) -> int: ...
```

The default ABC implementation raises `EstimateNotSupportedError` with
the locked remediation: `"Use --estimate with a BigQuery profile, or
wait for v0.3 multi-warehouse estimation support."` Concrete adapters
override; v0.2 ships the BigQuery override (`dry_run`) and the Snowflake
override (`EXPLAIN USING JSON`, issue #130). The Postgres stub still
inherits the default raise pending its own `EXPLAIN` override.

**BigQuery override mechanism.** A `dry_run=True` query asks BigQuery
to validate the SQL server-side and return the estimated bytes
processed, without committing to any actual scan or row return.
BigQuery does NOT bill bytes for a dry_run, so the production
`QueryJobConfig` deliberately omits `maximum_bytes_billed` — a cap on
something that never bills would be dead config; worse, it could
mislead a reader into thinking the dry_run was guarded against
runaway cost. The job is tagged with the `warehouse_estimate_query_bytes`
stage label for `INFORMATION_SCHEMA.JOBS_BY_PROJECT` cost attribution.

The same `_sql_safety.validate_test_sql` cheap-reject pass that
`run_test_sql` applies fires before the SDK call: a SQL with a
top-level `;`, a `--` comment, a `/* */` block comment, or unbalanced
parens raises `QuerySyntaxError` and never reaches BigQuery.

**Snowflake override mechanism (issue #130).** The Snowflake adapter
validates the caller SQL through the same `_sql_safety.validate_test_sql`
pass, then prepends the literal `EXPLAIN USING JSON` prefix (with a single
trailing space) and runs the EXPLAIN through its connection cursor. Snowflake has no BigQuery-style
`dry_run` (bytes-without-billing); `EXPLAIN` is the closest primitive,
reporting the query planner's estimated partitions and bytes in a single
JSON cell. The override parses `GlobalStats.bytesAssigned` from that plan
as the `int`-bytes estimate. `EXPLAIN` is planner-only — it scans no
partitions and bills no bytes.

**Planner-estimate accuracy caveat.** The figure `EXPLAIN USING JSON`
returns is the planner's *estimate*, not a measured scan. It can differ
from the bytes a real query ultimately processes, and the planner's
output may vary across Snowflake releases (the same caveat that applies
to Snowflake's `HASH()` row-sampling expression — see
`prune-engine.md`). It is a cost *preview*, calibrated for "is this
roughly cheap or roughly expensive," not a billing guarantee.

**Estimation-unavailable degrade (`EstimateUnavailableError`).** When the
Snowflake `EXPLAIN` *succeeds* but the returned plan carries no parseable
`GlobalStats.bytesAssigned` — a metadata-only query, a plan-shape change
across Snowflake versions, or a malformed cell — the adapter raises
`EstimateUnavailableError` rather than fabricating a number (a `return 0`
would silently report $0 cost on a future plan-shape change). This is
distinct from `EstimateNotSupportedError`: the adapter *supports*
estimation, it just couldn't extract the figure for THIS query. The
`--estimate` engine catches it at the supplementary-source boundary
(issue #36 DEC-005) and renders `<unavailable: EstimateUnavailableError>`,
falling back to a price-only preview rather than aborting the run.

**v0.2 → v0.3 migration story for remaining adapters.** The Postgres stub
still inherits the default `estimate_query_bytes` →
`EstimateNotSupportedError` raise until it grows its own override
(Postgres's `EXPLAIN` is the natural primitive). The CLI's `--estimate`
flow surfaces the typed error with the locked remediation so operators
see the expansion plan inline. Snowflake's `--estimate` path is no longer
a degrade: it returns a real EXPLAIN-based estimate (issue #130), having
graduated from the issue #123 `<unavailable: EstimateNotSupportedError>`
placeholder once the connection seam landed (#122).

## Session cleanup & manual recovery

Sessions opened by `materialise_sample` need to be torn down so
their `_SESSION._sf_sample_<run_id>` temp tables don't linger until
BigQuery's server-side timeout reaps them (~24h). The adapter
implements a three-layer cleanup model — explicit close on the happy
path, swallow-and-warn on cleanup failure, BQ's own session timeout
as the durable fallback (issue #22 DEC-013 / DEC-014).

**Layer 1 — explicit `__exit__` close (happy path).** When an
operator wraps the adapter in a `with` block (the recommended pattern
that the CLI's `cmd_generate` always uses), `__exit__` checks
`self._active_session_id`; if non-`None`, it issues
`CALL BQ.ABORT_SESSION();` on the same session via
`ConnectionProperty(key="session_id", value=self._active_session_id)`.
On success, the adapter emits one `INFO` log (`session_id_hash`,
`ttl_remaining_seconds`) and resets `_active_session_id = None` in a
`finally` clause so subsequent `__exit__` calls are no-ops.

**Layer 2 — swallow-and-warn (cleanup failure).** If
`CALL BQ.ABORT_SESSION();` itself raises (network blip, session
already revoked, quota issue), the adapter **swallows the exception**
and emits a single multi-line WARNING — cleanup must never block the
user's actual work, which already succeeded. The WARNING contains
the **raw session_id** (deliberate exception to the otherwise-strict
session-id redaction rule, see DEC-003 / DEC-014 of issue #22) and
the manual `bq query` command the operator can run to clean up
immediately. State is reset in `finally` so a second `__exit__` call
is a no-op.

**Layer 3 — BigQuery server-side session timeout (durable
fallback).** Hard process death (SIGKILL, OOM, host failure, the
operator forgetting to use a `with` block in a notebook session)
cannot fire `__exit__`. BigQuery's own session timeout (BQ-managed,
~24h max regardless of activity) reaps the orphan automatically.
The operator pays a small cost in temp-table storage until the
timeout fires, but no human intervention is required.

**Manual recovery command.** When the cleanup-failure WARNING
fires, the operator copy-pastes the manual command verbatim from
the WARNING body. The exact form is (per DEC-014 of issue #22 —
this is the text the WARNING emits, do not paraphrase):

```bash
bq query --connection_property=session_id=<raw> --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"
```

`<raw>` is the same raw session_id printed in the WARNING's
`Session ID:` line; the manual command is the only remediation
that doesn't wait for the BQ timeout. Authorisation: BigQuery
rejects `BQ.ABORT_SESSION()` calls from any identity other than the
session's owner, so only the operator who started the prune run can
execute the manual command — hence the raw session_id in the WARNING
is bounded in surface (read-only to the principal who already owned
the session).

**Reading the cleanup-failure WARNING.** See
[`docs/cli-ops.md` § Stderr shapes (WARNING)](cli-ops.md#stderr-shapes-warning)
for the full WARNING shape and the `--quiet` interaction (the
cleanup-failure WARNING is operator-actionable and is NOT suppressed
by `--quiet`).

### Edge case: SDK returns `session_info=None`

If `materialise_sample`'s first query succeeds server-side but the
BigQuery SDK returns `job.session_info=None` (or `session_id=None`),
the adapter cannot stash the id and `__exit__` will not fire
`BQ.ABORT_SESSION()` — `_active_session_id is None`, so the cleanup
short-circuits to a no-op. The server-side session lives until BQ's
own timeout (~24h max). This is the SDK contract violating its own
documented behaviour and is not expected in practice; the
**Spotting orphaned sessions** query below catches it. If you see
materialisation jobs in `INFORMATION_SCHEMA.JOBS_BY_PROJECT` whose
`session_info.session_id` is set but no matching `BQ.ABORT_SESSION`
job ever ran for that session, this is the path that produced them.

### Spotting orphaned sessions

When a maintainer wants to audit a project for orphan sessions
(e.g., after a known-bad release that bypassed `__exit__`, or as a
periodic cleanup hygiene task), the adapter's `signalforge_stage`
job label is the durable signal. Run this `INFORMATION_SCHEMA.JOBS_BY_PROJECT`
query to list materialisation jobs older than 2× the expected TTL
(default `ttl_seconds=3600` → look for jobs older than 2h that may
have leaked sessions):

```sql
SELECT
  job_id,
  user_email,
  creation_time,
  session_info.session_id,
  state,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), creation_time, MINUTE) AS age_minutes
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE labels.signalforge_stage = 'warehouse_sample_materialise'
  AND creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), creation_time, MINUTE) > 120
ORDER BY creation_time DESC;
```

Adjust `region-us` to your dataset's region and the
`INTERVAL 24 HOUR` window to your retention if `INFORMATION_SCHEMA.JOBS_BY_PROJECT`
is set up differently. Each row identifies one session that was
opened but whose `__exit__`-driven `CALL BQ.ABORT_SESSION();` never
fired (or fired and failed silently in v0.2 if the WARNING was
suppressed). Operators with permission to abort each session can
reuse the manual recovery command above with the per-row
`session_info.session_id`.

## Snowflake adapter (v0.2, epic #118)

The Snowflake seam ships across issues #119 (skeleton), #120 (profile),
#121 (compiler dialect), #122 (sampling + session), #130 (EXPLAIN
estimate), and #124 (test harness + ops docs). This section consolidates
what an operator running SignalForge against Snowflake needs; the
cross-cutting sections above (sampling, materialised sampling, estimation,
error reference) carry the BigQuery + Snowflake detail inline.

**Install.** `pip install "signalforge-dbt[snowflake]"` (or `uv pip install
"signalforge-dbt[snowflake]"`). The base install never pulls
`snowflake-connector-python`; it lives only under the `[snowflake]` extra.

**Profile keys.** A `type: snowflake` target requires `account`, `user`,
and `warehouse`; `database` / `schema` / `role` are optional (dbt allows
them at model level / via the user's default role). Auth scope:
password, key-pair (`private_key_path` + optional `private_key_passphrase`),
and SSO (`authenticator: externalbrowser`). `oauth` /
`username_password_mfa` are deferred (raise `UnsupportedAuthMethodError`).
Known limitation: the strict identifier grammar rejects Snowflake's legal
`$` in identifiers.

**Dialect.** `SNOWFLAKE_DIALECT` sets `quote_char='"'`,
`identifier_case='upper'` (Snowflake folds unquoted identifiers to
upper-case, so the compiler folds-then-quotes — `"CUSTOMER_ID"` resolves
against conventional unquoted DDL), per-component quoting
(`"DB"."SCHEMA"."T"`), `ABS(HASH(*))` as the deterministic row-sample
hash, `'{value}'::TIMESTAMP/::DATE` partition-filter literals, and a
quoted `"sample"` CTE alias (`SAMPLE` is a Snowflake reserved word, so an
unquoted `WITH sample AS …` is a syntax error). `HASH()` is deterministic
only *within a Snowflake release* — sufficient for within-run prune
determinism, not cross-time stable (mirrors the EXPLAIN planner-estimate
caveat).

**Connection-bound session.** Unlike BigQuery (which threads a server-side
`session_id` on every query), the Snowflake *connection* holds the session
— `materialise_sample` creates a session-scoped
`CREATE TEMPORARY TABLE "<SRC DB>"."<SRC SCHEMA>"."_SF_SAMPLE_<RUN_ID>"`
colocated with the source (each component fold-to-UPPER then quoted, per
`_quote`), and `__exit__` closes the connection (reaping its temp tables).
**Cost-relevant consequence:** the source table must be **writable** —
materialised sampling against a read-only shared database (e.g.
`SNOWFLAKE_SAMPLE_DATA`) fails the CTAS. Note that *all* sample-mode prune
is currently non-functional on live Snowflake regardless (see "Known
limitations" below); use `prune.scope: full` today.

**Session cleanup is fail-soft with no manual command.** A Snowflake temp
table is unreachable outside its owning session, so there is no
`bq`-style abort command and no `auto-expire in <N>s` countdown — the
honest durable fallback is Snowflake's server-side idle-session reap. On a
cleanup failure the adapter swallows the error and emits one
operator-actionable WARNING quoting the raw `session_id` (the only path
where the raw id appears; success logs hash it). `--quiet` does **not**
suppress this WARNING.

**Estimate.** `signalforge generate --estimate` runs `EXPLAIN USING JSON`
and parses `GlobalStats.bytesAssigned` (see [§ Query-bytes
estimation](#query-bytes-estimation-v02-issue-36)). EXPLAIN figures are
*planner estimates* — a calibration signal, not a billing guarantee — and
may be absent for metadata-only queries (`EstimateUnavailableError` →
the engine degrades to a price-only preview).

**Error taxonomy (#124).** `map_snowflake_exception` mirrors
`map_bq_exception`: a connector `ProgrammingError` for "object does not
exist" (errno 002003) → `TableNotFoundError`; "invalid identifier"
(errno 000904) → `ColumnNotFoundError`; residual `ProgrammingError` →
`QuerySyntaxError`; `ForbiddenError` / auth-flavoured
`DatabaseError`/`OperationalError` → `WarehouseAuthError`; everything else
passes through unchanged. No `BytesBilledExceededError` equivalent —
Snowflake has no bytes-billed cap (cost is governed by warehouse size +
auto-suspend, see below).

**Known limitations on live Snowflake (v0.2) — use `safety: schema-only` +
`prune.scope: full`.** Three deferred/defective paths mean a live Snowflake
run today must stick to schema-only drafting and full-scope prune (the
combination certified green by the gated live e2e). Each is tracked for a
later fix:

- **`safety: aggregate-only` — unsupported.** Profiles columns via
  `adapter.column_stats`, which `SnowflakeAdapter` leaves as a deferred
  `NotImplementedError` (the one v0.2 method not yet implemented).
  `generate` with `safety.mode: aggregate-only` fails (exit 1).
- **`safety: sample` and `prune.sample_strategy` (oneshot / materialised) —
  unsupported.** All sampling emits the deterministic row-hash predicate
  `MOD(ABS(HASH(*)), n) < 1` in `WHERE` / `ORDER BY`, which Snowflake rejects
  (`002079`: `HASH(*)` is valid only in the `SELECT` projection). Needs a
  projection-subquery sample shape (a Snowflake dialect-design gap).
- **`prune.scope: sample` (oneshot) — also blocked** at the engine seam: the
  sample row-count is fetched through a BigQuery-only `_get_client`;
  `SnowflakeAdapter` exposes `_get_num_rows` instead. Needs a vendor-neutral
  `WarehouseAdapter.get_table_metadata` seam.

`schema-only` (redacted column names/types to the LLM) + `prune.scope: full`
runs the full draft → prune → grade → diff pipeline against Snowflake today;
`run_test_sql` (full-scope test execution) and `estimate_query_bytes` are
implemented and certified live.

**Cost guidance — read before running any live Snowflake test.** Snowflake
bills compute by warehouse-second, so an unbounded or forgotten run costs
real money. **Set a resource monitor on your account FIRST** (a hard
credit ceiling with a suspend action), use an **XS warehouse**, and
configure **aggressive auto-suspend** (e.g. 60s) so an idle warehouse
stops billing promptly. The gated live tests (below) sample with `LIMIT`
and target the tiny `TPCH_SF1` / a small engineered table to keep scans
minimal, but the resource monitor is the load-bearing guardrail.

**Offline test harness.** The adapter's emitted SQL is validated offline
two ways without a live warehouse: (1) `fakesnow` (in-memory DuckDB
Snowflake emulator) **executes** the non-`HASH` SQL (`run_test_sql`
COUNT-wrapper, `INFORMATION_SCHEMA.TABLES.ROW_COUNT` sizing) with
rule-semantic assertions — never `HASH()` value-equality; (2) the
hash-mod sample-mode SQL (which fakesnow's DuckDB `HASH` cannot execute)
is asserted to **parse** under `sqlglot`'s Snowflake dialect — the syntax
gate that caught the `"sample"` reserved-word bug. A hand-rolled
`FakeSnowflakeConnection` (`expect_execute` / `assert_all_expectations_met`)
covers session/cleanup/error-mapping behaviour. Real `HASH` execution +
case-folding are certified only by the gated live tests.

## Integration tests (maintainer-only)

The default `pytest` invocation skips warehouse-touching tests via
`addopts = -m 'not bigquery and ... and not snowflake'`. To run them locally:

```bash
# BigQuery (free under the 1 TB/month tier; bills GOOGLE_CLOUD_PROJECT)
SF_RUN_BQ=1 uv run pytest -m bigquery --no-cov

# Snowflake — set a resource monitor + XS warehouse + auto-suspend FIRST (bills compute)
export SF_RUN_SNOWFLAKE=1
export SNOWFLAKE_ACCOUNT=<org-account> SNOWFLAKE_USER=<user> SNOWFLAKE_PASSWORD=<pw>
export SNOWFLAKE_WAREHOUSE=<xs-warehouse>
# warehouse+prune live e2e additionally needs a WRITABLE target:
export SNOWFLAKE_DATABASE=<your-db> SNOWFLAKE_SCHEMA=<your-schema>
# full generate-pipeline e2e additionally needs an LLM key:
export ANTHROPIC_API_KEY=sk-...
uv run pytest -m snowflake --no-cov
```

The `SF_RUN_BQ=1` / `SF_RUN_SNOWFLAKE=1` env-var gates live on top of the
markers so even an explicit `-m bigquery` / `-m snowflake` will not fire
warehouse requests in CI by accident. Each test surfaces a distinct
skip-with-reason when a prerequisite is missing. The `snowflake` marker
covers both the offline `fakesnow`/`sqlglot` suites (which run with no env
vars) and the gated live tests (which self-skip without credentials):
`tests/prune/test_compiler_fakesnow.py`,
`tests/warehouse/test_snowflake_adapter_fakesnow.py` (offline);
`tests/warehouse/test_snowflake_prune_live.py` (live, materialised, writable
schema), `tests/warehouse/test_snowflake_estimate_live.py` (live EXPLAIN),
`tests/cli/test_e2e_snowflake_smoke.py` (live full pipeline vs `TPCH_SF1`,
`oneshot`).

Fixtures use `bigquery-public-data.samples.shakespeare` (BigQuery, free
under the 1 TB/month tier) and `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1` plus a
hand-crafted seed at `tests/fixtures/snowflake/` (Snowflake). There is no
CI job for these tests; they run on whatever interpreter `uv run` resolves
(target the matrix ceiling for packaging-sensitive paths).

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
| `UnsupportedProfileTypeError`            | dbt profile's `type` is not `"bigquery"`, `"postgres"`, or `"snowflake"`.                                 | `profile_type`                                       | `bigquery` is fully implemented; `postgres`/`snowflake` dispatch to v0.2 stub/skeleton adapters (warehouse ops raise `NotImplementedError`). Other types are unsupported.  |
| `UnsupportedAuthMethodError`             | dbt profile's `method` is not `"oauth"` (or unset).                                                      | `method`                                             | v0.1 supports `method: oauth` (or unset) only; run `gcloud auth application-default login`.     |
| `ProfileNotFoundError`                   | None of the three search paths yielded a `profiles.yml` (or the project file is missing/malformed).      | `searched_paths`                                     | Create a `profiles.yml` at one of the searched paths, or set `DBT_PROFILES_DIR`.                |
| `ProfileTargetNotFoundError`             | The profile resolved but the requested `target` is missing. Inherits `ProfileNotFoundError`.             | `profile_name`, `target`, `searched_paths`           | Add the target to `profiles.yml`, or pass an explicit `target=` that exists in the profile.     |
| `IncompleteProfileError`                 | A profile parsed but is missing required keys for its `type` (e.g. a `snowflake` target without `account`/`user`/`warehouse`). Collect-all — lists every missing key. | `profile_type`, `missing`                            | Add the listed missing key(s) to the target in `profiles.yml`.                                  |
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
| `MaterialisationFailedError` (v0.2)      | `BigQueryAdapter.materialise_sample` wraps an SDK / network / quota failure during the materialisation query. | `cause`                                              | Inspect `.cause` for the underlying exception; falls back to `prune.sample_strategy: oneshot` to bypass materialisation. |
| `MaterialisationNotSupportedError` (v0.2)| `WarehouseAdapter.materialise_sample` default impl raised because the concrete adapter doesn't override it (any non-BigQuery adapter in v0.2). | _(none)_                                             | Set `prune.sample_strategy: oneshot` in `signalforge.yml` to fall back to per-test sampling, or wait for v0.3 multi-warehouse materialisation support. |
| `EstimateNotSupportedError` (v0.2)       | `WarehouseAdapter.estimate_query_bytes` default impl raised because the concrete adapter doesn't override it (the Postgres stub in v0.2; BigQuery + Snowflake both override). | `adapter_name`                                       | Use `--estimate` with a BigQuery or Snowflake profile, or wait for v0.3 multi-warehouse estimation support. |
| `EstimateUnavailableError` (v0.2, #130)  | The adapter *supports* estimation but couldn't extract a figure for THIS query — e.g. Snowflake's `EXPLAIN USING JSON` ran but the plan carried no parseable `GlobalStats.bytesAssigned` (metadata-only query / plan-shape change / malformed cell). The `--estimate` engine degrades to a price-only preview. | `detail`                                             | The query plan carried no parseable byte estimate; the run falls back to a price-only cost preview. EXPLAIN figures are planner estimates and may be absent for some query shapes — re-run without `--estimate` to skip the preview entirely. |

## v0.2 follow-ups

Known gaps that ship in a later release; tracked here so users can plan
around them and maintainers can keep the doc honest as scope changes.

- **Lazy `column_stats` proxy.** v0.1 flushes one query per
  `column_stats` call (see [§`column_stats` access
  pattern](#column_stats-access-pattern)). v0.2 will land a proxy on
  `ColumnStats` so the queued columns of a table flush as a single
  batched query at first field read.
- **Legacy domain-scoped project IDs.** v0.1 rejects
  `example.com:my-project` style IDs (Google Workspace tenants
  pre-2014). The fix needs both a regex update *and* a `_quote()` change
  to render the colon outside the backtick group. Defer to v0.2 with
  proper round-trip tests against an actual domain-scoped project.
- **Drift detector for `DbtProfileTarget`.** v0.1 validates a
  hand-curated fixture against a hand-curated `StrictModel` — both
  drift together if a maintainer updates one without the other. v0.2
  will regenerate `dbt_bigquery_drift_v1_X.yml` from a pinned
  `dbt-bigquery==1.X.*` release via `uvx`, mirroring the manifest
  fixture regeneration pattern from issue #2's `tests/fixtures/regenerate.sh`.
