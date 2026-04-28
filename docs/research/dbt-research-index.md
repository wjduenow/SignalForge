# DBT Tooling Research — Master Index

Compiled 2026-04-25 to inform a decision on whether to build an LLM-leveraged DBT tool that synergizes with `clauditor` (this repo's LLM-graded skill evaluation framework).

## The five research artifacts

1. **[dbt-tooling-opportunity-report.md](dbt-tooling-opportunity-report.md)** — initial scan: top 10 pain points, full tooling landscape table, seven LLM-shaped opportunities ranked, strategic notes, sources.
2. **[dbt-pain-deep-dive.md](dbt-pain-deep-dive.md)** (~5,400 words) — voice-of-the-user research with direct quotes, failure stories, indirect signals across writing/testing/validating/maintaining. 40+ sourced quotes from Pedram Navid, Tristan Handy, Max Halford, dbt Discourse, GitHub issues.
3. **[dbt-ai-tools-deep-dive.md](dbt-ai-tools-deep-dive.md)** (~4,225 words) — every AI/LLM tool in dbt as of April 2026: comparative matrix, incumbent map across 12 jobs-to-be-done, quality-of-output assessment, architectural patterns, the eval gap, strategic openings. Live GitHub data (stars, last-push, open issues).
4. **[dbt-tool-design-sketches.md](dbt-tool-design-sketches.md)** (~7,800 words) — full design treatment of three top opportunities (PR Companion, YAML Forge, ProcMigrate) with named-persona user journeys, mock UX, technical architecture, hard problems, MVP scope, path-to-100-users, 12-month evolution, why-not. Cross-cutting recommendation matrix.
5. **[dbt-claude-technical-surface.md](dbt-claude-technical-surface.md)** (~5,300 words) — implementer reference: dbt artifact schemas (manifest v9–v20), dbt MCP server tool inventory, Anthropic SDK / Agent SDK / Claude Code subprocess surfaces, warehouse integration patterns, CI/CD shapes with action.yml snippets, output integration, OSS templates to learn from, token economics ($0.11–$0.80/PR).

Total: ~28,000 words of research across five files.

## Headline conclusion

**Build a YAML/tests/docs generator with quality eval baked in** ("YAML Forge"-shaped). The decisive factors:

- **Synergy with clauditor (10/10).** The Phase-3 rubric grader is literally `quality_grader.grade_quality()` applied to a new artifact class. Every clauditor improvement compounds; every Forge dollar funds clauditor R&D.
- **The eval gap is the only durable moat.** Every vendor ships generators (dbt Copilot, Paradime, Altimate, codegen). Almost nobody systematically grades their own output on dbt artifacts. The single OSS dbt-native eval framework (`paradime-io/dbt-llm-evals`, 25⭐) targets application output, not artifact quality.
- **The "drop always-pass tests" pruning premise is a story** that no incumbent tells. Stories drive adoption.
- **Solo-dev feasibility (7/10).** The LLM work IS the core value, unlike PR Companion (mostly integration plumbing) or ProcMigrate (agent loop + dialects + parity = years of work).

Honest tradeoffs:
- Forge has the *lowest* pain severity (6/10) — chronic, not acute. PR Companion (7/10) and ProcMigrate (10/10) have more urgent pain but worse defensibility / capital requirements for a solo dev.
- ProcMigrate has the biggest dollar TAM but is sales-led, capital-intensive, brutally competed by Datafold's existing Migration Agent. Wrong shape.
- PR Companion is the recommended **year-2 expansion** after Forge proves the clauditor-grading-as-product pattern.

Final scoring (out of 60): YAML Forge **46** > PR Companion 43 > ProcMigrate 38.

## Top three pain points to anchor on

From the deep-dive (with quotes traceable in `dbt-pain-deep-dive.md`):

1. **schema.yml drudgery** — the most-cited single complaint. Hand-writing column descriptions and tests, keeping them in sync with SQL. `dbt-codegen` and dbt Copilot help but neither evaluates output quality.
2. **Test coverage is noise, not signal** — ~1% column unit-test coverage typical. Hundreds of `not_null`/`unique` tests that catch nothing. Unit tests (1.8) have YAML-fixture friction that killed adoption. The "green CI, broken dashboards" failure mode is the canonical horror story.
3. **PR review is vibes** — reviewers can't see what data changed or what breaks downstream without manual checkout. Slim CI tests SQL syntax, not data. Datafold solves it for $30k–$75k/yr.

YAML Forge attacks #1 and #2 directly. PR Companion would attack #3 in year 2.

## Strategic context (April 2026)

- **dbt Labs + Fivetran merger (Oct 2025)** — community fear of Cloud-only features driving Core-native interest. Tailwind for OSS-first tooling.
- **dbt Fusion engine (Rust, May 2025 beta)** — manifest v20 is additive over v12; readers continue working. Cross-engine artifact mixing breaks Recce-style diffs.
- **dbt MCP Server (2025)** — 544⭐, 39 issues, last push 2026-04-24. Most interesting tools (Discovery, Admin, Semantic Layer) are dbt Cloud-only. For OSS-first tools, depending on dbt-mcp over direct artifact reads has questionable ROI.
- **dbt-labs/dbt-agent-skills (Feb 2026)** — 430⭐ in under 3 months. The Skills shape is winning as a substrate.
- **Altimate dbt Power User stability complaints** dominate over AI-quality complaints — incumbents are losing on basic reliability, not on intelligence.

## Recommended next moves

If the user wants to validate this direction:

1. **Spike**: Build a 1-day prototype that loads `manifest.json` for a real dbt project, picks one model, asks Claude to draft schema.yml, samples warehouse data via `dbt show`, runs each candidate test, prunes always-pass. Measure: are the surviving tests actually useful? (This is the load-bearing assumption.)
2. **Validate the eval rubric**: Define a clauditor-style rubric for "good schema.yml" — coverage, accuracy, terminology consistency, no-noise-tests. Grade 5 hand-written examples and 5 LLM examples. Does the rubric discriminate? (This is whether clauditor's grading methodology survives in this domain.)
3. **Distribution test**: Post the prototype to dbt Slack #tools-and-utilities and r/dataengineering with a "what would make this useful?" framing. Volume of replies = demand signal.

If signals are positive, the YAML Forge MVP is shippable in 2-3 days per the design doc.

## File map

```
docs/temp/
├── dbt-research-index.md             ← you are here
├── dbt-tooling-opportunity-report.md ← executive scan (start here)
├── dbt-pain-deep-dive.md             ← user voice
├── dbt-ai-tools-deep-dive.md         ← competitive landscape
├── dbt-tool-design-sketches.md       ← three full designs + recommendation
└── dbt-claude-technical-surface.md   ← implementer reference
```

Read order for a builder evaluating viability: opportunity-report → pain-deep-dive → ai-tools-deep-dive → tool-design-sketches → technical-surface.

Read order for a builder ready to start: tool-design-sketches (Forge section) → technical-surface → opportunity-report Section 4.
