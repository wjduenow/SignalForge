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
  _path_safety.py        # symlink-hardened path canonicalisation
  _test_result_repr.py   # deterministic compact_repr for sample failures
  adapters/
    bigquery.py          # the v0.1 concrete adapter
    postgres.py          # v0.2 stub (issue #53)
    _client.py           # pyright-noise shim around google.cloud.bigquery
```

The ABC is warehouse-agnostic. v0.2 Snowflake/Postgres slot under `adapters/` without restructuring (DEC-001). `_`-prefixed helpers stay reachable via dotted import but are absent from the package namespace.

**Second-adapter stub (issue #53).** `adapters/postgres.py` implements only `__init__` (captures params) and `dialect()` (returns Postgres-flavoured `Dialect`: `quote_char='"'`, `identifier_case='lower'`, `supports_qualify=False`); every other abstract method raises `NotImplementedError("…issue #53…")`. `WarehouseAdapter.from_profile` dispatches `profile.type == "postgres"` so an operator with a Postgres profile sees `NotImplementedError` rather than `UnsupportedProfileTypeError`. The stub exists to verify Architectural Commitment #3 ("warehouse-agnostic by design") by forcing the ABC + factory seam through a second code path right now.

**Snowflake skeleton (issue #119, v0.2 epic #118).** `adapters/snowflake.py` graduates the stub pattern to the vendor the epic targets: `SnowflakeAdapter` captures the forward-compat conn surface (`account` / `user` / `password` / `role` / `warehouse` / `database` / `schema`), `dialect()` returns `SNOWFLAKE_DIALECT` (`quote_char='"'`, **`identifier_case='upper'`** — Snowflake folds unquoted identifiers to upper-case, the *opposite* of Postgres `'lower'`, which #121's anchor-contract column matching depends on; `supports_qualify=True`), and the three abstract op methods raise `NotImplementedError("…issue #118…")`. at the #119 skeleton stage `materialise_sample` / `estimate_query_bytes` were **not** overridden — the ABC's typed degrade (`MaterialisationNotSupportedError` / `EstimateNotSupportedError`) was the correct skeleton-era behaviour (`materialise_sample` was later implemented in #122; `estimate_query_bytes` graduated to a real `EXPLAIN USING JSON` override in #130 — see the graduation note below). `from_profile` dispatches `profile.type == "snowflake"` (lazy import; passes only `database=profile.project, schema=profile.dataset` — the profile-model relaxation to carry `account`/`user`/`role`/`warehouse` is deferred to #120). Three #119-specific notes: (1) **`__repr__` shows only `account` + `warehouse`, never credentials** (`user`/`password`/`role`/`database`/`schema`) — the repr-redaction rule below, pinned by a test asserting the secret substrings are absent. (2) The one-shim-per-vendor rule now has a **warehouse-side confinement test** (`tests/warehouse/test_snowflake_client_confinement.py`): every `snowflake-connector-python` type/pyright-ignore must live only in `adapters/_snowflake_client.py`. (3) `_SnowflakeClientProtocol` (connection: `cursor()` / `close()`) is split from `_SnowflakeCursorProtocol` (`execute(...)` / `fetchall()` / `close()`) so the protocol honestly describes the real DB-API shape — query execution lives on the cursor, not the connection (a CodeRabbit catch; getting the seam shape right now keeps #118's `conn.cursor().execute(...)` path type-checking against a faithful protocol). `snowflake-connector-python` ships under the `[snowflake]` optional extra (+ dev group); the SDK import stays lazy inside `make_real_client`.

**Snowflake sampling + connection-bound session (issue #122).** `SnowflakeAdapter` implements the sampling surface — `sample_rows`, `materialise_sample`, `run_test_sql` (`column_stats` stays `NotImplementedError`; `estimate_query_bytes` was the ABC degrade pending #123 at the time of #122, then graduated to a real `EXPLAIN USING JSON` override in #130 — see the `estimate_query_bytes` graduation note below). Five load-bearing conventions, most diverging from BigQuery because Snowflake's session model differs:

- **The connection IS the session (no `session_id` string).** BigQuery threads a server-side `session_id` via `connection_properties` on every query (§ "Session/connection state"); Snowflake's *connection* holds the session, so `self._active_session` stores the **connection object** itself and every op runs on the one connection. A `connection=` injection seam on `__init__` (mirrors BigQuery's `client=`) + lazy `_get_connection()` (builds via the shim's `make_real_client` on first use) is the testability seam. `_get_connection()` sets `_active_session` on first open.
- **Reuse the `SNOWFLAKE_DIALECT` SQL-fragment fields — never hard-code `HASH(*)`.** `sample_rows` and the `materialise_sample` CTAS delegate to `signalforge.warehouse._sample_sql.render_sample_select(..., order_by_hash=True)` (shared with the prune compiler's sample CTE — issue #139), which reads `dialect.sample_row_hash_expr`, `dialect.sample_hash_in_projection`, `dialect.sample_hash_alias`; partition filters are rendered by the caller via `dialect.timestamp_literal_template` / `date_literal_template` and passed in as `extra_where`. Reading the dialect (not hard-coding) keeps the adapter's sample SQL byte-consistent with the prune compiler's sample CTE (Architectural Commitment #5). **Since #139 Snowflake emits the projection-subquery shape** — `SELECT * EXCLUDE (_sf_sample_hash) FROM (SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash FROM <src> AS t) WHERE MOD(_sf_sample_hash, b) < 1 [AND <pf>] ORDER BY _sf_sample_hash LIMIT n` — because `HASH(*)` is rejected as a `WHERE`/`ORDER BY` predicate (`002079`); BigQuery keeps the inline form (`sample_hash_in_projection=False`).
- **Table size via `INFORMATION_SCHEMA.TABLES.ROW_COUNT`, case-insensitive.** `_get_num_rows` queries `SELECT ROW_COUNT FROM <db>.INFORMATION_SCHEMA.TABLES WHERE UPPER(TABLE_SCHEMA)=UPPER('…') AND UPPER(TABLE_NAME)=UPPER('…')` (schema/name embedded as escaped string literals via `escape_bq_string_literal` — Snowflake uses backslash escaping too; when `project is None` the query is left **unqualified** — `INFORMATION_SCHEMA.TABLES`, resolved against the session's current database — NOT `CURRENT_DATABASE().INFORMATION_SCHEMA`, which is invalid Snowflake since `CURRENT_DATABASE()` is a scalar function, not a namespace qualifier). `ROW_COUNT` is NULL for views/MVs → routes through the *same* fail-loud sizing decision BigQuery uses (unknown+no-filter → `UnknownTableSizeError`; unknown+filter → `bucket=1000`; `>= _LARGE_TABLE_THRESHOLD` (100M, re-declared not imported) + no-filter → `SamplingRequiresPartitionFilterError`; else `max(num_rows//n, 1)`). `num_rows == 0` follows the unknown pathway (pin a test — easy to split from `None` by accident).
- **`materialise_sample` returns a fully-qualified temp `TableRef`; reuse the shared `run_id` recipe.** `CREATE TEMPORARY TABLE "<src db>"."<src schema>"."_sf_sample_<run_id>" AS SELECT …` colocated with the source; returns `TableRef(project=table.project, dataset=table.dataset, name="_sf_sample_<run_id>")`. `run_id` comes from `signalforge.warehouse._sample_id` (`_compute_run_id` / `_canonical_partition_filter` / `_hash_session_id` were **hoisted there from `adapters/bigquery.py` in #122** so the recipe is byte-identical across vendors — both adapters import it; relocation is a pure move, BigQuery snapshots unchanged). The #116 materialised-sample-substitution gotcha (`prune-engine.md`) is pinned with a **`custom_sql` `{{ this }}`** test at `scope="full"` — NOT `not_null`, which trivially `FROM`s `table_ref` and so can never bypass substitution.
- **Cleanup-boundary fail-soft, reshaped for Snowflake (no manual command).** `__exit__` → `_cleanup_active_session()` closes the connection (reaping its session-scoped temp tables), swallows failure, emits ONE operator-actionable WARNING. Unlike BigQuery's `bq query … CALL BQ.ABORT_SESSION()` remediation, **there is no manual drop command** — a temp table is unreachable outside its owning session, so the honest durable fallback is Snowflake's server-side idle-session reap. The WARNING quotes **no `auto-expire in <N>s` countdown** (the timeout is server-side/account-config; `ttl_seconds` is accepted for ABC parity but ignored; `_session_started_at` is provenance only). Raw `session_id` appears only in the failure WARNING (DEC-014 narrow exception); the success INFO uses `_hash_session_id`. State resets in `finally` for idempotency — but **do NOT null `self._connection`** there: idempotency comes from the `_active_session is None` early-return, and nulling an injected connection would silently route a re-entry into a real lazy-build (mirrors BigQuery, which never nulls its client).

Minimal `map_snowflake_exception` (in the shim, lazy SDK-error import) maps auth → `WarehouseAuthError`, programming → `QuerySyntaxError`, else passthrough; the full taxonomy + fakesnow harness + gated live e2e + ops docs are #124.

**`Dialect` now carries prune-compiler SQL-fragment templates (issue #121, extended #139).** The `Dialect` value object (`warehouse/models.py`) graduated from pure capability flags to also carrying the declarative SQL fragments the prune compiler reads so it can emit warehouse-correct SQL without branching on `dialect.name`: `sample_row_hash_expr`, `timestamp_literal_template`, `date_literal_template`, `quote_qualified_per_component`, `sample_cte_alias`, and (issue #139) `sample_hash_in_projection: bool` + `sample_hash_alias: str` (all BigQuery-defaulted so existing constants/snapshots are byte-unchanged). `SNOWFLAKE_DIALECT` sets them to Snowflake forms (`ABS(HASH(*))`, `'{value}'::TIMESTAMP/::DATE`, per-component quoting, the quoted `"sample"` CTE alias — `SAMPLE` is reserved in Snowflake — and `sample_hash_in_projection=True` + `sample_hash_alias="_sf_sample_hash"` so the shared `_sample_sql.render_sample_select` emits the projection-subquery sample shape rather than an inline `HASH(*)` predicate). When a future vendor adapter ships its own `Dialect`, populate these fields too — the compiler is the consumer and it never name-branches (see `prune-engine.md` § "Compiler is dialect-driven"). `identifier_case` graduated from declared-but-unused to load-bearing in the same change (the compiler folds identifiers per it before quoting). `POSTGRES_DIALECT` keeps BigQuery defaults for the new fields with a docstring note — the Postgres stub never invokes the compiler, so they're corrected when its ops land.

## Unified multi-warehouse `DbtProfileTarget` + per-type cross-field validator (issue #120)

`DbtProfileTarget` is a **single Pydantic model carrying every warehouse's fields** (BigQuery `project`/`location`/`priority`/`maximum_bytes_billed`; Snowflake `account`/`user`/`role`/`warehouse`/`database`/`password`/`private_key_path`/`private_key_passphrase`/`authenticator`; the shared `dataset` via the `schema` alias + `threads`). It keeps `extra="forbid"` and uses a `@model_validator(mode="after")` to get **discriminated-union behaviour without a union type** — so `load_profile`'s return type and every consumer stay unchanged. When a fourth warehouse (Databricks/Redshift) lands, copy this shape rather than splitting into a union. Load-bearing conventions:

- **Per-type field-set constants drive the foreign-field check.** `_BIGQUERY_ONLY` / `_SNOWFLAKE_ONLY` frozensets are the one declarative source for "which field belongs to which warehouse." The after-validator rejects, in BOTH directions, any field belonging to the *other* type's set when set non-`None` (e.g. `account` on a `bigquery` target; `location` on a `snowflake` target). Add a new warehouse's fields to a new `_<X>_ONLY` set in lockstep.
- **Two distinct failure modes, two errors.** Missing required keys → `IncompleteProfileError(profile_type, missing)` (collect-all, lists every missing key at once). Foreign fields → a plain `ValueError` (surfaces as Pydantic `ValidationError`). Reserve `IncompleteProfileError` for "missing," never "foreign." Snowflake requires `account`/`user`/`warehouse` (NOT `database`/`schema`/`role` — dbt allows those at model level / via the user's default role).
- **The `mode="after"` validator inspects-and-raises only — never `model_copy`/mutates** (the model is frozen; mirrors `safety-layer.md`'s `with_mode` rule). **Pydantic v2 wraps only `ValueError` / `TypeError` / `AssertionError` raised in a validator into `ValidationError`; any other exception type propagates raw** — and this is true for BOTH `field_validator` and `model_validator`, the validator *kind* is irrelevant. So a `WarehouseError` subclass (e.g. `IncompleteProfileError`, `UnsupportedAuthMethodError`, `InvalidIdentifierError`) raised from either validator propagates raw, the CLI exit-code mapping keys on the typed error directly, and tests pin the exact type (NOT a `(TypedError, ValidationError)` tuple, whose second arm is dead). The foreign-field check deliberately raises a plain `ValueError`, so THAT path surfaces as `ValidationError` — match the assertion to whichever exception the code actually raises.
- **Identifier hygiene at the validator, dialect-agnostic.** Every field that becomes SQL downstream (#122 interpolates `warehouse`/`database`/`schema`/**`role`** — `USE ROLE <role>`) runs through the strict `_sql_safety.validate_identifier`. The Snowflake `account` is NOT SQL (the connector consumes it) so it routes through the permissive `validate_snowflake_account` (`^[A-Za-z0-9][A-Za-z0-9._-]{1,253}$` — accepts `xy12345.us-east-1` / `myorg-account1`, rejects quotes/`;`/whitespace/newlines/backticks/control-chars; hyphens incl. `--` are legal in locators and the value never reaches SQL, so `--` is intentionally accepted). **Known deferral:** the strict identifier regex rejects Snowflake's legal `$` in identifiers; documented in `profiles.py` (mirrors the domain-scoped-project-ID deferral) — broaden only on a real need.
- **Auth scope = password + key-pair + SSO.** The `authenticator` field accepts `None` / `"snowflake"` / `"externalbrowser"`; deferred values (`oauth`, `username_password_mfa`, anything else) raise `UnsupportedAuthMethodError(method=value, remediation=errors._SNOWFLAKE_DEFERRED_AUTH_REMEDIATION)` — reuse the shared remediation constant rather than growing a near-duplicate error class. (Cosmetic wart: the message header reads "Unsupported auth **method**" though the key is `authenticator`; the remediation disambiguates.)
- **`threads` is a shared field** — added in #120 because production fixtures never carried it, so a real Snowflake profile (which always sets `threads:`) would otherwise trip `extra="forbid"`; also closes a latent BigQuery gap.
- **Drift detector is mandatory for the new read-back fields.** Even though the *class* isn't new, the Snowflake field set is — `tests/warehouse/test_profiles.py` ships a `StrictSnowflakeModel(extra="forbid")` mirror + `tests/fixtures/profiles/dbt_snowflake_drift_v1_x.yml`. The strict mirror uses `dataset = Field(alias="schema")`, NOT a `schema:` field, to avoid the Pydantic `BaseModel.schema` shadow (`safety-layer.md` issue #93).
- **`from_profile` wires every parsed field, import stays lazy.** The `snowflake` branch passes `account`/`user`/`password`/`role`/`warehouse`/`database`/`schema=profile.dataset` + the key-pair/SSO fields into `SnowflakeAdapter`; the `SnowflakeAdapter` import stays inside the branch so the no-eager-BigQuery-SDK-import contract holds.

## ABC + lazy-import factory (DEC-019)

```python
class WarehouseAdapter(abc.ABC):
    ...
    @classmethod
    def from_profile(cls, profile: DbtProfileTarget) -> WarehouseAdapter:
        if profile.type == "bigquery":
            from signalforge.warehouse.adapters.bigquery import BigQueryAdapter  # lazy
            return BigQueryAdapter(...)
        raise UnsupportedProfileTypeError(profile_type=profile.type)
```

Concrete-adapter import lives **inside** the factory so callers who never invoke `from_profile` don't pay the import cost or pull in `google-cloud-bigquery`. Direct concrete instantiation stays supported for tests and explicit-config use.

## `_client.py` contains every `# pyright: ignore`

`google-cloud-bigquery` has gaps in its type stubs. Confine **every** `# pyright: ignore[...]` and `# type: ignore[...]` for the SDK to `adapters/_client.py`. The shim exposes `_BQClientProtocol` duck-typed at the surface the adapter consumes (`project`, `query`, `get_table`, `list_rows`). Both `bigquery.Client` and `FakeBigQueryClient` satisfy the protocol.

Each new vendor adapter gets its own `_<vendor>_client.py` shim under `adapters/` for the same reason. Don't pool SDK ignores into a generic util module.

## Test fakes use an `expect_*` helper API (DEC-002 / DEC-028)

Hand-rolled fakes only — `pytest-bigquery-mock` is unmaintained and `MagicMock`-style fakes auto-pass everything (violates `testing-signal.md`).

```python
fake = FakeBigQueryClient(project="p")
fake.expect_query(matching=r"^SELECT COUNT", returns=[{"failures": 0}])
fake.expect_get_table(ref=ref, returns=FakeTable(num_rows=1_000_000, schema=[]))
adapter = BigQueryAdapter(client=fake, project="p")
adapter.run_test_sql(...)
fake.assert_all_expectations_met()
```

Each call consumes one matching expectation; calls outside the canned set raise `AssertionError("unexpected query: ...")`. The fake lives under `tests/warehouse/_fake.py` — never import from production code.

## Deterministic sampling, fail-loud sizing (DEC-006 / DEC-024)

`sample_rows(table, n, *, partition_filter=None)` uses deterministic hash-mod, **not** `TABLESAMPLE`:

```sql
SELECT * FROM `<quoted>` WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1 LIMIT n
```

Bucket size derived from `Table.num_rows` so the expected sample is ~3–5× `n` before LIMIT. Architectural Commitment #5 (explainable diffs) requires same input → same prune decision; `TABLESAMPLE SYSTEM` is non-deterministic and doesn't work on views/MVs/wildcard tables.

Fail loud rather than silently scan TBs:

- `num_rows` missing on the `Table` resource → raise `UnknownTableSizeError`.
- `num_rows >= 100M` and no `partition_filter` → raise `SamplingRequiresPartitionFilterError`.

`partition_filter` is a typed `PartitionFilter` ADT (DEC-014), not a raw string. Raw-string filter input is a SQL-injection seam.

## Identifier validation at construction time (DEC-013)

Every public-API string that becomes part of a SQL string runs through `_sql_safety.validate_identifier` at construction time — `TableRef.{dataset, name}`, `PartitionFilter.column`, the `column` parameter on `column_stats(...)`. Regex is strict: `^[A-Za-z_][A-Za-z0-9_]*$`.

GCP **project IDs** use a different hyphen-permissive grammar — route those through `validate_project_id`, not the strict identifier regex.

`run_test_sql(sql)` does NOT parse SQL. Runs cheap rejects (`;`, `--`, unbalanced parens) inside `_sql_safety` and documents the contract: callers must supply a single SELECT returning rows. Full SQL parsing is overkill — the LLM drafter is the practical caller and we control the prompt.

## `QueryJobConfig` originates in `_default_job_config` (DEC-015)

Every query the adapter issues builds config through one private helper:

```python
def _default_job_config(self, *, stage: str) -> QueryJobConfig:
    # use_query_cache=False           — Architectural Commitment #5
    # maximum_bytes_billed=<limit>    — DEC-005: 100 MB default cap, per-call override-down only
    # labels={signalforge_stage, signalforge_version}  — v0.2 INFORMATION_SCHEMA cost attribution
```

`use_query_cache` is **not** user-overridable in v0.1. Per-call `cost_limit_bytes` overrides the limit *downward only*; a profile-level `maximum_bytes_billed` caps both.

## Error hierarchy: typed + remediation, mirroring manifest layer

Every distinct failure mode is its own subclass of `WarehouseError`. Each carries a class-level `default_remediation`; the base `__str__` renders both message and `↳ Remediation:` line. The prune/CLI layers pattern-match on type rather than sniffing message text.

User-supplied strings render via `repr()` (`_format_value` helper) in every error message — a crafted dataset name like `foo'\nINFO: spoofed log line` cannot pollute log viewers. Same DEC applies to `__repr__` on the adapter: shows only `project` and `location`, never the client / credentials / tokens.

## Path safety: layer-neutral common module + per-layer wrappers (issue #43)

`signalforge._common.path_safety.canonicalise_path` is the single canonical home for the project's symlink / containment defence. Raises a project-neutral `PathContainmentError(Exception)` — no layer prefix.

Two consumer patterns:

1. **Cross-package consumers (cli / diff / grade / prune)** import directly from `signalforge._common.path_safety`. At the orchestrator boundary they catch `PathContainmentError` and re-raise as their own layer-typed error so each layer's downstream catch surface stays homogeneous.
2. **Layer wrappers (`warehouse/_path_safety.py`, `safety/_path_safety.py`)** are thin shims that delegate to the common helper and translate `PathContainmentError` → layer-typed error at the helper level. Exists because every internal call site within those layers wants the same translation.

The manifest loader still ships its own `_canonicalise_path` inline — predates the common module; promotion is future-clean-up.

When introducing a new stage that needs path canonicalisation, prefer pattern (1). Don't create a new layer-local `_path_safety.py` that duplicates the helper's body — that's the historical mistake issue #43 reverses.

## No logging in stage-0 modules; one-line warnings only at adapter boundary

Reader/parser modules emit no routine logs. Only exceptions are soft-threshold WARNINGs that signal the user is on a path likely to be slow or expensive — `profiles.py` emits one when `profiles.yml` exceeds the soft size cap (DEC-023); `_sql_safety.py` and `_path_safety.py` emit nothing.

The adapter emits sparing `WARNING`-level signal when behaviour deviates from the deterministic happy path:

- `column_stats` queued batch exceeds the warning threshold (DEC-023).
- `sample_rows` num_rows >= threshold without a partition filter (before raising loud).

`INFO` and `DEBUG` are reserved for prune/grade stages where signal-vs-volume tradeoffs surface. Don't add adapter-level INFO logging in v0.1.

## Don't pass our `TableRef` straight into vendor SDK methods (issue #21 lesson)

`bigquery.Client.get_table()` accepts `str | bigquery.TableReference | Table | TableListItem` and explodes on `AttributeError: 'TableRef' object has no attribute 'path'` when handed our Pydantic value object. Always pass the **stringified form** (`ref.qualified_name`) to vendor SDK call sites. Two-part `dataset.table` (when `project=None`) is a valid `qualified_name` shape; the BQ SDK resolves the project from the client's billing project.

Two takeaways for future adapter work:

1. Every public adapter method that calls `client.<method>(ref, ...)` needs a live integration test that runs against the real SDK, not just the fake. The fake is for behaviour assertions; only the real SDK enforces input-type contracts. Gate the live test on `SF_RUN_BQ=1` (or per-vendor equivalent).
2. When the fake's coercion helper has a special case for accepting strings, that's a load-bearing signal that the production path passes strings — don't accept non-string forms in the fake without a paired test that proves the real SDK accepts them too.

## Session/connection state on the adapter (DEC-002 of #22 generalised)

When a vendor protocol requires per-call state (BigQuery `session_id`, Snowflake transaction id, Postgres prepared-statement cache), store it on the adapter instance as `self._active_<x>_id: <type> | None` rather than threading through ABC method signatures. The ABC stays vendor-neutral; concrete adapters wire state internally; subsequent per-call methods on the same instance read the state and attach the right vendor primitive.

`__exit__` is the durable cleanup seam — the adapter is invoked as a context manager (`with adapter:`) and teardown lives there, NOT in the orchestrator. Cleanup-on-success and cleanup-on-failure share one path; hard process death falls back to the vendor's server-side timeout. State resets in a `finally` clause so a second `__exit__` is a no-op.

This generalises the `column_stats` batching-state precedent (DEC-008 / DEC-025 of #3) and the BigQuery `_active_session_id` pattern (DEC-002 of #22). Match this shape for any v0.3 adapter with per-call state.

## ABC graceful-degrade methods for warehouse-specific features

When introducing a v0.x ABC method whose support is genuinely warehouse-specific, the default impl raises a typed `<Name>NotSupportedError(WarehouseError)` with a remediation that names the v0.x→v0.(x+1) graduation path. The orchestrator that consumes the method catches the typed error and degrades the affected output section; never propagates. Non-BQ adapters opt out gracefully without forcing a v0.3 multi-warehouse blocker.

Two methods follow this pattern in v0.2:

- **`materialise_sample(table, n, *, partition_filter=None, ttl_seconds=3600) -> TableRef`** (issue #22, DEC-004). Default raises `MaterialisationNotSupportedError`. BigQuery override runs `CREATE TEMP TABLE _sf_sample_<run_id> AS SELECT ...` with `create_session=True`; subsequent `run_test_sql` calls attach `ConnectionProperty(key="session_id", value=self._active_session_id)`. Returned `TableRef` carries `project=None, dataset="_SESSION", name="_sf_sample_<run_id>"` — `project=None` is load-bearing because BigQuery rejects the three-part form even inside the owning session. `run_id = blake2b(table.qualified_name + signalforge_version + str(n) + canonical_json(partition_filter), digest_size=8).hexdigest()` (16 hex, NUL-separator) — OUR derivation, NOT BQ's `session_id`. `ttl_seconds` is OUR-side cleanup-WARNING hint, NOT a BQ knob (BQ enforces ~24h server-side max regardless).
- **`estimate_query_bytes(sql) -> int`** (issue #36, DEC-004). Default raises `EstimateNotSupportedError`. BigQuery override uses `QueryJobConfig(dry_run=True)` and reads `job.total_bytes_processed` (dry_run doesn't bill — no `maximum_bytes_billed` needed). Snowflake override (issue #130) runs `EXPLAIN USING JSON` and parses `GlobalStats.bytesAssigned`, raising the new `EstimateUnavailableError` when the plan carries no parseable figure — see the `estimate_query_bytes` graduation note below.

Both errors → CLI tier 3. Both remediations are locked verbatim and pinned for stability. Fake-parity contract: each gets a separate expectation queue on `FakeBigQueryClient` (`expect_materialise_sample`, `expect_abort_session`, `expect_dry_run`); cannot fall through to `expect_query`. Apply the same isolation for any future `expect_<vendor_specific>` helper.

**Verify each non-BQ adapter's degrade with its adapter-specific NotSupported error (issue #123).** A graceful-degrade ABC method is only proven for an adapter when a test drives *that concrete adapter* (not a fake raising a generic `WarehouseError`) through the consuming orchestrator and asserts on the adapter's specific error class. Keying the assertion on the specific class name (not the `WarehouseError` parent) is load-bearing — it breaks if the engine's `except WarehouseError` is ever narrowed to exclude the subclass.

**`estimate_query_bytes` graduated for Snowflake to a real EXPLAIN estimate (issue #130).** #123 was Phase 1: it pinned the Snowflake `--estimate` *degrade* with a real `SnowflakeAdapter()` raising the ABC default — engine level (`warehouse_unavailable_reason.startswith("EstimateNotSupportedError:")`) AND CLI level (`<unavailable: EstimateNotSupportedError>`, exit 0, no-traceback floor). **#130 replaced that degrade with a real implementation:** `SnowflakeAdapter.estimate_query_bytes` now OVERRIDES the ABC default — it runs `EXPLAIN USING JSON <validated-sql>` and parses `GlobalStats.bytesAssigned` (pure fn `_parse_explain_json_bytes`); it no longer inherits `EstimateNotSupportedError`. The #123 degrade tests asserting `EstimateNotSupportedError` were *rewritten*, not deleted (a fake-injected happy path returning real bytes + a no-stat path asserting `EstimateUnavailableError`) — flipping a degrade means rewriting the prior phase's degrade tests, not leaving them stale.

When EXPLAIN succeeds but the plan carries no parseable `GlobalStats.bytesAssigned` (metadata-only query / plan-shape change across Snowflake releases / malformed cell), the adapter raises the new typed `EstimateUnavailableError(WarehouseError)` (CLI tier 3) rather than fabricating a `0` — distinct from `EstimateNotSupportedError` ("does no estimation at all"): this one means "supports estimation, couldn't extract the figure for THIS query." The `--estimate` engine catches it as a supplementary failure (#36 DEC-005) and renders `<unavailable: EstimateUnavailableError>`. **Planner-estimate accuracy caveat:** `EXPLAIN` figures are planner *estimates* — they may differ from actual scanned bytes and vary across Snowflake releases (mirrors the `HASH()` reproducibility caveat from #121); the `--estimate` preview is a calibration signal, not a billing guarantee. The Postgres stub still inherits the ABC degrade (its `EXPLAIN` override is future work). EXPLAIN-based real estimation was certified by a `@pytest.mark.snowflake`-gated live test (fixtures under `tests/fixtures/warehouse/snowflake/`).

**Generalised graduation recipe (reuse for the next adapter — e.g. Postgres `EXPLAIN`).** A graceful-degrade ABC method (`materialise_sample`, `estimate_query_bytes`, …) graduates *per-adapter* by overriding it with the warehouse's own native primitive; the ABC default raise stays the correct behaviour for every adapter that hasn't grown the primitive yet. Three load-bearing moves, every time: (1) **parse in a pure module-level function** (e.g. `_parse_explain_json_bytes`) so the warehouse-specific extraction is unit-testable without a connection, and raise a typed "supported-but-unavailable-for-THIS-query" error (never fabricate a `0`) distinct from the "not-supported-at-all" error; (2) **pin shape with a hand-crafted fixture + a maintainer-gated live test** — workers can't reach a live warehouse, so the fixture certifies the parse and the gated test certifies validity (snapshot/fixture equality alone certifies shape, not that the warehouse accepts the SQL — keep a parser/executor in the loop, mirroring the #121 `sqlglot` lesson); (3) **flipping a prior phase's degrade means rewriting that phase's degrade tests, not deleting them** — the Phase-1 tests that asserted `<unavailable: NotSupported>` become a fake-injected happy path plus a fixture-driven new-degrade path. Apply the 5-surface graduation rule (`prune-engine.md` § "5-surface parity") when promoting the reserved-degrade surface to active.

## Cleanup-boundary fail-soft pattern (DEC-013 / DEC-014 of #22 generalised)

Cleanup-boundary errors are fail-SOFT, in deliberate contrast to primary-work fail-CLOSED (`safety-layer.md` DEC-011 — propagation IS the defence; an unaudited LLM call must abort the run). A cleanup boundary fires AFTER the user's work has succeeded; blocking on cleanup failure punishes the user for housekeeping they cannot fix in the moment.

Three layers: (1) explicit close on the happy path, (2) swallow-and-warn on cleanup failure with an operator-actionable WARNING, (3) durable server-side fallback (vendor timeout, retry queue, etc.) for hard process death.

**The WARNING is the load-bearing surface.** It must give the operator three things: (a) the **identifier** required to act (raw `session_id`, transaction id, file handle — NOT a hash on this path), (b) the **exact copy-pasteable command** for manual cleanup (verbatim, not paraphrased), (c) the **durable fallback** ("auto-expire in Ns" / "OS reaps on process exit"). Without all three the WARNING is just noise.

**`--quiet` does NOT suppress cleanup-failure WARNINGs.** The CLI default raises the floor to WARNING; this WARNING still surfaces. The operator-actionable contract is the contract.

### BigQuery session teardown (DEC-013 / DEC-014 of #22 — concrete instance)

**Layer 1 — explicit close (happy path).** `__exit__` checks `self._active_session_id`; if non-`None`, issues `client.query("CALL BQ.ABORT_SESSION();", job_config=QueryJobConfig(connection_properties=[ConnectionProperty(key="session_id", value=self._active_session_id)]))`. On success, one `INFO` log with `{"session_id_hash": blake2b-4(session_id), "ttl_remaining_seconds": ...}` (lazy-format JSON; raw `session_id` redacted per DEC-003 of #22).

**Layer 2 — swallow-and-warn (cleanup failure).** Adapter swallows the exception and emits the DEC-014 multi-line WARNING (verbatim, do not paraphrase):

```text
BigQuery session cleanup failed; session will auto-expire in <N>s (BigQuery TTL).
  Session ID: <raw session_id>
  Reason: <exception class name>
  To clean up immediately:
    bq query --connection_property=session_id=<raw> --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"
```

`<N>` is `max(1, int(ttl_seconds - elapsed_in_session))`. State still resets in `finally` so a second `__exit__` is a no-op.

**Layer 3 — BigQuery server-side session timeout.** Hard process death cannot fire `__exit__`. BigQuery's BQ-managed ~24h max lifetime reaps the orphan.

**Raw `session_id` surfaces ONLY in the cleanup-failure WARNING (DEC-003 narrow exception).** Logs emit `session_id_hash` for every normal event. Three reasons the raw id is allowed in this WARNING: (a) it's the only piece the operator needs to act — a hash defeats the purpose; (b) audience is the same principal who owns the session (BigQuery rejects `BQ.ABORT_SESSION()` from any other identity); (c) the surface is bounded (one WARNING per failed cleanup, never on the happy path, never in audit JSONL).

When introducing a new fail-soft cleanup boundary (Postgres temp-table cleanup, etc.), match this shape verbatim. **Snowflake session teardown landed in #122** and follows the same three-layer shape with one deliberate divergence: there is NO manual cleanup command (a session-scoped temp table is unreachable outside its owning session), so the WARNING quotes no `bq`-style remediation and no `auto-expire in <N>s` countdown — see the "Snowflake sampling + connection-bound session (issue #122)" section above.

## Snowflake test harness + full error taxonomy (issue #124)

#124 is the epic's closing test+docs ticket. Three durable conventions:

- **Full `map_snowflake_exception` taxonomy, reusing existing typed errors.** The mapper now mirrors `map_bq_exception`: connector `ProgrammingError` "object does not exist" (errno 002003) → `TableNotFoundError`; "invalid identifier" (errno 000904) → `ColumnNotFoundError`; residual `ProgrammingError` → `QuerySyntaxError`; `ForbiddenError` / auth-marker `DatabaseError`/`OperationalError` → `WarehouseAuthError`; everything else passthrough. The Table/Column split runs BEFORE the `QuerySyntaxError` fallthrough. **No new `WarehouseError` subclass** — reusing the existing ones means no exit-code-table entry and no 7th-AST-scan churn (`cli-layer.md`). `BytesBilledExceededError` is deliberately omitted: Snowflake has no bytes-billed cap. The `snowflake.connector.errors` import stays lazy in the function body (one-shim rule; `test_snowflake_client_confinement.py` gates it). A private `_extract_invalid_identifier` regex helper mirrors `_client._extract_unrecognized_column`. Tested offline by constructing genuine `sfe.*` instances (the connector is a dev-dep). **Gotcha:** `test_snowflake_client.py` deletes `snowflake.connector` from `sys.modules` without restoring it — a module-level `from snowflake.connector import errors` in a sibling test goes stale under full-suite ordering (class identity differs from the mapper's lazy re-import → `isinstance` fails). Import `errors` lazily inside each test (a `_sfe()` helper).

- **Read-only shared data forces a sample-strategy split.** `materialise_sample` colocates its `CREATE TEMPORARY TABLE` in the **source** db/schema, so a CTAS against read-only shared data (`SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`) fails. The two live e2e tests diverge by design: the warehouse+prune-only e2e (`test_snowflake_prune_live.py`) creates a tiny engineered table in the maintainer's **writable** schema and exercises `sample_strategy="materialised"` + the always-passes drop; the full-pipeline e2e (`test_e2e_snowflake_smoke.py`) runs `generate` against read-only `TPCH_SF1` with `prune.sample_strategy: oneshot` (no CTAS). Any future test that materialises against shared/read-only data must make the same choice.

- **Adapter-SQL validation: fakesnow-execute + sqlglot-parse, split by capability.** A new dialect's *adapter* SQL (not just the compiler's) needs a parser/executor in the loop, not snapshot equality (the #121 lesson, applied to the adapter). `test_snowflake_adapter_fakesnow.py` drives a real `SnowflakeAdapter(connection=<fakesnow>)`: fakesnow's DuckDB backend **executes** the non-`HASH` SQL (the `run_test_sql` COUNT-wrap, `INFORMATION_SCHEMA.TABLES.ROW_COUNT` sizing) with rule-semantic assertions (never `HASH()` value-equality), but **cannot** execute the variadic `HASH(*)` sample-mode SQL, a qualified `CREATE TEMPORARY TABLE`, or `OBJECT_CONSTRUCT(*)` — those degrade to a `sqlglot.parse_one(sql, dialect="snowflake")` syntax assertion (capture the emitted SQL via the hand-fake). Real `HASH` execution + case-folding are certified only by the gated live tests. The hand-rolled `FakeSnowflakeConnection` (`expect_execute` / `assert_all_expectations_met` / `close_raises`) stays the behaviour-assertion fake; `expect_execute(returns=<Exception>)` already drives error-mapping and cleanup-fail-soft tests, so no fake extension was needed.

The gated `snowflake` marker now spans offline (`fakesnow`/`sqlglot`, run with no env vars) AND live (`SF_RUN_SNOWFLAKE=1` + conn vars, +`SNOWFLAKE_DATABASE`/`SCHEMA` writable for the engineered prune-live table, +`ANTHROPIC_API_KEY` for the full pipeline) tests under one `uv run pytest -m snowflake --no-cov`. The `docs/warehouse-adapter-ops.md` § "Snowflake adapter (v0.2)" carries the consolidated operator story incl. the **cost guidance** (resource monitor FIRST, XS warehouse, aggressive auto-suspend) that any live Snowflake test must surface in its module docstring.

**Live-harness findings — fakesnow/fakes hid multiple live-only adapter bugs (the #124 payoff).** Running the gated suite against a real warehouse surfaced bugs that the offline `fakesnow` + hand-fakes could not, because fakesnow's DuckDB cursor/result shapes diverge from the real connector's. The lesson generalises the #121 "parser/executor in the loop" rule: a vendor adapter is NOT certified until a live run exercises every path; fakes prove behaviour-against-a-contract, not behaviour-against-the-vendor. Concretely #124's live run found, in order: (1) **`_quote` case-folding** (fixed here — the adapter must fold-then-quote identically to the compiler, else CREATE-vs-REFERENCE diverge); (2) **`ARRAY_AGG(OBJECT_CONSTRUCT(*))` capture-failures samples come back as a JSON *string*** (a VARIANT), not a Python list — `run_test_sql` must `json.loads` it before iterating (fakesnow returned a list, so offline passed); (3) **`HASH(*)` is invalid in WHERE/ORDER BY** on Snowflake → all sample-mode SQL was rejected (bead `bd_1-scaffolding-cdp`) — **FIXED by #139**: the shared `_sample_sql.render_sample_select` now emits the projection-subquery shape (`SELECT * EXCLUDE (_sf_sample_hash) FROM (SELECT t.*, ABS(HASH(*)) AS _sf_sample_hash …)`) driven by `Dialect.sample_hash_in_projection`, so `materialise_sample` / `sample_rows` / the compiler sample CTE all compute the hash in the SELECT projection rather than the rejected predicate position; (4) **oneshot sample-bucket row-count routes through a BigQuery-only `_get_client`** the engine never made vendor-neutral (bead `bd_1-scaffolding-tft`, still open). Net operator contract: **live Snowflake works today with `safety: schema-only` + `prune.scope: full` OR `prune.scope: sample` + `prune.sample_strategy: materialised`** (the #139 projection-subquery fix) — `prune.sample_strategy: oneshot` (bead `bd_1-scaffolding-tft`) + `aggregate-only` (`column_stats`) remain deferred/beaded. When the next vendor adapter ships, budget a live-debugging pass; expect VARIANT/array columns as JSON strings, identifier case-folding mismatches, AND vendor clause-position constraints (a SQL fragment legal in one clause may be rejected in another — the #139 `HASH(*)` lesson) as the first things to break.

## Reference

`plans/super/124-snowflake-test-docs.md` — DEC-001 … DEC-011 (test harness + full taxonomy + two-strategy live e2e + ops docs). `plans/super/3-bigquery-adapter.md` — DEC-001 … DEC-028. `plans/super/22-temp-table-sample.md` — v0.2 materialised-sample additions (`materialise_sample` ABC, BigQuery session-state pattern, cleanup-WARNING shape). `plans/super/36-estimate-cost-preview.md` — `estimate_query_bytes` ABC addition. `plans/super/120-snowflake-profile.md` — DEC-001 … DEC-010 (unified `DbtProfileTarget` + per-type cross-field validator, `IncompleteProfileError`, `validate_snowflake_account`, auth scope, drift detector). `plans/super/122-snowflake-sampling.md` — DEC-001 … DEC-010 (Snowflake `sample_rows` / `materialise_sample` / `run_test_sql`, connection-bound session state, shared `_sample_id` hoist, INFORMATION_SCHEMA sizing, Snowflake-shaped cleanup WARNING). `plans/super/130-snowflake-estimate-explain.md` — DEC-001 … DEC-009 (Snowflake `estimate_query_bytes` via `EXPLAIN USING JSON`, `EstimateUnavailableError` typed degrade, planner-estimate accuracy caveat). `src/signalforge/warehouse/_sample_id.py` — shared deterministic-sample-id seam. `src/signalforge/warehouse/` — current implementation. `tests/warehouse/_fake.py` — `FakeBigQueryClient` + `expect_*` API. `docs/warehouse-adapter-ops.md` — operational reference.
