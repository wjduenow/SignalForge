# Docs publishing (MkDocs Material → GitHub Pages)

The published documentation site at https://wjduenow.github.io/SignalForge/ is built by MkDocs Material on every push to `main` and pushed to the `gh-pages` branch. The setup mirrors clauditor's docs-publishing pattern.

## Two jobs: `docs-build` (always) + `docs` (deploy, main only)

- **`docs-build`** runs on every PR and push — a read-only "does the site still build?" gate. It runs `uv run --only-group docs mkdocs build` with NO write permissions. A broken `mkdocs.yml`, plugin config, or include-markdown syntax error fails the PR here instead of silently merging and only breaking the post-merge deploy.
- **`docs`** (deploy) is gated by `if: github.ref == 'refs/heads/main' && github.event_name == 'push'`. Dev pushes do NOT redeploy — the published site reflects the released line, not every in-flight iteration. The dev → main release merge IS the publish event. PRs against `main` don't trigger the deploy job either; only the post-merge push does.

The split matters: without the always-on `docs-build` gate, the deploy job (main-only) is the *first* place a doc-config regression surfaces, which is too late. Keep both.

## `docs` dependency group is separate from `dev`

`[dependency-groups].docs` carries ONLY `mkdocs-material` + `mkdocs-include-markdown-plugin`. Both CI docs jobs `uv run --only-group docs ...` so they pull just MkDocs + plugins — not the heavy `dev` set (`dbt-core`, `pyright`, etc.). The `dev` group includes `docs` via `{include-group = "docs"}`, so a single `uv sync --dev` still gives a contributor everything (docs preview included). When adding a new docs-only tool, put it in the `docs` group; when adding a dev tool that isn't needed to build docs, put it in `dev` directly.

## `docs/index.md` is a 4-line include-markdown stub

```markdown
{%
  include-markdown "../README.md"
  rewrite-relative-urls=true
%}
```

The root `README.md` stays the canonical authored "home" doc. The site's home page is auto-synced on every build via the `mkdocs-include-markdown-plugin`. Don't author a separate `docs/index.md` body — drift between the README and the site is exactly what this stub exists to prevent. The `rewrite-relative-urls=true` flag fixes relative links so they keep working when the README content renders under `/index.html` instead of repo-root.

When the README adds a new section, the site picks it up next push to main. No mkdocs.yml edit needed unless you want the section in the top-nav.

## `exclude_docs: research/` keeps internal analysis off the published site

`docs/research/` contains internal analysis (dbt-pain-deep-dive, dbt-research-index, etc.) used to drive product direction — not user-facing. The `exclude_docs:` block in `mkdocs.yml` keeps them out of the build. Adding a new research doc requires no mkdocs.yml change; adding a new user-facing doc DOES require a `nav:` entry.

## `edit_uri: edit/dev/docs/` — edits land on dev, not main

The "Edit this page" link on each rendered doc page targets the `dev` branch, not `main`. Doc edits land on dev (where every other PR work happens) and reach the published site after the next dev → main release. This keeps the doc-edit workflow aligned with the code-edit workflow; the alternative (`edit/main/docs/`) would bypass dev review.

## `persist-credentials` is the inverse of the lint-test/publish job

`actions/checkout` in the docs job deliberately does NOT set `persist-credentials: false`. `mkdocs gh-deploy` issues a real `git push` to the `gh-pages` branch; it relies on the persisted GITHUB_TOKEN in the runner's git config. The token's scope is bounded by `permissions.contents: write` at the job level (the workflow default stays `contents: read`), so this is the principle of least privilege at the *job* level even though the *step* persists credentials. The runner's post-job cleanup clears the token; this is the recommended pattern for the rare workflow that legitimately needs to push to a branch.

## Build the site locally

```bash
uv sync --dev
uv run mkdocs serve  # localhost:8000 with hot reload
uv run mkdocs build  # writes site/ — same recipe CI uses
```

Do NOT use `--strict` locally. The ops docs link to `plans/super/*.md`, `docs/rules/*.md`, and other repo-internal paths that are deliberately not part of the published site; `--strict` rejects them as broken links. The non-strict build still emits the warnings to stdout — useful as a sanity check that the doc set is internally consistent, but not a CI gate.

## When to update mkdocs.yml vs the docs themselves

- **New ops doc under `docs/`** (e.g. `docs/foo-ops.md`) → add a `nav:` entry under "Pipeline Stages" or a new section.
- **New research doc under `docs/research/`** → no mkdocs.yml change (covered by `exclude_docs:`).
- **Renaming an existing ops doc** → update `mkdocs.yml` `nav:` in the same commit; otherwise the nav link 404s.
- **Theme tweaks** → adjust `theme:` block. Keep palette toggle (DEC: accessibility floor).
- **New plugin** → add to `[dependency-groups].docs` in `pyproject.toml` (NOT `dev` — the `docs` group is what the CI jobs sync; `dev` picks it up via `include-group`) AND `plugins:` in mkdocs.yml.

## First-time setup the maintainer does once

After the first deploy lands a `gh-pages` branch on the repo, enable GitHub Pages:

1. **Settings → Pages → Build and deployment**: source = "Deploy from a branch", branch = `gh-pages`, folder = `/ (root)`.
2. The first publish takes ~1 minute to propagate. After that, every push to `main` triggers a redeploy within ~30 seconds of CI completion.

The deploy is idempotent — `--force` on `mkdocs gh-deploy` overwrites the prior gh-pages commit (the site is generated, not authored). `--no-history` keeps the gh-pages branch shallow so the repo stays small.

## Reference

`mkdocs.yml` — current site config. `.github/workflows/ci.yml` § `docs:` — the deploy job. `docs/index.md` — the include-markdown stub. clauditor's docs-publishing setup — the precedent this mirrors.
