# Test catalogue (what SignalForge generates)

> Single source of truth for the test shapes SignalForge's drafter proposes
> and the prune + grade pipeline evaluates.

These are the test shapes SignalForge auto-generates against your dbt models.
Each first-class primitive carries structural slots the drafter targets, an
ingest signature so `prune-existing` recognises hand-authored equivalents,
and rubric coverage so kept artifacts ship with a calibrated "why." The
catch-all `custom_sql` variant covers business rules the structured primitives
can't express. The "What we do NOT generate today" section names the shapes
SignalForge explicitly does not propose — useful for evaluating whether the
catalogue covers your project's patterns.

## The seven first-class primitives

| Variant | dbt equivalent | Scope | Structural slots | Semantics |
| --- | --- | --- | --- | --- |
| `not_null` | dbt generic test | column | — | Column has no NULL rows |
| `unique` | dbt generic test | column | — | Column has no duplicate values |
| `accepted_values` | dbt generic test | column | `values: list` | Column values are all in a declared set |
| `relationships` | dbt generic test | column | `to`, `field` | Every value matches a row in a parent table |
| `custom_sql` | singular test (`tests/*.sql`) | column or model | `sql` | Free-form business-rule SELECT (catch-all) |
| `row_count_between` | `dbt_expectations.expect_table_row_count_to_be_between` | model | `minimum`, `maximum`, optional `where` | Row count is within bounds |
| `unique_combination` | `dbt_utils.unique_combination_of_columns` | model | `columns` (≥2), optional `where` | Composite tuple of columns is unique |

### `not_null`

```yaml
models:
  - name: stg_bikeshare_trips
    columns:
      - name: trip_id
        tests:
          - not_null
```

The drafter proposes `not_null` for columns that the source SQL or the model's
SELECT body guarantees populated (primary keys, monotonic timestamps, columns
the source declares `NOT NULL`). Prune drops it as `always-passes` when zero
NULL rows surface in the warehouse sample.

### `unique`

```yaml
columns:
  - name: trip_id
    tests:
      - unique
```

The drafter proposes `unique` for columns it identifies as a model's grain
(natural keys, surrogate IDs, declared primary keys). Prune evaluates via
`GROUP BY <col> HAVING COUNT(*) > 1`.

### `accepted_values`

```yaml
columns:
  - name: subscriber_type
    tests:
      - accepted_values:
          values: ['member', 'casual', 'unknown']
```

The drafter proposes `accepted_values` for low-cardinality categorical columns
whose domain can be enumerated from sampled data or column comments. Prune
evaluates via `WHERE col IS NOT NULL AND col NOT IN (...)`.

### `relationships`

```yaml
columns:
  - name: start_station_id
    tests:
      - relationships:
          to: ref('stg_bikeshare_stations')
          field: station_id
```

The drafter proposes `relationships` for foreign-key columns identifiable from
SELECT joins, dbt `ref()` calls in the model SQL, or column-name conventions
(`*_id` matching a sibling model's grain). Prune evaluates via child→parent
LEFT JOIN, flagging child rows whose parent reference is absent.

### `custom_sql` — the catch-all

```yaml
# meta.signalforge.business_rules on the model:
config:
  meta:
    signalforge:
      business_rules: "total_amount must never be negative"
```

```sql
-- drafted as tests/dim_orders__total_amount_custom_sql_a1b2c3d4.sql
-- signalforge:generated a1b2c3d4
select * from {{ this }} where total_amount < 0
```

`custom_sql` is a full singular-test SELECT — the LLM translates a plain-English
business rule (declared via `meta.signalforge.business_rules`) or an inferred
checkable invariant from the model SQL into a failing-rows SELECT. The SELECT
may reference `{{ this }}`, `{{ ref('m') }}`, and `{{ source('s','t') }}`;
control-flow Jinja is rejected loudly. Multi-table rules (a JOIN) run full-scan
within the warehouse `maximum_bytes_billed` cap rather than against a sample.

**`custom_sql` is the catch-all — graded less structurally than the six
first-class primitives.** The grader scores it ad-hoc against the rubric
criteria (clarity, consistency, rationale, no-redundant), but the rubric has
no per-primitive calibration for it — operators get the same kept / dropped
outcome from the prune step, with rationale-based grade scoring rather than
the structured calibration the bounded variants enjoy. Reach for one of the
six first-class primitives when your rule fits one of their shapes; reach for
`custom_sql` when none of them do.

See [`docs/draft-ops.md` § Custom business-rule tests](draft-ops.md#custom-business-rule-tests-custom_sql)
for the full drafting path; [`docs/prune-ops.md` § `custom_sql` evaluation](prune-ops.md#custom_sql-evaluation)
for the prune cost shape; [`docs/ingest-ops.md`](ingest-ops.md) for how
`prune-existing` ingests hand-authored singular tests.

### `row_count_between`

```yaml
models:
  - name: daily_revenue_rollup
    tests:
      - dbt_expectations.expect_table_row_count_to_be_between:
          min_value: 1
          max_value: 100000
          where: "event_date >= '2024-01-01'"
```

`row_count_between` is a **model-level bounded-cardinality assertion**: the
table's `COUNT(*)` (optionally narrowed by `where`) must lie within
`[minimum, maximum]`. Either bound may be `None`; at least one must be set.
The drafter proposes it for bounded aggregations (a `GROUP BY` with a date
window), daily/weekly rollups where a sudden empty day is a real signal,
and monitoring-shaped reports. The prune engine evaluates via a failing-rows
CTE wrapping a single `COUNT(*)` — cheap even on petabyte tables.

**Sample-mode behaviour — engine routes past the materialised sample.** A
`COUNT(*)` against a sampled relation returns the *sample size*, not the
model's true row count, so a sample-mode verdict would be semantically
wrong by construction. The prune engine's per-test loop routes
`row_count_between` past the materialised-sample substitution back to the
source table under both `prune.scope="sample"` strategies (`materialised`
or `oneshot`). The same source-vs-temp pattern applies to
`unique_combination` below (issue #170 extended both conditionals in
lockstep). See [`docs/prune-ops.md` § `row_count_between`](prune-ops.md#row_count_between)
for the engine routing.

See [`docs/draft-ops.md` § Row-count tests](draft-ops.md#row-count-tests-row_count_between)
for drafting; [`docs/prune-ops.md` § Row-count cost model](prune-ops.md#row-count-cost-model)
for evaluation; [`docs/ingest-ops.md` § Recognition of `expect_table_row_count_to_be_between`](ingest-ops.md#recognition-of-expect_table_row_count_to_be_between)
for `prune-existing` recognition; [`docs/grade-ops.md` § Row-count calibration](grade-ops.md#row-count-calibration)
for grain-meaningfulness grading.

### `unique_combination`

```yaml
models:
  - name: fct_order_line_items
    tests:
      - dbt_utils.unique_combination_of_columns:
          combination_of_columns:
            - order_id
            - line_item_id
```

```yaml
# with optional where filter:
models:
  - name: dim_user_daily_active
    tests:
      - dbt_utils.unique_combination_of_columns:
          combination_of_columns:
            - user_id
            - day
          where: "is_active = true"
```

`unique_combination` is a **model-level composite-key uniqueness assertion**:
the tuple `(columns[0], columns[1], ...)` must be unique across the model
(optionally narrowed by `where`). The `columns` array must carry **at least
two columns** — single-column uniqueness is the existing `unique` test type.
The drafter proposes it for composite-key grains (`(order_id, line_item_id)`
on an order-line table, `(user_id, day)` on a daily activity rollup,
`(start_station_id, end_station_id, trip_date)` on a trip-pairs aggregate).
The prune engine evaluates via `GROUP BY <cols> HAVING COUNT(*) > 1` — the
same shuffle cost as single-column `unique`.

**Vacuously-unique tuples are dropped or flagged.** A tuple like
`(primary_key, anything)` is always unique by construction (the primary key
alone guarantees it) and adds no signal beyond the existing single-column
`unique` test — the drafter prompt steers away from these, the grade rubric's
`no-redundant` criterion scores them low (typically `0.0`–`0.2`, routing
the artifact to **`flagged`**), and the prune engine catches the strict
cases as `always-passes`.

**Sample-mode behaviour — engine routes past the materialised sample.** A
sampled `GROUP BY` is semantically approximate (uniqueness violations in the
full table may not surface in the sample → false-negative). `prune_tests`
therefore overrides the table reference to the **source table** for every
`unique_combination` candidate, regardless of `sample_strategy`
(`materialised` or `oneshot`). Mirrors the `row_count_between` metadata-bypass
pattern.

See [`docs/draft-ops.md` § Composite uniqueness](draft-ops.md#composite-uniqueness-unique_combination)
for drafting; [`docs/prune-ops.md` § `unique_combination`](prune-ops.md#unique_combination--same-engine-routing-group-by-shape-issue-170)
for the engine routing (same source-vs-temp pattern as `row_count_between`);
[`docs/ingest-ops.md` § Recognition of `dbt_utils.unique_combination_of_columns`](ingest-ops.md#recognition-of-dbt_utilsunique_combination_of_columns)
for `prune-existing` recognition; [`docs/grade-ops.md` § Row-count calibration](grade-ops.md#row-count-calibration)
for grain-meaningfulness grading (the `no-redundant` criterion extends to
`unique_combination` calibration too).

## What we do NOT generate today

SignalForge's catalogue is deliberately bounded — these shapes are tractable
to draft, prune against real warehouse data, and grade against a structured
rubric. The following classes of tests are NOT proposed today:

- **Column value range / statistical thresholds.** No
  `dbt_utils.expression_is_true` for numeric-range assertions, no
  `dbt_expectations.expect_column_values_to_be_between` for column-level
  bounds. These require operator-supplied semantic context the LLM can't
  reliably infer from the SELECT body.
- **Conditional uniqueness beyond a simple `where`.** `unique_combination`
  carries an optional `where` filter, but multi-clause / dynamic-condition
  uniqueness (e.g. "unique per user, unless the row is a soft-delete") is
  out of scope — express these as `custom_sql`.
- **Statistical / distributional anomalies.** No mean / stddev / outlier
  detection, no `dbt_expectations.expect_column_mean_to_be_between` family.
  These need a baseline the drafter doesn't compute.
- **Cross-table reconciliation.** No sum / count parity checks across two
  tables (a `relationships` test catches referential integrity, not
  aggregate equivalence). Express these as `custom_sql` with the join in
  the SELECT body.
- **Time-series anomaly detection.** No "consecutive nulls > N",
  "period-over-period change > X%", or freshness-without-`dbt source freshness`
  assertions. These need temporal context the drafter doesn't model.

Hand-authored `custom_sql` (a singular `tests/*.sql` file) covers any of the
above when the rule is checkable against a single SELECT. SignalForge's
`prune-existing --tests-dir` reads and prunes these alongside `schema.yml`
tests.

## References

- [`docs/draft-ops.md`](draft-ops.md) — Drafter operations: prompt structure,
  per-variant drafting heuristics, the cache-stability snapshot, the
  `exclude_tests` short-circuit.
- [`docs/prune-ops.md`](prune-ops.md) — Prune operations: per-variant SQL
  shapes, sample-mode behaviour, drop-reason taxonomy.
- [`docs/grade-ops.md`](grade-ops.md) — Grade operations: the four-criterion
  rubric, per-variant calibration prose (`no-redundant` covers
  `row_count_between` numeric bounds and `unique_combination` grain
  meaningfulness).
- [`docs/ingest-ops.md`](ingest-ops.md) — Ingest operations: which dbt test
  shapes `prune-existing` recognises (the four built-ins plus
  `expect_table_row_count_to_be_between` and `unique_combination_of_columns`),
  which it skip-records.
- [`.claude/rules/business-rule-tests.md`](https://github.com/wjduenow/SignalForge/blob/dev/.claude/rules/business-rule-tests.md)
  — The variant-extension pattern (the n-instance precedent for adding a
  new `CandidateTest` discriminated-union variant).
- Issue series: [#116](https://github.com/wjduenow/SignalForge/issues/116)
  established `custom_sql`; [#169](https://github.com/wjduenow/SignalForge/issues/169)
  added `row_count_between`;
  [#170](https://github.com/wjduenow/SignalForge/issues/170) added
  `unique_combination` and authored this catalogue.
