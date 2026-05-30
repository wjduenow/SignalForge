---
name: signalforge
description: Use when the user wants to draft, prune, or grade dbt tests / docs with an LLM, has a dbt project (manifest.json + sql models), or asks about SignalForge. Drives the `signalforge` CLI end-to-end: drafts candidate tests, runs them against warehouse samples, drops the noise, and explains every kept/dropped artifact.
compatibility: "Requires: signalforge installed (pip install signalforge-dbt). For the zero-credential demo: no warehouse needed. For real dbt projects: dbt-core + a populated manifest.json. For live e2e: a configured warehouse profile (BigQuery v0.1) + ANTHROPIC_API_KEY."
metadata:
  signalforge-version: "0.X.Y"
allowed-tools: Bash(signalforge *), Bash(uv run signalforge *), Bash(uv run pytest -m e2e*), Bash(cat *), Bash(ls *), Bash(grep *), Bash(head *), Bash(tail *), Read, Write, Edit
---

# SignalForge — draft, prune, and grade dbt tests with an LLM

You help the user drive SignalForge against a dbt project. SignalForge's differentiator vs. dbt Copilot / dbt-codegen / DinoAI is the **prune step**: competitors generate; SignalForge generates *and grades*. A candidate test that always passes on warehouse samples is **dropped, not shipped** — always-pass is noise that consumes reviewer attention.

The pipeline is four stages, each explainable:

```
model.sql + manifest + project ctx
  -> LLM drafts candidate artifacts          (draft)
  -> run candidates against warehouse samples (prune)
  -> drop always-pass tests; drop tests that fail on known-clean data
  -> grade kept artifacts against a rubric    (grade)
  -> emit graded YAML + diff with per-artifact "why"  (diff)
```

Every kept/dropped/flagged artifact ships with a one-line "why." Read the diff before writing it.

## Bootstrap: installing this skill into another project

If the user is asking how to get this skill into a fresh dbt project, the answer is:

```bash
pip install signalforge-dbt
signalforge install-skill
```

That drops `SKILL.md` into `<project>/.claude/skills/signalforge/`. A fresh Claude Code session activates the skill on the next relevant prompt. Use `signalforge install-skill --force` to overwrite an existing copy.

`signalforge version` confirms the install resolved.

---

## 1. Point at a dbt project

Before any pipeline command, verify the dbt project is in a state SignalForge can read.

```bash
ls target/manifest.json
```

If the file is missing, the project has not been parsed yet. Run:

```bash
dbt parse
```

This requires a configured dbt profile (`~/.dbt/profiles.yml`) — `dbt parse` does NOT hit the warehouse, but it does need the profile to exist. If the user has no profile yet, jump to Section 2 (zero-credential demo) so they can try SignalForge before standing up a real warehouse.

Once `target/manifest.json` is present, identify the model to work on. SignalForge accepts:

- A file path: `models/staging/stg_orders.sql`
- A unique_id: `model.<package>.<name>`
- A bare name (via `lint` only): `stg_orders`

Sanity-check a single model's manifest entry — useful when SignalForge later raises `ModelNotFoundError`:

```bash
signalforge lint --model stg_orders
```

`lint` reads the manifest and surfaces obvious manifest-shape issues (missing model, hidden by `enabled: false`, ambiguous bare name) without making any LLM or warehouse calls.

## 2. Zero-credential demo

The fastest way to see the full pipeline is the bundled Austin bikeshare demo. It needs **no warehouse, no API keys, no dbt profile** — it ships a frozen `manifest.json` and a public-data sampling mode that runs against fixture data.

```bash
signalforge init-demo
```

That writes a self-contained dbt project into `./signalforge-demo/` by default. Pass a path to override (`signalforge init-demo /tmp/sf-demo`). Then:

```bash
cd signalforge-demo
signalforge generate models/staging/stg_bikeshare_trips.sql --write
```

`--write` materialises the proposed `schema.yml` + any singular `tests/*.sql` files into the project. Without `--write`, `generate` prints the diff to stdout and exits — read-only is the safe default.

SignalForge defaults to `safety: schema-only` — only column **names** and **types** leave the warehouse / fixture. No row values, no aggregates. The demo runs under that posture; nothing sensitive can leak.

Read the printed diff. You should see:

- A **kept** column-test table (every test SignalForge believes adds signal).
- A **kept-uncertain** column (tests SignalForge couldn't positively evaluate — shipped under the conservative-bias contract).
- A **dropped** column with the always-pass reasons (`always-passes`, `failed-on-known-clean-data`, `requires-future-data`).
- A unified diff against the (initially empty) existing `schema.yml`.

Every row in every column has a one-line "why" — read these before deciding whether to keep the run.

## 3. Real project: draft + prune

For a user's own dbt project, the command shape is the same:

```bash
signalforge generate <model> --write
```

Where `<model>` is a file path or unique_id from Section 1. The safety posture matters:

- **Default** is `safety: schema-only` — schema-only is the deployment-blocker safe default. The LLM sees column names + types only.
- **Opt-in** `--mode sample` ships a small warehouse sample to the LLM. This has both **cost** (warehouse query + larger LLM prompt) and **privacy** (real row values leave the warehouse) implications. Use it only when the LLM's schema-only output is missing context the operator needs.
- **Opt-in** `--mode aggregate-only` ships per-column aggregates (count, distinct, null-rate) without raw rows. Middle-ground.

`signalforge version` verifies the install resolved before you spend warehouse / API budget. `signalforge generate --estimate` previews the LLM + warehouse byte cost without firing real calls.

The `.signalforge/` directory under the project carries durable audit JSONLs for every stage (safety, llm_responses, prune, grade) plus per-run sidecars (`grade.json`, `diff.json`). These are append-only; they survive crashes mid-run.

## 4. Grade tests you already have

If the user has **existing dbt tests** authored by dbt-codegen, dbt Copilot, DinoAI, or a human, SignalForge can grade them without re-drafting. This path makes **no LLM call** — it runs the ingest → prune → diff pipeline against externally-authored `schema.yml`:

```bash
signalforge prune-existing <model> --schema <path>
```

Where `<path>` is the `schema.yml` file containing the existing tests. The command is **read-only** — there's no `--write` flag, because the source `schema.yml` is hand-authored and overwriting it would be surprising. The diff shows what to **remove** from the file (always-pass tests, failed-on-known-clean tests). Apply the diff by hand, or pipe it through your usual review process.

The same scope / sample-strategy flags from `generate` apply (`--scope`, `--sample-strategy`). The `--mode` flag is inert here — `prune-existing` never builds an LLM payload, so the safety policy has nothing to shape.

## 5. Reading the diff

Every kept / kept-uncertain / dropped / flagged row carries a one-line "why." The four tiers:

| Tier | Meaning | Ships in `schema.yml`? |
|---|---|---|
| **kept** | Test ran with positive evidence — caught a real failing row on warehouse samples, OR grader passed. | Yes |
| **kept-uncertain** | Test could not be positively evaluated (budget elapsed, identifier rejected, materialisation failed, prune disabled). Shipped because the conservative-bias contract says "drop only with positive evidence." | Yes |
| **dropped** | Test ran with positive evidence to drop — always-pass on the sample, OR failed against a `trusted_models` opt-in (test is wrong). | No |
| **flagged** | Test survived prune AND a grader was attached AND the grader scored below threshold. | Yes (but flagged for review) |

The `why` cascade for kept tests: **drafter rationale** → **first non-empty grader evidence** → **fallback** (decision text for kept-uncertain rows, description for docs). One source per row; never concatenated.

If the user is surprised by a kept-uncertain row, the `why` text names the specific cause ("total prune budget exceeded before evaluation," "identifier rejected by SQL safety check," etc.). Tune the relevant `prune.*` knob in `signalforge.yml` if the cause is recurring.

For the full diff, sidecar, and per-run audit shapes, point at `docs/diff-ops.md` and `docs/cli-ops.md`.

## 6. Optional: live e2e demonstration

The repo ships a live end-to-end test that exercises the full pipeline against real BigQuery + real Anthropic. **It is gated for a reason** — it costs real money and real API quota. Before invoking it, you MUST:

1. **Confirm with the user, verbatim:**

   > **This will run paid LLM + warehouse queries — proceed?**

   If the user does not say yes, STOP. Do not run the test.

2. **Check the required env vars.** All three must be set:

   ```bash
   echo "SF_RUN_BQ=${SF_RUN_BQ:-<unset>}"
   echo "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:-<unset>}"
   echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:+<set>}"
   ```

   If any is unset, **clean-skip with a clear reason** — name the missing var, do NOT run the test. Example: "Skipping live e2e: `SF_RUN_BQ` is unset (need `SF_RUN_BQ=1`)."

3. **Surface the cost expectation.** The current live e2e costs on the order of a few cents per run (small Austin bikeshare model + one Anthropic Sonnet draft + one grade pass). Cost detail and tuning notes live in `docs/e2e-smoke-test.md` — point the user at that file before running.

4. **Only THEN run the test:**

   ```bash
   uv run pytest -m e2e --no-cov
   ```

   `--no-cov` is required — coverage's `--cov-fail-under` would fail a marker-specific run that exercises only a fraction of the codebase.

Surface the test output verbatim. The live e2e is the cleanest demonstration that SignalForge actually drops always-pass tests against real data, but it is **never** the default — Section 2's zero-credential demo is.

## 7. Troubleshooting

Common errors and their one-line fixes:

- **`ModelNotFoundError`** — the model arg did not resolve. Verify with `signalforge lint --model <name>` (accepts bare names + disambiguates collisions across packages). For `generate` / `prune-existing`, use the file path (`models/staging/stg_orders.sql`) or unique_id (`model.<pkg>.<name>`) form. Tier-2 exit code.
- **`WarehouseAuthError`** — adapter could not authenticate. Check `~/.dbt/profiles.yml` is configured for the target profile + that ambient credentials (gcloud ADC, service-account JSON) are valid. Tier-3 exit code (external dependency).
- **`LLMCacheTooLargeError`** — the cached prompt block (model under draft + its direct refs / depends_on neighbours) exceeded 8000 input tokens. Narrow the surface: either trim the model's manifest scope (smaller graph) or split the model into smaller models. Tier-3 exit code.
- **`PromptEnvelopeBreachError`** — drafted SQL or `meta.signalforge.business_rules` content contains the literal closing-tag fence (`</MODEL_SQL>` or `</BUSINESS_RULE>`). Remove or rephrase the offending content. Tier-2 exit code.
- **`SamplingRequiresPartitionFilterError`** — model is large (>= 100M rows) and no partition filter was supplied. Either configure `prune.partition_filter` in `signalforge.yml`, or scope to a smaller model.

Exit codes follow the four-tier taxonomy: **0** success; **1** load / parse failure (manifest missing, config malformed); **2** input validation (bad model id, anchor-contract violation); **3** external dependency (warehouse, LLM, audit-write durability). No traceback ever leaks — every CLI handler wraps its pipeline in one boundary catch.

For the full flag reference, exit-code table, and per-stage operational detail, point the user at `docs/cli-ops.md`. The per-stage ops docs (`docs/safety-ops.md`, `docs/draft-ops.md`, `docs/prune-ops.md`, `docs/grade-ops.md`, `docs/diff-ops.md`) carry the configuration surface for each layer's `signalforge.yml` block.
