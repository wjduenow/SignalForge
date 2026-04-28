# Contributing to SignalForge

SignalForge is pre-alpha and designing in the open. The differentiator is the prune step — generation that grades itself against real warehouse data — so the bar for new artifact classes and new code paths is "does this respect signal-over-volume?" Contributions that hold that line are welcome.

## Branching

- Feature branches are `feature/<n>-<short-name>` off `dev` (e.g., `feature/2-bigquery-adapter`).
- PRs land into `dev`. `main` is the released line — only `dev` → `main` merges.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # quoted for zsh; bash also accepts this form
```

Validate before pushing:

```bash
ruff check . && ruff format --check . && pyright && pytest
```

## Test markers

Tests are tagged with `@pytest.mark.{unit, integration, error}` (declared in
`pyproject.toml`). Run a single category with `pytest -m unit`. New tests
SHOULD use a marker; bare tests are fine for true smoke checks.

## Regenerating fixtures

Fixture regen lives in [`tests/fixtures/README.md`](tests/fixtures/README.md).
v12 is a one-liner against the in-`[dev]` `dbt-core` install; older schemas
(v9 / v10 / v11) use ephemeral `uvx` invocations.

## License

Contributions are Apache-2.0. The repo-level [LICENSE](LICENSE) covers it — do not add per-file license preambles.

## Issues

File issues at https://github.com/wjduenow/SignalForge/issues. v0.1 is design-in-the-open on `dev`; expect the shape of things to move.

## Out of scope for this iteration

`bark`, `/super-plan`, and `bd` are internal tooling, not contributor expectations. Tracked under #13.
