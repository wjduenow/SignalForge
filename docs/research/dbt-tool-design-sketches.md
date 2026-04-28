# dbt LLM Tool Design Sketches

Three concrete designs evaluated for viability. Aim: pick one to build.

---

## OPPORTUNITY 1: PR Review Companion (`dbt-pr-companion`)

A GitHub Action that reads a dbt PR, queries the warehouse for impact/cost,
and posts ONE structured comment summarizing what changed, what's at risk,
and what's missing.

### 1. User journey

**Sarah, senior analytics engineer at FinLoop (a 280-person fintech).**
Stack: dbt Cloud, Snowflake, GitHub PRs, Hightouch (reverse ETL to
Salesforce + Braze), Looker. ~620 dbt models, 14 sources, 4 mart
domains. Team of 6 analytics engineers reporting into Sarah; 3 of them
are juniors who joined in the last 8 months.

**Today, on a normal Tuesday:**

A junior opens PR #1247, "fix: refactor `fct_subscription_events` to
handle proration." The PR touches one model. Sarah's review process:

1. Read the SQL diff. The `case` statement got longer; she squints to
   figure out which branch is new.
2. Open `dbt-docs` in another tab to find downstream models. There are
   23 of them. She doesn't read them all — she scans for `mart_*` and
   `dim_customer_facts`.
3. She **forgets** that `fct_subscription_events` is the source for the
   Hightouch sync to Braze. That's not in dbt-docs; it's in a separate
   Hightouch UI nobody opens during code review.
4. She scans for new tests. There are none. She comments "can we add a
   test for the proration logic?" — but she doesn't say *which* test
   shape, so the junior adds `not_null` on a column that's already
   tested.
5. The PR merges. Three days later a Braze segment fires for 12,000
   users who shouldn't have received an upsell email because the new
   proration branch nulled `subscription_tier` for prorated users.

This happens once a quarter. Each time, Sarah's team writes a postmortem
and adds another bullet to the PR review checklist. The checklist is now
17 items long. Nobody follows it.

**With `dbt-pr-companion` installed:**

Sarah opens PR #1247. There's already a comment from `dbt-pr-companion`
posted 90 seconds after the PR opened. It says:

> **Logic change**: The `case` statement for `subscription_status` now
> emits `'prorated'` for the window between `proration_start` and the
> next `billing_cycle_start`. Previously these rows fell through to
> `else null`.
>
> **Downstream blast radius (4 high-risk consumers)**:
> - `dim_customer_facts` — joins on `subscription_status`; the new
>   `'prorated'` value is not in any `accepted_values` test
> - `mart_revenue_daily` — `sum(amount) where subscription_status =
>   'active'` will now exclude prorated rows
> - **Hightouch sync `braze_subscription_segments`** (detected via
>   `meta.hightouch_sync` on the model) — segment definitions reference
>   `subscription_status` IN ('active', 'trial')
> - Looker LookML `subscription_status` dimension — flagged via the
>   Looker Git integration (manual confirmation needed)
>
> **Suggested test (1)**:
> ```yaml
> - name: subscription_status
>   tests:
>     - accepted_values:
>         values: ['active', 'trial', 'cancelled', 'prorated', 'expired']
> ```
> Rationale: the new `'prorated'` enum value silently breaks any
> downstream consumer that filters on `subscription_status`. An
> `accepted_values` test catches future enum drift.
>
> **Cost class change**: NEUTRAL. Estimated bytes-scanned via
> `EXPLAIN`: 2.4 GB → 2.5 GB (+4%). Estimated $ delta on the daily
> run: +$0.08.
>
> **Doc/yaml drift**: ❌ The `subscription_status` column description
> in `schema.yml` still says "active|trial|cancelled|expired". Update
> needed.

Sarah skims the comment in 30 seconds. She approves with one
addition ("can you also update the Hightouch sync's IN clause?"). The
junior fixes it. PR merges. Braze sends the right emails.

**The change**: Sarah's review goes from 12 minutes of tab-switching
context-rebuilding to 2 minutes of validating an LLM-prepared
summary. The 17-item checklist is replaced by a structured comment
that *always runs*.

### 2. Concrete UX

**GitHub Action installation** (one-time):

```yaml
# .github/workflows/dbt-pr-companion.yml
name: dbt-pr-companion
on:
  pull_request:
    paths: ['models/**', 'tests/**', 'macros/**', '**.yml']

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # need base ref for diff
      - uses: dbt-pr-companion/action@v1
        with:
          warehouse: snowflake
          warehouse-creds: ${{ secrets.SNOWFLAKE_DBT_PR_COMPANION }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          # Optional integrations
          hightouch-api-key: ${{ secrets.HIGHTOUCH_API_KEY }}
          looker-git-repo: my-org/looker-config
```

**Configuration** (`.dbt-pr-companion.yml` at repo root):

```yaml
version: 1
profile: prod  # which dbt profile to run --target against
analysis:
  cost_estimation: true
  column_lineage: true
  enum_drift_detection: true
  reverse_etl_integrations:
    - hightouch
  bi_integrations:
    - looker
risk_thresholds:
  # Models tagged 'critical' always get a top-level comment block
  critical_tags: ['mart', 'finance', 'pii']
  # Cost change above this triggers warning
  cost_delta_warning_pct: 25
review_persona: senior  # affects verbosity: senior|junior|verbose
```

**CLI mode** (for local dry-runs before opening a PR):

```bash
$ dbt-pr-companion review --base main
[1/5] Compiling dbt...                                        ✓ 8.3s
[2/5] Diffing manifest.json (base vs HEAD)...                 ✓ 0.4s
      → 1 model changed, 0 added, 0 removed
[3/5] Resolving downstream impact (column-level)...           ✓ 1.1s
      → 23 immediate consumers, 4 high-risk
[4/5] Querying warehouse for cost estimate (EXPLAIN)...       ✓ 2.7s
[5/5] Generating LLM review (claude-sonnet-4-7)...            ✓ 11.4s

=== PR REVIEW DRAFT ===
[same content as PR comment above]

Tokens: 18.4k in / 2.1k out  •  Cost: $0.062  •  Total: 24s
```

**Where it lives**: GitHub Action (primary), with CLI for local
preview. NOT a VSCode extension v1 — the value is in the comment
showing up automatically without anyone remembering to invoke it.

### 3. Technical architecture

**Inputs read at runtime**:

- `target/manifest.json` — built from `dbt parse` on both base and
  HEAD refs. Used for: model graph, column-level lineage (where dbt
  has it), tags, meta fields, descriptions.
- `target/catalog.json` (optional, if `dbt docs generate` cached) —
  warehouse-resolved column types and stats.
- `target/run_results.json` from the most recent prod run (fetched
  from S3 / dbt Cloud API) — used for cost-class baseline.
- Git diff of `models/**/*.sql` and `**/*.yml` — the actual change.
- For each touched model: `dbt compile` to get the rendered SQL.

**Warehouse queries** (all run as a low-privilege role
`DBT_PR_COMPANION_RO`):

- `EXPLAIN <compiled-sql>` for cost estimation. Snowflake:
  `EXPLAIN USING JSON`. BigQuery: `--dry_run`. Redshift: `EXPLAIN`.
  Databricks: `EXPLAIN COST`.
- `INFORMATION_SCHEMA.COLUMNS` query to detect rename/add/drop
  against the model's *currently materialized* shape.
- Optionally: `QUERY_HISTORY` lookup (last 30 days) to estimate how
  often this model is read and by whom — feeds the "blast radius"
  scoring.

NO sample data queries by default. The warehouse never sees row
contents. (See Hard Problems §1 for why this matters.)

**LLM API surface**: Anthropic SDK direct via `claude-sonnet-4-7`.
Single-shot per PR (NOT an agent loop). One call assembles the
comment from a prompt that includes:

- The compiled SQL diff (trimmed to changed regions ±20 lines context)
- Manifest excerpt for the changed model + its 4 highest-risk consumers
- Column-level diff (computed deterministically before the LLM call)
- Cost estimate (computed deterministically)
- A structured-output schema (JSON) that the LLM fills in

The LLM is NOT used as an agent. It's used as a *narrative
generator over a deterministically-prepared input*. This is the key
architectural choice: the impact analysis, cost estimate, and column
diff are all computed by deterministic code that doesn't need an
LLM. The LLM's job is to (a) explain the SQL change in plain
English, (b) suggest tests with rationale, (c) write the prose.

**Output schema** the LLM fills:

```json
{
  "logic_change_summary": "string, 2-4 sentences, plain English",
  "suggested_tests": [
    {"yaml_block": "...", "rationale": "...", "criticality": "high|medium"}
  ],
  "doc_drift_findings": [
    {"file": "...", "current_text": "...", "should_be": "..."}
  ]
}
```

Deterministic blocks (downstream impact, cost class, column diff)
are templated into the comment without going through the LLM at
all.

**Caching**:

- Manifest diff cached by (base_sha, head_sha) tuple → reuse on
  re-runs of the same PR.
- LLM response cached by hash of (compiled SQL diff + manifest
  excerpt + schema version). A force-push that doesn't change
  semantic SQL won't burn another API call.
- Warehouse `EXPLAIN` cached by SQL hash for 24h.

**Cost per invocation**: ~$0.05–$0.15 per PR comment at
`claude-sonnet-4-7` pricing (avg ~20k input, ~2k output). Heavy
PRs touching 5+ models: ~$0.40. A team doing 200 dbt PRs/month:
~$15–$30/month in API costs.

**Idempotency**: comments are upserted (one comment per PR, edited
on each commit) keyed by a hidden HTML marker. Re-runs replace the
prior comment, never spam.

### 4. The hard problems

**Technical risks**:

1. **Column-level lineage is shallow in dbt.** dbt only tracks
   model-to-model lineage natively; column-level requires
   `sqlglot`-based parsing of compiled SQL or paid integrations
   (Datafold has the best engine here). MVP can use sqlglot but
   it'll mis-handle macros and complex CTEs in 5–10% of cases.
2. **Reverse ETL / BI integrations are bespoke per vendor.**
   Hightouch, Census, Polytomic, Looker, Tableau, Mode each need
   custom adapters. v1 picks ONE (Hightouch — best API, biggest
   overlap with dbt users).
3. **Cost estimation accuracy varies by warehouse.** Snowflake's
   `EXPLAIN USING JSON` is decent; Redshift's is misleading;
   BigQuery's `--dry_run` only gives bytes-scanned (no slot-time);
   Databricks needs a Photon-aware estimator. v1: Snowflake +
   BigQuery only.
4. **Compiled SQL needs warehouse credentials at PR time.** Means
   either dbt Cloud API integration OR running `dbt parse`
   (compile-only, no warehouse) in the action. Latter is preferred
   but loses materialization/relation resolution.

**Quality risks**:

1. **Suggested tests that are technically correct but socially
   wrong** — e.g. suggesting a `not_null` test on a column the team
   has explicitly decided can be null for business reasons. Mitigation:
   read existing tests + model `meta.testing_philosophy` field.
2. **Hallucinated downstream consumers.** The deterministic-first
   architecture mitigates this — the impact list comes from manifest
   parsing, not the LLM. The LLM only writes prose around a
   pre-computed list.
3. **Comment fatigue.** If the comment is wrong 1 in 5 times,
   reviewers stop reading. Need a "Was this helpful?" 👍/👎 reaction
   capture and an explicit confidence indicator on the suggested
   tests.

**Adoption risks**:

1. **GitHub permissions.** Posting comments needs `pull-requests:
   write`. Many enterprise GitHub orgs disable third-party Actions
   from posting comments. Workaround: action runs in
   *customer's own Action environment* (no SaaS), uses
   `${{ github.token }}` — no third-party trust needed.
2. **Snowflake credentials in CI.** Some teams flat-out won't put
   prod warehouse creds in GitHub Action secrets. Mitigation: support
   a read-only "review" warehouse account; document the
   minimum-privilege role.
3. **"We already have Datafold."** Datafold's value-diff is
   excellent but expensive ($60k+/yr enterprise). This tool's pitch
   is "Datafold-lite for the 90% of teams that can't justify
   Datafold pricing."

**Maintenance risks**:

1. **manifest.json schema changes between dbt versions.** dbt-core
   1.5 → 1.6 → 1.7 → 1.8 each broke something. Need version-pinned
   adapters (`dbt-artifacts-parser`-style).
2. **Anthropic model deprecations.** Centralized SDK call (a la
   clauditor's `_anthropic.py`) makes upgrades a one-line change.

### 5. Differentiation from incumbents

| Tool | Strength | Where it loses to dbt-pr-companion |
|------|----------|------------------------------------|
| **Datafold** | Best column-level lineage + value diffs in the industry | $60k+/yr; overkill for 90% of teams; doesn't write prose summaries; no test suggestions |
| **Recce** | Open source, great UI for diffs | No GitHub-native PR comments; no LLM-written summaries; requires a separate UI |
| **dbt Copilot (Cloud)** | Native integration, autocomplete | Authoring-time, not review-time; no PR comment; dbt Cloud only |
| **Paradime AI** | Solid IDE assistant | IDE-bound, not CI; doesn't ground in your manifest |
| **GitHub Copilot Workspace** | Generic PR review | No dbt-awareness; no warehouse cost estimate; no Hightouch detection |

**The honest case for incumbents winning**: Datafold wins on
enterprise teams that need data-diff-on-every-PR and have the
budget. Recce wins on teams that prefer a self-hosted UI for
exploration. dbt-pr-companion wins on the long tail of mid-market
teams (50–500 employees, 200–2000 dbt models) who need *something
better than a checklist* and don't have $5k/month to spend.

### 6. Ship-in-a-weekend MVP

**In scope (3 days):**

- GitHub Action that runs on PR open/sync
- `dbt parse` to produce manifest, diff against base manifest
- Compiled SQL diff for changed models (`dbt compile`)
- Deterministic downstream-consumer list (model-level only, not
  column-level)
- Single `claude-sonnet-4-7` call producing logic-change summary +
  test suggestions
- Markdown comment posted via GitHub API
- Snowflake-only cost estimate via `EXPLAIN`
- Caching by PR SHA

**Out of scope:**

- Hightouch / Looker integrations
- Column-level lineage (model-level is the demo)
- BigQuery / Redshift / Databricks
- Doc-drift detection (v1.1)
- Confidence scoring / 👍👎 capture (v1.2)

The MVP gives you a screenshot for the launch tweet. That's enough.

### 7. Path-to-100-users

1. **Build it on a real OSS dbt project first** (e.g. `dbt-labs/jaffle_shop`,
   or contribute it to a fast-moving OSS dbt project that has PRs to
   demo against). Get screenshots of real comments.
2. **Show, don't tell, on dbt Slack** — `#tools-and-integrations`
   channel. Post a 60-second screen recording of a real PR comment.
3. **Coalesce 2026 lightning talk submission.** "How we replaced our
   17-item PR checklist with one LLM comment."
4. **Listing on Awesome dbt + dbt package hub** (the action wraps a
   `dbt-pr-companion` package).
5. **Targeted outreach to 20 mid-market data teams** via LinkedIn —
   teams between 100 and 1000 employees that have public dbt repos
   on GitHub. Offer to install for free in exchange for a quote.
6. **Pricing**: free OSS for solo / hobby / public repos. Paid
   ($199/mo per 10 contributors) for private repos with the SaaS
   features (BI integration adapters, cost analytics dashboard,
   👍👎 telemetry).

OSS-first because the value is *seeing the comment on a real PR*.
You can't gate the demo behind a sales call.

### 8. 12-month evolution

- **v1 (month 0–2)**: GitHub Action, Snowflake, model-level impact,
  single LLM call, free OSS.
- **v1.5 (month 3–4)**: Column-level lineage via sqlglot. BigQuery
  support. Doc-drift detection. First paid tier.
- **v2 (month 6–8)**: Hightouch + Looker integrations. Historical
  cost-trend dashboard. Slack integration ("PR #1247 looks risky,
  want to ping the data team?"). $499/mo per team plan.
- **v2.5 (month 9–12)**: "PR companion" expands to "release
  companion" — same engine reviews dbt model deploys to prod, posts
  to Slack with a daily/weekly digest of risk patterns.

**Moat that compounds**: the warehouse query history + 👍👎
telemetry. After 6 months you know which test suggestions actually
got accepted, which downstream warnings actually correlated with
production incidents. That feedback loop is unobtainable from a
cold start.

### 9. Why-not-build-this

- **Datafold has been doing column-level lineage for 5 years and
  has the engine.** They could add an LLM summary in a sprint and
  bundle it into existing contracts. The bear case: Datafold ships
  this exact feature in Q2 2026 and crushes the OSS option.
- **dbt Labs has the manifest, the Cloud product, and a roadmap.**
  dbt Cloud's "Explorer" already does column lineage. Adding a PR
  comment is two engineers and a quarter.
- **GitHub Copilot Workspace is going to swallow generic PR
  review.** A GH-native PR review feature with claude-sonnet
  underneath is plausible by end of 2026.
- **The Hightouch / Looker integration story is gnarly.** Each
  vendor has its own auth, schema, rate limits. Building 6
  integrations is a year of work.

What kills it: dbt Labs ships PR review in dbt Cloud and bundles
it free. Realistic mitigation: stay open-source-first, dbt-Cloud-
agnostic (works with dbt-core too), and own the
"non-dbt-Cloud half of the market" segment.

---

## OPPORTUNITY 2: Schema/Tests/Docs Generator (`dbt-yaml-forge`)

LLM drafts `schema.yml` + tests + docs for a model, samples warehouse
data, runs candidate tests against real data, drops always-pass /
uninformative tests, emits only signal-bearing artifacts. Each
artifact graded by an LLM rubric for usefulness before being kept.

### 1. User journey

**Marcus, staff data engineer at HelmCorp (a 1,400-person logistics
company).** Stack: dbt-core (self-hosted), BigQuery, Airflow, GitHub.
~1,800 models inherited from a 4-year-old project. Test coverage
on `dim_*` and `fct_*` is decent. Test coverage on `stg_*` and
`int_*` (which together are 60% of models) is *abysmal* — most have
zero tests, no descriptions, no column docs.

**Today, on a normal sprint:**

His team has been told by leadership "improve data quality." Marcus
spends 2 hours writing a `schema.yml` for `int_orders_unioned`.
The model has 47 columns. He:

1. Eyeballs the SQL to figure out what each column means
2. Adds `not_null` to columns that *seem* like they shouldn't be
   null
3. Adds `unique` to `order_id` because it's an `int_` model and
   "should be" unique
4. Half the tests fail on the next CI run because his eyeballing
   was wrong (3 columns are nullable on Tuesdays due to a known
   pipeline race; `order_id` is unique within `(order_id,
   order_version)` not standalone)
5. He spends another hour debugging false positives
6. He never gets around to the other 28 `int_*` models on his list
7. After 3 sprints, leadership stops asking about "data quality"
   and the project dies

**With `dbt-yaml-forge`:**

```bash
$ dbt-yaml-forge propose --model int_orders_unioned
[1/6] Compiling model...                                      ✓
[2/6] Sampling 5,000 rows from materialized table...          ✓ 1.4s
[3/6] Profiling columns (null %, distinct %, ranges)...       ✓ 0.8s
[4/6] LLM drafting schema.yml + 18 candidate tests...         ✓ 14.2s
[5/6] Running 18 candidate tests against real data...
        ✓ unique: (order_id, order_version)        — discriminating
        ✗ not_null: order_id                       — keep (1 fail)
        ✗ not_null: shipped_at                     — DROP (50% null,
                                                    informative null)
        ✓ accepted_values: status (...)             — discriminating
        ✓ relationships: customer_id → ...          — discriminating
        ✗ not_null: warehouse_code                 — DROP (always passes,
                                                    uninformative)
        ... (18 total: 11 kept, 7 dropped)
[6/6] LLM rubric grading kept artifacts (signal score)...     ✓ 8.1s
        Avg signal score: 7.2/10  (threshold: 6.0)
        2 column docs flagged low-signal — rewriting...

Wrote: models/intermediate/_int_orders_unioned__schema.yml
       (47 columns documented, 11 tests, est. coverage 78%)

Tokens: 31.4k in / 4.8k out  •  Cost: $0.18  •  Total: 28s
```

The generated `schema.yml` is checked into the PR. Marcus reviews
it — it took him 4 minutes instead of 2 hours, and the tests are
better than what he would have written.

**The change**: Marcus's team can document and test 28 models in a
sprint instead of 1. The "data quality initiative" actually finishes.

### 2. Concrete UX

**CLI is the primary surface** (this is a developer tool, not a CI
tool):

```bash
# Single model
dbt-yaml-forge propose --model int_orders_unioned

# Whole directory
dbt-yaml-forge propose --select intermediate

# All untested models
dbt-yaml-forge propose --untested

# Dry-run (no warehouse queries, no LLM calls)
dbt-yaml-forge propose --model X --dry-run

# Re-grade existing schema.yml (no regeneration)
dbt-yaml-forge audit --model X
```

**Configuration** (`.dbt-yaml-forge.yml`):

```yaml
version: 1
sample:
  rows: 5000
  strategy: random  # random | recent | stratified
  pii_columns_pattern: "(email|ssn|phone|address|name)$"
  pii_handling: hash  # hash | redact | exclude
test_acceptance:
  min_failure_rate: 0.001  # tests must fail on >0.1% of rows to be "discriminating"
  max_failure_rate: 0.5    # tests that fail >50% are signal-less
rubric:
  min_score: 6.0  # below this, regenerate
  max_attempts: 3
generation:
  test_types_enabled:
    - unique
    - not_null
    - accepted_values
    - relationships
    - dbt_utils.expression_is_true
  doc_style: terse  # terse | descriptive
```

**Generated YAML** (excerpt):

```yaml
version: 2
models:
  - name: int_orders_unioned
    description: |
      Union of orders from the legacy `orders_v1` source and the new
      `orders_v2` source, with version reconciliation. One row per
      `(order_id, order_version)` — orders can have multiple
      versions when amended post-checkout.
    columns:
      - name: order_id
        description: Stable order identifier across both source systems.
        # NOTE: not unique alone — see (order_id, order_version) test below
      - name: order_version
        description: |
          Monotonically increasing version per `order_id`. Starts at 1.
          Incremented when a customer amends shipping address or
          adds/removes line items pre-shipment.
      - name: status
        description: Order lifecycle state.
        tests:
          - accepted_values:
              values: ['pending', 'confirmed', 'shipped', 'delivered',
                       'cancelled', 'returned']
              # auto-discovered from sample data; please verify
    tests:
      - dbt_utils.unique_combination_of_columns:
          combination_of_columns: ['order_id', 'order_version']
      - relationships:
          to: ref('stg_customers')
          field: customer_id
          column_name: customer_id
```

Each generated test carries a comment: `# auto-discovered from sample
data; please verify` — explicit about provenance.

**Where it lives**: CLI tool, distributed as a dbt package +
Python CLI. NOT a GitHub Action (yet). The reason: schema/test
generation is a deliberate authoring action, not a passive review.
The author *chooses* when to invoke it.

### 3. Technical architecture

**Inputs**:

- `target/manifest.json` — model graph, existing tests, existing
  descriptions (so we don't overwrite)
- `target/catalog.json` — column types, relations
- Compiled SQL via `dbt compile`
- A sample of warehouse rows (deterministic seed)
- Source SQL of upstream `ref()`'d models (for context)

**Warehouse queries**:

- `SELECT * FROM {{ this }} TABLESAMPLE (...) LIMIT N` — sample
  for profiling
- `SELECT COUNT(*), COUNT(col), COUNT(DISTINCT col), MIN(col),
  MAX(col) FROM {{ this }} GROUP BY col` style profiling per column
- For each candidate test: a `dbt test --select <model> --store-failures`
  invocation in a sandboxed schema

**LLM API**:

- **Anthropic SDK direct**, single-shot per phase (NOT an agent).
- Phase 1 — Drafter (Sonnet): given compiled SQL + column profile,
  produce candidate `schema.yml` + candidate tests
- Phase 2 — Test execution: deterministic, no LLM
- Phase 3 — Grader (Haiku for cost — runs once per artifact):
  rubric-grade each kept test/description against criteria like
  "is this description specific enough to disambiguate from
  similarly-named columns elsewhere?", "does this test catch a
  realistic failure mode?"
- Phase 4 — Regenerator (Sonnet, only if avg score < threshold):
  rewrite low-scoring artifacts with critique included

**This is where clauditor synergy is enormous.** The grader phase
is *exactly* what clauditor does today — a per-artifact LLM-graded
rubric with thresholds and regeneration. `dbt-yaml-forge`'s grader
literally calls a `clauditor`-style harness:

```python
# inside dbt-yaml-forge's grader phase
from clauditor.quality_grader import grade_quality
from clauditor.schemas import EvalSpec

eval_spec = EvalSpec.from_dict({
    "skill_name": "yaml-forge-test-quality",
    "grading_criteria": [
        {"id": "specificity", "criterion": "..."},
        {"id": "non-trivial", "criterion": "..."},
        {"id": "actionable-on-failure", "criterion": "..."},
    ],
    # ...
}, spec_dir=...)

report = await grade_quality(eval_spec, candidate_test_yaml)
if report.pass_rate < 0.6:
    # regenerate
```

**Grounding/context**:

- Sample data is *profiled* (statistics) not *passed verbatim* to
  the LLM in v1 — keeps PII out of the prompt.
- Optional `--allow-sample-rows N` for non-PII columns (whitelist
  in config).
- The compiled SQL is the primary context (~ 5–50 lines per
  model). Manifest excerpts are pruned to direct upstream/downstream.

**Caching**:

- Per model + manifest version (`{model_name}@{manifest_hash}`).
- Cache invalidated when SQL changes or upstream schema changes.

**Idempotency**: re-running on a model with existing `schema.yml`
*augments*, never overwrites. Existing descriptions are preserved
unless `--overwrite` is passed.

**Cost per invocation**: $0.10–$0.30 per model. A team running
forge on 50 models: ~$10. A one-time backfill across 1,000 models:
~$200.

### 4. The hard problems

**Technical risks**:

1. **Sampling without leaking PII.** Even profiling can leak (a
   `MIN(email)` reveals the alphabetically-first email). Need a
   PII detector + redactor pipeline before any data hits the LLM.
   Mitigation: `pii_columns_pattern` config + a hardcoded blocklist
   of common PII patterns + a `--no-warehouse-data` mode that
   relies only on schema + SQL.
2. **Test execution in a sandboxed schema.** Can't pollute
   prod. Need dbt's `--target` + a writable analyst schema, or
   `dry-run` test execution (some tests support it).
3. **Always-pass test detection requires actually running the
   tests.** This means warehouse compute on every forge invocation.
   Costs add up for big teams.
4. **`dbt_utils` and other macros require dbt project context.**
   Forge needs to know which packages are installed before suggesting
   `dbt_utils.expression_is_true`.

**Quality risks**:

1. **Tests that pass on sample but fail on full table.** A 5k-row
   sample of a 5B-row table might miss the one rogue NULL.
   Mitigation: profile across the full table (cheap aggregation),
   only sample rows for context.
2. **Generated descriptions that are "plausible but wrong".** The
   LLM sees `customer_id BIGINT NOT NULL` and writes "Unique
   identifier for the customer." But maybe in this model
   `customer_id` is the *billing* customer, not the *shipping*
   customer. Mitigation: rubric grades on "specific enough to
   disambiguate"; force regeneration on generic descriptions.
3. **Suggested tests become a wall of noise.** If forge suggests 18
   tests per model and devs accept all, the test suite slows down
   and tests stop being read. The "drop always-pass /
   uninformative" pruning is the load-bearing quality move.
4. **Rubric calibration drifts.** Same problem clauditor solves.
   Same solution: a small held-out set of "known good" /
   "known bad" YAML examples that the rubric must score correctly,
   verified on every release.

**Adoption risks**:

1. **"I don't trust LLM-generated tests."** The provenance comment
   on each test (`# auto-discovered from sample data; please
   verify`) is a partial answer. Better answer: ship a "show me
   your work" mode that prints *why* each test was kept.
2. **Warehouse access at authoring time.** Many devs work locally
   without prod warehouse access. Need a `--warehouse-snapshot`
   mode that can run against a pre-captured profile JSON.
3. **YAML conflicts with hand-edits.** If a user hand-edits the
   YAML and re-runs forge, what happens? Need explicit merge
   semantics + a `--preserve-existing` default.

**Maintenance risks**:

1. **dbt test API changes between versions.** Same as opportunity 1.
2. **PII detection regex maintenance** — a never-ending arms race.

### 5. Differentiation from incumbents

| Tool | Strength | Where it loses |
|------|----------|----------------|
| **dbt-codegen** (OSS) | Generates boilerplate `schema.yml` skeleton from a model | No tests, no descriptions, no quality grading; produces literally `description: ""` |
| **dbt Copilot (Cloud)** | Inline suggestions in IDE | dbt Cloud only; doesn't run candidate tests against data; no rubric scoring |
| **Paradime AI Schema Generator** | Decent inline generator | No "drop always-pass" pruning; no rubric grading; closed source |
| **dbt-checkpoint pre-commit hooks** | Enforces missing tests/docs | Doesn't *generate* them — just nags |
| **Manually-written schema.yml** | Always correct (when correct) | Slow, inconsistent, last-priority |

**Where incumbents win**: dbt-codegen wins for teams that just want
the skeleton and prefer to write tests by hand. dbt Cloud Copilot
wins for dbt Cloud customers who don't want another tool. forge
wins on teams with large untested model bases that need a
high-quality automated pass.

### 6. Ship-in-a-weekend MVP

**In scope (3 days):**

- CLI `dbt-yaml-forge propose --model <name>`
- Snowflake + BigQuery (one each, simplest profiling queries)
- Phase 1 (drafter, Sonnet) + Phase 2 (test execution, real
  warehouse)
- Drop-always-pass + drop-always-fail pruning
- Output: `_<model>__schema.yml` file
- PII detection: regex against column names only (`email`,
  `phone`, `ssn`, `name`, `address`)

**Out of scope:**

- Phase 3 rubric grading (uses simple heuristic in v1: tests must
  have failure rate in [0.001, 0.5])
- Phase 4 regeneration loop
- Description quality grading (descriptions are emitted but not
  scored)
- Backfill mode (`--select intermediate` for many models)
- Custom test types beyond the dbt builtins + dbt_utils

The MVP demo: pick a real OSS dbt project, run forge on a model
with no tests, show the generated YAML and how the dropped tests
were the right ones to drop.

### 7. Path-to-100-users

1. **Make it a dbt package** so installation is one line in
   `packages.yml`. dbt package hub listing.
2. **Demo on `jaffle_shop` and `dbt_artifacts`** — well-known OSS
   dbt projects. PRs against them showing forge's output.
3. **dbt Slack `#i-made-this`** post with a 90-second screen
   recording.
4. **Coalesce talk submission**: "How we documented and tested
   1,200 models in a week."
5. **Targeted blog post**: "An LLM wrote 600 dbt tests — only
   142 were worth keeping." (The pruning story IS the story.)
6. **Pricing**: free OSS for the CLI. Paid SaaS ($299/mo) for
   teams who want a hosted dashboard showing test coverage trends,
   PII compliance audit log, and a Slack bot for nightly forge
   runs against new models.

### 8. 12-month evolution

- **v1 (month 0–2)**: CLI, single-model, Snowflake + BigQuery,
  basic pruning.
- **v1.5 (month 3–4)**: Rubric grading via clauditor integration.
  Multi-model `--select` mode. Backfill report ("forge would
  improve coverage from 23% → 71% if you accept these 412
  generated tests").
- **v2 (month 5–8)**: Source freshness inference, custom test
  generation (e.g. business-rule tests from natural-language
  descriptions in `meta.business_rules`). Hosted SaaS dashboard.
- **v2.5 (month 9–12)**: Cross-model invariant detection ("these
  3 mart models all reference `customer_id`; forge inferred a
  shared `dim_customers` ref"). Continuous coverage
  monitoring with weekly digest.

**Moat that compounds**: a *corpus of human-graded test quality
labels*. After 6 months you have thousands of "this generated test
was kept / dropped / edited" decisions. That data trains a much
better grader than anyone else can build cold.

### 9. Why-not-build-this

- **dbt Labs has the model context AND the YAML schema.** They can
  build this *and* charge dbt Cloud customers for it. Bear case:
  dbt Cloud ships "Auto-document & test" feature in Q3 2026.
- **Quality is hard to demonstrate without real-world adoption.**
  The whole pitch is "the tests forge generates are *good*" — but
  proving that requires a public benchmark suite or a year of
  case studies.
- **Warehouse-write access is a real friction.** Test execution
  needs writable schema. Many enterprise teams won't grant it to a
  third-party tool.
- **The "always-pass test pruning" idea is novel-seeming but
  contested.** Some teams want documentation tests
  (`not_null` on every PK column even if it always passes —
  proves the assumption is checked). The pruning needs to be
  configurable, not opinionated.

---

## OPPORTUNITY 3: Stored-Proc → dbt Migration Agent (`procmigrate`)

Open-source LLM agent that parses procedural SQL (T-SQL stored procs,
PL/pgSQL functions, BigQuery procedures), clusters logic into
staging/intermediate/mart layers, emits dbt models with `ref()`s, and
generates `audit_helper`-style row-count/value-diff tests so the
migration is verifiable against the source. Iterates until parity.

### 1. User journey

**Priya, lead data architect at MidWestern Insurance Co. (a
6,200-person insurer founded 1924).** Stack: SQL Server (still),
SSIS pipelines, 1,400 stored procedures spanning 280k lines of
T-SQL. They bought Snowflake 18 months ago. The directive: "be
on dbt + Snowflake by end of 2026." They have one analytics
engineer who knows dbt (Priya) and three SQL Server DBAs who
know T-SQL but have never written a dbt model.

**Today, on a normal Wednesday:**

Priya is migrating `usp_calculate_monthly_premium_adjustments` — a
1,400-line stored procedure that:
- declares 23 temp tables
- has 4 nested cursors
- calls 6 other stored procs
- runs on the 1st of each month and writes to 8 tables

She:

1. Spends 2 days reading the proc to understand it
2. Manually traces the data flow on a whiteboard
3. Identifies what *should* be staging vs intermediate vs mart
4. Writes 14 dbt models translating the logic
5. Spends a week debugging row-count differences ("why does my dbt
   version produce 12,847 rows when the proc produces 12,851?")
6. Discovers the proc has an undocumented `WHERE policy_status !=
   'ARCHIVED' AND created_date >= '2018-01-01'` filter buried on
   line 982 that her dbt models missed
7. Migrates this one proc in 3 weeks
8. Has 1,399 procs left
9. Realizes the project will take 80 person-years at this rate

**With `procmigrate`:**

```bash
$ procmigrate convert \
    --source-sql ./sqlserver_procs/usp_calculate_monthly_premium_adjustments.sql \
    --target-project ./dbt_project \
    --source-warehouse "sqlserver://prod-readonly" \
    --target-warehouse "snowflake://migration-sandbox" \
    --layer-strategy auto

[1/8] Parsing T-SQL with sqlglot (sqlserver dialect)...      ✓ 0.7s
[2/8] Building dataflow graph (23 temp tables, 6 sub-proc calls)... ✓
[3/8] LLM clustering logic into dbt layers (12 turns)...     ✓ 67s
        Proposed: 4 stg_, 6 int_, 3 fct_ models
[4/8] Generating dbt models with ref()s...                   ✓ 8s
[5/8] Running source proc against migration sandbox...       ✓ 142s
        → 12,851 rows in 8 output tables
[6/8] Running generated dbt models against same sandbox...   ✓ 89s
        → 12,847 rows in 8 output tables (DELTA: 4)
[7/8] LLM iterating on parity (audit_helper diffs)...
        Round 1: 4 row delta in fct_premium_adjustments
                 → identified missing filter: policy_status != 'ARCHIVED'
                 → patched int_active_policies model
        Round 2: 0 row delta. ✓ PARITY ACHIEVED.
[8/8] Generating audit_helper tests + dbt docs...            ✓ 12s

Wrote: models/staging/insurance/stg_policies__active.sql (+ 12 more)
       tests/audit/test_usp_calculate_monthly_premium_adjustments.sql
       analyses/migration_notes/usp_calculate_..._notes.md

Tokens: 184k in / 38k out  •  Cost: $4.20  •  Wall time: 5m 32s
```

**The change**: Priya goes from 3 weeks per proc to 5 minutes
(automated) + 1–2 hours (human review of the generated dbt). The
80 person-year estimate becomes 6–9 months. The project actually
ships.

This is the **highest-pain, highest-stakes, highest-budget** of
the three opportunities. Insurance/banking/healthcare teams will
literally pay $50k–$500k for this.

### 2. Concrete UX

**CLI** (primary):

```bash
# Single proc
procmigrate convert --source-sql proc.sql --target-project ./dbt

# Batch (the real use case)
procmigrate convert --source-dir ./all_procs --target-project ./dbt \
                    --parallelism 4

# Audit-only mode (re-verify parity for already-migrated procs)
procmigrate audit --target-project ./dbt --source-warehouse ...

# Plan-only (no warehouse, no LLM execution)
procmigrate plan --source-sql proc.sql
```

**Configuration** (`procmigrate.yml`):

```yaml
version: 1
source:
  dialect: sqlserver  # sqlserver | postgres | bigquery | oracle
  connection: ${SOURCE_WAREHOUSE_URL}
  read_only: true  # safety
target:
  dbt_project_dir: ./dbt_project
  dialect: snowflake
  layer_naming:
    staging_prefix: stg_
    intermediate_prefix: int_
    mart_prefix: fct_
parity:
  sample_size: 10000  # rows to compare for value diff
  row_count_tolerance: 0  # 0 = exact match required
  value_diff_tolerance: 0.001  # 0.1% per numeric column
  max_iterations: 5
agent:
  model: claude-sonnet-4-7  # or claude-opus-4-7 for hard procs
  max_turns: 30
  human_review_required: true  # never commit without --yes
```

**Generated artifacts** for each proc:

```
models/
  staging/insurance/stg_policies__active.sql
  staging/insurance/_stg_policies__schema.yml
  intermediate/int_active_policies_with_premiums.sql
  intermediate/int_premium_adjustments_calculated.sql
  marts/insurance/fct_premium_adjustments.sql
tests/audit/
  test_usp_calculate_monthly_premium_adjustments_row_counts.sql
  test_usp_calculate_monthly_premium_adjustments_value_diff.sql
analyses/migration_notes/
  usp_calculate_monthly_premium_adjustments.md  # LLM-written
                                                   migration commentary
```

**Migration notes excerpt**:

> ## `usp_calculate_monthly_premium_adjustments` — migration notes
>
> **Original**: 1,402 lines T-SQL, 23 temp tables, 4 cursors.
>
> **Translated to**: 13 dbt models across stg_/int_/fct_ layers.
>
> **Key transformations**:
> 1. Cursor on lines 412–478 (per-policy iteration) → `INNER JOIN`
>    in `int_premium_adjustments_calculated`.
> 2. Temp table `#archived_policies` (line 89) → CTE inlined, no
>    materialization needed (used only once).
> 3. Sub-proc call to `usp_get_policy_status` (line 612) replaced
>    with `ref('stg_policies__active')`.
>
> **Behavioral notes for reviewer**:
> - The original proc had a filter `WHERE policy_status !=
>   'ARCHIVED'` on line 982 that wasn't in the docstring. We
>   preserved it in `int_active_policies`. Confirm this filter is
>   still desired.
> - The cursor in lines 1100–1180 implemented a custom rounding
>   strategy (`ROUND(x, 2, 1)` — banker's rounding). We translated
>   to Snowflake's default rounding; **verify financial impact**.

**Where it lives**: CLI tool. Optionally a hosted SaaS for the
"managed migration" use case (insurance company doesn't want to
run this themselves; they want a contractor to deliver migrated
dbt + parity proof).

### 3. Technical architecture

**Inputs**:

- Source SQL files (the procs)
- Source warehouse connection (read-only, for parity testing)
- Target dbt project structure (to know naming conventions, package
  list, existing models)
- Target warehouse connection (for parity testing — must be
  isolated/sandbox)

**Parsing**:

- `sqlglot` for parse-and-translate (SQL Server → Snowflake dialect)
- Custom AST walkers for:
  - Temp table dataflow → "this temp table feeds these 3 later
    queries" → candidate for an intermediate model
  - Cursor patterns → "this is iteration; can it be set-based?" →
    candidate for a JOIN
  - Sub-proc calls → "what does this proc return?" → candidate
    for a `ref()`

**LLM API**: this is the ONE opportunity that genuinely needs an
**agent loop**, not a single shot. The shape:

- **Claude Agent SDK** (or Claude Code subprocess) — multi-turn,
  with tools.
- Tools the agent has access to:
  - `read_sql_file(path)` — read source SQL
  - `query_source_warehouse(sql)` — run a SELECT (read-only)
  - `query_target_warehouse(sql)` — run against migration sandbox
  - `write_dbt_model(name, sql, layer)` — write a `.sql` file
  - `dbt_compile(model)` — compile and check for errors
  - `dbt_run(model)` — materialize in target sandbox
  - `audit_helper_compare(source_table, target_model)` — row count
    + value diff
  - `read_existing_model(name)` — for `ref()` resolution
- The agent runs ~8–30 turns per proc:
  1. Parse + understand the proc (1–3 turns)
  2. Propose a layer breakdown (1 turn)
  3. Write each model (5–15 turns, one per model)
  4. Run parity check (1 turn)
  5. Iterate on diffs (1–10 turns)
  6. Generate tests + notes (1–2 turns)

**Why this needs an agent and the others don't**: the iteration
loop is *intrinsic*. You don't know what the right `WHERE` clause
is until you see the row count diff. You don't know the temp
table is unused until you see the dataflow. The agent's
turn-by-turn back-and-forth with the warehouse IS the value.

**Grounding**:

- Source warehouse provides **ground truth** — agent can always
  verify "does this column exist?", "how many distinct values?",
  "what's the range?"
- `audit_helper` (the dbt package) provides the comparison
  primitives.
- Migration-sandbox isolation: target warehouse writes go to a
  schema named `procmigrate_sandbox_<run_id>` — never touch prod.

**Caching**:

- Per-proc cache keyed by source SQL hash + dbt project version
- Re-runs that hit cache: $0, ~5 seconds (just verifies parity
  again)

**Cost per invocation**: $2–$15 per medium proc (1k–3k lines).
$20–$50 for monsters (5k+ lines, deep iteration). A team
migrating 1,400 procs: **~$10k–$30k in API costs total**, vs.
80 person-years of T-SQL labor.

### 4. The hard problems

**Technical risks**:

1. **T-SQL → Snowflake SQL has nasty edge cases.** Cursors,
   temp tables, dynamic SQL, `MERGE` with non-deterministic
   `WHEN MATCHED`, recursive CTEs with ordering. sqlglot covers
   80%; the other 20% needs LLM repair.
2. **Procs that mutate the same table multiple times in a single
   transaction don't translate cleanly to dbt's set-based
   model.** Need to detect these patterns and either (a) refuse to
   translate and flag for human, or (b) translate to a sequence of
   incremental models with explicit `unique_key`.
3. **Parity proof requires running the original proc.** If the
   proc has side effects (writes to multiple tables, sends emails,
   calls REST APIs from T-SQL), running it in a "sandbox" is
   non-trivial. Need to detect side-effecting procs and fall back
   to a different verification strategy.
4. **Long-running procs** that take 4 hours on prod can't be
   run in a tight iteration loop. Need a "snapshot the source
   tables once, run dbt against the snapshot, compare to a stored
   reference output" mode.

**Quality risks**:

1. **"Parity" is fuzzy.** Floating point diffs, ordering diffs in
   array-aggregates, NULL-vs-empty-string. The tolerance config
   helps, but a proc that produces 99.97% identical output might
   still have a critical 0.03% bug.
2. **The LLM might find a bug in the *original* proc** and "fix"
   it in the dbt translation, breaking parity. Need to surface
   "we believe the original is wrong because X" explicitly,
   never silently fix.
3. **Generated dbt models that compile but are unreadable.** A
   1,400-line proc translating to 13 dbt models can still produce
   each individual model that's a 200-line CTE chain. Need a
   secondary "readability" pass.

**Adoption risks**:

1. **Trust gap.** Migrating financial/insurance/healthcare procs
   with an LLM is *terrifying* to compliance teams. The audit
   trail (every diff, every iteration, every LLM decision) is the
   load-bearing feature for adoption. Without it, this tool is
   dead in the regulated-industry segment that needs it most.
2. **"Why not just rewrite by hand?"** Some teams will. The pitch
   has to be cost: $30k of API + 6 months of human review beats
   80 person-years.
3. **Source warehouse access is sensitive.** SQL Server, Oracle,
   on-prem warehouses often live behind firewalls. Need a
   self-hosted mode (the OSS CLI works fully offline given the
   credentials).

**Maintenance risks**:

1. **sqlglot dialect coverage.** Each warehouse dialect has its
   own quirks. sqlglot maintainers are great but not infinite.
2. **dbt package versions** (audit_helper, dbt_utils) churn.
3. **Agent loop reliability.** 30-turn agents can derail. Need
   tight per-turn budgets, recovery from tool errors, and a
   max-cost circuit breaker.

### 5. Differentiation from incumbents

| Tool | Strength | Where it loses |
|------|----------|----------------|
| **Datafold Migration Agent** | Recent product, exact same use case, well-resourced | Closed source, expensive (~$200k-$500k engagements), enterprise-sales-gated, slow procurement |
| **AWS SCT (Schema Conversion Tool)** | Free, official AWS | Translates DDL well, terrible at procedural code, no dbt awareness |
| **Manual rewrite by consulting firm** | High quality (when consultants are good) | $500k–$5M, 12–24 months, 50% chance of failure |
| **In-house team** | Owns the result | Years of work, attrition risk, deep domain knowledge required |
| **dbt Labs (no offering today)** | Could build it tomorrow | Hasn't yet; would likely be Cloud-only |

**Honest assessment**: Datafold's Migration Agent is the direct
competitor and has a 12+ month head start. The OSS angle is the
defensible position — Datafold won't open-source theirs. The
mid-market segment ($1k–$10k procs to migrate, can't afford
Datafold's $200k engagement) is the wedge.

### 6. Ship-in-a-weekend MVP

**This is the hardest one to MVP** because the value
*requires* end-to-end parity proof. A skeleton without parity
verification is just "another LLM that writes SQL."

**In scope (3 days):**

- CLI `procmigrate convert --source-sql <one-proc>`
- T-SQL → Snowflake dialect translation via sqlglot only
  (no LLM repair of edge cases yet)
- Naive layer detection: every temp table → intermediate model;
  every final INSERT → mart model
- Single-shot agent loop (max 5 turns) with a hardcoded set of
  tools
- `audit_helper` row-count comparison (no value diff yet)
- Output: dbt models + a markdown report
- Snowflake target only

**Out of scope:**

- Multi-proc batch mode
- Source dialects other than T-SQL
- Cursor-to-set-based translation (just flag and ask human to
  rewrite)
- Value diff (row count is the v1 parity proof)
- Migration notes generation (just a stub markdown)

The MVP demo: take a real-world OSS T-SQL proc (Microsoft's
sample AdventureWorks or Northwind DB), migrate it, show the
parity check passing.

### 7. Path-to-100-users

This one is *not* a 100-user OSS tool. It's a 10-customer paid
product. Different distribution playbook:

1. **Build a public demo** on AdventureWorks. Blog the
   transcript: "We migrated `dbo.uspGetEmployeeManagers` to dbt
   in 4 minutes."
2. **Cold outreach to data leaders at insurance/banking/healthcare
   companies on LinkedIn.** Specifically titles like "VP of Data
   Engineering" at companies in their dbt-migration RFP phase.
3. **Listing on dbt package hub** as `procmigrate` — the audit
   tests use audit_helper, so it gets visibility there.
4. **Coalesce talk**: "We migrated 1,400 stored procs in 6 months
   with an LLM agent." (Find a willing customer, do it for free,
   present the case study.)
5. **Pricing**: free OSS CLI for the engine. Paid SaaS for:
   (a) hosted multi-proc dashboard with parity audit log
   ($2k/mo), (b) "white-glove migration" managed service
   ($50k–$200k per project — bring in real consultants for the
   human review).

This is a sales-led motion, not a viral OSS motion. 10 customers
at $50k each is $500k ARR — bigger than 1000 users at $10/mo.

### 8. 12-month evolution

- **v1 (month 0–3)**: T-SQL → Snowflake, single proc, parity
  proof. CLI only.
- **v1.5 (month 3–6)**: PL/pgSQL + BigQuery procedures. Batch
  mode. Hosted audit dashboard.
- **v2 (month 6–9)**: Oracle PL/SQL (the biggest legacy market —
  insurers and banks). White-glove managed service launches.
- **v2.5 (month 9–12)**: SSIS package translation (the *other*
  half of the legacy SQL Server world).

**Moat that compounds**: the *parity-test corpus*. After 50
migrations you have hundreds of patterns of "the LLM tried X, it
failed parity, the fix was Y." That training data makes round-2
migrations 10x faster than round-1 for any new customer.

### 9. Why-not-build-this

- **Datafold has Migration Agent and is shipping it in
  enterprise deals NOW.** They have a 12-month head start, a
  sales team, and reference customers.
- **The bear case is brutal**: regulated-industry teams will pay
  Datafold or a consultancy because they need someone to *blame*
  if it goes wrong. An OSS tool can't be blamed.
- **Sales cycles in insurance/banking are 9–18 months.** A solo
  developer can't fund the runway needed.
- **Per-customer support burden is enormous.** Each customer's
  T-SQL has unique patterns. The first 5 customers will eat a
  full developer's time forever.
- **The prize is huge but the field is brutal.** Everyone in
  data tooling sees this opportunity; many will try.

What kills it: Datafold open-sources their migration engine to
neutralize the OSS angle, OR dbt Labs ships a "Migrate from SQL
Server" wizard in dbt Cloud and bundles it with existing contracts.

---

## Comparison: which opportunity should the user build?

| Dimension | PR Companion | YAML Forge | ProcMigrate |
|---|---|---|---|
| **Underservedness** | 7/10 (Datafold/Recce serve top + DIY long tail) | 8/10 (codegen exists, quality grading doesn't) | 6/10 (Datafold + consultancies, but premium-only) |
| **Pain severity** | 7/10 (annoying, occasional incidents) | 6/10 (chronic underinvestment, rarely acute) | 10/10 (mission-blocking, $millions on the line) |
| **Tech feasibility (1 dev)** | 9/10 (deterministic core + thin LLM) | 7/10 (warehouse sandbox + PII + grader) | 4/10 (agent loop + parity + dialects) |
| **Distribution clarity** | 9/10 (Action + dbt Slack + hub) | 8/10 (package hub + Slack + hands-on devs) | 4/10 (sales-led, long cycles) |
| **Defensibility / moat** | 6/10 (telemetry compounds; replicable) | 7/10 (rubric calibration data compounds) | 8/10 (parity corpus + regulated trust) |
| **Synergy with clauditor** | 5/10 (light — could grade the comments) | **10/10 (perfect fit — rubric grading IS clauditor)** | 6/10 (could grade migration notes) |
| **TOTAL** | **43/60** | **46/60** | **38/60** |

### Recommendation: **Build YAML Forge (Opportunity 2).**

Three reasons, in order of weight:

1. **Synergy with clauditor is unmatched.** The Phase 3 grader is
   *literally* the clauditor harness with a different rubric.
   You'd be building one product that strengthens your existing
   product rather than two unrelated codebases. Every improvement
   to clauditor's grading (better rubrics, faster Haiku graders,
   tier semantics) directly improves YAML Forge. Every dollar of
   YAML Forge revenue subsidizes clauditor R&D. The two products
   share an `_anthropic.py`, share an eval-spec schema, share a
   testing methodology. This is the *only* opportunity where
   "build YAML Forge" effectively means "build clauditor v2 with
   a vertical wedge."

2. **Feasibility for one developer is genuinely there.** PR
   Companion is also feasible, but its core value (impact analysis,
   cost estimation, BI integrations) is mostly *not* LLM work — it's
   integration plumbing. A solo dev can ship the LLM part in a
   weekend but the integrations are a year. YAML Forge's core value
   IS the LLM-graded generation, which is what you already know how
   to build well. ProcMigrate is brutal for one person.

3. **The "drop always-pass tests" pruning is a defensible idea
   that nobody else is talking about.** Every other YAML
   generator (codegen, Copilot, Paradime) emits everything and
   leaves curation to the human. The forge premise — "an LLM
   wrote 600 tests, only 142 are worth keeping, here's why" —
   is a *story*. Stories drive adoption. PR Companion's story
   is "another PR review tool"; ProcMigrate's story is "Datafold
   but cheaper." Forge's story is unique.

**The honest tradeoff**: Forge has the lowest pain severity of
the three (chronic underinvestment in tests is a nag, not a
crisis). Pain severity drives urgency, which drives willingness
to pay. Forge's monetization will be slower than ProcMigrate's
and its user count will grow slower than PR Companion's. But the
clauditor synergy is decisive — build it as a vertical
demonstration of clauditor's grading methodology applied to a
real-world artifact class, and you compound your existing work
rather than dilute it.

**Second-best pick**: PR Companion. Build it after Forge proves
the clauditor-as-grading-engine pattern works in production. By
month 6 you'll have grading infrastructure mature enough to plug
into PR Companion's "suggested test rationale" grading. That's
the year-2 expansion path.

**Don't build**: ProcMigrate. The TAM is real and the dollars
are large, but it's a sales-led, capital-intensive product that
requires a team. It's the right business for Datafold, not for a
solo developer with an existing OSS project to grow.
