# The dbt AI/LLM Tooling Ecosystem — Deep Dive (April 2026)

**Scope.** Every meaningful AI/LLM-powered tool in or adjacent to the dbt ecosystem as of April 2026 — what it does, who's actually using it, what users complain about, and where the gaps are. Written in the wake of the Fivetran ↔ dbt Labs merger (announced Oct 13, 2025), the dbt Fusion engine GA push, the Anthropic Skills standard adoption (Dec 2025), and Snowflake Cortex Code GA (Mar 9, 2026).

---

## 0. Executive summary

Three things happened between mid-2024 and now that re-shaped the entire competitive map:

1. **dbt Labs internalized AI as a platform feature.** dbt Copilot (GA Mar 2025), dbt Insights' Analyst agent, the official `dbt-mcp` server, and `dbt-agent-skills` (430 stars, published via the Anthropic Skills standard) make dbt Labs the center of gravity for "AI on a dbt project." Pre-2025, this surface area was almost entirely owned by Paradime and Altimate.
2. **The Fivetran merger** turned the AI story into an ingestion-to-activation play, leaving daylight underneath at the *quality of the AI output* layer — which essentially nobody owns.
3. **MCP + Skills won as the integration substrate.** Every serious tool now ships an MCP server (dbt Labs, Altimate, Cube, Monte Carlo, Ragstar, Snowflake Cortex Code) and many ship skills. The differentiation has moved from "do you have AI features" to "is the AI output any good."

The gap nobody fills: **systematic evaluation of LLM output quality on dbt artifacts** (docs, tests, models, semantic layer definitions). Paradime's `dbt-llm-evals` is the only OSS attempt and it's at 25 GitHub stars. Everyone else ships AI features and asks users to eyeball the result.

---

## A. Comparative matrix

| Tool | What it does | Who it's for | LLM features | Pricing | Distribution | Strengths | Gaps users mention |
|---|---|---|---|---|---|---|---|
| **dbt Copilot** (dbt Labs) | Inline assistance + Developer agent inside dbt Cloud Studio IDE, Canvas, Insights | dbt Cloud customers (Starter+) | Code/docs/tests/semantic-model gen; refactor; multi-step Developer agent | Bundled with Starter ($100/seat/mo), Enterprise, Enterprise+; BYOK on Enterprise tiers only | dbt Cloud-native | Tight metadata integration (lineage, semantic layer); BYOK; no warehouse data egress for training | Locked to dbt Cloud; quality of generated docs/tests rated by users as "starting point, not final product"; no public eval methodology |
| **dbt-mcp** (`dbt-labs/dbt-mcp`) | MCP server exposing dbt Cloud + Core + Fusion to AI agents | Anyone using Claude/Cursor against a dbt project | ~50 tools across SQL, Semantic Layer, Discovery, CLI, Admin, codegen, LSP, Docs | OSS Apache-2.0 | PyPI / `uvx`; remote HTTP variant on dbt Platform | Most complete tool surface; official; both local and remote transports | Remote variant requires dbt Platform; CLI tools can mutate warehouse — README ships an explicit warning |
| **dbt-agent-skills** (`dbt-labs/dbt-agent-skills`) | Anthropic-Skills format markdown packages teaching agents dbt best practices | Claude Code, Cursor, Codex, Factory, Kilo Code users | Skills auto-load on prompt match (not slash commands); includes evals/ A/B testing harness | OSS | GitHub; Vercel Skills CLI; Tessl | Captures workflow knowledge, not just syntax; 13 skills covering core + migration; published Feb 5, 2026; 430 stars in <3 months | Rapid churn (23 open issues); no central registry of community skills yet |
| **Paradime DinoAI** | "Cursor for Data" — full IDE with inline AI, voice, .dinoprompts, Mermaid, GitHub PR mgmt | Teams that don't want dbt Cloud or want a richer IDE | Inline gen, agentic refactor, cost-aware context truncation, voice-to-text, .dinoprompts library | $25/user/mo (infrequent), $55/user/mo (standard), Enterprise custom; DinoAI was preview-bundled, now add-on | SaaS web IDE | Best-in-class IDE UX; explicit cost optimization; PR lifecycle integration; published `dbt-llm-evals` (the only OSS eval framework) | Lock-in to Paradime workspace; smaller ecosystem than dbt Cloud |
| **Altimate dbt Power User** (`AltimateAI/vscode-dbt-power-user`) | VSCode/Cursor extension: lineage, AI docs, health checks, cost est., embedded MCP server | Local-IDE-first dbt Core teams | AI docs gen, AI test gen, SQL translation, embedded MCP for Cursor | Free OSS extension; "Altimate Code" SaaS (10M tokens of Sonnet/Opus/GPT-5 free) | VSCode marketplace; GitHub | 572 stars, 122 forks; embedded MCP avoids round-trip CLI; "#1 on ADE-Bench" claim; column-level lineage | **Stability issues are the dominant complaint**: extension crashes ("Extension host terminated unexpectedly 3 times in 5 mins" — issue #1798), endless parsing loops on dbt 1.11.2, file-detection misses on container restart |
| **Datafold Migration Agent** | AI-powered SQL translation + cross-DB diff for legacy → dbt and dbt → dbt repointing | Enterprises migrating from Informatica/SSIS/Talend/stored procs to dbt; Snowflake → Databricks | LLM SQL translation handles 300k-char stored procs; cross-DB diffing for parity validation | **Outcome-based**: fixed price per legacy object + complexity tier; "no hourly billing, no scope creep" | SaaS + delivery engagement | Genuinely solves a $hundreds-of-K SI problem; 100% parity claim with full validation; 6x faster than alternatives | Migration-only — not a daily-use tool; G2 reviews praise the diff product more than the migration agent specifically |
| **Snowflake Cortex Code** | AI coding agent inside Snowsight; supports dbt + Airflow workflows | Snowflake-first shops | Scaffold dbt models, add tests, run dbt commands, generate docs; Cortex Analyst for NL→SQL; warehouse-native judge models | Bundled with Snowflake credits | Snowsight UI + CLI | GA Mar 9, 2026; native warehouse access; no data egress; 9,100+ accounts on Cortex driving 200%+ AI workload growth | Snowflake-only; less code-centric than Claude Code/Cursor; nascent |
| **Hex Magic** | Notebook-native AI generating chained SQL/Python/Chart cells | Analysts in Hex notebooks | NL→SQL, auto-doc, error fix, Magic cell chains; consumes dbt Cloud metadata | Bundled (incl. free tier) | SaaS notebook | Multi-cell chain generation is genuinely differentiated; live dbt Cloud metadata sync; Data Manager governs context | Notebook scope only — doesn't ship dbt models or PRs back |
| **Cube MCP Server** | MCP exposing the Cube semantic layer to AI agents | Teams using Cube for semantic layer | NL questions answered against governed metrics with chart + summary | Cube pricing (free OSS Core, paid Cloud) | OSS + SaaS; npm/Docker | Solves the "ground the agent in business reality" problem via the semantic layer; agentic D3 product | Requires committing to Cube; less relevant if you use dbt Semantic Layer |
| **Monte Carlo MC Agent Toolkit** | MCP server exposing data observability state to coding agents | dbt teams already on MC | "Asset Health" skill answers "how is table X doing" with monitor coverage + lineage health | Monte Carlo enterprise pricing | SaaS + MCP | Right primitive: trust-check before-change; March 2026 Agent Observability covers 4 pillars (context/perf/behavior/output) | MC pricing barrier for SMB; MCP-only — no agent of its own |
| **Wobby / Actian AI Analyst** | NL→SQL agents on top of warehouse + dbt | Business users on Slack/Teams | Agentic NL→insight, semantic-layer-aware, governance hooks | Acquired by HCLSoftware (now Actian); enterprise pricing | SaaS + Slack/Teams | Ships answers into Slack natively; semantic-layer-aware | Acquired late 2025 → integration uncertainty during 2026 |
| **Ragstar / dbt-llm-agent** (`pragunbhutani/dbt-llm-agent`) | Self-hosted AI data analyst: web dashboard + Slack + MCP server | Teams that want to self-host | RAG over dbt models with pgvector; OAuth-authenticated MCP for Claude.ai | OSS (self-host) | Docker / Next.js+Django | Only meaningful self-hosted competitor to commercial NL-Q&A; 170 stars, active (v1.3.1 Jul 2025) | "Public βeta. Expect rapid changes and occasional rough edges" per README; small team; 4 open issues but only 0 PRs suggests low community contribution |
| **dbt-llm-evals** (`paradime-io/dbt-llm-evals`) | Warehouse-native LLM-as-a-judge eval framework for AI output in dbt projects | Teams shipping AI-generated content (support replies, classifications, etc.) | Snowflake Cortex / Vertex AI / Databricks AI Functions as judge; 5 built-in dimensions (accuracy, relevance, tone, completeness, consistency); baseline detection | OSS | dbt package | Only OSS warehouse-native eval framework; zero data egress; sampling + drift detection | 25 GitHub stars; only 3 warehouses; primarily oriented at *application* output (support replies) not *dbt artifact* output |
| **Elementary `ai_data_validation`** | Plain-English AI-powered data tests in dbt | Existing Elementary users | Test by natural language ("There should be no contract date in the future"); warehouse-native | OSS + Elementary Cloud | dbt package | Easy adoption (it's a dbt test); warehouse-native; no egress | Beta; quality of NL-derived tests not independently audited |
| **dbt-coves** (`datacoves/dbt-coves`) | CLI codegen for sources/staging/yml; AI-assisted via OpenAI/Anthropic/Azure/Gemini | Datacoves customers + OSS users | LLM-assisted yml + staging gen; Airflow DAG gen | OSS | PyPI | Multi-provider LLM; broader than `dbt-codegen` (the Jinja-macro original) | Smaller community than dbt-codegen; not all warehouse adapters tested |
| **dbt-codegen** (`dbt-labs/dbt-codegen`) | dbt-Labs Jinja macros for boilerplate | All dbt users | None — pure macros | OSS | dbt Hub | Stable, widely used | **Not LLM** — listed for contrast; this is what AI-codegen tools displace |
| **GitHub Copilot / Cursor / Claude Code (general)** | Generalist AI coding agents used on dbt repos | All devs | Whatever the agent supports — code completion, chat, agent mode | Per-seat ($10-39/seat/mo for Copilot Business/Enterprise; Cursor $20/seat; Claude Code via Anthropic API/sub) | IDE plugin / CLI | Universal; extensible via MCP + skills | Without dbt MCP/skills they "hallucinate table names"; verdict (Feb 2026 ranking): **Claude Code best for backend/dbt**, Cursor best for dashboards, Copilot best for enterprise scale |

---

## B. The "incumbent map" — jobs-to-be-done

For each common AE job, who's the leader, who's challenger, who's nobody's-yet:

| Job-to-be-done | Leader | Challengers | Nobody-yet / weak coverage |
|---|---|---|---|
| **Generate `schema.yml` from model** | dbt Copilot (in-Cloud), Altimate Power User (in-VSCode) | Paradime DinoAI; dbt-coves (CLI); Loblaw Digital's pattern (Cortex-based) | "Generate schema.yml *and verify it round-trips* against `dbt parse`" — every tool ships generation; almost nothing ships verification |
| **Generate dbt tests** | dbt Copilot; Altimate Power User | Paradime DinoAI; Elementary `ai_data_validation` (NL → warehouse-native AI tests) | **Useful tests.** Universal complaint that AI generates trivial `not_null`/`unique` noise (see C below). No tool reliably proposes tests that catch real bugs |
| **Generate model docs** | dbt Copilot; Paradime DinoAI; Altimate | dbt-coves; Hex (in-notebook); Loblaw blog post pattern | **Verifiably accurate** docs. Generic-sounding output is the dominant complaint |
| **Explain a model in plain English** | Hex Magic (in-notebook); Cortex Analyst | dbt Insights Analyst agent; Wobby/Actian; Ragstar | Cross-project explanations that respect mesh boundaries |
| **Refactor SQL → dbt** | Datafold Migration Agent (best at scale); Claude Code (best for ad-hoc); Cortex Code | Cursor; Paradime DinoAI; Altimate skill | Mid-complexity (hundreds of models, no migration budget) — Datafold over-fits high-end, Claude Code under-fits scale |
| **Detect breaking changes** | Datafold (the diff product, not the agent) | dbt Cloud's CI checks; SQLMesh's intrinsic versioning | LLM-mediated breaking-change *explanation* (not just detection) |
| **Write the PR review comment** | Paradime DinoAI's GitHub PR actions; CodeRabbit (general); Claude Code | dbt Copilot Developer agent | dbt-aware PR review that comments on semantic-layer drift, mesh-contract violations, materialization regressions |
| **Detect stale/dead code** | None purpose-built for dbt | Manual `--select` introspection + `dbt-project-evaluator` | **Wide open.** Any tool that combines manifest + query history could do this; nobody productizes it |
| **Cost optimization advice** | Altimate Power User (cost estimation feature); Cortex Code | dbt Labs cost-optimization features (Fusion-powered) | Specific spend-attribution tied to model lineage with LLM-narrated remediation |
| **Onboarding new users to a project** | dbt Insights Analyst agent; Wobby; Ragstar; Hex Magic | Snowflake Cortex Analyst | Project-tour generators (pull manifest + docs → guided walkthrough) |
| **Lineage Q&A** | dbt-mcp (`get_lineage`, `get_column_lineage`); Atlan; SelectStar | Altimate Power User column lineage; Monte Carlo MC Agent Toolkit | LLM-narrated impact analysis ("if I drop column X, here's what breaks and why") |
| **Test quality evaluation** | **paradime-io/dbt-llm-evals** (only entrant) | DeepEval (general LLM eval, not dbt-specific); dbt-agent-skills' eval/ A/B harness | **Almost nobody.** This is the gap |

---

## C. Quality of LLM output — what users actually say

The single most consistent finding across every source: **AI-generated tests and docs are noisy, generic, or trivially wrong, and users have learned to treat them as drafts**.

Specific patterns from the substack reviews, dbt Developer Blog, Reddit, GitHub issues, and Paradime/Altimate's own marketing copy:

- **Generic docs.** "AI-generated documentation can be effective with the right approach. AI can generate accurate definitions based on the column names, data types, upstream sources, and transformations applied. The key differentiator is context." — restated everywhere, but the implicit admission is that without rich context (warehouse schema + lineage + macros), output is generic.
- **Trivial tests.** From Mikkel Dengsoe's substack and the dbt blog: "Don't blindly accept tests but instead spend time reviewing each of the suggestions… riff with the AI but chip in with your own knowledge and context." Translation: the default output is not directly mergeable.
- **The slop tax.** Industry-wide quote that applies directly: *"If an AI reviewer posts 20 comments and 18 of them are trivial style suggestions, developers will start ignoring all 20 — including the 2 that catch real bugs."* This describes dbt-Copilot-generated test suites in practice.
- **Hallucinated table names.** The Feb 2026 index.app ranking explicitly notes that all three top tools (Claude Code, Cursor, Copilot) "hallucinate table names" without proper MCP/skills grounding. The fix is the dbt-mcp server + dbt-agent-skills, not better models.
- **Stability over quality.** For Altimate dbt Power User specifically, the dominant GitHub-issue complaint is *not* about AI quality — it's about extension crashes ("Extension host terminated unexpectedly 3 times within the last 5 minutes," issue #1798), endless parse loops on dbt 1.11.2, and file-watcher misses inside dev containers. Quality matters less when the tool isn't running.
- **dbt Copilot is well-reviewed inside official channels.** Customer quotes from dbt Labs ("Cody McLean, Sr. Data Engineer at Hard Rock Digital: dbt Copilot has completely changed how we approach documentation and query optimization") are positive but vendor-curated. Independent Reddit/HN coverage is sparse — surprisingly so given dbt's profile.
- **Semantic-layer recommendations rate higher than test-gen.** Multiple secondary sources say the *semantic-model and metric* gen quality in dbt Copilot is "perhaps the highlight" — better than the test/docs generation. This is consistent with the pattern that AI does best when the schema constrains the search space.

**The verdict from the Feb 2026 index.app ranking, paraphrased**: Claude Code wins for backend dbt refactors (terminal-native, multi-file changes), Cursor wins for dashboard UIs, Copilot wins for enterprise speed-to-merge (55% faster task completion, 9.6→2.4 day cycle time). For pure dbt work, Claude Code + dbt-mcp + dbt-agent-skills is the recommended combo.

---

## D. Architectural patterns

The eight visible distribution shapes, with pros/cons:

| Shape | Examples | Pros | Cons |
|---|---|---|---|
| **MCP server (local)** | `dbt-mcp`, Altimate embedded MCP, Cube MCP, Monte Carlo MC Agent Toolkit | Composable; works with any MCP host; easy to ship as `uvx` / `npx` | Each user installs separately; tool discoverability lives outside the server |
| **MCP server (remote/managed)** | dbt-mcp remote, Snowflake Cortex, Ragstar | No install; auth handled centrally; works from web agents | Network latency; vendor lock-in for the auth surface |
| **Anthropic Skills (markdown)** | `dbt-agent-skills` (13 skills), Altimate's Claude Code Skills, dbt Model Generator | Encodes workflow knowledge, not just APIs; auto-loads on prompt match; portable across 30+ agents | Quality entirely depends on the prose; no enforcement mechanism — the skill can say "always X" but the agent may ignore |
| **VSCode/Cursor extension** | Altimate dbt Power User, dbt Power User (innoverio fork) | Lives where AEs already work; can ship UI panels (lineage, lineage diff) | Stability burden — every dbt version bump can break parsing; Microsoft owns the marketplace |
| **CLI tool** | dbt-coves, dbt-codegen, Cortex Code CLI | Scriptable; works in CI; no IDE coupling | Not where most AEs live day-to-day |
| **Web SaaS IDE** | Paradime, dbt Cloud Studio, Hex | Lock-in is a feature for buyers; tightest integration with own backend | Deep moat to displace; users skeptical of vendor-only IDEs |
| **GitHub App / CI bot** | CodeRabbit (general), some dbt teams' homegrown bots | PR-time is the right moment for AI review; async | Latency to merge; review-noise tax |
| **Slack/Teams agent** | Wobby/Actian AI Analyst, Ragstar's `/ask`, Hex Magic | Meets business users where they live | Hard to provide rich context (lineage graphs, SQL diffs) in-chat |

**Distribution insight.** The serious players are stacking shapes: dbt Labs ships an MCP server *and* skills *and* a Cloud-native Copilot. Altimate ships a VSCode extension *and* an embedded MCP *and* Claude Code skills. Single-shape entrants (just-an-MCP, just-an-extension) are losing footprint.

**Stickiness ranking (informally observed):** Web IDE > VSCode extension > MCP server > Skills > CLI. Web IDE moves contracts; CLI moves stars.

---

## E. The eval gap

The single weakest link in this entire ecosystem is **systematic measurement of LLM-output quality on dbt artifacts**. Nobody is doing this well.

**Who evaluates?**

- **Paradime `dbt-llm-evals`** is the only OSS, dbt-native eval framework. It's the warehouse-native LLM-as-a-judge pattern: judge model is one of `llama3-70b` / `mistral-large` / `mixtral-8x7b` (Snowflake), `gemini-pro` (BQ), or `llama-2-70b-chat` (Databricks). 5 built-in dimensions (accuracy, relevance, tone, completeness, consistency) on a 1-10 scale, with baseline detection and drift alerts. **25 stars.** Designed primarily for *application output* (support replies, classifications) — using it on schema.yml or dbt-test output requires the user to supply baselines.
- **dbt Developer Blog (`docs.getdbt.com/blog/ai-eval-in-dbt`)** describes a hand-rolled pattern using Snowflake Cortex Complete as a judge against IMDB sentiment data. Walks through dbt-tests-as-quality-gates with a 75% accuracy threshold. **No reusable framework — it's a worked example.**
- **`dbt-agent-skills/evals/`** ships an A/B testing harness for skill variations (per the README). This evaluates *skill prompts*, not generated artifacts.
- **DeepEval / Confident AI** are the general-purpose alternatives. Powerful, but require the user to design dbt-specific metrics from scratch.
- **Elementary `ai_data_validation`** evaluates *data*, not *AI output about data* — adjacent but different problem.
- **Monte Carlo Agent Observability** (announced March 12, 2026) covers context/performance/behavior/output across 4 pillars — but framed as observability, not as a graded eval that ships with reproducible scores per artifact.

**Why isn't this everywhere?**

1. **It's the part nobody wants to fund.** Eval frameworks don't sell themselves; they make the *other* product look worse before it gets better.
2. **The judge problem is genuinely hard.** Grading "is this dbt test useful" requires domain knowledge the judge LLM mostly lacks. Schema-extraction (does the YAML parse? does it round-trip?) is doable; *quality* grading needs rubric design that nobody's standardized.
3. **Vendor incentive misalignment.** dbt Copilot, DinoAI, and Altimate are all generators. Publishing a rigorous eval would mean publishing their own miss rates.
4. **The standard workflow is "human reviews."** Every blog post says "always validate AI output." That's a dodge — it pushes the cost onto the user instead of automating verification.

**The opening:** A *deterministic + LLM-graded* eval framework that operates on dbt artifacts directly (schema.yml, tests, model SQL), with reproducible scoring and CI integration, is missing. Paradime's framework is closest but is application-output-shaped, not artifact-output-shaped.

---

## F. Strategic openings — where a 2026 entrant can play

Three macro shifts make 2026 the right time:

1. **Fivetran ↔ dbt merger consolidates the platform layer.** Buyers are now considering one vendor for ingest+transform. Smaller AI tools that orbit either side will be either acquired or marginalized. The opening is *outside the merged platform's gravity well*: warehouse-native, OSS-first, or IDE-first plays.
2. **dbt Fusion adoption flips the runtime cost curve.** 30x faster parse means agents can iterate against `dbt parse` / `dbt compile` in tight loops without burning CI minutes. Tools that exploit this (proposer + verifier loops on local Fusion) get a step-change improvement that pre-Fusion tools can't match.
3. **Agentic coding (Claude Code, Cursor agents, Codex) is the new default.** The Feb 2026 ranking is unambiguous that agent-mode is winning. dbt-mcp + skills make the agent dbt-aware. The remaining gap is *quality control on what the agent produces*.

**Specific openings, ranked by tractability:**

### F1. Eval-as-a-product for dbt AI output (highest leverage)

Nobody owns "did the AI's `schema.yml` actually fit my data?" Paradime's `dbt-llm-evals` is application-shaped; Monte Carlo's Agent Observability is enterprise-priced; the dbt Developer Blog's example is hand-rolled per project.

A tool that takes a dbt project, runs the candidate AI generator (any of dbt Copilot, DinoAI, Altimate, Claude Code), grades the output deterministically (does it parse, do tests pass, does the lineage stay coherent) *and* via LLM-as-judge (is the doc accurate to the SQL, does the test catch real bugs), and ships reproducible scores into CI — would have no direct competitor today. This is the *clauditor pattern* applied to dbt.

### F2. The "useful test" generator

Universal complaint: AI-generated tests are trivial. A generator that uses query history + production-failure history + schema to propose tests that catch *historical incidents* would be differentiated. Datafold has the diff piece; nobody combines it with AI test proposal.

### F3. Mesh-aware PR reviewer

dbt Mesh + contracts opened a class of breaking-change failures (downstream contract violations, semantic-model drift) that current PR reviewers (CodeRabbit, dbt Cloud CI) catch only structurally. An LLM-mediated reviewer that explains *why* a contract change is breaking, with downstream impact narration, would slot above generic Copilot review.

### F4. Stale-code / dead-model detector

Wide open. Combine `manifest.json` + warehouse query history → "these 47 models haven't been queried in 90 days; here's the kill list with rationale." LLM is the narrator, not the detector.

### F5. Local Fusion-powered proposer + verifier loop

Fusion's 30x-faster parse makes proposer/verifier-style agents cheap. A CLI agent that *proposes* a model change, *verifies* via `dbt parse` + `dbt build` against a sample, and *iterates* until the verification passes — would beat any current single-shot generator on quality. dbt-mcp gives the surface; nobody's shipping the loop.

### F6. Warehouse-cost-attributed advisor

Altimate's cost estimation is the first stab. The full version: ingest warehouse query/credit history, attribute to dbt models, narrate optimization opportunities ("change this `view` to `incremental`, save $X/month"). LLM does the narration and the SQL rewrite; the attribution math is the moat.

### F7. Skills-marketplace differentiation

The Anthropic Skills standard is now adopted by OpenAI/Codex too (Dec 2025). 160k+ skills indexed on SkillsMP. dbt-agent-skills is the official 13-skill collection. The opening: domain-specific skill packages (industry verticals, warehouse-specific, BI-tool-specific) that ship with their own evals so users can trust them. "Verified skills" is a market that doesn't exist yet.

---

## G. Sources & repos

**Official dbt Labs**
- dbt Copilot docs: https://docs.getdbt.com/docs/cloud/dbt-copilot
- dbt Copilot blog (GA): https://www.getdbt.com/blog/dbt-copilot-is-ga
- dbt-mcp repo (544⭐, 39 issues, last push 2026-04-24): https://github.com/dbt-labs/dbt-mcp
- dbt-agent-skills repo (430⭐, 23 issues): https://github.com/dbt-labs/dbt-agent-skills
- dbt-fusion repo (690⭐, 386 issues): https://github.com/dbt-labs/dbt-fusion
- Agentic coding blog: https://www.getdbt.com/blog/agentic-coding-in-analytics-engineering
- Bring structured context to agentic data dev: https://www.getdbt.com/blog/bring-structured-context-to-agentic-data-development-with-dbt
- AI eval in dbt blog: https://docs.getdbt.com/blog/ai-eval-in-dbt
- dbt-codegen: https://github.com/dbt-labs/dbt-codegen
- Analyst agent docs: https://docs.getdbt.com/docs/dbt-ai/analyst-agent
- Fivetran merger announcement: https://www.getdbt.com/blog/dbt-labs-and-fivetran-sign-definitive-agreement-to-merge

**Paradime**
- DinoAI vs dbt Copilot comparison: https://www.paradime.io/blog/paradime-dinoai-vs-dbt-copilot-a-comparative-analysis
- DinoAI features: https://www.paradime.io/blog/dinoai-features-build-faster-spend-less-dbt-development
- DinoAI Context: https://www.paradime.io/blog/introducing-dinoai-context-supercharging-analytics-engineering-workflows
- dbt-llm-evals repo (25⭐): https://github.com/paradime-io/dbt-llm-evals
- dbt-llm-evals blog: https://www.paradime.io/blog/get-started-with-dbt%E2%84%A2-llm-evals-warehouse-native-llm-evaluation-in-15-minutes
- LLM eval criteria: https://www.paradime.io/blog/llm-evaluation-criteria-how-to-measure-ai-quality

**Altimate**
- vscode-dbt-power-user (572⭐, 132 issues): https://github.com/AltimateAI/vscode-dbt-power-user
- Embedded MCP for Cursor: https://blog.altimate.ai/supercharging-cursor-ide-how-the-dbt-power-user-extensions-embedded-mcp-server-unlocks-ai-driven-dbt-development
- Claude Code Skills: https://blog.altimate.ai/teaching-claude-code-the-art-of-data-engineering-introducing-altimate-skills
- Stability issue #1798: https://github.com/AltimateAI/vscode-dbt-power-user/issues/1798
- Endless parse loop: https://discourse.getdbt.com/t/dbt-power-user-extension-endless-parsing-loop-on-dbt-1-11-2/20563

**Datafold**
- Migration agent blog: https://www.datafold.com/blog/data-migrations-reimagined-introducing-the-ai-powered-datafold-migration-agent/
- Pricing: https://www.datafold.com/pricing
- G2 reviews: https://www.g2.com/products/datafold/reviews

**Snowflake Cortex**
- Cortex Code GA Mar 2026: https://docs.snowflake.com/en/release-notes/2026/other/2026-03-09-cortex-code-snowsight-ga
- Cortex + dbt blog: https://www.snowflake.com/en/blog/cortex-code-governed-agent-data-stack/
- Cortex Analyst docs: https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst
- Conversational analytics with dbt + Cortex: https://docs.getdbt.com/blog/semantic-layer-cortex
- LLM-powered AE with Cortex (dbt Developer Blog): https://docs.getdbt.com/blog/dbt-models-with-snowflake-cortex

**Other tools / repos**
- Hex Magic: https://hex.tech/product/magic-ai/
- Hex × dbt: https://hex.tech/product/integrations/dbt/
- Cube MCP server docs: https://cube.dev/docs/product/apis-integrations/mcp-server
- Monte Carlo MC Agent Toolkit: https://www.montecarlodata.com/mc-agent-toolkit/
- MC Agent Observability launch: https://www.apmdigest.com/monte-carlo-launches-agent-observability
- Wobby × dbt: https://www.wobby.ai/integrations/dbt
- Wobby acquisition by HCLSoftware: https://www.actian.com/company/press-releases/hclsoftware-to-acquire-ai-data-analyst-agents-startup-wobby/
- Ragstar / dbt-llm-agent (170⭐): https://github.com/pragunbhutani/dbt-llm-agent
- dbt-coves: https://github.com/datacoves/dbt-coves
- Elementary AI data validation docs: https://docs.elementary-data.com/data-tests/ai-data-tests/ai_data_validations
- Atlan dbt catalog: https://atlan.com/dbt-data-catalog/
- SelectStar dbt docs sync: https://www.selectstar.com/resources/dbt-docs

**Independent commentary**
- Feb 2026 AI coding tools ranking for data teams: https://index.app/blog/ai-coding-tools-data-teams-claude-cursor-copilot-ranked
- Why we aren't achieving the Copilot experience in data systems: https://clkao.substack.com/p/why-we-arent-achieving-the-copilot
- Cursor for analytics (paywalled): https://learnanalyticsengineering.substack.com/p/cursor-for-analytics-where-it-fails
- Mikkel Dengsoe — AI for testing: https://mikkeldengsoe.substack.com/p/using-ai-to-build-a-robust-testing
- Loblaw Digital — LLM dbt docs: https://medium.com/loblaw-digital/leveraging-llms-to-generate-ai-driven-dbt-documentation-c4735faa6ca5
- "AI-Generated dbt Models Are Actually Good Now (I Tested 50 of Them)": https://medium.com/@reliabledataengineering/ai-generated-dbt-models-are-actually-good-now-i-tested-50-of-them-b87bd82bc7c2
- Awesome-dbt curated list: https://github.com/Hiflylabs/awesome-dbt

---

## H. One-paragraph TL;DR for a strategy memo

dbt Labs has effectively boxed in the AI-on-dbt-Cloud surface area with Copilot, the Insights Analyst agent, dbt-mcp, and dbt-agent-skills. Paradime and Altimate own the "richer-than-Cloud IDE" and "VSCode/Cursor extension" niches respectively; both ship MCP servers and skills now. Datafold owns large-scale migration. Snowflake Cortex Code (GA Mar 2026) and Hex Magic own warehouse-native and notebook-native generation. The Fivetran merger turned the platform story into ingest-to-activation; the runway underneath is **quality assurance on AI-generated dbt artifacts** — schema.yml, tests, docs, semantic-model definitions — where Paradime's `dbt-llm-evals` (25 stars) is the only OSS attempt and is application-output-shaped, not artifact-output-shaped. A clauditor-style framework that treats dbt AI generation as something to *evaluate*, not just *invoke*, has no direct incumbent and aligns with the "verifier-cheap on Fusion" runtime shift coming through 2026.
