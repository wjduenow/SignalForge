"""Cache-stability snapshot for the LLM-judge prompt (#170 / DEC-012).

Established by #170 US-009 / DEC-012 to make the rule-file claim in
``.claude/rules/business-rule-tests.md`` § "Lockstep ``_PROMPT_VERSION``
rotation when extending the catalogue (#169 DEC-012)" actually reflect
reality — pre-#170 the rule file claimed a grade-side snapshot existed
but only the dynamic per-event ``rubric_hash`` shipped.

Mirrors the drafter-side surface (``tests/llm/test_prompt_cache_stability.py``)
verbatim in shape. The cached prefix Anthropic's prompt cache keys on,
on the grade side, is :func:`signalforge.grade.prompts.render_rubric_block`
applied to the rubric in use. Any byte-level change to that block — or
to :data:`signalforge.grade.prompts._SYSTEM_PROMPT` or the envelope tags —
invalidates every cached prefix in flight for the dozens of judge calls
one :func:`signalforge.grade.grade_artifacts` invocation issues. The
:data:`signalforge.grade.prompts._PROMPT_VERSION` constant covers the
*template + default-rubric* combination (load-bearing for cache stability);
this test covers BOTH the constant hash AND the rendered rubric block.

On rendered-block mismatch, the assertion message includes a
:func:`difflib.unified_diff` so the regression is reviewable in PR.

Pinned ``_PROMPT_VERSION``: tracked by :data:`_EXPECTED_PROMPT_VERSION`
below — the constant in source is the source of truth, this docstring
deliberately does not hard-code a hash so it can't drift. Latest known
rotations:

- ``4dae4421972e9c2d`` — current. Established by #170 (DEC-012) when
  the grade-side ``_PROMPT_VERSION`` snapshot surface was added.
  Coincides with the #170 (DEC-007) rotation of the ``no-redundant``
  criterion (US-008) that grew sibling calibration prose for
  composite-key tests (``unique_combination``). Two surfaces, one
  commit-pair: US-008 extended the criterion text + rotated the
  dynamic ``rubric_hash`` and ``prompt_version_template`` returns
  (already pinned at ``tests/grade/test_prompts.py``); US-009
  established this snapshot at the same value the live helper
  returns for ``DEFAULT_RUBRIC`` today.

If this rotates again, update both :data:`_EXPECTED_PROMPT_VERSION` and
:data:`_RUBRIC_BLOCK_GOLDEN` in lockstep — the rotation is the signal
that the templates / criterion texts changed.
"""

from __future__ import annotations

import difflib

import pytest

from signalforge.grade.prompts import _PROMPT_VERSION, render_rubric_block
from signalforge.grade.rubric import DEFAULT_RUBRIC

_EXPECTED_PROMPT_VERSION: str = "4dae4421972e9c2d"


# Captured once via ``render_rubric_block(DEFAULT_RUBRIC)``. Any byte-level
# change to the rubric criterion ids/texts, the header text, or the per-line
# rendering format rotates this snapshot. The golden value is intentionally
# inline (not a fixture file) so reviewers see the diff in the PR rather
# than chasing a separate file. Mirrors the drafter-side convention.
_RUBRIC_BLOCK_GOLDEN: str = """\
## Rubric criteria

The judge will be asked to score against ONE of these criteria per call:

clarity: Is the column description clear, specific, and actionable? Does it unambiguously explain the column's purpose and business meaning without jargon or vagueness?
consistency: Are column names and descriptions consistent in terminology? Do related concepts use the same term throughout, and do synonyms or conflicting terminology appear?
rationale: Does every test have a clear rationale explaining why it is needed? Are vague or missing rationales present?
no-redundant: Are any tests redundant — semantically identical to another test, already dropped by the prune layer as always-passing, or trivially satisfiable? For tests carrying numeric bounds (e.g. `row_count_between`), is each bound a meaningful guardrail calibrated to the model's expected size, rather than a vacuous floor or ceiling (`minimum=0` with no `maximum`, or a `maximum` so high it cannot fire)? For composite-key tests (e.g. `unique_combination`), is the column tuple a meaningful grain (e.g. `(order_id, line_item_id)`), or vacuously unique because one member is already a primary key on its own? A tuple of the shape `(primary_key, anything)` is unique by construction and adds no signal.
"""


def test_prompt_version_pinned_to_us_009_value() -> None:
    """The :data:`signalforge.grade.prompts._PROMPT_VERSION` constant is
    pinned by #170 US-009. Any change to the system prompt, envelope tags,
    or any of the 4 default criterion texts rotates the hash; updating
    this constant without also rotating :data:`_RUBRIC_BLOCK_GOLDEN`
    would silently desync the snapshot.
    """
    assert _PROMPT_VERSION == _EXPECTED_PROMPT_VERSION, (
        f"_PROMPT_VERSION rotated: expected {_EXPECTED_PROMPT_VERSION!r}, "
        f"got {_PROMPT_VERSION!r}. If this is intentional (a system prompt "
        "or default-rubric criterion edit), update _EXPECTED_PROMPT_VERSION "
        "AND re-capture _RUBRIC_BLOCK_GOLDEN in lockstep — the rendered "
        "snapshot below will also fail until you do."
    )


def test_rubric_block_byte_stable_against_golden() -> None:
    """Byte-equality assertion between the rendered rubric block and the
    inline golden constant. On mismatch, prints a unified diff so the
    regression is reviewable in PR.

    Cache cost regressions are silent in production: a one-character
    change to the rendered rubric block invalidates every cached prefix
    and rebills each per-criterion judge call at full input-token rate
    (a typical run issues ~48 per-criterion calls — 4 criteria × ~12
    artifacts; see ``grade-layer.md`` § "One LLM call per (artifact ×
    criterion)"). This test, together with the constant-pin above,
    is the only thing standing between an inadvertent criterion-text
    edit and a cost spike across an entire ``grade_artifacts`` run.
    """
    rendered = render_rubric_block(DEFAULT_RUBRIC)
    if rendered != _RUBRIC_BLOCK_GOLDEN:
        diff = "".join(
            difflib.unified_diff(
                _RUBRIC_BLOCK_GOLDEN.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile="_RUBRIC_BLOCK_GOLDEN",
                tofile="render_rubric_block(DEFAULT_RUBRIC)",
                n=3,
            )
        )
        pytest.fail(
            "Rendered rubric block drifted from the pinned golden snapshot.\n"
            "If this is intentional, update _RUBRIC_BLOCK_GOLDEN to the new "
            "render and verify _PROMPT_VERSION rotated in lockstep.\n\n"
            f"Unified diff:\n{diff}"
        )
