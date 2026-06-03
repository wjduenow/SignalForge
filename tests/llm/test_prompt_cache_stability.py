"""Cache-stability snapshot for the LLM-drafter prompt (US-014 / DEC-019).

The cached block produced by :func:`signalforge.draft.prompts.render_prompt`
is the prefix Anthropic's prompt cache keys on. Any byte-level change to
that block invalidates every cached prefix in flight — a silent cost
regression. The :data:`signalforge.draft.prompts._PROMPT_VERSION` hash
covers the *templates*; this test covers the *rendered output* for the
canonical fixture (the load-bearing part for cache stability).

On mismatch, the assertion message includes a :func:`difflib.unified_diff`
so the regression is reviewable in PR.

Pinned ``_PROMPT_VERSION``: tracked by :data:`_EXPECTED_PROMPT_VERSION`
below — the constant is the source of truth, this docstring deliberately
does not hard-code a hash so it can't drift. Latest known rotations:

- ``1c55806467984090`` — original pin from #5 / DEC-019.
- ``c7d15d59f78bab2d`` — rotated under #10 when an explicit JSON-shape
  example was added to the system prompt (DEC-025 of #10).
- ``8a0d81994275b803`` — rotated under #10 review feedback when the
  JSON example's outer ```json fence was removed (the prompt instructs
  "do not wrap in markdown fences" so showing a fenced example was a
  foot-gun; CodeRabbit/Copilot both flagged it).
- ``2563a71c5e31f0db`` — rotated under #54 when the system
  prompt became parameterised by ``DraftConfig.exclude_tests`` (the
  test catalogue + SCOPE line are now templated). The default render
  (no exclusions) is semantically identical to the prior text but
  has a different line-wrap in the SCOPE paragraph because the enum
  list is now rendered via :func:`str.format` rather than a literal.
- ``2e465018c1f6db22`` — rotated under #116 when the
  ``custom_sql`` singular-business-rule test type was added to the
  system prompt's test catalogue + SCOPE section (DEC-001 / DEC-015).
  Operator-supplied business rules render into the DYNAMIC (non-cached)
  block, so the cached-block golden below is unchanged — only the
  system prompt (and therefore ``_PROMPT_VERSION``) rotated.
- ``c9e7ee1f6f465933`` — rotated under #117 review feedback
  (CodeRabbit/Copilot) when ``custom_sql`` was made to participate in
  ``exclude_tests`` filtering: the SCOPE phrase now reads
  "Propose only ..., plus ``custom_sql`` tests" and both the catalogue
  line and the custom_sql instruction are omitted when ``custom_sql`` is
  excluded. Only the system prompt changed; the cached-block golden is
  unchanged.
- ``77e9ee8a6ae7d875`` — rotated under #169 (DEC-012) when
  ``row_count_between`` was added to ``_TEST_CATALOGUE_LINES`` as a
  6th first-class test primitive. The new catalogue entry illustrates
  BOTH the no-``where`` and with-``where`` shapes so the drafter has
  two forms to mirror. Only the system prompt changed; the cached-block
  golden (manifest summary) is unchanged.
- ``389c8aa970df86cc`` — rotated under #170 (DEC-002, US-003)
  when ``unique_combination`` was added to ``_TEST_CATALOGUE_LINES`` as
  the 7th first-class test primitive. The new catalogue entry illustrates
  BOTH the no-``where`` and with-``where`` shapes (composite uniqueness
  whole-table vs. filtered). A new ``_UNIQUE_COMBINATION_SCOPE_INSTRUCTION``
  block was added to the SCOPE section with cautionary prose steering the
  drafter away from vacuously-unique tuples like ``(pk, anything)``.
  Only the system prompt changed; the cached-block golden (manifest
  summary) is unchanged.
- ``a4fea640b3b60f24`` — rotated under #171 (DEC-007, US-005)
  when ``row_count_anomaly_by_period`` was added to ``_TEST_CATALOGUE_LINES``
  as the 8th first-class test primitive. The new catalogue entry
  illustrates THREE forms (bare default-method call; ``seasonality="dow"``
  for business-calendar grain; explicit ``method`` + ``threshold``
  override). A new ``_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION`` block was
  added to the SCOPE section teaching the "propose this variant when
  the projection includes ``loaded_at`` / ``created_at`` / ``event_date``
  / ``partition_date``" heuristic plus per-method calibration prose.
  Only the system prompt changed; the cached-block golden (manifest
  summary) is unchanged.
- ``e568fb3e4602e465`` — rotated under #183 (US-001) when the
  missing ``_ROW_COUNT_BETWEEN_SCOPE_INSTRUCTION`` block was added to the
  SCOPE section (the ``row_count_between`` primitive shipped its catalogue
  line in #169 but never got a dedicated narrative scope-instruction block
  like its two sibling variants). The block embeds the DEC-012 worked
  example, teaching the "propose this when the SQL shows a bounded
  aggregation whose cardinality is predictable from the grain" heuristic
  plus the ``minimum``/``maximum``/``where`` calibration prose. Only the
  system prompt changed; the cached-block golden (manifest summary) is
  unchanged.
- ``32d33f14a3a57060`` — current. Rotated under #184 when
  ``_ROW_COUNT_ANOMALY_SCOPE_INSTRUCTION`` gained explicit model-vs-column
  scope teaching (DEC-001 / DEC-007). The rewritten prose adds the
  verbatim sentence "This test goes in the model-level ``tests:`` list,
  NOT inside any column's ``tests:`` list — the ``date_column`` argument
  names the column but the test itself is model-scoped" plus a worked
  YAML example showing model-level placement under a surrounding
  ``models:`` / ``tests:`` structure. All pre-#184 calibration prose
  (incremental-fact-table heuristic, dow seasonality, method defaults,
  the four example column names) is preserved verbatim. Only the system
  prompt changed; the cached-block golden (manifest summary) is unchanged.

If this rotates again, update both :data:`_EXPECTED_PROMPT_VERSION` and
:data:`_CACHED_BLOCK_GOLDEN` in lockstep — the rotation is the signal
that the templates changed.

Project-scope golden (#188 US-006)
----------------------------------

Issue #188 (bulk-cache-prefix) adds a second, orthogonal cache prefix:
the *project* scope. When ``signalforge generate --select <expr>`` matches
≥ 2 models, the cached block is restructured to a project-wide shared
prefix (one ``- <name> (<N> cols)`` line per model, wrapped in a
``<PROJECT_MANIFEST>`` envelope) that is byte-identical across every model
in the batch — that byte-identity is the cache-hit precondition (DEC-007).

This module now pins BOTH scopes:

- **Per-model** — :data:`_EXPECTED_PROMPT_VERSION` /
  :data:`_CACHED_BLOCK_GOLDEN` (unchanged from above; single-model runs
  keep this byte-for-byte).
- **Project** — :data:`_EXPECTED_PROMPT_VERSION_PROJECT` (the
  ``_PROMPT_VERSION_PROJECT`` *base* constant) /
  :data:`_CACHED_BLOCK_GOLDEN_PROJECT` (the rendered ``<PROJECT_MANIFEST>``
  cached block for the canonical multi-model fixture).

Lockstep rotation policy (the two scopes rotate on DISJOINT template sets):

- **Per-model** ``_PROMPT_VERSION_PER_MODEL`` rotates on any byte change to
  ``_SYSTEM_PROMPT`` (the per-model system-prompt variant — no
  ``<PROJECT_MANIFEST>`` defence line), ``_MANIFEST_SUMMARY_TEMPLATE``, or
  ``_DATA_SECTION_TEMPLATES``. A rotation here updates
  :data:`_EXPECTED_PROMPT_VERSION` + (if the manifest-summary render itself
  changed) :data:`_CACHED_BLOCK_GOLDEN`.
- **Project** ``_PROMPT_VERSION_PROJECT`` rotates on any byte change to the
  *project-scope* ``_SYSTEM_PROMPT`` variant (which carries the
  ``<PROJECT_MANIFEST>`` injection-defence line, DEC-005),
  ``_PROJECT_SUMMARY_TEMPLATE``, or ``_DATA_SECTION_TEMPLATES``. A rotation
  here updates :data:`_EXPECTED_PROMPT_VERSION_PROJECT` + (if the
  project-summary render itself changed) :data:`_CACHED_BLOCK_GOLDEN_PROJECT`.

``_DATA_SECTION_TEMPLATES`` is the only shared input — editing it rotates
BOTH versions, so both goldens-and-versions must be re-captured together.

The two versions MUST differ (project folds in the extra defence line +
project-summary template); :func:`test_project_version_differs_from_per_model`
pins that invariant so a future refactor that accidentally collapses the
two bases fails loud.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from signalforge.draft.prompts import (
    _PROMPT_VERSION_PER_MODEL,
    _PROMPT_VERSION_PROJECT,
    render_prompt,
)
from signalforge.manifest.models import Manifest
from signalforge.safety import LLMRequest, SamplingMode

pytestmark = pytest.mark.llm


_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "draft"
    / "manifest_one_model_with_neighbours.json"
)


_EXPECTED_PROMPT_VERSION: str = "32d33f14a3a57060"


# Captured once via ``render_prompt`` against the canonical fixture below.
# Any byte-level change to the cached block — model description text,
# column rendering, neighbour ordering — will rotate this snapshot. The
# golden value is intentionally inline (not a fixture file) so reviewers
# see the diff in the PR rather than chasing a separate file.
_CACHED_BLOCK_GOLDEN: str = """\
## Model under draft

Name: fct_orders
Description: Order fact table joined to the customer dimension.

Columns:
- amount (NUMERIC)
- customer_id (STRING)
- order_id (STRING)
- ordered_at (TIMESTAMP)

## Neighbouring models

### dim_customers

Description: Customer dimension keyed by customer_id.

Columns:
- country_code (STRING): ISO 3166-1 alpha-2 country code.
- created_at (TIMESTAMP): Customer record creation timestamp.
- customer_id (STRING): Primary key for a customer.
- customer_name (STRING): Display name for the customer.

### stg_orders

Description: Staged orders pulled from the raw orders source.

Columns:
- amount_cents (INT64): Order total in cents.
- customer_id (STRING): Foreign key to dim_customers.customer_id.
- order_id (STRING): Surrogate key from the source orders table.
- ordered_at (TIMESTAMP): Wall-clock timestamp the order was placed.
"""


_EXPECTED_PROMPT_VERSION_PROJECT: str = "1bfec8385707a6ca"
"""The ``_PROMPT_VERSION_PROJECT`` *base* constant (#188 US-006, DEC-009).

This is the project-scope base hash — ``blake2b-8`` over the project-scope
system prompt + ``_PROJECT_SUMMARY_TEMPLATE`` + ``_DATA_SECTION_TEMPLATES``
JSON (DEC-014: the project version rotates on the *project* template, NOT
``_MANIFEST_SUMMARY_TEMPLATE`` which governs only the per-model block). It is the value
:func:`test_project_prompt_version_pinned` asserts against
``signalforge.draft.prompts._PROMPT_VERSION_PROJECT``.

Note: this is NOT what ``render_prompt(..., cache_scope="project")`` returns.
``_prompt_version_for((), "project")`` folds a ``"|scope=project|exclude=[]"``
suffix into a fresh hash because the scope dimension is non-default (only the
per-model / no-exclusions case short-circuits to the bare base). The composed
return value is pinned separately by
:func:`test_project_render_prompt_version_composed` so a refactor of either
the base OR the composition rule fails loud."""


# The composed prompt_version that ``render_prompt(..., cache_scope="project")``
# actually returns for the canonical fixture (#188 US-006). Differs from the
# base constant above because ``_prompt_version_for`` folds the non-default
# scope into the hash.
_RENDERED_PROMPT_VERSION_PROJECT: str = "77edde8ae270de86"


# Captured once via ``render_prompt(..., cache_scope="project")`` against the
# canonical multi-model fixture below. Byte-identical across every model in the
# batch (the cache-hit precondition, DEC-007) — pinned inline so reviewers see
# the diff in the PR rather than chasing a separate file. Any byte-level change
# to the project summary — model ordering, column-count rendering, the
# ``<PROJECT_MANIFEST>`` envelope, or an added project business rule — rotates
# this snapshot.
_CACHED_BLOCK_GOLDEN_PROJECT: str = """\
<PROJECT_MANIFEST>
## Project models

Every model in this dbt project, listed by name with its column count.
Full column detail for the model under draft and its direct neighbours
appears in the per-model section below.

- dim_customers (4 cols)
- fct_orders (4 cols)
- mart_orders_summary (4 cols)
- stg_orders (4 cols)

</PROJECT_MANIFEST>"""


def _build_canonical_request() -> LLMRequest:
    """Construct the canonical request used to pin the cached block.

    Schema-only mode, four columns matching the ``fct_orders`` model in
    the fixture, no aggregates / sampled_rows / redactions. Constructed
    directly (not via ``build_llm_request``) because this is the
    cache-stability snapshot — the safety layer's audit-write seam is
    exercised elsewhere.
    """
    return LLMRequest(
        model_unique_id="model.sf_demo.fct_orders",
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("order_id", "customer_id", "amount", "ordered_at"),
        redactions=(),
        schema=(
            ("order_id", "STRING"),
            ("customer_id", "STRING"),
            ("amount", "FLOAT64"),
            ("ordered_at", "TIMESTAMP"),
        ),
    )


def test_prompt_version_pinned_to_us_010_value() -> None:
    """The :data:`_PROMPT_VERSION` hash is pinned by US-010. Any template
    edit rotates the hash; updating this constant without also rotating
    :data:`_CACHED_BLOCK_GOLDEN` would silently desync the snapshot.
    """
    manifest = Manifest.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    model = manifest.nodes["model.sf_demo.fct_orders"]
    request = _build_canonical_request()
    _system, _cached, _dynamic, prompt_version = render_prompt(model, request, manifest)
    assert prompt_version == _EXPECTED_PROMPT_VERSION, (
        f"_PROMPT_VERSION rotated: expected {_EXPECTED_PROMPT_VERSION!r}, "
        f"got {prompt_version!r}. If this is intentional (a template change), "
        "update _EXPECTED_PROMPT_VERSION AND re-capture _CACHED_BLOCK_GOLDEN "
        "in lockstep — the cached snapshot below will also fail until you do."
    )


def test_cached_block_byte_stable_against_golden() -> None:
    """Byte-equality assertion between the rendered cached block and the
    inline golden constant. On mismatch, prints a unified diff so the
    regression is reviewable in PR.

    Cache cost regressions are silent in production: a one-character
    change to the cached block invalidates every cached prefix and rebills
    each call at full input-token rate. This test is the only thing
    standing between an inadvertent prompt edit and a cost spike.
    """
    manifest = Manifest.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    model = manifest.nodes["model.sf_demo.fct_orders"]
    request = _build_canonical_request()
    _system, cached, _dynamic, _prompt_version = render_prompt(model, request, manifest)

    if cached != _CACHED_BLOCK_GOLDEN:
        diff = "".join(
            difflib.unified_diff(
                _CACHED_BLOCK_GOLDEN.splitlines(keepends=True),
                cached.splitlines(keepends=True),
                fromfile="_CACHED_BLOCK_GOLDEN",
                tofile="render_prompt(...).cached",
                n=3,
            )
        )
        pytest.fail(
            "Cached block drifted from the pinned golden snapshot.\n"
            "If this is intentional, update _CACHED_BLOCK_GOLDEN to the "
            "new render and verify _PROMPT_VERSION rotated in lockstep.\n\n"
            f"Unified diff:\n{diff}"
        )


# ---------------------------------------------------------------------------
# Project-scope goldens (#188 US-006)
# ---------------------------------------------------------------------------


def _request_for(model_unique_id: str, manifest: Manifest) -> LLMRequest:
    """Build a schema-only request for ``model_unique_id`` from the manifest.

    Used by the cross-model byte-identity test: the project cached block must
    be byte-identical regardless of which model is "under draft", so we render
    it for two distinct models and compare. The request shape varies per model
    (columns differ) but feeds only the dynamic block — the project cached
    block ignores ``request`` entirely.
    """
    model = manifest.nodes[model_unique_id]
    columns = tuple(model.columns)
    return LLMRequest(
        model_unique_id=model_unique_id,
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=columns,
        redactions=(),
        schema=tuple((name, "STRING") for name in columns),
    )


def test_project_prompt_version_pinned() -> None:
    """The project-scope base version constant is pinned by US-006.

    ``_PROMPT_VERSION_PROJECT`` folds the project-summary template + the
    project-scope system prompt (with its ``<PROJECT_MANIFEST>`` defence line)
    into its hash inputs. Editing any of those rotates this hash; updating the
    constant without re-capturing :data:`_CACHED_BLOCK_GOLDEN_PROJECT` would
    silently desync the project snapshot.
    """
    assert _PROMPT_VERSION_PROJECT == _EXPECTED_PROMPT_VERSION_PROJECT, (
        f"_PROMPT_VERSION_PROJECT rotated: expected "
        f"{_EXPECTED_PROMPT_VERSION_PROJECT!r}, got {_PROMPT_VERSION_PROJECT!r}. "
        "If this is intentional (a project-scope template change), update "
        "_EXPECTED_PROMPT_VERSION_PROJECT AND re-capture "
        "_CACHED_BLOCK_GOLDEN_PROJECT in lockstep — the project cached "
        "snapshot below will also fail until you do."
    )


def test_project_render_prompt_version_composed() -> None:
    """``render_prompt(..., cache_scope="project")`` returns the COMPOSED
    project version, not the bare base constant.

    ``_prompt_version_for((), "project")`` short-circuits to the bare base
    only for the per-model / no-exclusions case; the non-default ``project``
    scope folds a ``"|scope=project|exclude=[]"`` suffix into a fresh hash.
    This test pins that composed value so a refactor of the composition rule
    (separate from the base) fails loud. The composed value MUST differ from
    the base — that difference is the contract this test documents.
    """
    manifest = Manifest.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    model = manifest.nodes["model.sf_demo.fct_orders"]
    request = _build_canonical_request()
    _system, _cached, _dynamic, prompt_version = render_prompt(
        model, request, manifest, cache_scope="project"
    )
    assert prompt_version == _RENDERED_PROMPT_VERSION_PROJECT, (
        f"composed project prompt_version rotated: expected "
        f"{_RENDERED_PROMPT_VERSION_PROJECT!r}, got {prompt_version!r}. If this "
        "is intentional (base rotation or composition-rule change), update "
        "_RENDERED_PROMPT_VERSION_PROJECT (and _EXPECTED_PROMPT_VERSION_PROJECT "
        "if the base itself rotated)."
    )
    assert prompt_version != _EXPECTED_PROMPT_VERSION_PROJECT, (
        "the composed project version must differ from the bare base — "
        "_prompt_version_for folds the non-default scope into the hash"
    )


def test_project_cached_block_byte_stable_against_golden() -> None:
    """Byte-equality between the rendered project cached block and the inline
    golden constant. On mismatch, prints a unified diff (mirrors the per-model
    test) so the regression is reviewable in PR.

    The project cached block is the shared prefix Anthropic's prompt cache
    keys on across a ``--select`` batch. A one-character drift invalidates the
    shared prefix and rebills every model in the batch at full input-token
    rate — the silent cost regression this snapshot exists to catch.
    """
    manifest = Manifest.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    model = manifest.nodes["model.sf_demo.fct_orders"]
    request = _build_canonical_request()
    _system, cached, _dynamic, _prompt_version = render_prompt(
        model, request, manifest, cache_scope="project"
    )

    if cached != _CACHED_BLOCK_GOLDEN_PROJECT:
        diff = "".join(
            difflib.unified_diff(
                _CACHED_BLOCK_GOLDEN_PROJECT.splitlines(keepends=True),
                cached.splitlines(keepends=True),
                fromfile="_CACHED_BLOCK_GOLDEN_PROJECT",
                tofile="render_prompt(..., cache_scope='project').cached",
                n=3,
            )
        )
        pytest.fail(
            "Project cached block drifted from the pinned golden snapshot.\n"
            "If this is intentional, update _CACHED_BLOCK_GOLDEN_PROJECT to the "
            "new render and verify _PROMPT_VERSION_PROJECT rotated in lockstep.\n\n"
            f"Unified diff:\n{diff}"
        )


def test_project_cached_block_byte_identical_across_models() -> None:
    """The project cached block is byte-identical across DIFFERENT models in
    the batch — the load-bearing cache-hit precondition (DEC-007).

    The project summary iterates ``sorted(manifest.nodes)`` and ignores which
    model is "under draft", so rendering it for ``fct_orders`` vs ``stg_orders``
    must yield the same bytes. If a future change makes the project prefix
    depend on the model under draft (e.g. by leaking neighbour detail into the
    cached block), the cache would silently never hit and this test fails loud.
    Both renders also equal the pinned golden.
    """
    manifest = Manifest.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))

    model_a = manifest.nodes["model.sf_demo.fct_orders"]
    model_b = manifest.nodes["model.sf_demo.stg_orders"]
    assert model_a.unique_id != model_b.unique_id  # two genuinely distinct models

    _sa, cached_a, _da, _va = render_prompt(
        model_a, _request_for(model_a.unique_id, manifest), manifest, cache_scope="project"
    )
    _sb, cached_b, _db, _vb = render_prompt(
        model_b, _request_for(model_b.unique_id, manifest), manifest, cache_scope="project"
    )

    assert cached_a == cached_b, (
        "project cached block differs between models — the cache-hit "
        "precondition (byte-identical shared prefix) is broken. The project "
        "summary must not depend on which model is under draft."
    )
    assert cached_a == _CACHED_BLOCK_GOLDEN_PROJECT


def test_project_version_differs_from_per_model() -> None:
    """The two base prompt-version constants MUST differ (#188 US-006, DEC-009).

    A run in project scope must not collide with a per-model run on Anthropic's
    prompt cache — the project prefix carries a different system prompt (the
    ``<PROJECT_MANIFEST>`` defence line) and a different cached template. If a
    refactor accidentally collapsed the two bases (e.g. dropped the project
    defence line from the hash inputs), this asserts loud.
    """
    assert _PROMPT_VERSION_PROJECT != _PROMPT_VERSION_PER_MODEL, (
        "project and per-model base prompt versions collided — a project-scope "
        "run could share a cache prefix with a per-model run, defeating the "
        "two-scope separation"
    )
    # Belt-and-braces: the pinned constants reflect the same invariant.
    assert _EXPECTED_PROMPT_VERSION_PROJECT != _EXPECTED_PROMPT_VERSION
