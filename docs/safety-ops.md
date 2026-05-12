# PII safety layer — operations guide

Operational reference for users of `signalforge.safety`. Companion to
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md) and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md), and to the
design record in [`plans/super/4-pii-safety.md`](../plans/super/4-pii-safety.md).

The safety layer sits between the warehouse adapter and the LLM-drafting
layer. Every LLM call goes through one entry point —
`signalforge.safety.build_llm_request` — which writes a structured audit
record before the request is handed back. There are no other sanctioned
constructors of `LLMRequest`.

## Default posture

Schema-only is the default sampling mode. Architectural Commitment #1
(signal over volume) means the LLM should see the *least* data needed to
draft useful artifacts; raw row data is opt-in, not opt-out. See §4.5 of
[`docs/research/dbt-claude-technical-surface.md`](research/dbt-claude-technical-surface.md)
for the threat model the layer is sized against.

The layer is fail-closed: any audit-write failure aborts the LLM call
(DEC-011) and propagates as `AuditWriteError`. The default mode is
asserted at three layers — config defaults, policy defaults, and the
schema-only branch in `build_llm_request` performs zero adapter calls
(DEC-012). Column NAMES are redacted in addition to values, because a
column name alone (`john_smith_ssn_1234`) can leak PII (DEC-010).

User-facing tagline: **the LLM never sees row data unless you've
explicitly opted in via `safety.mode: sample`** (`aggregate-only` still
sends per-column statistics — count, distinct, nulls, min, max — to the
LLM; only `schema-only` sends column names + types and nothing else).
Note: `safety.mode` controls what the *LLM* sees — the *prune step*
(`signalforge.prune.prune_tests`) runs warehouse SQL on every invocation
regardless of `safety.mode`, because it needs warehouse evidence to
decide which candidate tests carry signal (always-pass tests get
dropped; tests that fail on known-clean data also get dropped). To skip
the prune step entirely, see
[`prune.enabled`](prune-ops.md#configuration-signalforgeyml-prune-block)
in `docs/prune-ops.md`.

## Modes

Three modes, set via `safety.mode` in `signalforge.yml` or overridden
via the CLI's `--mode` flag (post-#9):

### `schema-only` (default)

Only column names + types reach the LLM. No warehouse queries are
issued — `build_llm_request` does not even open the adapter context.
Names matching a redaction signal (per-column meta/tags or a redact
pattern) are replaced with stable hashed placeholders of the form
`col_<8 hex>` (DEC-010). The resulting `LLMRequest.schema` is a tuple
of `(display_name, type_string)` pairs; `sampled_rows` and `aggregates`
are both `None`.

This applies only to `build_llm_request`. The prune step still queries
the warehouse on every run — see
[`prune.enabled`](prune-ops.md#configuration-signalforgeyml-prune-block)
to skip it entirely.

```python
from signalforge.safety import SafetyPolicy, SamplingMode, build_llm_request

policy = SafetyPolicy(mode=SamplingMode.SCHEMA_ONLY)
request = build_llm_request(model, adapter, policy)
# request.schema == (("customer_id", "INT64"), ("col_a3f29c61", "STRING"), ...)
# request.sampled_rows is None
# request.aggregates is None
```

### `aggregate-only`

Column-level statistics — `count`, `distinct`, `nulls`, `min`, `max`,
`data_type` — reach the LLM via the `aggregates` field. Calls
`WarehouseAdapter.column_stats` once per non-redacted column inside a
single `with adapter:` block. Redacted columns still appear as entries
in the returned tuple, but their statistic value is `None` and their
column name is the hashed placeholder.

`LLMRequest.aggregates` is a `tuple[tuple[str, ColumnStats | None], ...]`
(not a dict) so `frozen=True` actually prevents mutation downstream
(DEC-022 transitive immutability).

```python
policy = SafetyPolicy(mode=SamplingMode.AGGREGATE_ONLY)
request = build_llm_request(model, adapter, policy)
# request.aggregates == (
#     ("customer_id", ColumnStats(count=42, distinct=42, nulls=0, ...)),
#     ("col_a3f29c61", None),  # redacted
#     ...
# )
```

### `sample`

Row-level data reaches the LLM. Calls `WarehouseAdapter.sample_rows`
inside a `with adapter:` block. Default sample size is 100 rows
(`safety.sample_size`). Values for redacted columns are replaced with
the literal string `"<REDACTED>"`. Row dict keys for redacted columns
are rewritten to their hashed placeholders so the keys match the
identifiers the LLM sees in `schema` and `columns_sent`.

Constructing a `SafetyPolicy(mode=SamplingMode.SAMPLE)` emits a single
WARNING via `signalforge.safety` on policy load (DEC-021).

```python
policy = SafetyPolicy(mode=SamplingMode.SAMPLE, sample_size=100)
request = build_llm_request(model, adapter, policy)
# request.sampled_rows == (
#     {"customer_id": 1, "col_a3f29c61": "<REDACTED>", "country": "US"},
#     ...
# )
```

## `signalforge.yml` reference

Top-level namespace is locked to `safety:` per DEC-025; every other
top-level key (`llm:`, `prune:`, …) is reserved for future stages and
silently ignored by the safety loader.

```yaml
safety:
  mode: schema-only          # one of schema-only / aggregate-only / sample (case-insensitive)
  sample_size: 100
  audit_path: .signalforge/audit.jsonl   # default; must stay inside project_dir
  redact:
    extend: ["*custom_*"]    # appends to built-ins; mutually exclusive with replace
    # replace: [...]         # substitutes built-ins entirely; empty list disables (WARNING)
```

Field-by-field:

- **`mode`** — `schema-only` | `aggregate-only` | `sample`. Case- and
  separator-insensitive: `Schema-Only`, `schema_only`, `SCHEMA-ONLY`
  all parse. Anything else raises `InvalidSamplingModeError` at load
  time.
- **`sample_size`** — Integer row count for `sample` mode; ignored by
  the other two. Default `100`.
- **`audit_path`** — JSONL audit-log path. Project-relative or
  absolute; must canonicalise to a path inside `project_dir`. Default
  is `.signalforge/audit.jsonl`. Containment is enforced via the same
  symlink-hardened gate used by the manifest loader (`..` segments
  rejected outright; symlink loops raise `InvalidConfigError`).
- **`redact.extend`** — List of fnmatch globs appended to the built-in
  patterns. Mutually exclusive with `redact.replace`.
- **`redact.replace`** — List of fnmatch globs that substitutes the
  built-ins entirely. An empty list disables redaction and emits a
  WARNING.

Unknown keys under `safety:` or `safety.redact:` raise
`UnknownConfigKeyError` (the policy uses Pydantic's `extra="forbid"`,
DEC-015). Typos like `redacts:` or `mode_:` fail loud at load time
rather than silently doing nothing.

## Redaction patterns

Patterns are case-insensitive `fnmatch` globs matched against the
*lowercased* column name. The six built-ins are:

```
*email
email
*phone
phone
*ssn
ssn
```

Each PII class has a prefixed form (`*email`) and a bare form (`email`)
so both `user_email` and `email` match.

**Override semantics.**

- `redact.extend` appends to the six built-ins.
- `redact.replace` substitutes the built-ins entirely.
- `redact.replace: []` disables redaction (with a WARNING).
- Specifying both `extend` and `replace` raises `InvalidConfigError`.

**Footgun-rejected patterns.** Three values are rejected at policy-load
time with `InvalidPatternError`:

- `""` — never matches anything.
- `"*"` — matches every column; use `redact.replace: []` to disable
  explicitly.
- `"?"` — matches every single-character column.

**Suspicious-unmatched-column heuristic.** When a column's lowercased
name contains one of `email` / `phone` / `ssn` / `password` / `token` /
`secret` / `api_key` AND no signal fired (no opt-out, no pattern
match), the redactor logs a single WARNING with the model unique_id
and column name. The column is **not** auto-redacted — the heuristic
flags potential misconfiguration for the operator to decide.

## Per-column opt-out

Four signals override the pattern matcher (DEC-003). Listed by
precedence — first match wins, top to bottom:

1. **Column-level `meta.signalforge.sample: false`** — strongest;
   beats every other signal. Reason code `column_meta_optout`.
2. **Column-level `tags: ["pii"]`** — case-insensitive; `["PII"]` and
   `["Pii"]` both fire. Lowercase recommended. Reason code
   `tag_pii_column`.
3. **Column-level `meta.contains_pii: true`** — truthy values
   accepted; non-bool truthy values (`"yes"`, `1`) emit a DEBUG log
   noting the coercion. Reason code `meta_contains_pii_column`.
4. **Model-level fallbacks** — the same three signals at the model
   level (`meta.signalforge.sample`, `tags: [pii]`,
   `meta.contains_pii`) cascade to every column. Reason codes
   `model_meta_optout` / `tag_pii_model` / `meta_contains_pii_model`.
5. **Pattern match** — last resort. Reason code `pattern_match`.

Concrete dbt YAML:

```yaml
# models/marts/customers.yml
version: 2
models:
  - name: customers
    columns:
      - name: customer_email
        # Signal 1: strongest opt-out, beats everything else.
        meta:
          signalforge:
            sample: false
      - name: phone_number
        # Signal 2: case-insensitive tag match.
        tags: ["pii"]
      - name: home_address
        # Signal 3: truthy values accepted.
        meta:
          contains_pii: true
```

```yaml
# Model-level fallback — every column on the model is redacted.
models:
  - name: hr_employees
    config:
      meta:
        signalforge:
          sample: false
```

## Column-name redaction

Every redacted column's name is hashed via `blake2b` with
`digest_size=4` (DEC-010), yielding a placeholder of the form
`col_<8 hex>`:

```
customer_ssn  ->  col_a3f29c61
```

The mapping (`real_name -> hashed_name`) is recorded in the audit log's
`redactions` array; the LLM never sees the real name. This closes the
"column name itself leaks PII" gap — names like `john_smith_ssn_1234`
or `card_number_last4` would otherwise reach the LLM even when the
*values* were redacted.

The hash is deterministic across runs, so re-running a draft against
the same model produces the same placeholder; reviewers can correlate
audit records to manifest columns by re-hashing the real name.

## Audit JSONL schema

> **Consumer guide.** For cross-stage joins, `jq` / pandas worked examples,
> the forward-compat policy, and the redaction surface across all five
> stages, see [`docs/audits.md`](audits.md). This section is the
> safety-layer production contract.

Every LLM call produces exactly one JSONL record at `safety.audit_path`
(default `.signalforge/audit.jsonl`). One record per line; atomic
append via `O_APPEND` + a single `os.write` (DEC-005).

`AuditEvent` fields:

| Field                    | Type                          | Meaning                                                                 | Example                                |
| ------------------------ | ----------------------------- | ----------------------------------------------------------------------- | -------------------------------------- |
| `timestamp`              | ISO 8601 datetime             | UTC timestamp of the LLM call.                                          | `"2026-04-28T14:33:01.122Z"`           |
| `model_unique_id`        | string                        | dbt unique_id of the drafted model.                                     | `"model.shop.dim_customers"`           |
| `mode`                   | string                        | Sampling mode in effect.                                                | `"schema-only"`                        |
| `columns_sent`           | array of string               | LLM-visible column names (hashed for redacted columns).                 | `["customer_id", "col_a3f29c61"]`      |
| `redactions`             | array of `RedactionRecord`    | Full redaction decisions — both real and hashed names. **Sensitive.**   | `[{"column_name": "customer_ssn", ...}]` |
| `row_count`              | integer or `null`             | Row count for sample mode; `null` for schema-only / aggregate-only.     | `100` or `null`                        |
| `signalforge_version`    | PEP-440 version string        | The package version that produced the record.                           | `"0.1.0"`                              |
| `policy_hash`            | 16 hex chars                  | First 16 hex chars of `SHA-256(policy)`. DEC-014.                       | `"6f1c0e3d2c44c012"`                   |
| `audit_schema_version`   | integer                       | Audit shape version. Currently `1`.                                     | `1`                                    |
| `policy_flags`           | array of string               | Closed set of flag literals — see below.                                | `["sample_mode_enabled"]`              |

**`policy_flags` closed set:**

- `sample_mode_enabled` — `policy.mode is SamplingMode.SAMPLE`.
- `redaction_disabled` — `policy.redact_patterns` is empty.
- `audit_path_overridden` — `policy.audit_path != DEFAULT_AUDIT_PATH`.

`RedactionRecord` fields: `column_name` (real), `hashed_name`
(`col_<8 hex>`), `redacted` (bool), `reason` (one of seven literal
strings — see [Per-column opt-out](#per-column-opt-out)).

## Audit log sensitivity

The audit JSONL contains plaintext column names in
`RedactionRecord.column_name`. For PII-laden schemas this metadata can
itself be sensitive; treat the file at-rest as such.

Recommendations:

- **Gitignore `.signalforge/`** (already configured in this repo's
  `.gitignore`).
- **Restrict at-rest permissions.** The writer creates
  `.signalforge/` at `0o700` and `audit.jsonl` at `0o600` on first
  call. Don't relax these. Note: if `.signalforge/` already exists
  with looser permissions (e.g. created by a different process or
  user), the writer's `mkdir(exist_ok=True, mode=0o700)` will NOT
  tighten the existing directory — Python's `mkdir` only applies
  `mode` when creating. Verify pre-existing directory permissions
  before deploying; tighten manually if needed
  (`chmod 700 .signalforge/`).
- **Don't ship as a build artifact.** Strip from container images and
  CI uploads.
- **Don't check in.** The "explainable diffs" commitment applies to
  the YAML SignalForge writes — not to the audit log.

## Audit log rotation

User responsibility. v0.1 has no built-in rotation.

Suggestions:

- **`logrotate` on the JSONL.** Standard Linux log rotation; works
  because each line is self-contained.
- **External log shipping.** Tail to a SIEM or centralised log store;
  rotate the on-disk file on the shipping side.
- **Per-run prefixed paths.** Set `safety.audit_path` to
  `.signalforge/audit-<run_id>.jsonl` per CI run if rotation is too
  coarse.

Why no in-process rotation: it would put failure modes (rename races,
disk-full mid-rotate, fsync ordering) inside the fail-closed
audit-write hot path, which conflicts with DEC-011. v0.2 may revisit.

## Debugging

Logger name: `signalforge.safety`.

```python
import logging
logging.getLogger("signalforge.safety").setLevel(logging.DEBUG)
```

Levels:

- **INFO** — One line per `audit.write` (the JSON-encoded summary:
  `unique_id`, `mode`, `columns_sent` count, `redactions` count,
  `audit_schema_version`).
- **WARNING** — Sample-mode-enabled (one per policy construction); the
  empty-redaction `redact: replace: []` warning; the
  suspicious-unmatched-column heuristic (one per offending column).
  Oversize records raise `AuditRecordTooLargeError` rather than log.
- **DEBUG** — `meta.contains_pii` coercion notes (e.g. value `"yes"`
  coerced to `True`); empty-config-file fallback to defaults.

The safety layer never logs full row data, full column lists, or the
real names of redacted columns. INFO output is a hint about *that* a
call happened, not *what* was in it.

**Reading a fail-closed `AuditWriteError`.** The cause is exposed as
`.cause`; the path is exposed as `.path`. Common causes:

- Parent directory not writable (no `+w` for the user, or
  `.signalforge/` is a symlink to a read-only mount).
- Disk full (`ENOSPC`).
- Oversize record (raises `AuditRecordTooLargeError` instead — reduce
  `columns_sent` or `redactions` count; the cap is 4000 bytes for
  POSIX-atomic concurrent appends).

## Typed-error reference

Public API: `from signalforge.safety import errors`. Every exception
subclasses `SafetyError` and carries a class-level `default_remediation`
rendered on a `↳ Remediation:` line by `__str__`.

| Class                          | When raised                                                                              | Where it surfaces                              | How to fix                                                                              |
| ------------------------------ | ---------------------------------------------------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------- |
| `SafetyError`                  | Base class; never raised directly.                                                       | `signalforge.safety.errors`                    | Catch it to handle every safety-layer failure uniformly.                                |
| `ConfigNotFoundError`          | Explicit `path=` argument to `load_safety_config` pointed at a missing file.             | `load_safety_config`                           | Verify the path, or pass `path=None` to fall back to defaults.                          |
| `InvalidConfigError`           | Parent for parse / schema failures in `signalforge.yml`. Free-form message.              | `load_safety_config`                           | Check `signalforge.yml` against this doc's schema.                                      |
| `InvalidSamplingModeError`     | `safety.mode` is not one of `schema-only` / `aggregate-only` / `sample`.                 | `SafetyPolicy._normalise_mode`                 | Set `safety.mode` to one of the documented values.                                      |
| `InvalidPatternError`          | A redact pattern is empty or one of the bare wildcards `"*"` / `"?"`.                    | `SafetyPolicy._validate_patterns`              | Use a non-empty fnmatch glob; use `redact.replace: []` to disable redaction explicitly. |
| `ColumnNotInModelError`        | A safety helper looked up a column not declared on the manifest model.                   | `aggregate_columns` / sibling helpers          | Verify the column exists in `manifest.nodes[model].columns`.                            |
| `AuditWriteError`              | Appending to the JSONL audit log failed (any I/O or encoding error). DEC-011 fail-closed.| `audit.write`                                  | Check `<project_dir>/.signalforge/` exists and is writable; resolve disk / permission.  |
| `AuditRecordTooLargeError`     | Serialised audit line exceeded the POSIX-atomic-append cap (4000 bytes).                 | `audit.write`                                  | Reduce `columns_sent` or `redactions` count.                                            |
| `PolicyValidationError`        | Generic Pydantic validation failure not covered by a more specific subclass.             | `load_safety_config` (last-resort wrap)        | Inspect `.field`, `.value`, `.reason`; reconcile against the documented field types.    |
| `UnknownConfigKeyError`        | A typo'd / unsupported key under a known scope (`safety.redacts:`, etc.).                | `SafetyPolicy.model_validate` / redact resolver | Remove or rename the unknown key; see this doc's schema.                                |

## CLI integration note

Tracked in [issue #9](https://github.com/wjduenow/SignalForge/issues/9).
The `signalforge generate` CLI's `--mode` flag will load the policy via
`load_safety_config(...)` and override via
`policy.with_mode(SamplingMode.SAMPLE)` (DEC-018) — that is the
canonical override seam. `SafetyPolicy` is frozen, so `with_mode` is
the only sanctioned mutation path.

**There is no env-var override for `mode`.** The mode must be set in
`signalforge.yml` or via `--mode` (post-#9). This is intentional:
an env-var override would let a CI misconfiguration silently flip
schema-only into sample mode, which conflicts with the layer's
fail-closed posture.
