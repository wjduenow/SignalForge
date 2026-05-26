# dbt + Claude: Technical Surface for 2026

Reference material for an implementer building Claude-powered dbt tooling. Focuses on artifact schemas, MCP server surface, SDK options, warehouse integration, CI shapes, and token economics. Not narrative — scan-and-grep oriented.

> Companion to `docs/temp/dbt-tooling-opportunity-report.md` (product framing). This doc is the "what do I actually have to integrate against" answer.

---

## 1. dbt artifact deep dive

All artifacts land in `target/` after a dbt invocation. Their schemas are versioned at `https://schemas.getdbt.com/dbt/<artifact>/<version>/index.json`.

### 1.1 manifest.json — the project graph

The single most important artifact. Contains a complete representation of every node in the project plus parent/child dependency maps.

**Version-to-dbt-version mapping:**

| dbt Core version | Manifest schema | Notes |
| --- | --- | --- |
| 1.5 | v9 | |
| 1.6 | v10 | |
| 1.7 | v11 | |
| 1.8 | v12 | First "stable" version for many integrations |
| 1.9 / 1.10 / 1.11 | v12 | No schema bump |
| Fusion (v2.0) | v20 | "Identical to v12" per docs — same shape, different namespace |

Fusion's v20 means **manifest-shape compatibility is preserved across the Core/Fusion split**, but Fusion-only fields (e.g. column-level lineage from static analysis) appear as additive properties. Core ≠ Fusion at the *engine* level even when manifests look the same; cross-environment Recce-style diffs break if you mix engines.

**Top-level keys** (v12):

```jsonc
{
  "metadata": {
    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
    "dbt_version": "1.11.6",
    "generated_at": "2026-04-24T...",
    "invocation_id": "uuid",
    "project_id": "hash",
    "project_name": "my_project",
    "user_id": "uuid",
    "adapter_type": "snowflake",
    "env": {},
    "send_anonymous_usage_stats": true
  },
  "nodes": { /* models, seeds, snapshots, tests, analyses, operations, hooks */ },
  "sources": { /* source definitions from sources.yml */ },
  "macros": { /* every macro, including from packages */ },
  "docs": { /* doc blocks */ },
  "exposures": {},
  "metrics": {},      // v1.6+ MetricFlow
  "groups": {},       // v1.5+
  "selectors": {},
  "disabled": {},
  "parent_map": { "model.x.foo": ["source.x.bar"], ... },
  "child_map": { "source.x.bar": ["model.x.foo"], ... },
  "group_map": {},
  "saved_queries": {},      // v1.7+
  "semantic_models": {},    // v1.6+
  "unit_tests": {}          // v1.8+
}
```

**Per-node fields that matter for an LLM tool:**

```jsonc
"model.my_project.dim_users": {
  "resource_type": "model",
  "unique_id": "model.my_project.dim_users",
  "name": "dim_users",
  "fqn": ["my_project", "marts", "dim_users"],
  "database": "ANALYTICS",
  "schema": "MARTS",
  "alias": "dim_users",
  "package_name": "my_project",
  "path": "marts/dim_users.sql",
  "original_file_path": "models/marts/dim_users.sql",
  "checksum": { "name": "sha256", "checksum": "..." },
  "config": {
    "materialized": "table",
    "on_schema_change": "fail",
    "contract": { "enforced": true, "alias_types": true },
    "pre-hook": [], "post-hook": [],
    "tags": ["pii"], "meta": {}, "grants": {}
  },
  "tags": ["pii"],
  "description": "...",
  "columns": {
    "user_id": {
      "name": "user_id",
      "data_type": "varchar(36)",
      "description": "...",
      "constraints": [{"type": "not_null"}, {"type": "unique"}],
      "meta": {}, "tags": []
    }
  },
  "depends_on": {
    "macros": ["macro.dbt.statement"],
    "nodes": ["source.my_project.raw.users"]
  },
  "refs": [{"name": "stg_users", "package": null, "version": null}],
  "sources": [["raw", "users"]],
  "compiled": true,
  "compiled_code": "select ...",   // post-Jinja, pre-warehouse
  "raw_code": "{{ config(...) }} select ...",
  "language": "sql",
  "access": "protected",   // v1.5+ public/private/protected
  "version": null,         // v1.5+ model versioning
  "latest_version": null,
  "constraints": [],       // table-level constraints
  "deprecation_date": null,
  "primary_key": ["user_id"]
}
```

**Parsing gotchas:**

- `manifest.json` is a snapshot; always run `dbt parse` (cheap, no warehouse) or `dbt compile` (more expensive, renders Jinja) before reading.
- `compiled_code` is `null` until compilation has run. After `dbt parse`, only `raw_code` is populated.
- `nodes` is **flat** — every resource type lives in this dict, distinguished by `resource_type`. Tests, seeds, snapshots, models, analyses, operations, and hooks all collide on key prefix (`model.x.y`, `test.x.y`, `seed.x.y`, `snapshot.x.y`, `analysis.x.y`).
- `disabled` is a parallel dict — disabled nodes do NOT appear in `nodes`. If you need full project visibility, walk both.
- `parent_map` / `child_map` are pre-computed by dbt; do NOT rebuild from `depends_on.nodes` (you'll miss source/macro edges).
- Source code: [`dbt-labs/dbt-core/core/dbt/contracts/graph/manifest.py`](https://github.com/dbt-labs/dbt-core/blob/main/core/dbt/contracts/graph/manifest.py) defines `WritableManifest`.
- Schema: [`https://schemas.getdbt.com/dbt/manifest/v12.json`](https://schemas.getdbt.com/dbt/manifest/v12.json).

**Size in the wild:**

| Project size | Models | Approx manifest.json size | Approx Claude tokens |
| --- | --- | --- | --- |
| Small | ~100 | 500 KB – 2 MB | 100k – 400k |
| Medium | ~500 | 5 – 15 MB | 1M – 3M |
| Large | ~2000 | 30 – 100+ MB | 6M – 20M+ |

A medium-to-large manifest **does not fit in a 1M context window** raw. Strategies: (a) pre-extract a subset to relevant `unique_id`s, (b) stream summary records via tool calls, (c) feed only `parent_map`/`child_map` for graph reasoning then fetch node details on demand.

### 1.2 catalog.json — warehouse reality

Generated by `dbt docs generate`, which queries `information_schema` (or adapter equivalent). Documents what physically exists in the warehouse for every `model | seed | snapshot | source` in the manifest.

```jsonc
{
  "metadata": { /* same shape as manifest metadata */ },
  "nodes":   { "model.x.dim_users": {...} },
  "sources": { "source.x.raw.users": {...} },
  "errors":  null      // populated if information_schema queries failed
}
```

Per-entry shape:

```jsonc
{
  "metadata": {
    "type": "TABLE",
    "schema": "MARTS",
    "name": "DIM_USERS",
    "database": "ANALYTICS",
    "comment": null,
    "owner": "TRANSFORMER"
  },
  "columns": {
    "USER_ID": {
      "type": "VARCHAR(36)",
      "index": 1,
      "name": "USER_ID",
      "comment": null
    }
  },
  "stats": {
    "row_count": { "id": "row_count", "label": "Row Count", "value": 1234567, "include": true },
    "bytes": { ... }
  },
  "unique_id": "model.x.dim_users"
}
```

**Differs from manifest:**

- Manifest = "what dbt thinks it's building." Catalog = "what's actually in the warehouse."
- Catalog requires a warehouse round-trip; manifest does not.
- Catalog column casing follows warehouse conventions (Snowflake = upper).
- Catalog is the only way to detect actual schema drift between dbt's declared columns and warehouse reality.

### 1.3 run_results.json — execution outcomes

Written after `dbt run | test | build | seed | snapshot | compile`.

```jsonc
{
  "metadata": { /* ... */ },
  "results": [
    {
      "status": "success | error | skipped | pass | fail | warn | runtime error",
      "timing": [
        {"name": "compile", "started_at": "...", "completed_at": "..."},
        {"name": "execute", "started_at": "...", "completed_at": "..."}
      ],
      "thread_id": "Thread-1",
      "execution_time": 12.34,
      "adapter_response": {
        "_message": "SUCCESS 1",
        "rows_affected": 1234567,
        "code": "SUCCESS",
        "bytes_processed": 89012345,    // BigQuery
        "query_id": "..."               // Snowflake
      },
      "message": "OK created sql table model ANALYTICS.MARTS.DIM_USERS [SUCCESS 1 in 12.34s]",
      "failures": null,                  // populated on tests
      "unique_id": "model.x.dim_users",
      "compiled": true,
      "compiled_code": "select ...",
      "relation_name": "ANALYTICS.MARTS.DIM_USERS"
    }
  ],
  "elapsed_time": 123.45,
  "args": { /* invocation args */ }
}
```

**For CI:** join on `unique_id` against the manifest to attribute every failure / slow run to a specific model file. Test failures populate `failures` (count) and `message` (sample failing rows when configured).

### 1.4 sources.yml / schema.yml — author-controlled YAML

Not artifacts — they're inputs. dbt parses these into `manifest.json` under `nodes` (tests) and `sources`. Author surface:

```yaml
# models/marts/_marts__models.yml
version: 2
models:
  - name: dim_users
    description: "..."
    access: public            # 1.5+
    config:
      contract: {enforced: true}
      tags: [pii]
    columns:
      - name: user_id
        data_type: varchar(36)
        description: "..."
        constraints:
          - type: not_null
          - type: unique
        data_tests:
          - unique
          - not_null
          - relationships:
              to: ref('stg_users')
              field: user_id

# models/staging/_sources.yml
sources:
  - name: raw
    schema: RAW
    tables:
      - name: users
        loaded_at_field: _ingested_at
        freshness:
          warn_after: {count: 12, period: hour}
          error_after: {count: 24, period: hour}
        columns:
          - name: id
            data_type: string
```

Evolution: 1.5 added `access` and groups. 1.6 added MetricFlow `metrics` and `semantic_models`. 1.7 added `saved_queries`, dropped Python 3.7. 1.8 added `unit_tests` and renamed `tests:` to `data_tests:` (both still parse). Fusion adds stricter `data_type` validation via static analysis.

### 1.5 sources.json — freshness results

Generated only by `dbt source freshness`. Schema v3 today. Per-result:

```jsonc
{
  "unique_id": "source.x.raw.users",
  "max_loaded_at": "2026-04-24T14:00:00Z",
  "snapshotted_at": "2026-04-24T14:05:00Z",
  "max_loaded_at_time_ago_in_s": 300,
  "criteria": {"warn_after": {...}, "error_after": {...}},
  "status": "pass | warn | error | runtime error",
  "execution_time": 0.42
}
```

### 1.6 graph.gpickle — the compiled DAG

Pickled `networkx.DiGraph` of `unique_id` -> `unique_id` edges. Not stable across Python or networkx versions; consumers should rebuild from `manifest.parent_map` / `child_map` rather than depend on the gpickle. Fusion may not emit it at all in some configurations.

### 1.7 partial_parse.msgpack — the perf cache

dbt's incremental-parse cache. Stores hashed config + last-parsed manifest in msgpack format under `target/`. **Do not read this from a downstream tool** — internal format, not versioned for external consumers. `dbt clean` deletes it. Useful only as a signal that partial parsing is enabled (skips re-parsing unchanged files between invocations).

### 1.8 Other artifacts

- **`semantic_manifest.json`** — MetricFlow's separate manifest for semantic models / metrics. Required for the dbt Semantic Layer.
- **`graph_summary.json`** — Fusion-only condensed graph for fast loading.
- **`.user.yml`** — dbt Cloud's per-user UUID file.

---

## 2. dbt MCP Server (`dbt-labs/dbt-mcp`)

Apache 2.0, Python, ~96% Python codebase. Two deployment shapes: **local** (`uvx dbt-mcp`) and **remote** (HTTP, hosted by dbt Labs). The local form is what an OSS CI tool would target.

### 2.1 Tool surface

50+ tools in 8 categories. Names and intent:

**SQL & Semantic Layer (8):** `execute_sql`, `text_to_sql`, `get_dimensions`, `get_entities`, `get_metrics_compiled_sql`, `list_metrics`, `list_saved_queries`, `query_metrics`.

**Discovery API (16+):** `get_all_macros`, `get_all_models`, `get_all_sources`, `get_exposure_details`, `get_exposures`, `get_lineage`, `get_macro_details`, `get_mart_models`, `get_model_children`, `get_model_details`, `get_model_health`, `get_model_parents`, `get_model_performance`, `get_related_models`, `get_seed_details`, `get_semantic_model_details`, `get_snapshot_details`, `get_source_details`, `get_test_details`, `search`. **The `get_*` family reads from dbt Platform's Discovery API, not local artifacts** — requires `DBT_HOST` + `DBT_TOKEN`.

**dbt CLI (9):** `build`, `clone`, `compile`, `docs`, `list`, `parse`, `run`, `show`, `test` plus `get_lineage_dev`, `get_node_details_dev` for local-only manifest reads.

**Admin API (10):** `cancel_job_run`, `get_job_details`, `get_job_run_details`, `get_job_run_error`, `list_job_run_artifacts`, `list_jobs`, `list_jobs_runs`, `list_projects`, `retry_job_run`, `trigger_job_run`. dbt Cloud only.

**Code Generation (3):** `generate_model_yaml`, `generate_source`, `generate_staging_model`. Wraps the `dbt-codegen` package.

**Fusion / LSP (3):** `fusion.compile_sql`, `fusion.get_column_lineage`, `get_column_lineage`.

**Product Docs (2):** `get_product_doc_pages`, `search_product_docs`.

**Metadata (2):** `get_mcp_server_branch`, `get_mcp_server_version`.

### 2.2 Auth

Local CLI category requires:

| Var | Purpose |
| --- | --- |
| `DBT_PROJECT_DIR` | Absolute path to dir containing `dbt_project.yml` |
| `DBT_PATH` | Absolute path to the `dbt` executable |
| `DBT_PROFILES_DIR` | Optional, defaults to `~/.dbt/` |
| `DBT_CLI_TIMEOUT` | Seconds, default 60 |

dbt Platform (Discovery, Admin, Semantic Layer, SQL):

| Var | Purpose |
| --- | --- |
| `DBT_HOST` | e.g. `cloud.getdbt.com` |
| `DBT_TOKEN` | Service token or PAT (PAT required for `execute_sql`) |
| `DBT_PROD_ENV_ID` | Numeric environment ID |
| `DBT_DEV_ENV_ID` | Required for `execute_sql` |
| `DBT_USER_ID` | Required for `execute_sql` |
| `DBT_ACCOUNT_ID` | Required for Admin API + PAT auth |

**Tool category gating:** by default everything is enabled except SQL, Codegen, and metadata. Disable with `DISABLE_DBT_CLI=true` etc. Switch to allowlist mode with `DBT_MCP_ENABLE_*=true` (the moment you set ANY enable flag, only enabled categories are active).

### 2.3 Install + invoke

```bash
# Local install via uv
uvx dbt-mcp                        # one-shot run, fetches latest
uvx --from dbt-mcp@1.x dbt-mcp     # pin version

# Or via the MCPB bundle
mcpb install dbt-mcp.mcpb          # Anthropic's bundle installer
```

Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` or platform equivalent):

```json
{
  "mcpServers": {
    "dbt": {
      "command": "uvx",
      "args": ["dbt-mcp"],
      "env": {
        "DBT_PROJECT_DIR": "/path/to/dbt_project",
        "DBT_PATH": "/usr/local/bin/dbt",
        "DISABLE_DBT_CLI": "false",
        "DISABLE_SEMANTIC_LAYER": "true",
        "DISABLE_DISCOVERY": "true",
        "DISABLE_ADMIN_API": "true"
      }
    }
  }
}
```

Claude Code uses the same `mcpServers` schema in `.mcp.json` at the project root, or via `claude mcp add`.

Anthropic SDK invocation: spawn `uvx dbt-mcp` as a stdio child, speak MCP over stdin/stdout. Or set `MCP_TRANSPORT=streamable-http` for an HTTP transport (useful for debugging).

### 2.4 What's missing for an interesting CI tool

- **No diff / impact-analysis tools.** `get_lineage` returns the lineage as-of-now, not "what changed between this PR and main." Recce-style PR diffs are not in the surface.
- **No data-sample tool with safety controls.** `execute_sql` is raw — no PII redaction, no row-cap enforcement, no warehouse-side sampling helpers. Have to wrap.
- **No PR / GitHub awareness.** MCP server doesn't know about CI context; that surface lives outside.
- **No artifact-version awareness.** Tools assume "the manifest" — no helper for "compare manifest A vs manifest B."
- **No streaming for large results.** `get_all_models` on a 2k-model project returns a single massive blob.
- **Authentication is per-process.** No way to swap credentials per request — important if you want one server instance to serve multiple repos / accounts.

### 2.5 Stability assessment

dbt-mcp moved to v1.0 in late 2025. Tool names and env-var contract have been stable since. Discovery/Admin tools are tightly coupled to dbt Cloud's GraphQL API (which IS stable but evolves). LSP/Fusion tools are newer and still labelled experimental. **For an OSS CI tool, depend on the local-CLI subset and the basic Discovery tools; treat LSP/Fusion as moving targets.**

---

## 3. Anthropic SDK / Claude Agent SDK / Claude Code surface

### 3.1 Anthropic SDK (raw)

Direct `messages.create` with tool-use:

```python
from anthropic import Anthropic

client = Anthropic()
resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    tools=[
        {
            "name": "read_manifest",
            "description": "Read a slice of manifest.json by unique_id.",
            "input_schema": {
                "type": "object",
                "properties": {"unique_id": {"type": "string"}},
                "required": ["unique_id"],
            },
        }
    ],
    messages=[{"role": "user", "content": "..."}],
)
# Loop on resp.stop_reason == "tool_use", append tool_result, recurse.
```

Pros: full control, lowest token overhead per turn, minimal dependencies. Cons: you write the agent loop, the file I/O, the parallel-tool-use coordination.

### 3.2 Claude Agent SDK

Higher-level wrapper that gives you Claude Code as a library. Subagents, sessions, hooks, and a built-in tool execution loop. Python and TypeScript.

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

options = ClaudeAgentOptions(
    system_prompt="You are a dbt CI reviewer...",
    allowed_tools=["Read", "Grep", "Bash(dbt parse:*)", "Bash(dbt compile:*)"],
    max_turns=20,
    permission_mode="acceptEdits",
)
async with ClaudeSDKClient(options) as client:
    await client.query("Review manifest changes between main and HEAD")
    async for message in client.receive_response():
        ...  # stream events
```

**Subagents** — define in `.claude/agents/<name>.md` with frontmatter (`name`, `description`, `tools`, `model`). Main agent invokes via the Task tool; each subagent runs in its own context window with its own permissions. Multiple subagents run in parallel for read-only work.

**Tool annotations** — `readOnlyHint: true` lets Claude batch tools for parallel execution. Important for reading many model files at once.

### 3.3 Claude Code as a subprocess (clauditor's pattern)

Spawn `claude -p "<prompt>" --output-format stream-json --verbose`, read NDJSON from stdout. clauditor's runner (`src/clauditor/runner.py`) is the reference shape — defensive parsing per `docs/rules/stream-json-schema.md`, four-layer error classification, watchdog timeout. See also `docs/transport-architecture.md`.

**Pros for a CI tool:** zero SDK dependency, leverages user's existing Claude Code config (subagents, MCP servers, permissions), inherits subscription auth (Pro/Max/Teams) without an API key. Same auth surface as developer's local Claude Code.

**Cons:** requires `claude` binary on the runner image, larger surface to mock for tests, subprocess overhead per invocation (~1-2s warm-up), output is stream-json which you parse defensively. Per-call latency is higher than direct SDK.

### 3.4 Token cost considerations

Three things blow up the token budget on a dbt tool:

1. **manifest.json itself.** Already covered: medium projects don't fit raw. Mitigations:
   - Build a thin "node summary" structure (just `unique_id`, `resource_type`, `tags`, `depends_on.nodes`, `description`, column count) and feed that as the index.
   - Use tool calls to fetch full node details on demand.
   - Cache the compact summary as a static prompt prefix (see prompt caching below).

2. **Model SQL files.** A 2000-model project has 2000 .sql files, average 50 lines each. Reading them all is 1-3M tokens. Use grep / glob to narrow first, then Read only the relevant files.

3. **Catalog data** scaling with column count. A 100-model warehouse with avg 20 columns is 2000 column rows; mostly fine to load fully, but skip catalog if you don't need warehouse reality.

### 3.5 Prompt caching

5-minute cache writes cost 1.25x base input; 1-hour cache writes cost 2x; cache reads cost 0.1x. **Caching pays off after one hit at the 5-min tier and two hits at the 1-hour tier.**

For a dbt tool, the caching candidate is the *static project context* — manifest summary, project conventions doc, style guide. Mark up to 4 cache breakpoints in your prompt; everything before the breakpoint is cached:

```python
client.messages.create(
    model="claude-sonnet-4-6",
    system=[
        {"type": "text", "text": "You are a dbt CI reviewer..."},
        {
            "type": "text",
            "text": MANIFEST_SUMMARY,
            "cache_control": {"type": "ephemeral"},  # 5-min default
        },
    ],
    messages=[...],
)
```

A 200k-token project context cached at 1-hour TTL: write costs $3/M * 0.2M * 2 = $1.20 once. Subsequent reads in the same hour: $3/M * 0.2M * 0.1 = $0.06 per request. Run 50 PR reviews in an hour: $1.20 + 50 * $0.06 = $4.20 vs. uncached $3/M * 0.2M * 50 = $30.00. **86% savings.**

Important: starting Feb 2026 caches are isolated per workspace (Claude API + Azure AI Foundry). Bedrock and Vertex remain org-isolated.

### 3.6 Model picks for a dbt CI tool

| Tier | Model | Use case |
| --- | --- | --- |
| Cheap & fast | Haiku 4.5 ($1/$5 per 1M) | Single-file lint, deterministic checks, structured extraction |
| Workhorse | Sonnet 4.6 ($3/$15) | PR review, diff explanation, test proposal |
| Reasoning | Opus 4.7 ($5/$25) | Cross-cutting refactor analysis, root-cause for cascading test failures |

For a clauditor-style L2 (extraction) + L3 (rubric grading) split: Haiku does L2, Sonnet does L3. Opus stays on a manual escalation tier.

---

## 4. Warehouse integration patterns

A dbt-aware tool wants to *see data*, not just metadata, for: schema diffs, profile diffs (Recce-style), value-distribution comparisons, sample-row inspection.

### 4.1 Reading via dbt itself

```bash
dbt show --inline "select * from {{ ref('dim_users') }} limit 10"
dbt run-operation print_models
```

**Pros:** uses the user's existing `profiles.yml` — no new credentials path, inherits dbt's connection pooling. **Cons:** subprocess overhead per query, 60s default timeout, awkward for bulk profiling. Fine for CI sample queries; bad for interactive exploration.

### 4.2 Direct adapter access

```python
import snowflake.connector
conn = snowflake.connector.connect(**creds)
cur = conn.cursor()
cur.execute("SELECT * FROM ANALYTICS.MARTS.DIM_USERS TABLESAMPLE (1000 ROWS)")
```

Adapter libraries: `snowflake-connector-python`, `google-cloud-bigquery`, `databricks-sql-connector`, `psycopg2` / `psycopg` for Postgres/Redshift, `duckdb` for local.

**Pros:** fast, full SQL surface, parameterized queries. **Cons:** new credentials path (don't reuse `profiles.yml` unless you parse it), per-warehouse SQL dialect differences for sampling, you own the connection lifecycle.

### 4.3 Reading via dbt MCP semantic layer

`query_metrics`, `get_dimensions` — operates on declared metrics in the Semantic Layer. **Pros:** governed, semantic-aware, no SQL injection surface. **Cons:** Semantic Layer is a dbt Cloud feature, OSS users won't have it, only works for declared metrics.

### 4.4 Sampling strategies

| Warehouse | Sampling syntax |
| --- | --- |
| Snowflake | `TABLESAMPLE (1000 ROWS)` or `TABLESAMPLE BERNOULLI (1)` |
| BigQuery | `TABLESAMPLE SYSTEM (1 PERCENT)` |
| Databricks | `TABLESAMPLE (1000 ROWS)` |
| Postgres | `TABLESAMPLE BERNOULLI (1)` |
| Redshift | No native; use `WHERE random() < 0.01` |

For diff workloads also constrain by time: `WHERE _ingested_at > current_date - 7`. Sampling for an LLM context is bounded by **token budget per row × row count**; 100 rows × 20 cols × ~5 tokens = 10k tokens per sample is a reasonable target.

### 4.5 PII / safety

The single biggest deployment blocker. Options:

- **Schema-only mode.** Never query data; only show column names + types from catalog.
- **Aggregate-only.** `count`, `count(distinct)`, `min`, `max`, `null_count`, value distribution buckets — no raw values.
- **Tag-based redaction.** Honor dbt `meta.pii: true` or `tags: [pii]` and refuse to fetch those columns. Models supporting this: `dbt-snow-mask`, `dbt-tags`, plus Snowflake's `AI_REDACT` Cortex function for unstructured text.
- **Explicit allowlist.** Only sample from `models/marts/safe_for_review/`.
- **Dynamic Data Masking** at the warehouse layer (Snowflake masking policies). The cleanest answer; the tool runs as an LLM-bot role that sees masked values for any tagged column.
- **Client-side redaction.** Run regex-based PII detectors over fetched rows before the LLM sees them. Snowflake's `AI_REDACT` is a server-side equivalent.

For a public-CI shape, **default to schema-only with explicit opt-in for sampling** is the only defensible posture.

---

## 5. CI/CD shapes

### 5.1 GitHub Action

Two-workflow pattern (Recce's shape): a base-branch workflow uploads `target/` artifacts; a PR workflow downloads the base and diffs. Reference `action.yml`:

```yaml
# .github/actions/dbt-claude-review/action.yml
name: dbt Claude Review
description: LLM-powered dbt PR review
inputs:
  dbt-project-dir:
    required: true
    default: '.'
  base-artifact:
    required: false
    description: 'Name of the base manifest artifact to download'
  anthropic-api-key:
    required: false
  claude-binary:
    required: false
    default: 'claude'
runs:
  using: composite
  steps:
    - uses: actions/checkout@v4
    - name: Install dbt
      shell: bash
      run: pip install dbt-core dbt-snowflake
    - name: Parse PR manifest
      shell: bash
      working-directory: ${{ inputs.dbt-project-dir }}
      run: dbt parse
    - name: Download base manifest
      if: inputs.base-artifact != ''
      uses: actions/download-artifact@v4
      with:
        name: ${{ inputs.base-artifact }}
        path: target-base
    - name: Run review
      shell: bash
      env:
        ANTHROPIC_API_KEY: ${{ inputs.anthropic-api-key }}
      run: |
        my-tool review \
          --pr-manifest target/manifest.json \
          --base-manifest target-base/manifest.json \
          --pr-number ${{ github.event.pull_request.number }} \
          --repo ${{ github.repository }}
    - name: Post comment
      if: github.event_name == 'pull_request'
      uses: actions/github-script@v7
      with:
        script: |
          const fs = require('fs');
          const body = fs.readFileSync('review.md', 'utf8');
          await github.rest.issues.createComment({
            issue_number: context.issue.number,
            owner: context.repo.owner,
            repo: context.repo.repo,
            body
          });
```

Base-branch workflow is symmetric: `dbt parse` → `actions/upload-artifact@v4` with `name: manifest-main`.

**Pros:** native to GH, free for OSS, integrates with status checks / PR comments / annotations. **Cons:** GitHub-only.

### 5.2 GitLab CI

```yaml
dbt_claude_review:
  image: python:3.12
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - pip install dbt-core dbt-snowflake my-tool
    - dbt parse
    - my-tool review --mr $CI_MERGE_REQUEST_IID
  artifacts:
    paths: [target/]
    reports:
      codequality: review.json
```

Use the GitLab Code Quality report shape for inline annotations. MR comments via `python-gitlab` against `CI_API_V4_URL`.

### 5.3 Pre-commit hook

```yaml
# .pre-commit-hooks.yaml
- id: dbt-claude-lint
  name: dbt Claude lint
  entry: my-tool lint
  language: python
  files: '\.(sql|yml)$'
  pass_filenames: true
```

**Pros:** local, fast, no CI roundtrip. **Cons:** no warehouse access in pre-commit (security), so limited to manifest/text checks. Reference: `dbt-checkpoint` (formerly `pre-commit-dbt`) is the canonical OSS hook collection.

### 5.4 dbt Cloud webhook

dbt Cloud emits webhooks on job completion (`run.completed`, `run.errored`). A Lambda / Cloud Run / Modal endpoint receives the webhook, downloads `manifest.json` + `run_results.json` via the Admin API, runs the review, posts to wherever.

**Pros:** triggered by real production runs, has access to Admin API artifacts. **Cons:** requires hosted compute, dbt Cloud only.

### 5.5 Standalone CLI

Plain `pip install` + invocation from any CI. The most portable shape and the lowest-coupling. clauditor's CLI is the model.

### 5.6 Pros/cons summary

| Shape | Reach | Setup cost | Warehouse access | Where output goes |
| --- | --- | --- | --- | --- |
| GitHub Action | GH only | Low (action.yml) | Yes (secrets) | PR comment / check |
| GitLab CI | GL only | Low | Yes | MR note / report |
| Pre-commit | Universal | Trivial | No | Terminal |
| dbt Cloud webhook | Cloud users only | Med (deploy endpoint) | Via Admin API | Anywhere |
| Standalone CLI | Universal | Trivial | Yes | Stdout / file |

---

## 6. Output integration

| Channel | API surface | Notes |
| --- | --- | --- |
| GitHub PR comment | `gh pr comment <num> --body-file review.md` or `actions/github-script@v7` (Octokit) | Markdown supported, 65k char limit per comment |
| GitHub PR review (multi-thread) | `POST /repos/{o}/{r}/pulls/{n}/reviews` with `comments: [{path, line, body}]` | Inline annotations on diff lines |
| GitHub check run | `POST /repos/{o}/{r}/check-runs` with `output.annotations` | Pass/fail status + line-anchored annotations; max 50 annotations per request |
| GitHub job summary | `echo "..." >> $GITHUB_STEP_SUMMARY` in the runner | Free-form Markdown on the workflow run page |
| GitLab MR note | `POST /projects/:id/merge_requests/:iid/notes` | Same shape as PR comments |
| GitLab MR diff comment | `POST /projects/:id/merge_requests/:iid/discussions` with `position` | Inline on diff |
| Status check | GitHub: check run `conclusion`; GitLab: MR pipeline status | Required-check protection rules use this |
| Artifact upload | `actions/upload-artifact@v4`, GitLab `artifacts:` | HTML report, JSON sidecar — survive after run |
| Slack | Webhook URL or `chat.postMessage` | Rich blocks, threading |
| Linear | `IssueCreate` GraphQL mutation | Auto-file follow-up issues |
| Email | SES, SMTP | Last-resort, non-interactive |

**Recommended default for a CI tool:** check run with summary + annotations (gives both pass/fail status and inline comments) plus a job-summary write for free-form analysis. Gate behavior: never block merge in default configuration; add `--required` flag for explicit promotion.

---

## 7. Existing OSS templates worth stealing from

| Project | Repo | Architectural lesson |
| --- | --- | --- |
| Recce | `DataRecce/recce` | Two-workflow PR-vs-base diff via uploaded artifacts. TypeScript UI + Python backend; CLI for CI (`recce server`, `recce-cloud upload`). Profile/Value/Top-K/Histogram diffs are the lingua franca. PR gating via Cloud. |
| Elementary | `elementary-data/elementary` + `elementary-data/dbt-data-reliability` | dbt package writes test results / metadata to warehouse tables; CLI generates HTML reports + Slack alerts. The "package + CLI + GitHub Action" trio is a copy-worthy template. |
| dbt-osmosis | `z3z1ma/dbt-osmosis` | YAML inheritance / propagation. Reads manifest, mutates schema.yml. Streamlit workbench for interactive exploration. The "diff schema vs warehouse + propose YAML edits" pattern. |
| dbt-coves | `datacoves/dbt-coves` | Staging-model generation (yml + sql) from sources. Templating-heavy. |
| dbt-checkpoint | `dbt-checkpoint/dbt-checkpoint` | The canonical pre-commit hook collection. ~30 hooks for column descriptions, model tags, source freshness. Reads `manifest.json` for context. |
| Anthropic agent-sdk examples | `anthropics/claude-agent-sdk-python` examples dir | Shape for a long-running review agent with subagents and parallel tool use. |
| GitHub agent-toolkit | `github/agent-toolkit` | Patterns for agents that operate inside Actions — secret handling, artifact lifecycle, comment posting. |

**Patterns to steal:**

- Recce's two-workflow base/PR artifact handoff.
- Elementary's "dbt package writes reproducible state to warehouse tables" — gives you historical context without re-querying.
- dbt-osmosis's "read manifest, mutate YAML files, run formatter" loop.
- dbt-checkpoint's permissioning model — pre-commit hooks gate on local-only checks; warehouse-touching checks defer to CI.

**Patterns NOT to steal:**

- Heavy UI bundled with the OSS distribution (Recce's TS frontend doubles install footprint).
- Tight coupling to a hosted backend (limits OSS usability).

---

## 8. Token economics

### 8.1 Reading manifest.json

Compact summary (one line per node, ~50 tokens):

| Project | Models | Compact summary tokens |
| --- | --- | --- |
| Small | 100 | ~5k |
| Medium | 500 | ~25k |
| Large | 2000 | ~100k |

Full manifest.json (raw):

| Project | Models | Full manifest tokens |
| --- | --- | --- |
| Small | 100 | ~100k – 400k |
| Medium | 500 | ~1M – 3M |
| Large | 2000 | ~6M – 20M+ |

**Operational rule:** never load raw manifest into context past the small tier. Build a summary, cache it, and use tool calls for drill-down.

### 8.2 Reading model SQL files

Average model: 30-100 lines, ~500-1500 tokens. Reading 10 changed models in a PR: 5-15k tokens. Reading the upstream/downstream impact set (avg 20 nodes): 10-30k tokens.

### 8.3 PR comment output

A typical clauditor-style review comment runs 1500-3000 output tokens. At Sonnet 4.6 output ($15/M), that's $0.022-$0.045 per comment.

### 8.4 Per-PR cost estimates

Assumptions: medium project (500 models), 10 changed files, prompt caching enabled, Sonnet 4.6 main reviewer + Haiku for L2 extraction.

| Phase | Tokens | Cached? | Cost |
| --- | --- | --- | --- |
| System prompt + project context | 25k | Yes (1-hr cache, write once per hour) | Write: $0.15 ; reads after first: $0.0075 each |
| PR diff + changed file SQL | 15k input | No | $3 * 0.015 = $0.045 |
| Tool-call drill-down (avg 5 calls) | 10k input | No | $0.030 |
| Output comment | 2k output | n/a | $15 * 0.002 = $0.030 |
| **Per-PR total (cache hit)** | | | **~$0.11 / PR** |
| **Per-PR total (cache miss)** | | | **~$0.26 / PR** |

Active team doing 100 PRs/month: **$11–$26/month** at Sonnet rates. Add Opus escalation for ~10% of PRs at $0.50 each → **+$5/month**. Total roughly **$15-$30/month per active dbt repo**. Cheap enough that the bottleneck is human review time, not token budget.

Larger projects (2000 models): summary cost rises to 100k tokens, cached at $0.60/write, $0.030/read. Per-PR rises to $0.40-$0.80. Still ~$50-$100/month per active large repo.

### 8.5 Eval pass with rubric grading

A clauditor-style eval (rubric-graded by Sonnet, with 5-10 criteria) runs 8-15k input tokens + 2-3k output. Each pass: ~$0.05. A 20-skill regression suite: ~$1.

---

## 9. Open questions / unknowns

### 9.1 Worth a spike before committing

- **Manifest size handling at the medium-to-large tier.** Need to prototype the compact-summary builder + tool-call drill-down loop end-to-end on a real 500-model project to confirm the token math.
- **Cache TTL behavior across CI runs.** Cached prompts only survive the cache TTL (5-min or 1-hour) AND require workspace-level continuity (post-Feb-2026 isolation). A burst of 50 PRs in 5 minutes amortizes well; a single PR/week pays full price every time. Need to measure.
- **dbt-mcp vs direct artifact reads.** Open question whether the MCP server adds enough value over `json.load(open("target/manifest.json"))` to justify the dependency. The Discovery API tools are useful only for dbt Cloud users; the local-CLI tools mostly wrap `subprocess.run`. A spike comparing both for the canonical "diff PR vs main" workflow would settle it.
- **Subagent fan-out for parallel model review.** Subagents run in parallel — does that scale to "review 20 changed models concurrently with separate context windows"? Token cost is per-subagent (no shared cache between siblings unless explicitly architected).
- **PII safety posture default.** Schema-only is safe but boring; sampling is interesting but risky. Need a clear opt-in flow.

### 9.2 What dbt Fusion changes

- **Manifest shape:** v20 is "identical to v12" — additive fields only. Existing manifest readers keep working.
- **Static analysis:** Fusion produces validated logical plans for every query. Column-level lineage becomes a first-class artifact (vs Core, where it requires `sqlglot` post-processing). Tooling that surfaces lineage gets cheaper / more accurate on Fusion.
- **`baseline` vs `strict` static-analysis modes:** Fusion defaults to `baseline` (warnings, not errors). Tools that want to surface the warnings need to read Fusion's diagnostic output, not just `run_results.json`.
- **Cross-engine artifacts don't mix:** A Fusion-produced manifest in CI vs Core-produced manifest in dev breaks Recce-style diffs. Tool authors need explicit "engine match" gates.
- **`on` and `unsafe` static_analysis values deprecated, removed May 2026.** Project authors must migrate to `strict` or `baseline`. Tools that read `static_analysis` config need updated parsing.

### 9.3 dbt MCP server stability for 2026

Net assessment: **stable enough to depend on for the local-CLI subset and basic Discovery tools; treat LSP/Fusion tools as moving targets; build a thin abstraction so you can swap to direct artifact reads if the MCP surface diverges from what you need.**

- v1.0 shipped late 2025; tool names + env vars stable since.
- Discovery API + Admin API surfaces depend on dbt Cloud (which is stable but proprietary; OSS users are excluded).
- Codegen + LSP tools experimental.
- The MCP protocol itself is still evolving; transport (`stdio` vs `streamable-http`) may shift.

### 9.4 Other risks

- **dbt Cloud lock-in for the most interesting tools** (Discovery API, Semantic Layer, lineage). OSS-first tooling has to either skip these or replicate them.
- **Adapter sprawl.** Snowflake / BigQuery / Databricks / Redshift / Postgres / DuckDB all have different sample syntax, different `information_schema`, different auth. Each adapter you support is real surface.
- **PII compliance and contract law.** Any tool that fetches warehouse data needs explicit data-handling story (no logging row contents, masking-aware fetches, audit trail of what the LLM saw).
- **Subscription auth vs API key.** Like clauditor's #86 / #95 work — operators using Pro/Max via Claude Code as a subprocess get one cost story; operators using API keys get a different one. CI shape determines which.

---

Sources:

- [dbt manifest.json reference](https://docs.getdbt.com/reference/artifacts/manifest-json)
- [dbt run_results.json reference](https://docs.getdbt.com/reference/artifacts/run-results-json)
- [dbt catalog.json reference](https://docs.getdbt.com/reference/artifacts/catalog-json)
- [dbt sources.json reference](https://docs.getdbt.com/reference/artifacts/sources-json)
- [dbt artifact schemas](https://schemas.getdbt.com/dbt/manifest/v12.json)
- [dbt Project Parsing reference](https://docs.getdbt.com/reference/parsing)
- [dbt-labs/dbt-mcp on GitHub](https://github.com/dbt-labs/dbt-mcp)
- [dbt-mcp environment variables](https://docs.getdbt.com/docs/dbt-ai/mcp-environment-variables)
- [About the dbt MCP server](https://docs.getdbt.com/docs/dbt-ai/about-mcp)
- [dbt Fusion: static analysis](https://docs.getdbt.com/docs/fusion/new-concepts)
- [Upgrading to dbt Fusion v2.0](https://docs.getdbt.com/docs/dbt-versions/core-upgrade/upgrading-to-fusion)
- [Anthropic API pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Claude Agent SDK subagents](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [Claude Code custom subagents](https://code.claude.com/docs/en/sub-agents)
- [Recce: Data Review Agent for dbt PRs](https://docs.reccehq.com/)
- [Recce open-source CI setup](https://docs.reccehq.com/7-cicd/scenario-ci/)
- [Elementary OSS](https://github.com/elementary-data/elementary)
- [Elementary GitHub Actions integration](https://docs.elementary-data.com/oss/deployment-and-configuration/github-actions)
- [dbt-osmosis](https://github.com/z3z1ma/dbt-osmosis)
- [dbt-checkpoint](https://github.com/dbt-checkpoint/dbt-checkpoint)
- [dbt-coves](https://github.com/datacoves/dbt-coves)
- [Snowflake AI_REDACT for PII](https://docs.snowflake.com/en/user-guide/snowflake-cortex/redact-pii)
- [Snowflake Dynamic Data Masking](https://docs.snowflake.com/en/user-guide/security-column-ddm-use)
- [Securing data at scale with dbt + Snowflake](https://www.getdbt.com/blog/securing-data-at-scale-with-dbt-and-snowflake)
- [actions/upload-artifact](https://github.com/actions/upload-artifact)
