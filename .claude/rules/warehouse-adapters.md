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

**Snowflake skeleton (issue #119, v0.2 epic #118).** `adapters/snowflake.py` graduates the stub pattern to the vendor the epic targets: `SnowflakeAdapter` captures the forward-compat conn surface (`account` / `user` / `password` / `role` / `warehouse` / `database` / `schema`), `dialect()` returns `SNOWFLAKE_DIALECT` (`quote_char='"'`, **`identifier_case='upper'`** — Snowflake folds unquoted identifiers to upper-case, the *opposite* of Postgres `'lower'`, which #121's anchor-contract column matching depends on; `supports_qualify=True`), and the three abstract op methods raise `NotImplementedError("…issue #118…")`. `materialise_sample` / `estimate_query_bytes` are **not** overridden — the ABC's typed degrade (`MaterialisationNotSupportedError` / `EstimateNotSupportedError`) is the correct v0.2 behaviour. `from_profile` dispatches `profile.type == "snowflake"` (lazy import; passes only `database=profile.project, schema=profile.dataset` — the profile-model relaxation to carry `account`/`user`/`role`/`warehouse` is deferred to #120). Three #119-specific notes: (1) **`__repr__` shows only `account` + `warehouse`, never credentials** (`user`/`password`/`role`/`database`/`schema`) — the repr-redaction rule below, pinned by a test asserting the secret substrings are absent. (2) The one-shim-per-vendor rule now has a **warehouse-side confinement test** (`tests/warehouse/test_snowflake_client_confinement.py`): every `snowflake-connector-python` type/pyright-ignore must live only in `adapters/_snowflake_client.py`. (3) `_SnowflakeClientProtocol` (connection: `cursor()` / `close()`) is split from `_SnowflakeCursorProtocol` (`execute(...)` / `fetchall()` / `close()`) so the protocol honestly describes the real DB-API shape — query execution lives on the cursor, not the connection (a CodeRabbit catch; getting the seam shape right now keeps #118's `conn.cursor().execute(...)` path type-checking against a faithful protocol). `snowflake-connector-python` ships under the `[snowflake]` optional extra (+ dev group); the SDK import stays lazy inside `make_real_client`.

**`Dialect` now carries prune-compiler SQL-fragment templates (issue #121).** The `Dialect` value object (`warehouse/models.py`) graduated from pure capability flags to also carrying the declarative SQL fragments the prune compiler reads so it can emit warehouse-correct SQL without branching on `dialect.name`: `sample_row_hash_expr`, `timestamp_literal_template`, `date_literal_template`, `quote_qualified_per_component`, and `sample_cte_alias` (all BigQuery-defaulted so existing constants/snapshots are byte-unchanged). `SNOWFLAKE_DIALECT` sets them to Snowflake forms (`ABS(HASH(*))`, `'{value}'::TIMESTAMP/::DATE`, per-component quoting, the quoted `"sample"` CTE alias — `SAMPLE` is reserved in Snowflake). When a future vendor adapter ships its own `Dialect`, populate these fields too — the compiler is the consumer and it never name-branches (see `prune-engine.md` § "Compiler is dialect-driven"). `identifier_case` graduated from declared-but-unused to load-bearing in the same change (the compiler folds identifiers per it before quoting). `POSTGRES_DIALECT` keeps BigQuery defaults for the new fields with a docstring note — the Postgres stub never invokes the compiler, so they're corrected when its ops land.

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
- **`estimate_query_bytes(sql) -> int`** (issue #36, DEC-004). Default raises `EstimateNotSupportedError`. BigQuery override uses `QueryJobConfig(dry_run=True)` and reads `job.total_bytes_processed` (dry_run doesn't bill — no `maximum_bytes_billed` needed).

Both errors → CLI tier 3. Both remediations are locked verbatim and pinned for stability. Fake-parity contract: each gets a separate expectation queue on `FakeBigQueryClient` (`expect_materialise_sample`, `expect_abort_session`, `expect_dry_run`); cannot fall through to `expect_query`. Apply the same isolation for any future `expect_<vendor_specific>` helper.

**Verify each non-BQ adapter's degrade with its adapter-specific NotSupported error (issue #123).** A graceful-degrade ABC method is only proven for an adapter when a test drives *that concrete adapter* (not a fake raising a generic `WarehouseError`) through the consuming orchestrator and asserts on the adapter's specific error class. #123 pinned the Snowflake `--estimate` degrade at engine level (`estimate(...)` with a real `SnowflakeAdapter()` → `warehouse_unavailable_reason.startswith("EstimateNotSupportedError:")`) AND CLI level (`main(["generate","--estimate",...])` → stdout `<unavailable: EstimateNotSupportedError>`, exit 0, no-traceback floor). Keying the assertion on the specific class name (not the `WarehouseError` parent) is load-bearing — it breaks if the engine's `except WarehouseError` is ever narrowed to exclude the subclass. EXPLAIN-based real estimation for Snowflake is deferred to #130 (blocked on live connectivity, #118/#122).

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

When introducing a new fail-soft cleanup boundary in v0.3 (Snowflake session teardown, Postgres temp-table cleanup), match this shape verbatim.

## Reference

`plans/super/3-bigquery-adapter.md` — DEC-001 … DEC-028. `plans/super/22-temp-table-sample.md` — v0.2 materialised-sample additions (`materialise_sample` ABC, BigQuery session-state pattern, cleanup-WARNING shape). `plans/super/36-estimate-cost-preview.md` — `estimate_query_bytes` ABC addition. `plans/super/120-snowflake-profile.md` — DEC-001 … DEC-010 (unified `DbtProfileTarget` + per-type cross-field validator, `IncompleteProfileError`, `validate_snowflake_account`, auth scope, drift detector). `src/signalforge/warehouse/` — current implementation. `tests/warehouse/_fake.py` — `FakeBigQueryClient` + `expect_*` API. `docs/warehouse-adapter-ops.md` — operational reference.
