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
    bigquery.py          # the v0.1 concrete adapter
    postgres.py          # v0.2 stub (issue #53) — validates the warehouse-agnostic seam
    _client.py           # pyright-noise shim around google.cloud.bigquery
```

The ABC is warehouse-agnostic. v0.2 Snowflake/Postgres slot under `adapters/` without restructuring (DEC-001). `_`-prefixed helpers stay reachable via dotted import but are absent from the package namespace.

**Second-adapter stub lives at `adapters/postgres.py`; lights up the v0.2 work (issue #53).** The stub implements only `__init__` (capturing connection params) and `dialect()` (returning a Postgres-flavoured `Dialect`: `quote_char='"'`, `identifier_case='lower'`, `supports_qualify=False`); every other abstract method raises `NotImplementedError("…issue #53…")`. `WarehouseAdapter.from_profile` dispatches `profile.type == "postgres"` to it so an operator with a Postgres profile sees a `NotImplementedError` rather than the v0.1 `UnsupportedProfileTypeError`. The stub exists to verify Architectural Commitment #3 ("warehouse-agnostic by design") by forcing the ABC + factory seam through a second code path right now — pre-#53 the only concrete was `bigquery.py` and the abstraction was unverified. When the v0.2 Postgres implementation lands it should replace every `NotImplementedError` with the real call (and likely extend `DbtProfileTarget` to accept Postgres-specific fields — the current `extra="forbid"` model is BigQuery-shaped).

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

## Path safety: layer-neutral common module + per-layer wrappers (issue #43 graduation)

`signalforge._common.path_safety.canonicalise_path` is the single canonical home for the project's symlink / containment defence. It raises a project-neutral typed escape (`PathContainmentError(Exception)`) — no layer prefix, no inheritance from any stage's error hierarchy. The three traps from `manifest-readers.md` (resolve symlinks before containment check; catch `RuntimeError` on cycles; gate the *default* path through the same helper) live here.

Two distinct consumer patterns:

1. **Cross-package consumers (cli / diff / grade / prune)** import `canonicalise_path` and `PathContainmentError` directly from `signalforge._common.path_safety`. At the orchestrator boundary they catch `PathContainmentError` and re-raise as their own layer-typed error (`CliPathError`, `DiffSidecarWriteError`, `GradeAuditWriteError`, `PruneAuditWriteError`) so each layer's downstream catch surface stays homogeneous — every "we couldn't durably persist X" condition in one layer raises one typed error.
2. **Layer wrappers (`warehouse/_path_safety.py`, `safety/_path_safety.py`)** are thin shims that delegate to the common helper and translate `PathContainmentError` → the layer's typed error (`ProfileNotFoundError`, `InvalidConfigError`) at the helper level. Warehouse callers (`warehouse/profiles.py`) and safety callers (`safety/config.py`) keep their existing one-line call sites and one typed-error catch surface. The wrappers exist because every internal call site within those layers wants the same translation; pushing the try/except into every caller would be more code than the shim.

The manifest loader still ships its own `_canonicalise_path` helper inline — manifest predates the common module and its escape exception (`ModelPathOutsideProjectError`) is tied tightly to the loader's diagnostic shape. Promotion is a future-clean-up candidate; the cost/benefit is low until a third consumer wants to share manifest's behaviour.

When introducing a new stage that needs path canonicalisation, prefer pattern (1) — import from `_common.path_safety` directly, wrap at the orchestrator. Reach for pattern (2) only when the layer has many internal callers that all want the same typed-error translation. **Don't** create a new layer-local `_path_safety.py` that duplicates the helper's body — that's the historical mistake issue #43 reverses.

## No logging in stage-0 modules; one-line warnings only at adapter boundary

Reader/parser modules emit no routine logs. The only exceptions are soft-threshold WARNINGs that signal the user is on a path likely to be slow or expensive — `profiles.py` emits one when `profiles.yml` exceeds the soft size cap (DEC-023); `_sql_safety.py` and `_path_safety.py` emit nothing. Observability beyond those soft warnings lives at the adapter boundary, where stage labels are known.

The adapter emits sparing `WARNING`-level signal when behaviour deviates from the deterministic happy path:

- `column_stats` queued batch exceeds the warning threshold (DEC-023) — signal that batching is doing real work.
- `sample_rows` num_rows >= threshold without a partition filter — signal before raising loud.

`INFO` and `DEBUG` are reserved for the prune/grade stages where signal-vs-volume tradeoffs surface. Don't add adapter-level INFO logging in v0.1.

## Don't pass our `TableRef` straight into vendor SDK methods (issue #21 lesson)

Third-party SDK methods that "take a table reference" accept a fixed set of vendor types — `bigquery.Client.get_table()` accepts `str | bigquery.TableReference | Table | TableListItem` and explodes on `AttributeError: 'TableRef' object has no attribute 'path'` when handed our Pydantic value object. Our `TableRef` has the same field names but is not duck-compatible with the SDK's internal type checks.

Always pass the **stringified form** (`ref.qualified_name`) to vendor SDK call sites. Two-part `dataset.table` (when `project=None`) is a valid `qualified_name` shape; the BQ SDK resolves the project from the client's billing project. The rule applies symmetrically in tests: `FakeBigQueryClient._coerce_to_tableref` accepts both `str` (parsed) and `TableReference`-shape (duck-typed via `.dataset_id` / `.table_id`).

This bug shipped silently in the BigQuery adapter (#3) for two issues' worth of work because every test that exercised `_get_table` used `FakeBigQueryClient`, which matched on `TableRef` equality and never noticed the production path was wrong. The integration tests at `tests/warehouse/test_bigquery_integration.py` had the same defect and were never run live. Issue #21's AR-B1 cost probe was the first code path that touched a real BQ client and broke.

Two takeaways for future adapter work:

1. Every public adapter method that calls `client.<method>(ref, ...)` needs a live integration test that runs against the real SDK, not just the fake. The fake is for behaviour assertions; only the real SDK enforces input-type contracts. Gate the live test on `SF_RUN_BQ=1` (or per-vendor equivalent) so default CI stays free, but require maintainers to run it before declaring an adapter "done."
2. When the fake's coercion helper has a special case for accepting strings, that's a load-bearing signal that the production path passes strings — don't accept non-string forms in the fake without a paired test that proves the real SDK accepts them too.

## Session/connection state on the adapter (DEC-002 of #22 generalised)

When a vendor protocol requires per-call state (BigQuery `session_id`, Snowflake transaction id, Postgres prepared-statement cache, Databricks SQL warehouse handle), store it on the adapter instance as `self._active_<x>_id: <type> | None` rather than threading it through ABC method signatures. The ABC stays vendor-neutral — a generic `materialise_sample(table, n, *, partition_filter=None, ttl_seconds=...) -> TableRef` does not need a `session_id` parameter that only one adapter populates. Concrete adapters wire the state internally; subsequent per-call methods on the same adapter instance read the state and attach the right vendor primitive (BigQuery `ConnectionProperty`, Snowflake `transaction_id`, etc.) without callers needing to know the dialect.

`__exit__` is the durable cleanup seam — the adapter is invoked as a context manager (`with adapter:`) and teardown lives there, NOT in the orchestrator. Cleanup-on-success and cleanup-on-failure share one path, and a hard process death falls back to the vendor's server-side timeout. State resets in a `finally` clause so a second `__exit__` is a no-op.

This generalises the v0.1 `column_stats` batching-state precedent (DEC-008 / DEC-025 of #3) and the v0.2 BigQuery `_active_session_id` pattern (DEC-002 of #22). When v0.3 ships a new adapter that needs per-call state, match this shape: `_active_<x>_id` instance fields, ABC stays neutral, cleanup in `__exit__`, no per-call state-id parameter on the ABC.

## `materialise_sample` ABC method + adapter session-state pattern (issue #22, v0.2)

Issue #22 lands `WarehouseAdapter.materialise_sample(table, n, *, partition_filter=None, ttl_seconds=3600) -> TableRef` (DEC-004 of #22). The ABC's default impl raises `MaterialisationNotSupportedError`; subclasses override (BigQuery in v0.2; Snowflake/Postgres in v0.3 via their own session/temp-table primitives). Method is NOT decorated `@abstractmethod` because the default impl IS the v0.2 behaviour for non-BQ adapters — the prune orchestrator's conservative-bias routing handles the no-support case gracefully and the operator opts out via `prune.sample_strategy: oneshot`.

**BigQueryAdapter session-state pattern (DEC-002 of #22).** The adapter carries `self._active_session_id: str | None`, `self._session_started_at: float | None`, and `self._session_ttl_seconds: int | None` for the duration of a `with` block. `materialise_sample` runs `CREATE TEMP TABLE _sf_sample_<run_id> AS SELECT ...` with `QueryJobConfig(create_session=True, ...defaults from _default_job_config(stage="warehouse_sample_materialise"))` — the CTAS itself uses the bare `_sf_sample_<run_id>` name, not a `_SESSION.` prefix. **BigQuery assigns the `session_id` server-side** and the SDK exposes it on the returned `QueryJob` via `job.session_info.session_id` only after `.result()` completes. The adapter captures and stores it; subsequent `run_test_sql` calls automatically attach `ConnectionProperty(key="session_id", value=self._active_session_id)` so per-test queries resolve `_SESSION._sf_sample_<run_id>` against the same session. The returned :class:`TableRef` carries `project=None, dataset="_SESSION", name="_sf_sample_<run_id>"` (two-part `qualified_name` `_SESSION._sf_sample_<run_id>`) — `project=None` is load-bearing because BigQuery rejects the three-part `<project>._SESSION.<name>` form even inside the owning session.

The pattern mirrors the `column_stats` batching-state precedent (DEC-008 / DEC-025 of #3 — adapter-instance state scoped to a `with` block; cleanup driven by `__exit__`). Don't introduce per-call session state; the prune orchestrator's `with adapter:` boundary is the unit. Tests reassign the adapter in a fresh `with` for each materialisation test.

**`run_id` is OUR derivation, not BQ's.** `run_id = blake2b(table.qualified_name + signalforge_version + str(n) + canonical_json(partition_filter), digest_size=8).hexdigest()` (16 hex chars) — DEC-001 of #22. Inputs joined with NUL separator. The temp-table name `_sf_sample_<run_id>` is deterministic across runs so the prune compiler's snapshot fixtures (DEC-023 of #6) and the `compiled_sql_hash` reproducibility invariant (DEC-005 of #6) survive unchanged. Don't conflate `run_id` (ours) with `session_id` (BQ's server-assigned UUID); they are not interchangeable.

**`ttl_seconds` is OUR-side hint, NOT a BQ knob (DEC-013 of #22).** BigQuery sessions have a server-enforced max lifetime (~24h regardless of activity) plus a BQ-default idle timeout. The `ttl_seconds=3600` parameter on `materialise_sample(...)` is NOT passed to BigQuery — it's a hint to OUR cleanup-WARNING text (the "auto-expire in Ns" line below). Don't go looking for a BQ SDK call to set it; v0.2 trusts the BQ default. v0.3 may revisit if BQ exposes a configurable per-session TTL knob.

**`_client.py` extension stays scoped (DEC-012 of #5 unchanged).** Any new `# pyright: ignore` for the session/connection-property surface lives in `adapters/_client.py` like every other BigQuery SDK ignore. The `_BQClientProtocol` may gain a session-property surface only if the existing `job_config: Any = None` typing is too loose — review during impl, but default to keeping the protocol minimal.

## `estimate_query_bytes` ABC method (issue #36, v0.2)

Issue #36 lands `WarehouseAdapter.estimate_query_bytes(sql: str) -> int` (DEC-004 of #36). The ABC's default impl raises `EstimateNotSupportedError` with the locked remediation `"Use --estimate with a BigQuery profile, or wait for v0.3 multi-warehouse estimation support."`; `BigQueryAdapter` overrides using `QueryJobConfig(dry_run=True)` (no `maximum_bytes_billed` set — dry_run doesn't bill) and reads `job.total_bytes_processed`. Method is NOT decorated `@abstractmethod` for the same reason as `materialise_sample`: the default IS the v0.2 behaviour for non-BQ adapters — the estimate engine's conservative-bias routing (`<unavailable: EstimateNotSupportedError>` in the warehouse section, exit 0) handles the no-support case gracefully.

This is now the **second non-BQ-adapter-graceful-degrade method** under the ABC (alongside `materialise_sample`). The pattern: when introducing a v0.x ABC method whose support is genuinely warehouse-specific (BigQuery dryRun, Snowflake `EXPLAIN`, Postgres prepared-statement plan), the default impl raises a typed `<Name>NotSupportedError(WarehouseError)` with a remediation that names the v0.x→v0.x+1 graduation path. The orchestrator that consumes the method catches the typed error and degrades the affected section of its output, never propagates. Non-BQ adapters opt out gracefully without forcing a v0.3 multi-warehouse blocker.

The fake-parity contract from `materialise_sample` extends: `FakeBigQueryClient.expect_dry_run(sql_matching, returns_bytes)` is a **separate queue** from `expect_query`. A `client.query(sql, job_config=QueryJobConfig(dry_run=True))` cannot fall through to `expect_query`, and a non-dry_run call cannot consume an `expect_dry_run`. Mirrors the `expect_materialise_sample` / `expect_abort_session` queue-isolation pattern from #22. Apply the same isolation when adding any future `expect_<vendor_specific>` helper.

## Cleanup-boundary fail-soft pattern (DEC-013 / DEC-014 of #22 generalised)

Cleanup-boundary errors are fail-SOFT, in deliberate contrast to primary-work fail-CLOSED (`safety-layer.md` DEC-011 — propagation IS the defence; an unaudited LLM call must abort the run). A cleanup boundary fires AFTER the user's actual work has succeeded; blocking on cleanup failure punishes the user for housekeeping problems they cannot fix in the moment. The pattern is three layers: (1) explicit close on the happy path, (2) swallow-and-warn on cleanup failure with an operator-actionable WARNING, (3) durable server-side fallback (vendor timeout, retry queue, etc.) for hard process death.

The WARNING is the load-bearing surface. It must give the operator three things, in this order: (a) the **identifier** required to act (raw `session_id`, transaction id, file handle — NOT a hash on this path; see DEC-003 of #22 for why redaction is relaxed only on this surface), (b) the **exact copy-pasteable command** to clean up manually (`bq query --connection_property=session_id=<raw> ...`, `kill -9 <pid>`, etc. — verbatim, not paraphrased), and (c) the **durable fallback** ("auto-expire in Ns", "will retry on next run", "OS reaps on process exit"). Without all three the WARNING is just noise; with all three the operator either acts or accepts the fallback in seconds.

`--quiet` does NOT suppress cleanup-failure WARNINGs (CLI default raises the floor to WARNING; the WARNING still surfaces). This is deliberate — the operator-actionable contract is the contract. Don't add a config knob to disable it; if a future caller genuinely needs silence (notebook tight loop), they configure Python's logging directly.

When introducing a new fail-soft cleanup boundary in v0.3 (Snowflake transaction rollback, Postgres connection pool drain, temp-file unlinking), match this shape verbatim. Cross-reference to `safety-layer.md` DEC-011 in the new section so the boundary distinction is explicit at the point a maintainer reads it.

## Best-effort cleanup in `__exit__` (DEC-013) with user-actionable failure WARNING (DEC-014)

The session must be torn down at the end of the prune run so the `_SESSION._sf_sample_<run_id>` temp table doesn't linger until BigQuery's server-side timeout. The adapter's `__exit__` implements a three-layer cleanup model — explicit close on the happy path, swallow-and-warn on cleanup failure, BQ's own session timeout as the durable fallback. **Contrast with `safety-layer.md` DEC-011: that's primary-work fail-closed (an unaudited LLM call must abort the run); this is cleanup-boundary fail-soft (the user's actual work succeeded; cleanup must never block them).** They look similar but apply to different boundaries; don't conflate.

**Layer 1 — explicit close (happy path).** `__exit__` checks `self._active_session_id`; if non-`None`, it issues `client.query("CALL BQ.ABORT_SESSION();", job_config=QueryJobConfig(connection_properties=[ConnectionProperty(key="session_id", value=self._active_session_id)]))`. On success, one `INFO` log: `"session closed"` with `{"session_id_hash": ..., "ttl_remaining_seconds": ...}` (lazy-format JSON; `session_id_hash` is `blake2b-4(session_id).hexdigest()` — DEC-003 of #22 redaction). State resets in a `finally` clause so subsequent `__exit__` calls are no-ops.

**Layer 2 — swallow-and-warn (cleanup failure).** If `CALL BQ.ABORT_SESSION();` itself raises (network blip, session already revoked, quota issue), the adapter **swallows the exception** and emits the DEC-014 multi-line WARNING. Cleanup never blocks the user; their actual work already succeeded. State still resets in `finally` so a second `__exit__` call is a no-op.

**Layer 3 — BigQuery server-side session timeout (durable fallback).** Hard process death (SIGKILL, OOM, the operator forgetting to wrap in a `with` block) cannot fire `__exit__`. BigQuery's BQ-managed ~24h max lifetime reaps the orphan automatically. The DEC-014 WARNING's "auto-expire in Ns" line communicates this fallback to the operator.

**WARNING shape (DEC-014 of #22 — verbatim, do not paraphrase).** The cleanup-failure WARNING is operator-actionable, multi-line, and uses lazy `%s` positional formatting (NOT f-strings — passes the grep gate). The body is exactly:

```text
BigQuery session cleanup failed; session will auto-expire in <N>s (BigQuery TTL).
  Session ID: <raw session_id>
  Reason: <exception class name>
  To clean up immediately:
    bq query --connection_property=session_id=<raw> --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"
```

`<N>` is `max(1, int(ttl_seconds - elapsed_in_session))`. Floor at 1 avoids "auto-expire in 0s" confusion. The manual `bq query` command is verbatim; operators copy-paste it. The trailing `--use_legacy_sql=false` and the `--connection_property=session_id=<raw>` form are load-bearing — strip either and the manual command fails.

**Raw `session_id` surfaces ONLY in the cleanup-failure WARNING (DEC-003 narrow exception).** Logs emit `session_id_hash = blake2b-4(session_id).hexdigest()` (8 hex chars) for every normal-operation event. Raw `session_id` stays in `BigQueryAdapter._active_session_id`, in the BQ `QueryJobConfig.connection_properties`, and in the DEC-014 cleanup-failure WARNING. Never in audit JSONL, never in error messages on the happy path, never in `__repr__` (DEC-022 of #3 unchanged — only `project` + `location` exposed). Three reasons the raw `session_id` is allowed in the WARNING:

1. **It's the only piece of info the operator needs to act.** Without it, the manual `bq` command is unconstructable. A hash defeats the purpose.
2. **Audience is the same principal who owns the session.** BigQuery rejects `BQ.ABORT_SESSION()` calls from any other identity — the `session_id` is only useful to its owner, who is the user reading their own stderr.
3. **The surface is bounded.** Raw `session_id` leaks ONLY on the cleanup-failure path, never on the happy path, never in audit JSONL, never in `__repr__`. Bulk log aggregators receive at most one such WARNING per failed cleanup, not per query.

The WARNING surfaces to stderr automatically via the CLI's `setup_logging`. Default level is INFO; `--quiet` raises the floor to WARNING — which means **`--quiet` does NOT suppress this WARNING**. Deliberate (DEC-014): the WARNING is operator-actionable (manual command + identifier inside), so we don't expose a path that silently drops it. If a future caller genuinely needs to silence everything (e.g., a notebook user testing in a tight loop), they configure Python's logging directly — no CLI flag for it.

When introducing a new fail-soft cleanup boundary in v0.3 (Snowflake session teardown, Postgres temp-table cleanup), match this shape: explicit close → swallow-and-warn with copy-pasteable manual recovery command → durable server-side fallback. Don't add a config knob to disable the WARNING; the operator-actionable contract is the contract.

## Reference

`plans/super/3-bigquery-adapter.md` — DEC-001 … DEC-028. `plans/super/22-temp-table-sample.md` — DEC-001 … DEC-014 (v0.2 materialised-sample additions: `materialise_sample` ABC, BigQuery session-state pattern, cleanup-WARNING shape). `src/signalforge/warehouse/` — current implementation. `tests/warehouse/_fake.py` — `FakeBigQueryClient` + `expect_*` API (extends with `expect_materialise_sample` / `expect_abort_session` per US-004 of #22). `docs/warehouse-adapter-ops.md` — operational reference.
