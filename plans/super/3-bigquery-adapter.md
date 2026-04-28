# Issue #3 — BigQuery warehouse adapter with sampling + dialect helpers

## Meta

- **Ticket:** [#3](https://github.com/wjduenow/SignalForge/issues/3)
- **Branch:** `feature/3-bigquery-adapter` (off `dev`)
- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/3-bigquery-adapter` (created via `bark new feature/3-bigquery-adapter --from dev`)
- **Phase:** devolved (epic `bd_1-scaffolding-8xk` + 14 tasks live; PR [#16](https://github.com/wjduenow/SignalForge/pull/16) draft)
- **Sessions:** 1 (started 2026-04-27, refined and detailed 2026-04-28)
- **Plan author:** Claude Code (Opus 4.7, 1M context)
- **Milestone:** v0.1
- **Labels:** `adapter`

## Discovery

### Ticket summary

Build the warehouse interaction layer of SignalForge: a `WarehouseAdapter` ABC that downstream prune/profile code calls, plus a `BigQueryAdapter` implementation. The adapter is **stage 2** of the SignalForge pipeline (manifest → LLM draft → **adapter samples warehouse** → prune → graded YAML). Without it, the prune step has no way to run candidate tests against real data, and the "drop always-pass / drop fails-on-clean-data" branches of the pipeline are blocked.

The interface must remain warehouse-agnostic. v0.1 ships BigQuery only; Snowflake/Postgres land in v0.2 (CLAUDE.md Architectural Commitment #3, README roadmap). The shape we lock here governs how cleanly v0.2 adapters slot in.

### Acceptance criteria (from ticket)

1. `WarehouseAdapter` ABC with: `sample_rows(table, n)`, `column_stats(table, column)`, `run_test_sql(sql) -> bool` (or count), `dialect()`.
2. `BigQueryAdapter` implementation using `google-cloud-bigquery`.
3. Auth via Application Default Credentials (ADC); document `gcloud auth application-default login`.
4. Configurable project/dataset; read from dbt `profiles.yml` if present.
5. Sampling uses `TABLESAMPLE SYSTEM` where supported, else `LIMIT` with deterministic ORDER BY hash.
6. Unit tests for the adapter using `pytest-bigquery-mock` or a thin fake; integration test gated by `SF_RUN_BQ=1`.

Ticket notes: `INFORMATION_SCHEMA.JOBS_BY_PROJECT` (cost analysis) is **out of scope** here.

### Codebase findings (Subagent B)

- **Mirror precedent: `signalforge.manifest`.** Layout: `__init__.py` (thin re-export, strict `__all__`), `errors.py` (typed hierarchy with `default_remediation` + `↳ Remediation:` rendering), `loader.py` (file IO + version detection), `models.py` (Pydantic v2 `frozen=True, extra="ignore", populate_by_name=True`).
- **Public API discipline (DEC-017 from #2):** `__init__.py` re-exports only documented names; `_`-prefixed helpers stay reachable via dotted import but absent from package namespace; `tests/manifest/test_public_api.py` enforces this.
- **Dependencies today:** runtime `pydantic>=2.5,<3`; dev `ruff`, `pyright`, `pytest`, `dbt-core>=1.8,<2` (fixture regen only). No `google-cloud-bigquery`, no `PyYAML`. Python 3.10 floor; pyright + CI locked to 3.11.
- **pytest markers already declared:** `unit`, `integration`, `error` — strict-markers mode is on (both `addopts` and `strict_markers = true` per pytest-9 quirk in `testing-signal.md`). A new `bigquery` (or `warehouse`) marker must be declared.
- **Test fixtures pattern:** real fixtures committed (`tests/fixtures/dbt_project_small/`), regeneration scripts via ephemeral `uvx` documented in `tests/fixtures/README.md`. Hand-derived error-path JSON for negatives.
- **Validation command (CLAUDE.md):** `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.
- **No prior warehouse code, no profiles.yml reading, no BigQuery references in code.** Greenfield, but the manifest subpackage establishes every convention this ticket should follow.

### Project rules (`.claude/rules/`) audit (Subagent C)

- **`python-build.md`** — Hatchling + src layout + explicit wheel `packages = ["src/signalforge"]` already covers any new subpackage. No new wheel target needed. Editable install via quoted `".[dev]"`.
- **`manifest-readers.md`** — Targets *external-format readers*. The warehouse adapter is a service client, not a parser, so the symlink-hardening / Pydantic-frozen rules don't directly bind. **Two carry-over principles do apply:** (a) every typed exception subclasses a module base and accepts a `remediation: str` kwarg; (b) "no logging in stage-0 modules" relaxes here — the adapter is consumed by the prune stage and *may* log auth failures and skipped tables, but not every row sampled.
- **`testing-signal.md`** — Hard-applies. No `assert True`-shaped tests. Strict markers (both settings). No `tests/__init__.py`. If we vendor live BigQuery snapshots as fixtures, regenerate via `uvx`. **`unittest.mock.MagicMock` is implicitly forbidden** for adapter mocking — it auto-passes everything = always-pass test = anti-signal. Use explicit fakes that fail loudly.
- **`ci-supply-chain.md`** — SHA-pinned actions, scoped GITHUB_TOKEN, single Python 3.11. Applies if we add a CI job for BQ integration tests (Phase 1 will decide we don't, in v0.1).
- **No `workflow-project.md`** — baseline review areas only in Phase 2.

### CLAUDE.md commitments that bite this ticket

- **#1 — Signal over volume.** The adapter is the engine of pruning. Sampling defaults must produce *meaningful* samples (not 1 row, not full-table-only). Falls out of the sampling-strategy decisions.
- **#3 — Warehouse-agnostic by design.** Load-bearing. The ABC must contain zero BigQuery-isms. `BigQueryAdapter` is where dialect/auth/cost specifics live. Snowflake/Postgres must be able to subclass the ABC without reshaping it (sanity-checked against Subagent D's mapping).
- **#4 — OSS-first / Core-friendly.** No dbt Cloud; ADC is the default auth path; `profiles.yml` reading uses `PyYAML`, not `dbt-core` runtime (`dbt-core` is a fixture-only dev dep).
- **#5 — Explainable diffs.** Errors carry remediation (per #2's pattern). Every adapter call that drops a candidate must surface a one-line "why" — relevant for `run_test_sql`'s return shape and for the typed exceptions.
- **Roadmap anchor.** v0.1 = BigQuery only. Don't bake Snowflake/Postgres adapter classes now — but design the ABC to absorb them.

### Domain research findings (Subagent D — load-bearing surprises)

The full brief lives in research notes; the load-bearing facts that drive Phase 1 decisions:

1. **`pytest-bigquery-mock` is dead.** Last release 0.0.4 (2021), targets `google-cloud-bigquery` 2.x; we're on 3.x. The ticket spec is out of date. Hand-rolled `FakeBigQueryClient` (~150 LOC, three methods: `query`, `get_table`, `list_rows`) is the recommended path.
2. **`TABLESAMPLE SYSTEM` cost asterisk.** It does NOT proportionally reduce bytes-billed on un-clustered, un-partitioned tables — it samples blocks *after* the scan. Cost win only on clustered tables, or when combined with a partition filter. Naive "1 PERCENT = 1% cost" assumption is wrong. Documenting this prominently is a v0.1 obligation.
3. **TABLESAMPLE is non-deterministic across runs.** No seed parameter. Same input → different sample → different prune decision. **For SignalForge's reproducibility commitment, hash-mod is the safer default**, even when TABLESAMPLE is technically supported.
4. **Where TABLESAMPLE doesn't work at all:** views, materialized views, wildcard tables, CTEs, `INFORMATION_SCHEMA`, some external tables. The fallback is therefore not edge-case — it's the path for any `dbt source` that's actually a view.
5. **Hash-mod canonical pattern:** `MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), 100) < N` — universal (no key column needed), 3× faster than SHA256. Always combine with a partition filter for partitioned tables, or you scan the whole thing.
6. **`maximum_bytes_billed` should be on every query.** Hard cap; query fails fast if estimate exceeds. Subagent recommends 100 MB default for the prune path. Without it, a malformed candidate test could scan a TB.
7. **`RowIterator` over `to_dataframe()`.** `to_dataframe` pulls in `pandas` (~50 MB) + `db-dtypes` + optionally `pyarrow`. SignalForge stays dep-light by reading rows directly. Dict-conversion at the adapter boundary.
8. **dbt `profiles.yml` schema for BigQuery.** Keys: `method` (`oauth` for ADC happy path), `project`, `dataset` (alias `schema`), `location`, `priority`, `maximum_bytes_billed`. Resolution: `--profiles-dir` flag → `DBT_PROFILES_DIR` env → project root → `~/.dbt/profiles.yml`. Active target via `dbt_project.yml`'s `profile:` and the inner `target:` field. **`PyYAML` is plenty** — never `dbt-core` runtime.
9. **`google-cloud-bigquery` typing is partial.** Pyright will complain on `Row.__getitem__` and the `QueryJob.result()` chain. Containment pattern: wrap all `google.cloud.bigquery` calls inside one private `_client.py`, expose typed SignalForge return types outward, file-local `# pyright: ignore[reportUnknownMemberType]` only where unavoidable. Don't pull third-party stubs (they go stale).
10. **Forward-compat sanity check** (mapping ABC to Snowflake/Postgres): table reference shapes diverge — BigQuery `` `project.dataset.table` ``, Snowflake `DATABASE.SCHEMA.TABLE` (uppercase), Postgres `schema.table`. Auth shapes diverge wildly (key-pair, password, conn string). Cost-control knobs diverge (`maximum_bytes_billed` is BQ-only). The ABC should accept a structured `TableRef` (or take a `Model` from the manifest layer) and let each adapter quote/render its own. Auth config is per-adapter; not abstracted on the ABC.

### Out of scope (explicit)

- **`INFORMATION_SCHEMA.JOBS_BY_PROJECT` cost analysis** — ticket says so; v0.2 work.
- **Snowflake / Postgres / Databricks adapters** — v0.2+ per roadmap.
- **The prune logic itself** — separate ticket; this one supplies the primitive (`run_test_sql`, `sample_rows`).
- **The LLM drafting layer** — separate ticket; this one is downstream.
- **CLI wiring** — separate ticket. The adapter is library-only here; the CLI ticket will pick which adapter to instantiate.
- **Caching of samples / query results across runs** — v0.2 (helps reproducibility but not v0.1 minimum).
- **Cost dashboards / billing surfacing beyond `maximum_bytes_billed`** — v0.2.
- **Service-account / key-pair auth flows** — ADC-only for v0.1 (matches dbt's `oauth` profile method). Documenting non-ADC paths is fine; implementing them is v0.2.

### Phase 1 housekeeping defaults (set unless flagged in Phase 2/3)

- New pytest marker `bigquery` declared in `pyproject.toml`; integration tests carry `@pytest.mark.bigquery` AND `@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"))` (belt + suspenders). `addopts` adds `-m 'not bigquery'` so collection skips them by default.
- `RowIterator` for results; never `to_dataframe()`. No `pandas` / `pyarrow` / `db-dtypes` in v0.1 deps.
- `maximum_bytes_billed` defaulted on every query (concrete value picked in scoping Q5 below).
- ADC-only auth in v0.1; `service-account` and `service-account-json` profile methods raise a typed `UnsupportedAuthMethodError` with a remediation pointing at `gcloud auth application-default login`.
- No CI job for BigQuery integration tests in v0.1; document maintainer-only path. Revisit when SignalForge has external contributors.
- Single Python 3.11 in CI; runtime supports 3.10+.

### Scoping decisions (Phase 1 — locked 2026-04-27, "use defaults")

- **DEC-001 — Subpackage layout: `signalforge.warehouse` with adapters subdir** (Q1=B). Public modules: `signalforge.warehouse.{__init__, base, errors, profiles, models}`; concrete adapters under `signalforge.warehouse.adapters.{bigquery}`. *Why:* "many adapters" home is explicit from day one; v0.2 Snowflake/Postgres slot into `adapters/` without restructuring; mirrors `signalforge.manifest` re-export discipline. *How to apply:* `__init__.py` re-exports the public ABC, typed return models, errors, and `BigQueryAdapter`; `_`-prefixed helpers stay reachable via dotted import only (DEC-017 from #2).
- **DEC-002 — Drop `pytest-bigquery-mock`; hand-rolled `FakeBigQueryClient`** (Q2=A). The package is unmaintained (last release 2021) and pinned to `google-cloud-bigquery` 2.x. *Why:* using a dead, version-mismatched package would either fail outright or paper over real bugs; `MagicMock`-style fakes auto-pass everything and violate `testing-signal.md`. *How to apply:* `tests/warehouse/_fake.py` defines `FakeBigQueryClient` with explicit `query`, `get_table`, `list_rows` methods, parameterised by canned responses keyed on SQL (regex-or-exact). Calls outside the canned set raise `AssertionError("unexpected query: ...")` so silent mismatches surface loudly.
- **DEC-003 — Typed `Dialect` ADT, not a bare string** (Q3=B). `dialect() -> Dialect` returns a frozen dataclass with `name: str`, `supports_tablesample: bool`, `supports_qualify: bool`, `quote_char: str`, `identifier_case: Literal["upper","lower","preserve"]`. *Why:* the LLM drafting stage will need these capability flags to render valid SQL per warehouse; pre-baking the ADT now avoids a v0.2 refactor that would touch every drafting code path. *How to apply:* `Dialect` lives in `signalforge.warehouse.models`; `BigQueryAdapter.dialect()` returns a module-level constant `BIGQUERY_DIALECT`. Test that the constant is frozen.
- **DEC-004 — `TableRef` frozen dataclass for the ABC's table parameter** (Q4=A). `TableRef(project: str | None, dataset: str, name: str)`. Each adapter implements `_quote(ref: TableRef) -> str` for its dialect. *Why:* raw qualified strings on the public ABC are a SQL-injection seam and don't survive cross-warehouse (BigQuery backticks, Snowflake uppercase, Postgres no project layer); coupling the adapter to `signalforge.manifest.Model` would over-couple two stages we deliberately decouple. *How to apply:* `TableRef` lives in `signalforge.warehouse.models`; `BigQueryAdapter._quote` renders `` `project.dataset.name` `` and rejects identifiers containing backticks/whitespace with `InvalidIdentifierError` (typed exception).
- **DEC-005 — Default `maximum_bytes_billed = 100 MB`; per-call override + dbt-profile upper bound** (Q5=A). `BigQueryAdapter(max_bytes_billed: int = 100_000_000)`; per-call `cost_limit_bytes` kwarg overrides downward only. If dbt profile has `maximum_bytes_billed`, it caps both. *Why:* without a default cap a malformed candidate test could scan a TB; 100 MB is generous for prune-stage sampling but bounds the worst case; respecting the dbt profile honours the user's existing cost discipline. *How to apply:* set `QueryJobConfig.maximum_bytes_billed = effective_limit` on every query (`sample_rows`, `column_stats`, `run_test_sql` alike); raise `BytesBilledExceededError` (typed, with remediation pointing at the kwarg) when BQ rejects with the corresponding `BadRequest`.
- **DEC-006 — Sampling default: always hash-mod with `FARM_FINGERPRINT`** (Q6=C). `sample_rows(table, n)` issues `SELECT * FROM <quoted> WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1 LIMIT n` with the bucket sized from a `get_table().num_rows` lookup so the expected sample is ~3-5× n before the LIMIT. *Why:* deterministic across runs (matches Architectural Commitment #5 — same input → same prune decision); works on views/MVs/wildcard tables/CTEs (TABLESAMPLE doesn't); same cost as TABLESAMPLE on un-clustered tables anyway. TABLESAMPLE becomes an opt-in for v0.2 cost-aware sampling on clustered tables. *How to apply:* `sample_rows` accepts `partition_filter: str | None` so callers can scope partitioned tables; without it, log a one-line warning when `num_rows > 100M` (signal: "you're scanning a lot for sampling — pass a partition filter").
- **DEC-007 — `run_test_sql` returns `TestResult` ADT, not bare `bool`** (Q7=A). `TestResult(passed: bool, failure_count: int, sample_failures: list[dict] | None)`. `passed = (failure_count == 0)`; `sample_failures` populated only when caller passes `capture_failures: int > 0`. *Why:* deviates from the ticket's "or count" wording in favour of the typed shape because Architectural Commitment #5 (explainable diffs) requires that every dropped artifact ship with a one-line "why" — discarding the failure count and a few example rows at the adapter boundary forces the prune layer to re-query for the same information. *How to apply:* `run_test_sql(sql: str, capture_failures: int = 0) -> TestResult`; the BigQuery impl wraps the candidate test in `SELECT COUNT(*) AS failures, ARRAY_AGG(t LIMIT @cap) AS samples FROM (<sql>) t` when capture is requested, else just `SELECT COUNT(*)`.
- **DEC-008 — `column_stats` per-column public signature, batched within `with adapter:` context** (Q8=B). Public: `column_stats(table: TableRef, column: str) -> ColumnStats`. Internally, when called inside an active context manager, repeated calls for the same table accumulate column names and the first stat access flushes a single batched `SELECT COUNT(...), COUNT(DISTINCT ...), COUNTIF(... IS NULL), MIN(...), MAX(...) FROM ...` per table. *Why:* matches ticket signature literally for callers; collapses the common "stats for every column of one model" pattern into one round-trip; outside a context the call is eager (predictable for ad-hoc use). *How to apply:* the batching cache lives on the adapter instance, scoped to `__enter__/__exit__`; `ColumnStats` is a frozen Pydantic v2 model with `count: int`, `distinct: int`, `nulls: int`, `min: Any`, `max: Any`.
- **DEC-009 — dbt `profiles.yml` reader at `signalforge.warehouse.profiles`** (Q9=A). Public: `load_profile(project_dir: Path, target: str | None = None) -> DbtProfileTarget`. Resolution order: `--target` arg → `target:` field of named profile → error. Profile file resolution order: `DBT_PROFILES_DIR` env → `<project_dir>/profiles.yml` → `~/.dbt/profiles.yml`. *Why:* reusable by Snowflake/Postgres adapters in v0.2 without moving code; sits one level above adapters (no adapter imports another); typed return shape is the contract every adapter consumes. *How to apply:* `DbtProfileTarget` is a Pydantic v2 model (`frozen=True, extra="ignore", populate_by_name=True`) with fields `type: str`, `method: str | None`, `project: str | None`, `dataset: str | None` (alias `schema`), `location: str | None`, `priority: str | None`, `maximum_bytes_billed: int | None`. Typed errors: `ProfileNotFoundError`, `ProfileTargetNotFoundError`, `UnsupportedProfileMethodError`. PyYAML's `safe_load` only — never `yaml.load`.
- **DEC-010 — Dependency strategy: required runtime deps** (Q10=A). Add to `[project.dependencies]`: `google-cloud-bigquery>=3.20,<4`, `PyYAML>=6,<7`. *Why:* v0.1 ships BigQuery only and the CLI assumes BQ; an optional `[bigquery]` extra would force `try/except ImportError` guards everywhere with no payoff until v0.2. The optional-extras refactor lands with the second adapter, where it's actually load-bearing. *How to apply:* update `pyproject.toml` dependencies; add `types-PyYAML` to `[project.optional-dependencies].dev` so pyright sees the YAML stubs.
- **DEC-011 — Integration tests: marker + `skipif(SF_RUN_BQ)`; default-skip via `addopts`** (Q11=A). Declare `bigquery: tests requiring BigQuery credentials (gated by SF_RUN_BQ=1)` in `pyproject.toml` markers. Add `-m 'not bigquery'` to `addopts`. Each integration test wears `@pytest.mark.bigquery` AND `@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason="requires SF_RUN_BQ=1 and ADC")`. Fixtures hit `bigquery-public-data.samples.shakespeare` (free under the 1 TB/month tier). *Why:* belt + suspenders — collection-time skip keeps default `pytest` runs quiet and fast; runtime skip stops `pytest -m bigquery` from blowing up when ADC is unconfigured. No CI job for v0.1 (maintainer-only path); revisit when external contributors arrive. *How to apply:* document `SF_RUN_BQ=1 pytest -m bigquery` in `docs/warehouse-adapter-ops.md`.
- **DEC-012 — Companion ops doc: `docs/warehouse-adapter-ops.md`** (Q12=A). Sections: ADC setup (`gcloud auth application-default login`), dbt profile resolution rules + the `oauth` method assumption, `maximum_bytes_billed` defaults and override knobs, sampling strategy with the TABLESAMPLE cost-asterisk explanation, integration-test invocation, and a typed-error reference matching `signalforge.warehouse.errors`. *Why:* `docs/manifest-loader-ops.md` set the precedent — every public-API subpackage gets an ops guide alongside the docstrings; the cost-asterisk and ADC story are user-visible enough to deserve doc-level treatment, not just docstrings. *How to apply:* the doc lands as part of US-007-equivalent in Phase 4; cross-link from README's "Configuration" section once written.

---

## Architecture Review

Reviewed 2026-04-27 by six parallel subagents (security, performance, data model, API design, observability, testing strategy) against the locked Phase 1 shape (DEC-001 … DEC-012). Result: **9 unique blockers, 18 concerns, broad agreement on subpackage layout, error pattern, fixture strategy, and warehouse-agnostic ABC.** Each blocker has a Phase-3 prompt below to lock the resolution.

### Findings table

| Area | Rating | Notes |
| --- | --- | --- |
| Security — `_quote(TableRef)` accepts unvalidated identifiers | **blocker** | DEC-004 says "rejects backticks/whitespace" but provides no logic. Attack: `dataset = "foo` ; DROP TABLE bar; --"`. Mitigation: `re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$")` on each `TableRef` field at construction time, raising `InvalidIdentifierError`. (Same regex applied to `column_stats(table, column)` resolves the column-name concern.) |
| Security/API — `partition_filter: str` is raw SQL | **blocker** | Both an injection seam *and* a leaky cross-warehouse abstraction (BQ uses `_PARTITIONDATE`, others differ). Replace with `PartitionFilter(column: str, op: Literal["=",">",">=","<","<=","!="], value: date \| datetime \| str)` ADT; each adapter renders its own SQL via `_render_partition_filter`. |
| Performance — `use_query_cache` default unspecified | **blocker** | BQ defaults to `True`, which gives free re-runs *but* breaks the determinism contract (Commitment #5: "same input → same prune decision") if cached results mask candidate-test changes. Set `QueryJobConfig.use_query_cache = False` on every query in v0.1; revisit with explicit opt-in in v0.2. |
| Data model — `ColumnStats.min`/`max` typed `Any`; complex types undefined | **blocker** | BQ has 18+ column types; GEOGRAPHY/JSON/ARRAY/STRUCT/RANGE have no useful min/max. Add `data_type: str` field; for complex types, set `min`/`max` to `None`; document per-type contract. Switch annotation to `int \| float \| str \| bool \| datetime \| date \| None` (or stricter `JsonValue \| None`). |
| Data model — `DbtProfileTarget` `extra="ignore"` silently drops `keyfile`/`impersonate_service_account` | **blocker** | Means a user with a service-account profile silently falls back to ADC = silent auth failure (worst possible UX). Switch to `extra="forbid"` and add a `method` field validator that raises `UnsupportedAuthMethodError` for `service-account` / `service-account-json` / `impersonate_service_account`. Loud failure with remediation. |
| Data model — `Manifest.Model` → `TableRef` gateway missing | **blocker** | Without `TableRef.from_model(model: Manifest.Model)` classmethod every caller hand-builds the translation; ad-hoc duplication across the prune/CLI layers is guaranteed. Add to `signalforge.warehouse.models` (one-way dep on manifest layer is acceptable; manifest stays unaware). Logic: `project=model.database`, `dataset=model.schema_`, `name=model.alias or model.name`. |
| API — Adapter factory `WarehouseAdapter.from_profile` missing | **blocker** | Plan defers polymorphic adapter selection but provides no factory. CLI and prune layer have nowhere to dispatch on `profile.type`. Add `WarehouseAdapter.from_profile(profile: DbtProfileTarget) -> WarehouseAdapter` classmethod that dispatches on `profile.type`; v0.1 supports only `"bigquery"` (others raise `UnsupportedProfileTypeError`). Direct instantiation (`BigQueryAdapter(...)`) remains supported for tests. |
| Observability — `TestResult.explanation()` not specified; adapter↔prune boundary blurred | **blocker** | Commitment #5 mandates a one-line "why" for every dropped artifact. Without an explanation method on `TestResult`, the prune layer reaches in and string-formats `failure_count` + `sample_failures` ad-hoc. Add `TestResult.explanation() -> str` that renders e.g. `"42 rows failed (example: id=123, name=...)"` deterministically. The prune layer can still override format, but the default lives on the typed return. |
| Testing — pytest `bigquery` marker + `addopts` not yet in `pyproject.toml` | **blocker** | DEC-011 specifies the marker shape but the actual edits to `pyproject.toml` (`markers = [..., "bigquery: ..."]` AND `addopts = "-ra --strict-markers -m 'not bigquery'"`) must land before integration tests are written, or strict-markers will reject them at collection. Config-only fix; lands in the first implementation story. |
| Testing — comprehensive unit-test list missing from plan | **concern** | `testing-signal.md` requires every test be capable of failing. Phase 4 stories must explicitly enumerate the ~22 unit tests below; without enumeration we risk landing thin coverage. Tracked by amending Phase 4 stories rather than a Phase 3 decision. |
| Security — `run_test_sql` wrapper `(<sql>) t` smuggling risk | concern | Caller-supplied SQL is the entire point, but a comment-out-trailing-paren attack (`SELECT 1 FROM t)) UNION SELECT secret--`) could subvert the wrapper. Mitigation: validate caller SQL has no top-level `;`, `--`, or unbalanced parens via lightweight tokenizer; OR document the contract loudly (the LLM drafter is the practical caller, and we control its prompts). Lean toward "document + validate balanced parens"; full SQL parsing is overkill. |
| Security — `profiles.yml` path-traversal hardening | concern | `<project_dir>/profiles.yml` should pass through symlink-hardened `_canonicalise_path` per `manifest-readers.md`. `DBT_PROFILES_DIR` env and `~/.dbt/profiles.yml` are user-trusted (document but do not gate). |
| Security — Credentials in `__repr__` / error messages | concern | Define `BigQueryAdapter.__repr__` to return only `<BigQueryAdapter project=...>`; never include credentials object. Quote user input in error messages via `repr()` (e.g. `f"target {target!r} not found"`) so reflection-style payloads can't render special characters into log viewers. |
| Security — Resource exhaustion on column batches and YAML | concern | `column_stats` batch with 10 000 columns produces a giant SELECT; soft warning at ~500 columns. `profiles.yml` >1 MB is weird but possible; soft warning at 1 MB matches the `MAX_MANIFEST_BYTES` precedent from #2. |
| Performance — Hash-mod on un-partitioned multi-TB tables | concern | Default `maximum_bytes_billed=100 MB` means the query *will* fail on a 10 TB un-partitioned table — but the user sees the failure post-hoc. Promote DEC-006's "log warning" to "raise `SamplingRequiresPartitionFilterError` when `num_rows >= 100M` and no `PartitionFilter` provided." Loud-fail beats silent over-spend. |
| Performance — `num_rows` accuracy fallback (views, external, 0/None) | concern | `Table.num_rows` is `None`/0 for views/MVs/external tables. Define hierarchy: `num_rows > 0` → bucket = `max(num_rows // n, 1)`; else if `PartitionFilter` is set → bucket = `1000` (debug-log we're guessing); else → raise `UnknownTableSizeError`. |
| Performance — `column_stats` memory + flush-latency surprise | concern | Lazy-flush on first stat access can surprise callers ("why did `.count` block?"). Document the recommended access pattern (call all stats for a table, then read) in docstrings + ops doc; emit `DEBUG`-level log on flush so debug traces reveal what's batched. |
| Performance — `get_table()` redundant RPCs | concern | `sample_rows` calls `get_table()` for bucket sizing every time. Cache `Table` metadata on the adapter instance, scoped to `with adapter:`; invalidation is the user's problem (document). |
| Performance/Obs — BQ job `labels` not set | concern | Cheap to add now: `QueryJobConfig.labels = {"signalforge_stage": "warehouse_sample"}` (or `"warehouse_test"` etc.). Painful to backfill. v0.2 cost-analysis (out of scope here) will filter on these. Bake in v0.1. |
| API — `column_stats` dual context-manager semantics | concern | DEC-008's "outside a context the call is eager" path adds branching for an unclear use case. Decision: **require active context** for `column_stats`; raise `RuntimeError("column_stats must be called inside a `with adapter:` block")` if not. Simplifies state machine; tests are clearer. |
| API — Error hierarchy gaps | concern | Plan lists 7 errors. Add: `TableNotFoundError`, `ColumnNotFoundError`, `QuerySyntaxError`, `SamplingError`, `SamplingRequiresPartitionFilterError`, `UnknownTableSizeError`, `WarehouseAuthError`, `UnsupportedProfileTypeError`. Total ~15 typed exceptions plus `WarehouseError` base. |
| Data model — `TestResult.sample_failures` row-schema fidelity | concern | `list[dict]` loses column types (TIMESTAMP renders as bare ISO string downstream). Add `row_schema: list[tuple[str, str]] | None` (column name + BQ type) so the prune layer renders SQL-safe values. Cheap; high signal. |
| Data model — `TableRef.project: str \| None` semantic ambiguity | concern | `None` should mean "use client's billing project / default project." Document this in the dataclass docstring; `BigQueryAdapter` resolves None to `self._client.project` at quote time. No exception raised on None alone. |
| Obs — Logger discipline | concern | `logging.getLogger("signalforge.warehouse")`; `WARNING` for sampling hint; `DEBUG` for batch flushes; **never** log full SQL or row data. Document the levels in `docs/warehouse-adapter-ops.md` debugging section. |
| Obs — Structured vs. unstructured logs | concern | Unstructured string templates for v0.1 (simpler). v0.2 cost-analysis would benefit from structured `extra={...}`; leave a one-line code comment marking the refactor target so the next person knows it's intentional. |
| Obs — Ops doc missing observability/debugging section | concern | DEC-012's ops doc must include a "Debugging" section: enable `DEBUG` logger, find the BQ job ID for failed queries, typed-error reference, integration-test invocation. |
| Testing — `FakeBigQueryClient` should expose assertion helpers | concern | DEC-002's "raw dict of SQL→responses" is brittle. Use `fake.expect_query(matching=..., returns=...)` / `fake.expect_get_table(...)` API; calls outside expectations raise `AssertionError("unexpected query: ...")`. Helper API lives in `tests/warehouse/_fake.py`. |
| Testing — Two integration tests missing | concern | Add `test_int_max_bytes_billed_blocks_oversize_query` (use `bigquery-public-data.crypto_ethereum.transactions` to trip the 100 MB cap) and `test_int_adc_unconfigured_raises_typed_error` (monkeypatch `google.auth.default()` to raise). Both maintainer-only, gated by `SF_RUN_BQ`. |

### Blockers (must resolve in Phase 3)

1. **B1** — `_quote(TableRef)` + `column_stats` column-name validation regex (`re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$")`).
2. **B2** — Replace `partition_filter: str` with `PartitionFilter` ADT.
3. **B3** — `use_query_cache=False` on every `QueryJobConfig`.
4. **B4** — `ColumnStats.data_type` field + complex-type contract (min/max=None for GEOGRAPHY/JSON/ARRAY/STRUCT/RANGE).
5. **B5** — `DbtProfileTarget` switches to `extra="forbid"` + `method` validator that loudly rejects non-ADC auth.
6. **B6** — `TableRef.from_model(model)` classmethod gateway.
7. **B7** — `WarehouseAdapter.from_profile(profile)` factory classmethod.
8. **B8** — `TestResult.explanation() -> str` method.
9. **B9** — pytest `bigquery` marker + `addopts = "-m 'not bigquery'"` in `pyproject.toml` (config-only).

### Concerns to resolve in Phase 3

C1 — `run_test_sql` wrapper SQL contract (validate balanced parens? document only?).
C2 — `<project_dir>/profiles.yml` symlink-hardening; document `DBT_PROFILES_DIR` and `~/.dbt/` exemptions.
C3 — `BigQueryAdapter.__repr__` redaction; user-input quoting in error messages (`repr()`).
C4 — Soft warning thresholds: column batch >500 columns; profiles.yml >1 MB.
C5 — Promote large-unfiltered-sampling warning to `SamplingRequiresPartitionFilterError`.
C6 — `num_rows` fallback hierarchy → `UnknownTableSizeError` when ambiguous.
C7 — Document `column_stats` access pattern (call all, then read); DEBUG log on batch flush.
C8 — Cache `Table` metadata on adapter instance within `with adapter:`.
C9 — Set `QueryJobConfig.labels = {"signalforge_stage": "..."}` on every query.
C10 — `column_stats` requires active context; raise `RuntimeError` if called outside.
C11 — Expand error hierarchy with `TableNotFoundError`, `ColumnNotFoundError`, `QuerySyntaxError`, `SamplingError`, `SamplingRequiresPartitionFilterError`, `UnknownTableSizeError`, `WarehouseAuthError`, `UnsupportedProfileTypeError`.
C12 — `TestResult.row_schema: list[tuple[str, str]] | None` for downstream render fidelity.
C13 — `TableRef.project = None` documented as "use client default project."
C14 — Logger naming + level discipline (`WARNING` for hints, `DEBUG` for flushes, no SQL).
C15 — Unstructured logs in v0.1 with a code comment marking the v0.2 refactor target.
C16 — Add Debugging section to `docs/warehouse-adapter-ops.md`.
C17 — `FakeBigQueryClient` `expect_query` / `expect_get_table` assertion-helper API.
C18 — Two missing integration tests (`test_int_max_bytes_billed_blocks_oversize_query`, `test_int_adc_unconfigured_raises_typed_error`).

## Refinement Log

### Phase 3 decisions (resolved 2026-04-28, "use defaults" — all blockers and all concerns)

Sixteen decisions consolidating the nine blockers and eighteen concerns from the Architecture Review.

- **DEC-013 — Identifier validation regex on every public-API string field that becomes SQL** (resolves B1 + C1). At construction time, `TableRef.{project,dataset,name}` and `PartitionFilter.column` validate against `re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$")` and raise `InvalidIdentifierError(field: str, value: str)` with remediation `"Identifiers must match [A-Za-z_][A-Za-z0-9_]*. Got: {value!r}"`. `column_stats(table, column)` re-validates the column at entry. `run_test_sql(sql)` does **not** parse SQL but documents the contract: callers must supply a single SELECT statement returning rows; `;`, top-level `UNION`, and `--` outside string literals are rejected by a lightweight balanced-paren / no-statement-terminator check (regex-tier, not full SQL parser). *Why:* the LLM drafter is the practical caller of `run_test_sql` and we control its prompt; full SQL parsing is overkill, but the four cheap rejects (`;`, `--`, `/* */`, unbalanced `()`) catch the easy mistakes without false-positives. *How to apply:* a `_sql_safety.py` private helper module hosts both the identifier regex and the cheap SQL sanity check; `tests/warehouse/test_sql_safety.py` covers happy + adversarial inputs.
- **DEC-014 — `PartitionFilter` ADT replaces raw `partition_filter: str`** (resolves B2). `@dataclass(frozen=True) class PartitionFilter(column: str, op: Literal["=",">",">=","<","<=","!="], value: date | datetime | str)` lives in `signalforge.warehouse.models`. `BigQueryAdapter._render_partition_filter(pf)` produces `` `column` op TIMESTAMP('...')`` / `DATE('...')` / `'...'` based on `value`'s Python type. `column` runs through DEC-013's regex; `value`-as-`str` is rendered via parameterised query (`@param`) where possible, else single-quote-escaped (`'`→`''`) and inlined. Snowflake/Postgres adapters get their own `_render_partition_filter` in v0.2. *Why:* closes the SQL-injection seam **and** the cross-warehouse leaky abstraction in one stroke. *How to apply:* `sample_rows(table, n, *, partition_filter: PartitionFilter | None = None)`.
- **DEC-015 — Determinism + cost-attribution `QueryJobConfig` defaults** (resolves B3 + C9). Every `QueryJobConfig` set by `BigQueryAdapter` starts from a private `_default_job_config()` that returns: `use_query_cache=False`, `maximum_bytes_billed=<effective_limit>`, `labels={"signalforge_stage": "<stage>", "signalforge_version": __version__}` where `<stage>` is one of `"warehouse_sample"`, `"warehouse_stats"`, `"warehouse_test"`. Per-call kwargs may override `maximum_bytes_billed` downward only; `use_query_cache` is **not user-overridable in v0.1** (revisit in v0.2 with explicit opt-in flag). *Why:* `use_query_cache=False` upholds Architectural Commitment #5's reproducibility (same input → same prune decision); `labels` are cheap to add now and load-bearing for v0.2 cost-attribution via `INFORMATION_SCHEMA.JOBS_BY_PROJECT`. *How to apply:* every method that constructs SQL (`sample_rows`, `column_stats`, `run_test_sql`) goes through `_default_job_config()`.
- **DEC-016 — `ColumnStats.data_type` field + complex-type contract** (resolves B4). `ColumnStats(count: int, distinct: int, nulls: int, min: int | float | str | bool | datetime | date | None, max: int | float | str | bool | datetime | date | None, data_type: str)`. For BigQuery types `GEOGRAPHY`, `JSON`, `ARRAY<...>`, `STRUCT<...>`, `RANGE<...>`, `BYTES`, the adapter sets `min=max=None` and the SQL emitted skips `MIN/MAX` (`SELECT COUNT(...), COUNT(DISTINCT ...), COUNTIF(... IS NULL) FROM ...`). For comparable types (numerics, strings, BOOL, DATE/TIME/DATETIME/TIMESTAMP/INTERVAL/NUMERIC/BIGNUMERIC), `MIN/MAX` runs and returns Python-native types via the BQ row converter. *Why:* `Any` punts the type problem and the prune layer needs `data_type` to render explainable diffs correctly; complex-type silent-None is well-documented contract beats silent-zero. *How to apply:* `tests/warehouse/test_models.py` parametrises across the type matrix.
- **DEC-017 — `DbtProfileTarget` is `extra="forbid"` with auth-method validator AND symlink-hardened path resolution** (resolves B5 + C2). Pydantic config: `ConfigDict(frozen=True, extra="forbid", populate_by_name=True)`. Field validator on `method`: only `"oauth"` or `None` accepted; everything else (`service-account`, `service-account-json`, `oauth-secrets`, `impersonate-service-account`, etc.) raises `UnsupportedAuthMethodError` with remediation pointing at `gcloud auth application-default login`. Forward-compat for *new* dbt-bigquery fields is handled by the **drift-detector test** (a one-off `StrictModel` against a current fixture, per `testing-signal.md`) — production stays strict. The **path resolution** layer in `load_profile` applies `manifest-readers.md`'s `_canonicalise_path` to `<project_dir>/profiles.yml`; `DBT_PROFILES_DIR` and `~/.dbt/profiles.yml` are user-trusted (no symlink gating, but documented). All three resolution paths gracefully handle `FileNotFoundError` and accumulate the searched locations into `ProfileNotFoundError.remediation`. *Why:* `extra="ignore"` on auth-config fields = silent ADC fallback = worst-possible UX; loud failure with remediation is correct. The symlink-hardening matches the precedent set by issue #2's pass-2 review. *How to apply:* `signalforge.warehouse.profiles` exposes `load_profile`, `DbtProfileTarget`, and the typed errors; private `_canonicalise_path` is copied (not imported) from the manifest module to keep subpackages decoupled.
- **DEC-018 — `TableRef.from_model(model)` classmethod gateway** (resolves B6). `TableRef.from_model(model: signalforge.manifest.Model) -> TableRef` lives on `TableRef` in `signalforge.warehouse.models`. Logic: `project = model.database` (raises `ManifestProjectNotFoundError` if `None`), `dataset = model.schema_` (raises `ManifestSchemaNotFoundError` if `None`), `name = model.alias or model.name`. The one-way import (`signalforge.warehouse.models` → `signalforge.manifest`) is acceptable: warehouse depends on manifest's typed shape; manifest is unchanged and unaware. *Why:* prevents ad-hoc `model.database`/`model.schema_`/`model.alias or model.name` translation in every caller (CLI, prune layer, future LLM-drafting layer); centralises the fallback rule. *How to apply:* if circular-import concerns surface, switch to `TYPE_CHECKING`-guarded import + string forward-ref; verify `pyright` accepts.
- **DEC-019 — `WarehouseAdapter.from_profile(profile)` factory classmethod** (resolves B7). On the ABC: `@classmethod def from_profile(cls, profile: DbtProfileTarget) -> WarehouseAdapter`. v0.1 dispatch: `profile.type == "bigquery"` → `BigQueryAdapter(project=profile.project, location=profile.location, max_bytes_billed=profile.maximum_bytes_billed or 100_000_000)`; anything else raises `UnsupportedProfileTypeError(profile_type=profile.type)` with remediation `"v0.1 supports only 'bigquery'; Snowflake/Postgres land in v0.2."`. Direct instantiation (`BigQueryAdapter(project=..., location=...)`) remains supported for tests and explicit-config use. *Why:* CLI and prune layer get a single dispatch point; v0.2 adds a case to one match-statement instead of touching every caller. *How to apply:* the factory lives on the ABC in `base.py`; concrete adapters in `adapters/` are imported lazily inside the factory to avoid forcing import cost when only one adapter is used.
- **DEC-020 — `TestResult.explanation()` method + `row_schema` field** (resolves B8 + C12). `TestResult(passed: bool, failure_count: int, sample_failures: list[dict] | None, row_schema: list[tuple[str, str]] | None)`. Method: `def explanation(self) -> str` returns `"passed"` if `passed`; else `f"{failure_count} rows failed"` and (if `sample_failures`) appends `f" (example: {compact_repr(sample_failures[0], schema=row_schema)})"` where `compact_repr` truncates each value to ≤40 chars and quotes string columns. `row_schema` is a list of `(column_name, bigquery_type)` populated when the adapter has it (always for BQ); `None` for adapters that can't supply it. *Why:* the prune layer gets a deterministic default "why" that respects column types (TIMESTAMP renders as `TIMESTAMP('...')`, not bare ISO string); pruning code can override format but doesn't *need* to. Carries the explainable-diffs commitment all the way to the typed return. *How to apply:* `compact_repr` is a `_test_result_repr.py` private helper; tests assert determinism (same inputs → same string) and per-type rendering.
- **DEC-021 — pytest config edits land in the first implementation story** (resolves B9). `pyproject.toml` `[tool.pytest.ini_options]`: `markers = [...existing..., "bigquery: tests requiring BigQuery credentials (gated by SF_RUN_BQ=1)"]`. `addopts = "-ra --strict-markers -m 'not bigquery'"` (the existing `-ra --strict-markers` plus the new `-m` filter). `strict_markers = true` is already set per the pytest-9 quirk in `testing-signal.md`. *Why:* without these, integration tests fail collection (strict markers) or run by default in CI (no filter). *How to apply:* lands in US-001 alongside the dependency edits; verified by a unit test that uses the marker.
- **DEC-022 — `__repr__` redaction + user-input quoting in error messages** (resolves C3). `BigQueryAdapter.__repr__` returns `f"<BigQueryAdapter project={self._project!r} location={self._location!r}>"` — never includes the credentials object, the underlying `bigquery.Client`, or any token. All warehouse error classes render user-supplied strings via `repr()` in their `__str__` / remediation: e.g. `f"Profile target {target!r} not found in {profile_path}"` (the `{value!r}` pattern). *Why:* a stack trace that reflects an attacker-controlled name into a log viewer should not be able to render special characters; `repr()` quoting also makes whitespace/control characters visible. *How to apply:* baked into the error base class (`WarehouseError._format_value` helper); enforced by a unit test that constructs each error with adversarial input and asserts `repr()`-style escaping in the message.
- **DEC-023 — Soft warning thresholds** (resolves C4). Two soft thresholds, neither hard-fails: (a) `column_stats` accumulated batch >500 columns logs `WARNING` `"Large column_stats batch: {n} columns for {table}; consider splitting"` once per flush. (b) `load_profile` parsing a `profiles.yml` >1 MB logs `WARNING` `"Unusually large profiles.yml ({size_mb} MB); parse may be slow"`. Both thresholds are module-level constants (`_COLUMN_BATCH_WARN_AT = 500`, `_PROFILES_YAML_WARN_AT = 1 * 1024 * 1024`) so tests can patch them. *Why:* signal not volume — these warnings only fire on genuinely unusual cases; no warning fatigue for normal use. *How to apply:* unit tests patch the constants down to (e.g.) 5 columns / 1 KB and assert the warning fires once.
- **DEC-024 — Sampling fail-loud + `num_rows` fallback hierarchy** (resolves C5 + C6). `sample_rows(table, n, *, partition_filter=None)` algorithm: (1) call `get_table(table)`; cache result on `self._table_metadata_cache` keyed by `TableRef`. (2) Compute `num_rows`: if `Table.num_rows` is `None` or `0`, treat as unknown. (3) Decision tree: (a) `num_rows > 0` → bucket = `max(num_rows // n, 1)`, run hash-mod query. (b) `num_rows` unknown AND `partition_filter` provided → bucket = `1000`, log `DEBUG` `"Sampling table with unknown num_rows; using bucket=1000"`, run query. (c) `num_rows` unknown AND no `partition_filter` → raise `UnknownTableSizeError(table)` with remediation `"Provide partition_filter to scope the sample, or call adapter.refresh_table_metadata(table)."`. (d) `num_rows >= 100_000_000` AND no `partition_filter` → raise `SamplingRequiresPartitionFilterError(table, num_rows)` with remediation pointing at `PartitionFilter`. *Why:* fail-loud beats over-spend; the typed errors carry the fix in their remediation. *How to apply:* the `100M` threshold is `_LARGE_TABLE_THRESHOLD = 100_000_000`, patchable by tests.
- **DEC-025 — `column_stats` requires active context; cached `Table` metadata; documented access pattern** (resolves C7 + C8 + C10). Calling `column_stats(table, column)` outside an active `with adapter:` context raises `RuntimeError("column_stats must be called inside a `with adapter:` block")`. Inside the context, calls accumulate per-table; the first read of any returned `ColumnStats` field flushes the batch (lazy). Flush emits `DEBUG` `"Flushed column_stats batch for {table}: {columns}"`. The same `_table_metadata_cache` populated by `sample_rows` (DEC-024) is reused; `__exit__` clears both caches. Docstring example shows the canonical pattern: collect all `ColumnStats` references first, then read fields. *Why:* simpler state machine (one path, not two); `RuntimeError` over a typed warehouse error because this is a programming bug, not a data problem; cache invalidation at `__exit__` is conservative (re-entering re-fetches metadata, which is the safer default). *How to apply:* tests assert RuntimeError outside context; assert one query for two columns inside; assert second `with` block re-fetches metadata.
- **DEC-026 — Typed-exception hierarchy expansion** (resolves C11). `signalforge.warehouse.errors` ships these classes (all subclass `WarehouseError`):
  - `WarehouseError` (base; `default_remediation`, `↳ Remediation:` rendering, mirrors `ManifestError`)
  - `WarehouseAuthError` — wraps `google.auth.exceptions.DefaultCredentialsError` / `RefreshError`
  - `UnsupportedProfileTypeError` — `profile.type` not "bigquery" (v0.1)
  - `UnsupportedAuthMethodError` — `method` not "oauth" / None
  - `ProfileNotFoundError` — none of the three search paths yielded a file
  - `ProfileTargetNotFoundError` — profile resolved but `target` field missing
  - `ManifestProjectNotFoundError` / `ManifestSchemaNotFoundError` — raised by `TableRef.from_model` (DEC-018)
  - `InvalidIdentifierError` — DEC-013 regex failure
  - `BytesBilledExceededError` — wraps the BQ `BadRequest` for max_bytes_billed
  - `TableNotFoundError`, `ColumnNotFoundError` — typed wrappers around BQ 404
  - `QuerySyntaxError` — wraps BQ `BadRequest` for SQL parse errors
  - `SamplingError` (parent), `SamplingRequiresPartitionFilterError`, `UnknownTableSizeError`
  Total: 14 typed subclasses + 1 base. Tests in `tests/warehouse/test_errors.py` mirror `tests/manifest/test_errors.py`: each error renders remediation; `SamplingRequiresPartitionFilterError` catches via `except SamplingError`; `ProfileTargetNotFoundError` catches via `except ProfileNotFoundError`. *Why:* every distinct failure mode users can hit gets a typed exception so the prune/CLI layer can pattern-match without sniffing message text. *How to apply:* errors module is the first implementation story after deps + fixtures; nothing else compiles without it.
- **DEC-027 — `TableRef.project` semantics + logger discipline + ops doc Debugging section** (resolves C13 + C14 + C15 + C16). `TableRef.project: str | None`; `None` documented as "use the BigQuery client's billing project (`bigquery.Client.project`)." `BigQueryAdapter._quote(ref)` resolves `None` to `self._client.project` at quote time. Logger naming: `_LOGGER = logging.getLogger("signalforge.warehouse")` at module top of every file in the subpackage. Levels: `WARNING` for the two soft thresholds (DEC-023) and the rare `getattr` fallbacks; `DEBUG` for batch-flush events and metadata-cache misses; **never** log full SQL or row data. Logging is unstructured (string templates) for v0.1; a comment `# v0.2: refactor to extra={...} for cost-analysis structured logs` lives next to each call site. `docs/warehouse-adapter-ops.md` adds a **Debugging** section: enable `DEBUG` via `logging.getLogger("signalforge.warehouse").setLevel(logging.DEBUG)`, find the BigQuery job ID via `BytesBilledExceededError.job_id` (a field carried on the typed error), the integration-test invocation `SF_RUN_BQ=1 pytest -m bigquery`, and a typed-error reference table cross-linked to `signalforge.warehouse.errors`. *Why:* one place to look when a query misbehaves. *How to apply:* docs change lands in US-012; logger calls are sprinkled through US-008.
- **DEC-028 — `FakeBigQueryClient` assertion-helper API + two missing integration tests** (resolves C17 + C18). `tests/warehouse/_fake.py` exposes:
  ```python
  class FakeBigQueryClient:
      def expect_query(self, *, matching: re.Pattern[str] | str, returns: list[dict] | Exception) -> None: ...
      def expect_get_table(self, *, ref: TableRef, returns: FakeTable | Exception) -> None: ...
      def expect_list_rows(self, *, ref: TableRef, returns: list[dict] | Exception) -> None: ...
      def query(self, sql: str, job_config=None) -> _FakeQueryJob: ...
      def get_table(self, ref) -> FakeTable: ...
      def list_rows(self, ref, max_results=None) -> list[dict]: ...
      def assert_all_expectations_met(self) -> None: ...
  ```
  Each call consumes one matching expectation; unexpected calls raise `AssertionError(f"unexpected query: {sql}")`. `assert_all_expectations_met()` runs in a fixture teardown to catch missing calls. The two new integration tests: (a) `test_int_max_bytes_billed_blocks_oversize_query` queries `bigquery-public-data.crypto_ethereum.transactions` (huge — easily exceeds 100 MB), asserts `BytesBilledExceededError`. (b) `test_int_adc_unconfigured_raises_typed_error` monkeypatches `google.auth.default` to raise `DefaultCredentialsError`, asserts `WarehouseAuthError` with remediation pointing at `gcloud auth application-default login`. *Why:* assertion helpers invert control (tests declare expectations, fake fails loudly on mismatch) — no brittle SQL→dict seeding, no MagicMock auto-pass. The two integration tests cover the cost-cap and auth-failure paths that the unit tests can only mock. *How to apply:* `_fake.py` lands in US-007; the integration tests in US-010.

## Detailed Breakdown

Fourteen stories. Architecture order: deps + config → fixtures → errors → typed models → profiles loader → ABC → fake client → BigQuery impl → unit tests → integration tests → public API → docs → quality gate → patterns. Validation command (run after every story): `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest`.

### US-001 — Deps + pytest markers + addopts

**Description:** Wire `google-cloud-bigquery>=3.20,<4`, `PyYAML>=6,<7`, and `types-PyYAML` (dev) into `pyproject.toml`. Declare the `bigquery` pytest marker and add `-m 'not bigquery'` to `addopts`.

**Traces to:** DEC-010, DEC-021.

**Acceptance criteria:**
- `[project.dependencies]` adds `google-cloud-bigquery>=3.20,<4`, `PyYAML>=6,<7`.
- `[project.optional-dependencies].dev` adds `types-PyYAML`.
- `[tool.pytest.ini_options].markers` adds `"bigquery: tests requiring BigQuery credentials (gated by SF_RUN_BQ=1)"`.
- `[tool.pytest.ini_options].addopts = "-ra --strict-markers -m 'not bigquery'"`.
- `strict_markers = true` already present (verify; do not duplicate).
- Validation command passes.

**Done when:** `pip install -e ".[dev]"` succeeds; `pytest --collect-only` returns the existing test set with no marker errors; `pytest -m bigquery --collect-only` returns 0 tests (none exist yet).

**Files:** `pyproject.toml` (dependencies + pytest config).

**Depends on:** none.

**TDD:** N/A (config-only).

---

### US-002 — Test fixtures: dbt profiles + dbt_project

**Description:** Hand-author the YAML fixtures used by profile-reader tests. No regeneration script — these mirror the dbt-bigquery docs as of dbt 1.9 and are bumped manually.

**Traces to:** DEC-009, DEC-017, DEC-021 (fixtures regen note in `tests/fixtures/README.md`).

**Acceptance criteria:**
- `tests/fixtures/profiles/bigquery_oauth.yml` — minimal valid ADC profile (`type: bigquery`, `method: oauth`, `project`, `dataset`, `location`, `target: dev`).
- `tests/fixtures/profiles/bigquery_service_account.yml` — `method: service-account`, `keyfile: /path/to/key.json` (test fodder for `UnsupportedAuthMethodError`).
- `tests/fixtures/profiles/multi_target.yml` — two targets (`dev`, `prod`) with different projects, to test `target=` override.
- `tests/fixtures/profiles/missing_target.yml` — outputs section omits the named target, to test `ProfileTargetNotFoundError`.
- `tests/fixtures/profiles/dbt_project.yml` — `name: signalforge_test`, `profile: signalforge_test`, no other config.
- `tests/fixtures/profiles/dbt_bigquery_drift_v1_9.yml` — current dbt-bigquery 1.9 profile schema (every documented key) for the strict-model drift test (DEC-017).
- `tests/fixtures/README.md` (existing) updated with a new "Profiles" section: regeneration trigger ("bump when dbt-bigquery releases a new minor"), source URL, hand-author note.

**Done when:** all six YAML files load via `yaml.safe_load` without errors; the README section is added.

**Files:** `tests/fixtures/profiles/*.yml` (new), `tests/fixtures/README.md` (modified).

**Depends on:** US-001.

**TDD:** N/A (fixtures only; consumed by US-009).

---

### US-003 — Errors module

**Description:** Implement `signalforge.warehouse.errors` with the full 15-class hierarchy from DEC-026.

**Traces to:** DEC-026, DEC-022 (user-input quoting), `manifest-readers.md` (remediation pattern).

**Acceptance criteria:**
- `signalforge/warehouse/errors.py` defines `WarehouseError(Exception)` with `default_remediation: ClassVar[str]`, `message`, `remediation` instance attrs, and `__str__` rendering `"{message}\n  ↳ Remediation: {remediation}"`.
- All 14 subclasses listed in DEC-026 implemented with class-level `default_remediation`.
- Each error class accepts and stores its discriminating attributes (e.g. `InvalidIdentifierError(field, value)`, `BytesBilledExceededError(job_id, bytes_billed, limit)`, `UnsupportedProfileTypeError(profile_type)`).
- User-supplied strings rendered via `repr()` in messages (DEC-022): test that `InvalidIdentifierError(field="dataset", value="foo'; DROP")` renders the value as `"\"foo'; DROP\""` or similar repr-quoted form.
- `WarehouseError._format_value(v)` helper centralises the repr-quoting logic.
- `signalforge.warehouse.errors.__all__` lists all 15 classes.
- Validation command passes.

**Done when:** `from signalforge.warehouse.errors import WarehouseError, BytesBilledExceededError, ...` works; `tests/warehouse/test_errors.py` (US-009) all pass.

**Files:** `src/signalforge/warehouse/__init__.py` (skeleton; full re-exports in US-011), `src/signalforge/warehouse/errors.py` (new).

**Depends on:** US-001.

**TDD:**
- `test_warehouse_error_renders_remediation` — base class formatting.
- `test_each_error_default_remediation_set` — every subclass has a non-empty `default_remediation`.
- `test_invalid_identifier_quotes_value` — adversarial input rendered repr-style.
- `test_sampling_subclass_catches_via_parent` — `except SamplingError` catches `SamplingRequiresPartitionFilterError` and `UnknownTableSizeError`.
- `test_profile_target_caught_via_profile_not_found` — `except ProfileNotFoundError` catches `ProfileTargetNotFoundError`.
- `test_bytes_billed_exceeded_carries_job_id` — field set + accessible.

---

### US-004 — Typed models module

**Description:** Implement `signalforge.warehouse.models` with `Dialect`, `TableRef` (incl. `from_model` and identifier validation), `PartitionFilter`, `ColumnStats`, `TestResult` (incl. `explanation()`).

**Traces to:** DEC-003, DEC-004, DEC-013, DEC-014, DEC-016, DEC-018, DEC-020, DEC-027 (TableRef.project semantics).

**Acceptance criteria:**
- `signalforge/warehouse/models.py` defines five public types:
  - `Dialect` — frozen dataclass: `name`, `supports_tablesample`, `supports_qualify`, `quote_char`, `identifier_case`. Module-level `BIGQUERY_DIALECT` constant.
  - `TableRef` — frozen dataclass with `project: str | None`, `dataset: str`, `name: str`. `__post_init__` validates each non-None field via the regex helper (DEC-013). Classmethod `from_model(model: signalforge.manifest.Model) -> TableRef`.
  - `PartitionFilter` — frozen dataclass with `column: str`, `op: Literal[...]`, `value: date | datetime | str`. `__post_init__` validates `column` via regex.
  - `ColumnStats` — frozen Pydantic v2 model: `count`, `distinct`, `nulls`, `min`, `max`, `data_type`. `min`/`max` typed `int | float | str | bool | datetime | date | None`.
  - `TestResult` — frozen Pydantic v2 model: `passed`, `failure_count`, `sample_failures: list[dict] | None`, `row_schema: list[tuple[str, str]] | None`. Method `explanation() -> str` per DEC-020.
- `_sql_safety.py` (private) hosts the identifier regex `_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")` and `_validate_identifier(field: str, value: str) -> None`.
- `_test_result_repr.py` (private) hosts `compact_repr(row: dict, schema: list[tuple[str,str]] | None) -> str` with per-type rendering and 40-char truncation.
- `TableRef.from_model` raises `ManifestProjectNotFoundError` / `ManifestSchemaNotFoundError` if those manifest fields are `None`.
- Validation command passes.

**Done when:** `tests/warehouse/test_models.py` (US-009) all pass.

**Files:** `src/signalforge/warehouse/models.py` (new), `src/signalforge/warehouse/_sql_safety.py` (new), `src/signalforge/warehouse/_test_result_repr.py` (new).

**Depends on:** US-003.

**TDD:**
- `test_bigquery_dialect_constant_frozen` — try mutating, assert `dataclasses.FrozenInstanceError`.
- `test_tableref_rejects_invalid_dataset` — adversarial inputs raise `InvalidIdentifierError`.
- `test_tableref_rejects_invalid_name`, `test_tableref_rejects_invalid_project` — same.
- `test_tableref_accepts_none_project` — None bypasses validation; semantic = "use client default."
- `test_tableref_from_model_happy_path` — manifest model → TableRef with correct fields.
- `test_tableref_from_model_uses_alias_over_name` — `alias="x"` overrides `name="y"`.
- `test_tableref_from_model_raises_when_database_none` — `ManifestProjectNotFoundError`.
- `test_partition_filter_rejects_invalid_column` — adversarial input raises `InvalidIdentifierError`.
- `test_partition_filter_accepts_each_op` — six ops valid; others rejected by `Literal`.
- `test_column_stats_min_max_none_for_geography` — DEC-016 contract: complex types → None.
- `test_test_result_explanation_passed` — returns `"passed"`.
- `test_test_result_explanation_failed_with_no_samples` — returns `"42 rows failed"`.
- `test_test_result_explanation_failed_with_sample` — uses `compact_repr`; respects `row_schema` for TIMESTAMP rendering.
- `test_compact_repr_truncates_long_strings` — values >40 chars are truncated with `...`.
- `test_compact_repr_renders_timestamp_with_type_hint` — uses `row_schema` to render `TIMESTAMP('...')`.

---

### US-005 — Profiles loader module

**Description:** Implement `signalforge.warehouse.profiles` with `DbtProfileTarget` (Pydantic, `extra="forbid"`, auth validator) and `load_profile()` with symlink-hardened path resolution.

**Traces to:** DEC-009, DEC-017, DEC-022, DEC-023.

**Acceptance criteria:**
- `signalforge/warehouse/profiles.py` defines `DbtProfileTarget` (Pydantic v2, `frozen=True, extra="forbid", populate_by_name=True`) with fields: `type`, `method` (validator: only `"oauth"` or `None`), `project`, `dataset` (alias `schema`), `location`, `priority`, `maximum_bytes_billed`.
- `load_profile(project_dir: Path, target: str | None = None) -> DbtProfileTarget`. Resolution: `DBT_PROFILES_DIR` env → `<project_dir>/profiles.yml` (symlink-hardened) → `~/.dbt/profiles.yml`.
- `<project_dir>` path goes through a private `_canonicalise_path` matching `manifest-readers.md`'s pattern (handles symlink loops, escapes via `..`, and the "default" path equally).
- Active target: `target=` arg → profile's `target:` field → `ProfileTargetNotFoundError`.
- File >1 MB triggers `WARNING` log per DEC-023.
- All errors carry remediation; `ProfileNotFoundError.remediation` lists the three searched locations.
- Drift-detector test (US-009) constructs a `StrictModel` (`extra="forbid"` BaseModel) against `dbt_bigquery_drift_v1_9.yml` and validates it loads.
- Validation command passes.

**Done when:** `tests/warehouse/test_profiles.py` (US-009) all pass.

**Files:** `src/signalforge/warehouse/profiles.py` (new), `src/signalforge/warehouse/_path_safety.py` (new — private `_canonicalise_path`).

**Depends on:** US-002, US-003, US-004.

**TDD:**
- `test_load_profile_resolves_dbt_profiles_dir_env`.
- `test_load_profile_resolves_project_root_profiles_yml`.
- `test_load_profile_resolves_home_dot_dbt_fallback`.
- `test_load_profile_target_arg_overrides_default`.
- `test_load_profile_missing_target_raises` — `ProfileTargetNotFoundError`.
- `test_load_profile_no_profiles_yml_anywhere_raises` — `ProfileNotFoundError`; remediation lists three paths.
- `test_load_profile_unsupported_method_raises` — service-account profile triggers `UnsupportedAuthMethodError`.
- `test_load_profile_unknown_field_raises` — extra field triggers Pydantic ValidationError (from `extra="forbid"`).
- `test_load_profile_dataset_alias_schema` — `populate_by_name` accepts both `dataset:` and `schema:` keys.
- `test_load_profile_symlink_to_outside_project_raises` — mirrors issue #2's symlink-loop / escape tests.
- `test_load_profile_warns_on_large_yaml` — patch `_PROFILES_YAML_WARN_AT` low; assert `WARNING` log.
- `test_drift_detector_extra_forbid_dbt_profile_target` — `StrictModel` against `dbt_bigquery_drift_v1_9.yml`.

---

### US-006 — `WarehouseAdapter` ABC + factory

**Description:** Implement `signalforge.warehouse.base` with the abstract base class and `from_profile` factory.

**Traces to:** DEC-019.

**Acceptance criteria:**
- `signalforge/warehouse/base.py` defines `WarehouseAdapter(abc.ABC)` with abstract methods: `dialect`, `sample_rows`, `column_stats`, `run_test_sql`, `__enter__`, `__exit__`.
- Each abstract method has a docstring describing its contract (return type, error conditions, context-manager requirement for `column_stats`).
- `WarehouseAdapter.from_profile(profile: DbtProfileTarget)` classmethod dispatches: `"bigquery"` → lazy-import + return `BigQueryAdapter(...)`; else raise `UnsupportedProfileTypeError`.
- Validation command passes.

**Done when:** `from signalforge.warehouse import WarehouseAdapter` works; instantiating the ABC raises `TypeError`; `from_profile` factory dispatches correctly under unit test (US-009).

**Files:** `src/signalforge/warehouse/base.py` (new).

**Depends on:** US-003, US-004, US-005.

**TDD:**
- `test_warehouse_adapter_is_abstract` — `WarehouseAdapter()` raises `TypeError`.
- `test_from_profile_dispatches_bigquery` — returns a `BigQueryAdapter` instance with fields populated from profile.
- `test_from_profile_raises_for_unknown_type` — `profile.type = "snowflake"` raises `UnsupportedProfileTypeError`.
- `test_from_profile_uses_default_max_bytes_when_unset` — profile without `maximum_bytes_billed` → 100 MB default.
- `test_from_profile_respects_profile_max_bytes_billed` — profile value flows through.

---

### US-007 — `FakeBigQueryClient` test infrastructure

**Description:** Implement the hand-rolled fake with assertion-helper API per DEC-028.

**Traces to:** DEC-002, DEC-028.

**Acceptance criteria:**
- `tests/warehouse/_fake.py` defines `FakeBigQueryClient`, `FakeTable`, `_FakeQueryJob`, `_FakeRowIterator`.
- `expect_query(matching, returns)`, `expect_get_table(ref, returns)`, `expect_list_rows(ref, returns)` register expectations.
- `query/get_table/list_rows` methods consume one matching expectation per call; unexpected calls raise `AssertionError` with the offending input.
- `assert_all_expectations_met()` raises if any expectation went unconsumed.
- `returns=Exception(...)` causes the call to raise (used to simulate `BadRequest`, `Forbidden`, etc.).
- Self-tests in `tests/warehouse/test_fake.py` cover the fake's own behaviour (so regressions in the fake don't masquerade as adapter bugs).
- Not exposed in `signalforge.warehouse.__all__`; importable only via `tests.warehouse._fake`.

**Done when:** `tests/warehouse/test_fake.py` passes; the fake is consumed by US-009 unit tests.

**Files:** `tests/warehouse/_fake.py` (new), `tests/warehouse/test_fake.py` (new).

**Depends on:** US-004 (consumes `TableRef`, `PartitionFilter`).

**TDD:**
- `test_expect_query_match_returns_rows`.
- `test_unexpected_query_raises_assertion_error`.
- `test_expectation_can_return_exception`.
- `test_assert_all_expectations_met_passes_when_consumed`.
- `test_assert_all_expectations_met_fails_when_unconsumed`.
- `test_regex_matching_supports_partial_match`.

---

### US-008 — `BigQueryAdapter` implementation

**Description:** Implement the concrete adapter in `signalforge.warehouse.adapters.bigquery`. Wraps `google-cloud-bigquery` inside `_client.py` to contain pyright noise.

**Traces to:** DEC-001, DEC-005, DEC-006, DEC-007, DEC-008, DEC-013, DEC-014, DEC-015, DEC-016, DEC-020, DEC-022, DEC-023, DEC-024, DEC-025, DEC-027.

**Acceptance criteria:**
- `signalforge/warehouse/adapters/__init__.py` empty marker.
- `signalforge/warehouse/adapters/bigquery.py` defines `BigQueryAdapter(WarehouseAdapter)`.
- `__init__(*, project: str | None = None, location: str | None = None, max_bytes_billed: int = 100_000_000, client: bigquery.Client | None = None)`. `client` parameter is for test injection; production uses `bigquery.Client(project=project, location=location)`.
- `__repr__` redacts credentials per DEC-022.
- Context manager: `__enter__` returns `self` and initialises `_table_metadata_cache: dict[TableRef, FakeTable]` and `_column_stats_pending: dict[TableRef, list[str]]`. `__exit__` flushes pending column_stats batches and clears caches.
- `dialect()` returns `BIGQUERY_DIALECT` constant.
- `_default_job_config(stage: str)` → `QueryJobConfig` with `use_query_cache=False`, `maximum_bytes_billed=<effective>`, `labels={"signalforge_stage": stage, "signalforge_version": __version__}` (DEC-015).
- `_quote(ref: TableRef) -> str` renders `` `project.dataset.name` ``; resolves `project=None` to `self._client.project`.
- `_render_partition_filter(pf: PartitionFilter) -> str` per DEC-014.
- `sample_rows(table, n, *, partition_filter=None)` per DEC-024 algorithm (table cache, num_rows hierarchy, fail-loud thresholds).
- `column_stats(table, column)` enforces active context (DEC-025), accumulates per-table, lazy-flushes on first stat read; emits DEBUG log on flush; warns at 500-column threshold per DEC-023.
- `column_stats` for complex types (GEOGRAPHY/JSON/ARRAY/STRUCT/RANGE/BYTES) skips MIN/MAX in the SQL and sets those fields to None per DEC-016.
- `run_test_sql(sql, *, capture_failures=0)` validates SQL via `_sql_safety.validate_test_sql(sql)` (rejects `;`, top-level `--`, unbalanced parens), wraps as `SELECT COUNT(*) AS failures FROM (<sql>) t` (no capture) or `SELECT COUNT(*) AS failures, ARRAY_AGG(t LIMIT @cap) AS samples FROM (<sql>) t` (capture); returns `TestResult` with `row_schema` populated from query result schema.
- BQ exceptions wrapped: `BadRequest(maximum bytes billed)` → `BytesBilledExceededError`; `BadRequest(syntax)` → `QuerySyntaxError`; `NotFound(table)` → `TableNotFoundError`; `NotFound(column)` → `ColumnNotFoundError`; `Forbidden` → `WarehouseAuthError`; `DefaultCredentialsError`/`RefreshError` → `WarehouseAuthError`.
- All `# pyright: ignore[...]` comments contained to `_client.py`.
- Validation command passes (pyright clean).

**Done when:** unit tests in US-009 all pass.

**Files:** `src/signalforge/warehouse/adapters/__init__.py` (new), `src/signalforge/warehouse/adapters/bigquery.py` (new), `src/signalforge/warehouse/adapters/_client.py` (new — pyright-ignore containment).

**Depends on:** US-006, US-007.

**TDD:** All 22+ unit tests in US-009 listed under `test_bigquery_unit.py`.

---

### US-009 — Unit tests for `signalforge.warehouse`

**Description:** Comprehensive unit-test coverage of the warehouse subpackage using `FakeBigQueryClient`.

**Traces to:** DEC-002, DEC-026, `testing-signal.md`, every other DEC.

**Acceptance criteria:** the following test files exist and pass:

- `tests/warehouse/test_errors.py` (per US-003 TDD list)
- `tests/warehouse/test_models.py` (per US-004 TDD list)
- `tests/warehouse/test_profiles.py` (per US-005 TDD list)
- `tests/warehouse/test_base.py` (per US-006 TDD list)
- `tests/warehouse/test_fake.py` (per US-007 TDD list)
- `tests/warehouse/test_bigquery_unit.py` — covers `BigQueryAdapter`:
  - `test_repr_redacts_credentials`
  - `test_default_max_bytes_billed_is_100mb`
  - `test_per_call_max_bytes_caps_downward_only`
  - `test_dbt_profile_max_bytes_caps_init`
  - `test_query_job_config_use_query_cache_false`
  - `test_query_job_config_labels_set`
  - `test_dialect_returns_bigquery_constant`
  - `test_quote_renders_backtick_form`
  - `test_quote_resolves_none_project_to_client_default`
  - `test_render_partition_filter_timestamp` / `_date` / `_string`
  - `test_sample_rows_uses_farm_fingerprint`
  - `test_sample_rows_includes_partition_filter`
  - `test_sample_rows_unknown_size_no_filter_raises_unknown_table_size`
  - `test_sample_rows_large_unfiltered_raises_sampling_requires_partition`
  - `test_sample_rows_caches_get_table_within_context`
  - `test_column_stats_outside_context_raises_runtime_error`
  - `test_column_stats_batches_within_context` (one query for two columns)
  - `test_column_stats_eager_lazy_flush_on_first_read`
  - `test_column_stats_skips_min_max_for_geography`
  - `test_column_stats_skips_min_max_for_json_array_struct`
  - `test_column_stats_warns_at_500_column_threshold`
  - `test_run_test_sql_wraps_with_count`
  - `test_run_test_sql_capture_uses_array_agg`
  - `test_run_test_sql_passed_when_zero_failures`
  - `test_run_test_sql_rejects_semicolons` / `_unbalanced_parens` / `_double_dash`
  - `test_run_test_sql_populates_row_schema`
  - `test_bytes_billed_exceeded_carries_job_id`
  - `test_query_syntax_error_wraps_bq_bad_request`
  - `test_table_not_found_wraps_bq_not_found`
  - `test_warehouse_auth_error_wraps_default_credentials_error`
  - `test_context_manager_exit_clears_caches`
- `tests/warehouse/test_public_api.py` mirrors `tests/manifest/test_public_api.py` — `__all__` complete, no `_`-prefixed leakage.

Validation command passes; coverage spot-check via `pytest --co | grep -c "tests/warehouse/"` ≥ 80 collected tests.

**Done when:** all the above tests pass; no `assert True`-shaped tests; every test would fail if its target broke.

**Files:** `tests/warehouse/test_errors.py`, `tests/warehouse/test_models.py`, `tests/warehouse/test_profiles.py`, `tests/warehouse/test_base.py`, `tests/warehouse/test_fake.py`, `tests/warehouse/test_bigquery_unit.py`, `tests/warehouse/test_public_api.py` (new), `tests/warehouse/conftest.py` (new — shared `FakeBigQueryClient` fixture, `tmp_profile_dir` fixture).

**Depends on:** US-003 through US-008.

**TDD:** the entire story IS the tests — write each test file alongside (or just before) the corresponding implementation story for true TDD; this story exists to ensure no test is forgotten.

---

### US-010 — Integration tests (gated, maintainer-only)

**Description:** Implement `tests/warehouse/test_bigquery_integration.py` with `@pytest.mark.bigquery` + `@pytest.mark.skipif` belt-and-suspenders.

**Traces to:** DEC-011, DEC-021, DEC-028.

**Acceptance criteria:**
- All tests carry both decorators: `@pytest.mark.bigquery` AND `@pytest.mark.skipif(not os.environ.get("SF_RUN_BQ"), reason="requires SF_RUN_BQ=1 and ADC")`.
- Tests:
  - `test_int_sample_rows_returns_n_rows_from_shakespeare` — public dataset, n=10.
  - `test_int_column_stats_returns_correct_count_for_corpus` — count of `bigquery-public-data.samples.shakespeare`.
  - `test_int_run_test_sql_passes_for_known_clean_query` — `SELECT * FROM ... WHERE FALSE`.
  - `test_int_run_test_sql_fails_for_known_dirty_query` — query that returns rows; assert `failure_count > 0`, `sample_failures` populated when capture > 0.
  - `test_int_max_bytes_billed_blocks_oversize_query` (DEC-028) — `bigquery-public-data.crypto_ethereum.transactions` exceeds 100 MB; assert `BytesBilledExceededError`.
  - `test_int_adc_unconfigured_raises_typed_error` (DEC-028) — monkeypatch `google.auth.default` to raise `DefaultCredentialsError`; assert `WarehouseAuthError` with remediation.
- Default `pytest` run skips them at collection (via `addopts = "-m 'not bigquery'"`).
- `SF_RUN_BQ=1 pytest -m bigquery` runs them all when ADC is configured.
- `CONTRIBUTING.md` adds a "BigQuery integration tests" section: `gcloud auth application-default login`, then `SF_RUN_BQ=1 pytest -m bigquery`.

**Done when:** running locally with ADC, all six tests pass; default `pytest` skips them silently.

**Files:** `tests/warehouse/test_bigquery_integration.py` (new), `CONTRIBUTING.md` (modified).

**Depends on:** US-008.

**TDD:** N/A (these *are* integration tests; mocking them defeats the purpose).

---

### US-011 — Public API re-exports

**Description:** Finalise `signalforge/warehouse/__init__.py` with strict `__all__` listing per DEC-001.

**Traces to:** DEC-001, DEC-017 from #2 (re-export discipline).

**Acceptance criteria:**
- `signalforge/warehouse/__init__.py` re-exports:
  - Function: `load_profile`.
  - Classes: `WarehouseAdapter`, `BigQueryAdapter`, `Dialect`, `TableRef`, `PartitionFilter`, `ColumnStats`, `TestResult`, `DbtProfileTarget`.
  - Constants: `BIGQUERY_DIALECT`.
  - All 15 error classes from DEC-026.
- Module-level docstring lists the public surface (matches `signalforge.manifest`'s pattern).
- `__all__` is a sorted list of exactly the re-exported names.
- `_`-prefixed helpers (`_sql_safety`, `_test_result_repr`, `_path_safety`, `_client`) are reachable via dotted import only; not in `__all__`.
- `tests/warehouse/test_public_api.py` validates: every `__all__` name is bound, none start with `_`, internal helpers absent from package namespace.
- Validation command passes.

**Done when:** `from signalforge.warehouse import WarehouseAdapter, BigQueryAdapter, load_profile, Dialect, TableRef, PartitionFilter, ColumnStats, TestResult, DbtProfileTarget, BIGQUERY_DIALECT, WarehouseError, ...` works for every name in `__all__`.

**Files:** `src/signalforge/warehouse/__init__.py` (modify — currently a skeleton from US-003).

**Depends on:** US-003 through US-008.

**TDD:** the public-API contract test in US-009 covers this story.

---

### US-012 — Documentation: `warehouse-adapter-ops.md` + research/index updates

**Description:** Write `docs/warehouse-adapter-ops.md` per DEC-012 and DEC-027; cross-link from README.

**Traces to:** DEC-012, DEC-027 (Debugging section).

**Acceptance criteria:**
- `docs/warehouse-adapter-ops.md` sections:
  - **Quick start** — `gcloud auth application-default login`, then `from signalforge.warehouse import BigQueryAdapter, load_profile; profile = load_profile(Path("my_dbt_project")); adapter = WarehouseAdapter.from_profile(profile)`.
  - **dbt profile resolution** — env / project / `~/.dbt` order; supported `method`s (`oauth` only in v0.1); deferred auth methods.
  - **Cost defaults** — 100 MB `maximum_bytes_billed`, override knobs, `use_query_cache=False` rationale, BQ job labels.
  - **Sampling strategy** — hash-mod default; the TABLESAMPLE cost-asterisk; `PartitionFilter` use; `SamplingRequiresPartitionFilterError` / `UnknownTableSizeError` triggers.
  - **`column_stats` access pattern** — call inside `with adapter:` block, collect references, then read fields.
  - **Integration tests** — `SF_RUN_BQ=1 pytest -m bigquery`; maintainer-only; public-dataset fixtures.
  - **Debugging** — enable `logging.getLogger("signalforge.warehouse").setLevel(logging.DEBUG)`; reading `BytesBilledExceededError.job_id` to find the BQ job; typed-error reference table.
  - **Error reference** — every typed exception with its trigger, fields, and default remediation.
- `README.md` Configuration section gains a one-paragraph link to the ops doc.
- `docs/research/dbt-research-index.md` (existing) updated with a "Warehouse adapter design" reference back to this plan and the ops doc.

**Done when:** `markdownlint docs/warehouse-adapter-ops.md` passes (if lint exists); a contributor can follow the Quick start to wire up an adapter.

**Files:** `docs/warehouse-adapter-ops.md` (new), `README.md` (modified — Configuration section), `docs/research/dbt-research-index.md` (modified).

**Depends on:** US-008.

**TDD:** N/A (docs).

---

### US-013 — Quality Gate

**Description:** Run code-reviewer four times across the full changeset, fixing all real bugs found each pass; run CodeRabbit if available; ensure validation passes.

**Acceptance criteria:**
- Four passes of code-reviewer agent; each pass's findings either fixed or explicitly accepted with rationale in the plan doc.
- CodeRabbit review (if available) — same disposition.
- `pip install -e ".[dev]" && ruff check . && ruff format --check . && pyright && pytest` passes after every fix.
- No `assert True`-shaped tests anywhere in `tests/warehouse/`.
- All 9 blockers (B1–B9) verifiably resolved by inspection of the code.
- All `_`-prefixed helpers absent from `signalforge.warehouse.__all__`.
- `pytest --collect-only -m bigquery` shows the integration tests; `pytest --collect-only` (default) does not.

**Done when:** four reviewer passes complete with no remaining real bugs; validation green.

**Files:** any file flagged by reviewer with a real bug.

**Depends on:** US-001 through US-012.

**TDD:** N/A.

---

### US-014 — Patterns & Memory (priority 99, always last)

**Description:** Capture patterns learned in this ticket into `.claude/rules/`, `docs/`, or memory.

**Acceptance criteria:**
- New rule file `.claude/rules/warehouse-adapters.md` distilling: ABC + factory pattern, `_client.py` pyright containment, `expect_*` fake-helper API, deterministic-sampling defaults (`use_query_cache=False`, hash-mod, `maximum_bytes_billed`), identifier-validation regex pattern, error-hierarchy expectations for adapter modules.
- Update `.claude/rules/manifest-readers.md` if any of its precedents were extended (e.g. the `_canonicalise_path` pattern is now duplicated in `signalforge.warehouse._path_safety`; consider promoting to a shared helper or noting the duplication is intentional).
- Update `CLAUDE.md` "Public API surface (v0.1)" to include `signalforge.warehouse` once shipped.
- If memory entries about adapter / BigQuery / cost-aware sampling are warranted (per the auto-memory rules), write them to `~/.claude/projects/.../memory/` and update `MEMORY.md`.
- Update `docs/manifest-loader-ops.md` if any cross-reference (`TableRef.from_model`) is now part of the public surface.

**Done when:** rule file lives at `.claude/rules/warehouse-adapters.md`; `CLAUDE.md` reflects the new public surface; the next plan can cite the rule file by path.

**Files:** `.claude/rules/warehouse-adapters.md` (new), `.claude/rules/manifest-readers.md` (possibly modified), `CLAUDE.md` (modified), `docs/manifest-loader-ops.md` (possibly modified).

**Depends on:** US-013.

**TDD:** N/A.

---

### Story dependency graph

```
US-001 (deps + markers)
  ├── US-002 (fixtures)
  ├── US-003 (errors)
  │     ├── US-004 (models)
  │     │     ├── US-005 (profiles loader)  [also depends on US-002, US-003]
  │     │     ├── US-006 (ABC + factory)    [also depends on US-005]
  │     │     │     ├── US-007 (FakeBigQueryClient)
  │     │     │     │     └── US-008 (BigQueryAdapter)
  │     │     │     │           ├── US-009 (unit tests — depends on US-003..US-008)
  │     │     │     │           ├── US-010 (integration tests)
  │     │     │     │           ├── US-011 (public API)
  │     │     │     │           └── US-012 (docs)
  │     │     │     │                 └── US-013 (Quality Gate)
  │     │     │     │                       └── US-014 (Patterns & Memory)
```

### Rules-compliance gate

Every story validated against the four `.claude/rules/*.md`:

- **`python-build.md`** — wheel target unchanged (already covers `src/signalforge`); editable install via `pip install -e ".[dev]"`. ✓
- **`manifest-readers.md`** — Pydantic v2 `frozen=True, extra="ignore"` for **manifest** parsers; `extra="forbid"` for `DbtProfileTarget` is a deliberate divergence (DEC-017) — documented in DEC-017 and the new `warehouse-adapters.md` rule (US-014). Symlink-hardened path canonicalisation duplicated; promotion to shared helper is a US-014 follow-up. ✓
- **`testing-signal.md`** — every test in the test list capable of failing; new `bigquery` marker declared in US-001; `tests/warehouse/__init__.py` is **not** created; drift detector for `DbtProfileTarget` per DEC-017. ✓
- **`ci-supply-chain.md`** — no new GitHub Actions workflows in v0.1 (DEC-011 defers integration-test CI). ✓

## Beads Manifest

Devolved 2026-04-28. Epic + 14 tasks created via `bd create --parent ... --deps ...`. `bd ready` returns US-001 as the only initially-unblocked task; the rest unlock as predecessors close.

- **Epic:** `bd_1-scaffolding-8xk` — *3: BigQuery warehouse adapter* (P2)
- **Worktree:** `/home/wesd/dev/worktrees/SignalForge/feature/3-bigquery-adapter`
- **External ref:** `gh-3` ([#3](https://github.com/wjduenow/SignalForge/issues/3))
- **Plan PR:** [#16](https://github.com/wjduenow/SignalForge/pull/16) (draft)

| Bead ID | Story | Priority | Depends on |
| --- | --- | --- | --- |
| `bd_1-scaffolding-8xk.1` | US-001 — Deps + pytest markers + addopts | P1 | — |
| `bd_1-scaffolding-8xk.2` | US-002 — Test fixtures: dbt profiles | P1 | .1 |
| `bd_1-scaffolding-8xk.3` | US-003 — Errors module | P1 | .1 |
| `bd_1-scaffolding-8xk.4` | US-004 — Typed models module | P1 | .3 |
| `bd_1-scaffolding-8xk.5` | US-005 — Profiles loader module | P1 | .2, .3, .4 |
| `bd_1-scaffolding-8xk.6` | US-006 — WarehouseAdapter ABC + factory | P1 | .5 |
| `bd_1-scaffolding-8xk.7` | US-007 — FakeBigQueryClient test infrastructure | P1 | .4 |
| `bd_1-scaffolding-8xk.8` | US-008 — BigQueryAdapter implementation | P1 | .6, .7 |
| `bd_1-scaffolding-8xk.9` | US-009 — Unit tests for warehouse subpackage | P1 | .3, .4, .5, .6, .7, .8 |
| `bd_1-scaffolding-8xk.10` | US-010 — Integration tests (gated) | P2 | .8 |
| `bd_1-scaffolding-8xk.11` | US-011 — Public API re-exports | P1 | .3, .4, .5, .6, .7, .8 |
| `bd_1-scaffolding-8xk.12` | US-012 — Documentation: warehouse-adapter-ops.md | P2 | .8 |
| `bd_1-scaffolding-8xk.13` | US-013 — Quality Gate | P2 | .1–.12 |
| `bd_1-scaffolding-8xk.14` | US-014 — Patterns & Memory | P4 | .13 |
