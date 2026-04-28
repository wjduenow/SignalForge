# Research snapshot

This folder is a pinned snapshot of seven dbt research files vendored from
[clauditor](https://github.com/wjduenow/clauditor)'s `docs/temp/` directory
(per DEC-006 of `plans/super/2-manifest-loader.md`). The originals live in
clauditor's gitignored `docs/temp/` tree, so without this copy, anyone outside
the maintainer's machine could not read the references that informed
SignalForge's manifest-loader design. Treat this folder as a snapshot — not a
sync source — and update it deliberately.

## Files

- **`dbt-claude-technical-surface.md`** — implementer-facing reference for
  Claude-powered dbt tooling: artifact schemas, MCP surface, SDK options,
  warehouse integration, CI shapes, token economics. **Section 1.1 is the
  canonical schema reference for the manifest module** — it informed
  DEC-001 (manifest-only scope), DEC-011 (schema-version detection via the
  `metadata.dbt_schema_version` URL), DEC-012 (fixture matrix shape), and
  DEC-017 (the resolver / `iter_models` / `schema_version` API surface).
- **`dbt-research-index.md`** — master index across the five research
  artifacts; the entry point if you're skim-reading the corpus.
- **`dbt-research.pdf`** — binary PDF compilation of the underlying research;
  supplementary, not load-bearing for code.
- **`dbt-ai-tools-deep-dive.md`** — survey of every meaningful AI/LLM tool in
  or adjacent to dbt as of April 2026 (Copilot, codegen, DinoAI, datapilot,
  Cortex Code), with user complaints and gap analysis.
- **`dbt-pain-deep-dive.md`** — direct quotes and failure stories from HN,
  Reddit, Substack, and dbt Slack about what hurts when running dbt in
  production; the source for SignalForge's "signal over volume" framing.
- **`dbt-tool-design-sketches.md`** — three concrete tool designs evaluated
  for viability; SignalForge is the design that survived.
- **`dbt-tooling-opportunity-report.md`** — product-framing synthesis,
  including the top-10 ranked pain points; the companion to
  `dbt-claude-technical-surface.md`.

## Refreshing this snapshot

If the originals update in clauditor, the maintainer can refresh this folder
in place:

```bash
cp clauditor/docs/temp/dbt-*.md docs/research/
cp clauditor/docs/temp/dbt-research.pdf docs/research/
```

Contributors outside the maintainer's machine can read this committed
snapshot but cannot reach the live clauditor copy — that's the point of
vendoring per DEC-006.
