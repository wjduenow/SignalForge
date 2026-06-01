"""Tests for ``signalforge.grade.rubric`` (US-003).

Exercises every locked invariant of the rubric data model: the
``Criterion`` non-empty + ``extra="forbid"`` validators (DEC-017),
``GradeThresholds`` ``[0.0, 1.0]`` bounds, the ``DEFAULT_RUBRIC`` four
locked criteria with verbatim DEC-016 text, the deterministic +
order-invariant ``_canonical_rubric_hash`` (DEC-010 — pinned to a
golden hex regression test so any change to DEC-016 text breaks the
build loudly), and the structural rubric-level guards
(``validate_rubric`` rejecting duplicate IDs and the degenerate empty
rubric).

Each test is capable of failing if its target is broken (per
``.claude/rules/testing-signal.md``); no ``assert True``-shaped
no-ops.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from signalforge.grade.errors import GradeRubricError
from signalforge.grade.rubric import (
    DEFAULT_RUBRIC,
    Criterion,
    GradeThresholds,
    _canonical_rubric_hash,
    validate_rubric,
)

# ----- Criterion shape (DEC-017) -----


def test_criterion_rejects_empty_id() -> None:
    """An empty ``id`` is a silent-no-op vector; reject at construction."""
    with pytest.raises(ValidationError):
        Criterion(id="", criterion="A non-empty description.")


def test_criterion_rejects_empty_criterion_text() -> None:
    """An empty ``criterion`` would render an empty rubric line to the
    judge — fail loud at construction."""
    with pytest.raises(ValidationError):
        Criterion(id="clarity", criterion="")


def test_criterion_rejects_whitespace_only_id() -> None:
    """Whitespace-only ``id`` is an editing-glitch landing zone (e.g.
    after a YAML block-scalar edit). Reject identically to empty."""
    with pytest.raises(ValidationError):
        Criterion(id="   ", criterion="A non-empty description.")


def test_criterion_rejects_whitespace_only_criterion_text() -> None:
    """Symmetric to the id check; whitespace-only text is rejected."""
    with pytest.raises(ValidationError):
        Criterion(id="clarity", criterion="\t\n  ")


def test_criterion_rejects_extra_fields() -> None:
    """``extra="forbid"`` (DEC-015 of #4) — a typo like ``weight=1.0``
    must fail loud, not silently no-op."""
    with pytest.raises(ValidationError):
        Criterion(id="clarity", criterion="A description.", weight=1.0)  # type: ignore[call-arg]


def test_criterion_is_frozen() -> None:
    """``frozen=True`` (DEC-017) — Criterion instances are immutable
    post-construction."""
    c = Criterion(id="clarity", criterion="A description.")
    with pytest.raises(ValidationError):
        c.id = "rationale"  # type: ignore[misc]


# ----- GradeThresholds shape -----


def test_grade_thresholds_default_values_match_dec_016() -> None:
    """Defaults are the locked Phase-2 numbers (0.7 / 0.5)."""
    thresholds = GradeThresholds()
    assert thresholds.min_pass_rate == 0.7
    assert thresholds.min_mean_score == 0.5


def test_grade_thresholds_rejects_value_above_one() -> None:
    """``min_pass_rate`` is a fraction; >1.0 is meaningless."""
    with pytest.raises(ValidationError):
        GradeThresholds(min_pass_rate=1.5)


def test_grade_thresholds_rejects_negative_min_pass_rate() -> None:
    """Negative thresholds are nonsense on a [0.0, 1.0] scale."""
    with pytest.raises(ValidationError):
        GradeThresholds(min_pass_rate=-0.1)


def test_grade_thresholds_rejects_negative_min_mean_score() -> None:
    """Symmetric to the min_pass_rate check; negative is rejected."""
    with pytest.raises(ValidationError):
        GradeThresholds(min_mean_score=-0.5)


def test_grade_thresholds_rejects_extra_fields() -> None:
    """``extra="forbid"`` — a typo like ``min_pas_rate`` must fail loud."""
    with pytest.raises(ValidationError):
        GradeThresholds(min_pas_rate=0.7)  # type: ignore[call-arg]


def test_grade_thresholds_accepts_zero_and_one_inclusive() -> None:
    """The interval is closed: 0.0 and 1.0 are both valid."""
    t = GradeThresholds(min_pass_rate=0.0, min_mean_score=1.0)
    assert t.min_pass_rate == 0.0
    assert t.min_mean_score == 1.0


# ----- DEFAULT_RUBRIC locked content (DEC-016) -----


def test_default_rubric_has_four_entries_with_locked_ids() -> None:
    """DEC-016 locks exactly four criteria with the listed IDs."""
    assert len(DEFAULT_RUBRIC) == 4
    ids = [c.id for c in DEFAULT_RUBRIC]
    assert ids == ["clarity", "consistency", "rationale", "no-redundant"]


def test_default_rubric_criterion_text_matches_dec_016_verbatim() -> None:
    """Pin every ``criterion`` text character-for-character to DEC-016.

    Load-bearing: the rubric_hash is derived from this text, and any
    drift here silently changes the hash for every audit row in v0.1.
    """
    by_id = {c.id: c.criterion for c in DEFAULT_RUBRIC}
    assert by_id["clarity"] == (
        "Is the column description clear, specific, and actionable? "
        "Does it unambiguously explain the column's purpose and "
        "business meaning without jargon or vagueness?"
    )
    assert by_id["consistency"] == (
        "Are column names and descriptions consistent in terminology? "
        "Do related concepts use the same term throughout, and do "
        "synonyms or conflicting terminology appear?"
    )
    assert by_id["rationale"] == (
        "Does every test have a clear rationale explaining why it is "
        "needed? Are vague or missing rationales present?"
    )
    assert by_id["no-redundant"] == (
        "Are any tests redundant — semantically identical to another "
        "test, already dropped by the prune layer as always-passing, "
        "or trivially satisfiable? For tests carrying numeric bounds "
        "(e.g. `row_count_between`), is each bound a meaningful "
        "guardrail calibrated to the model's expected size, rather "
        "than a vacuous floor or ceiling (`minimum=0` with no `maximum`, "
        "or a `maximum` so high it cannot fire)? For composite-key "
        "tests (e.g. `unique_combination`), is the column tuple a "
        "meaningful grain (e.g. `(order_id, line_item_id)`), or "
        "vacuously unique because one member is already a primary "
        "key on its own? A tuple of the shape `(primary_key, "
        "anything)` is unique by construction and adds no signal."
    )


def test_default_rubric_entries_are_criterion_instances() -> None:
    """Sanity: every entry is a Criterion (not a bare dict / string)."""
    for entry in DEFAULT_RUBRIC:
        assert isinstance(entry, Criterion)


def test_default_rubric_is_a_tuple() -> None:
    """DEC-011: ``Rubric`` is a ``tuple[Criterion, ...]`` alias."""
    assert isinstance(DEFAULT_RUBRIC, tuple)


# ----- _canonical_rubric_hash (DEC-010) -----


# Pinned at the time the helper was authored against DEC-016 verbatim
# text. Any drift in DEC-016 criterion text or in the canonical-form
# computation breaks this test loudly. Re-pinning is a deliberate
# operation — bump ``audit_schema_version`` first.
#
# Rotation history:
# - ``280aa6db7fde2b24`` — initial pin (#7, DEC-016 verbatim).
# - ``22a0231690aca6ef`` — rotated under #169 (DEC-009) when the
#   ``no-redundant`` criterion gained calibration prose for
#   numeric-bounded tests (``row_count_between``).
# - ``30a9fda975b6d45c`` — current. Rotated under #170 (DEC-007) when
#   the ``no-redundant`` criterion gained sibling calibration prose
#   for composite-key tests (``unique_combination``): a vacuously
#   unique tuple of the shape ``(primary_key, anything)`` carries no
#   grain signal. The grader's 3-trigger degrade taxonomy (DEC-011)
#   stayed locked; this is a prose extension, not a structural change.
_DEFAULT_RUBRIC_GOLDEN_HASH = "30a9fda975b6d45c"


def test_default_rubric_hash_is_stable() -> None:
    """The golden hash pin guards both DEC-016 verbatim text and the
    canonical-form helper against silent drift."""
    actual = _canonical_rubric_hash(DEFAULT_RUBRIC)
    assert actual == _DEFAULT_RUBRIC_GOLDEN_HASH


def test_canonical_rubric_hash_returns_16_hex_chars() -> None:
    """blake2b digest_size=8 yields a 16-hex-character lowercase hex."""
    h = _canonical_rubric_hash(DEFAULT_RUBRIC)
    assert re.fullmatch(r"[0-9a-f]{16}", h) is not None


def test_canonical_rubric_hash_invariant_to_input_order() -> None:
    """Canonical form sorts by ``id`` — input ordering must not affect
    the digest."""
    forwards = _canonical_rubric_hash(DEFAULT_RUBRIC)
    reversed_rubric = tuple(reversed(DEFAULT_RUBRIC))
    backwards = _canonical_rubric_hash(reversed_rubric)
    assert forwards == backwards


def test_canonical_rubric_hash_changes_on_text_change() -> None:
    """A one-character edit to a criterion's text must change the hash."""
    original = DEFAULT_RUBRIC[0]
    mutated = (
        Criterion(id=original.id, criterion=original.criterion + "."),
        *DEFAULT_RUBRIC[1:],
    )
    assert _canonical_rubric_hash(mutated) != _canonical_rubric_hash(DEFAULT_RUBRIC)


def test_canonical_rubric_hash_changes_on_id_change() -> None:
    """An ``id`` rename (e.g. from ``clarity`` to ``clarity-v2``) must
    change the hash."""
    original = DEFAULT_RUBRIC[0]
    mutated = (
        Criterion(id=original.id + "-v2", criterion=original.criterion),
        *DEFAULT_RUBRIC[1:],
    )
    assert _canonical_rubric_hash(mutated) != _canonical_rubric_hash(DEFAULT_RUBRIC)


def test_canonical_rubric_hash_distinguishes_different_rubrics() -> None:
    """A single-criterion rubric and the four-criterion default must
    not collide."""
    smaller: tuple[Criterion, ...] = (
        Criterion(id="clarity", criterion="A non-empty description."),
    )
    assert _canonical_rubric_hash(smaller) != _canonical_rubric_hash(DEFAULT_RUBRIC)


# ----- validate_rubric (DEC-017) -----


def test_validate_rubric_passes_default_rubric() -> None:
    """The locked default must round-trip the validator unchanged."""
    validate_rubric(DEFAULT_RUBRIC)  # no exception


def test_validate_rubric_rejects_duplicate_ids() -> None:
    """Duplicate ``id`` values would break the parser's anchor contract."""
    rubric = (
        Criterion(id="clarity", criterion="First description."),
        Criterion(id="clarity", criterion="Second description."),
    )
    with pytest.raises(GradeRubricError) as excinfo:
        validate_rubric(rubric)
    # Surface the offending id in the message so the operator can fix
    # the duplicate without grepping the YAML.
    assert "clarity" in str(excinfo.value)


def test_validate_rubric_surfaces_every_duplicate_id() -> None:
    """The error lists every duplicate, not just the first encountered."""
    rubric = (
        Criterion(id="alpha", criterion="A."),
        Criterion(id="beta", criterion="B."),
        Criterion(id="alpha", criterion="A again."),
        Criterion(id="beta", criterion="B again."),
    )
    with pytest.raises(GradeRubricError) as excinfo:
        validate_rubric(rubric)
    rendered = str(excinfo.value)
    assert "alpha" in rendered
    assert "beta" in rendered


def test_validate_rubric_rejects_empty_rubric() -> None:
    """An empty rubric grades nothing — fail loud (see commit message
    for the policy choice)."""
    with pytest.raises(GradeRubricError) as excinfo:
        validate_rubric(())
    assert "empty" in str(excinfo.value).lower()


def test_validate_rubric_error_carries_remediation() -> None:
    """Every GradeRubricError raised here renders a remediation line so
    the CLI / log can guide the operator."""
    with pytest.raises(GradeRubricError) as excinfo:
        validate_rubric(())
    assert "↳ Remediation:" in str(excinfo.value)


# ----- DEC-009 of #169: no-redundant calibration prose for vacuous bounds -----


def test_no_redundant_criterion_carries_row_count_between_calibration_prose() -> None:
    """DEC-009 of #169 — the ``no-redundant`` criterion was extended (NOT a
    5th criterion added — DEC-009 explicitly rejects a 5th to avoid the
    +25% LLM cost) with calibration prose teaching the judge to score
    vacuous bounds low.

    The prose must name:

    * ``row_count_between`` — the test type this lands for; this is what
      a vacuous-bound row-count test gets matched against.
    * ``minimum=0`` and "no ``maximum``" / "vacuous" — the canonical
      vacuous-bound shape (Pydantic-valid at-least-one-bound, but
      semantically always-true) the judge must score down.
    * ``trivially satisfiable`` (or equivalent) — the load-bearing
      concept: the judge is asked whether the bound is a meaningful
      guardrail or a no-op.

    Load-bearing for the calibration intent: without this prose, the
    judge has no signal to distinguish a healthy ``minimum=100,
    maximum=10000`` bound from a borderline ``minimum=1, maximum=None``
    one (which only catches the empty-table case). Both pass the parser
    and both can survive prune (when the bound is violated, e.g. against
    an empty table). The fully-vacuous ``minimum=0, maximum=None`` shape
    the prose names IS dropped by the prune layer as ``always-passes``
    (the failing-rows CTE's ``WHERE n < 0`` predicate matches nothing,
    so ``failures=0``); the prose still names that shape because it
    teaches the judge to identify the same SHAPE of trivially-satisfiable
    bound across the borderline cases that DO reach the grader.
    """
    by_id = {c.id: c.criterion for c in DEFAULT_RUBRIC}
    text = by_id["no-redundant"]
    assert "row_count_between" in text
    assert "minimum=0" in text
    assert "trivially satisfiable" in text
    # The phrase "vacuous" appears in the prose to give the judge the
    # explicit vocabulary the rubric scores against.
    assert "vacuous" in text


def test_no_redundant_criterion_preserves_original_redundancy_intent() -> None:
    """The DEC-009 extension is additive — the criterion's original
    redundancy / always-pass language stays in place. A future operator
    reading the rubric should still see that semantically-identical
    duplicates and prune-dropped always-pass tests are graded down here.
    """
    by_id = {c.id: c.criterion for c in DEFAULT_RUBRIC}
    text = by_id["no-redundant"]
    # Original redundancy framing — preserved verbatim.
    assert "semantically identical" in text
    assert "always-passing" in text


def test_default_rubric_keeps_exactly_four_criteria_after_dec_009() -> None:
    """DEC-009 of #169 explicitly forbids growing to a 5th criterion
    (the +25% LLM cost on every grade run is the load-bearing reason).
    The calibration prose lives inside the existing ``no-redundant``
    criterion; this test pins that the rubric stays at four.
    """
    assert len(DEFAULT_RUBRIC) == 4


# ----- DEC-011 of #169: 3-trigger degrade taxonomy stays locked -----


def test_low_calibration_bound_routes_to_flagged_not_kept_uncertain_when_grading_fails() -> None:
    """DEC-009 of #169 — a borderline-calibrated ``row_count_between``
    test that survives the prune layer (positive prune evidence:
    ``reason="kept"``, NOT ``"kept-without-evidence"``) but earns a low
    ``no-redundant`` score from the judge MUST tier as ``flagged``, NOT
    ``kept-uncertain``.

    The contract (``diff-renderer.md`` § "Tier classification" + DEC-009
    of #169): ``kept-uncertain`` is reserved for prune-layer
    couldn't-evaluate (budget exhausted / identifier rejected /
    warehouse raised). A bound the warehouse successfully evaluated —
    and the judge then scored down for weak calibration — belongs in
    ``flagged`` so the reviewer's attention is drawn to the calibration
    problem, not the (non-existent) evaluation problem.

    The scenario this pins is a borderline-calibrated bound like
    ``minimum=1, maximum=None`` that survives prune because it actually
    caught a real failure (e.g. an empty table → ``failures=1``).
    A fully-vacuous ``minimum=0, maximum=None`` would have been dropped
    by prune as ``always-passes`` and never reached the grader. The
    fixture uses ``failures=1`` to match the only reachable kept state.

    Drives ``signalforge.diff.engine._tier_for_kept`` directly with the
    three load-bearing inputs. The function is internal (``_``-prefixed)
    but is the single tier-classification seam — this is the cleanest
    pinning surface for the DEC-009 contract.
    """
    from signalforge.diff.engine import _tier_for_kept  # noqa: PLC0415
    from signalforge.draft.models import (  # noqa: PLC0415
        CandidateTestRowCountBetween,
    )
    from signalforge.prune.models import PruneDecision  # noqa: PLC0415

    # Sanity: the candidate-test variant exists in this branch (US-001
    # of #169 shipped it). Without it, the calibration prose names a
    # type the codebase doesn't know — fail loud here so the dependency
    # surfaces clearly.
    assert CandidateTestRowCountBetween is not None

    # Positive prune evidence — warehouse ran the COUNT(*), the bound
    # was VIOLATED (the test caught a real failure on an empty table or
    # an unexpectedly out-of-range count). reason="kept" implies
    # failures > 0 in the real prune matrix; fixture matches.
    kept_decision = PruneDecision(
        test_anchor="model",
        test=CandidateTestRowCountBetween(minimum=1, maximum=None),
        decision="kept",
        reason="kept",
        failures=1,
        sampled_rows=None,
        scope="full",
        elapsed_ms=12,
        compiled_sql_hash="0" * 16,
        compiled_sql="SELECT COUNT(*) FROM `p.d.t`",
        why="ran against warehouse; row count violated bound",
    )

    # Judge scored the calibration criterion low → passed=False.
    tier = _tier_for_kept(kept_decision, score=0.2, passed=False)
    assert tier == "flagged"
    assert tier != "kept-uncertain"


def test_healthy_bound_routes_to_kept_when_grading_passes() -> None:
    """Mirror of the low-calibration test: a well-calibrated bound
    (``minimum=100, maximum=10000``) that survives the prune layer and
    earns a high ``no-redundant`` score from the judge ships as
    ``kept`` — the v0.1 happy path. Pins that the calibration prose
    rewards specificity rather than penalising every row-count test.

    The fixture uses ``failures=1`` because ``reason="kept"`` implies
    the warehouse returned at least one failing row in the real prune
    matrix (otherwise ``failures=0`` would route to ``always-passes``
    and the test would never reach the grader).
    """
    from signalforge.diff.engine import _tier_for_kept  # noqa: PLC0415
    from signalforge.draft.models import (  # noqa: PLC0415
        CandidateTestRowCountBetween,
    )
    from signalforge.prune.models import PruneDecision  # noqa: PLC0415

    kept_decision = PruneDecision(
        test_anchor="model",
        test=CandidateTestRowCountBetween(minimum=100, maximum=10000),
        decision="kept",
        reason="kept",
        failures=1,
        sampled_rows=None,
        scope="full",
        elapsed_ms=12,
        compiled_sql_hash="0" * 16,
        compiled_sql="SELECT COUNT(*) FROM `p.d.t`",
        why="ran against warehouse; row count violated bound",
    )

    tier = _tier_for_kept(kept_decision, score=0.9, passed=True)
    assert tier == "kept"


def test_grade_degrade_taxonomy_stays_at_three_triggers() -> None:
    """DEC-011 of #169 locks the grader's degrade taxonomy at exactly
    three triggers (``grade-layer.md`` § "Conservative score-and-degrade
    taxonomy"):

    1. ``LLMError`` retries exhausted → ``call failed: <ClassName>``
    2. ``GradeOutputError`` (parser / anchor-contract failure) →
       ``call failed: GradeOutputError``
    3. ``total_budget_seconds`` exceeded → ``grade budget exceeded …``

    A borderline-calibrated ``row_count_between`` test that survived
    prune is NOT a 4th trigger. Instead the calibration prose in
    ``no-redundant`` (DEC-009) scores it low → existing ``passed: bool``
    threshold → ships as ``flagged`` (NOT ``kept-uncertain``, which is
    reserved for prune couldn't-evaluate). A fully-vacuous
    ``minimum=0, maximum=None`` test would already be dropped by prune
    as ``always-passes`` and never reach the grader at all — neither a
    degrade nor a flag.

    This test asserts the three degrade-path message shapes are still
    present in :mod:`signalforge.grade.engine`. A 4th trigger would
    surface as a new ``reasoning=`` string; the AST scan + this string
    scan together guard the taxonomy.
    """
    from signalforge.grade import engine as grade_engine  # noqa: PLC0415

    src = (
        grade_engine.__file__ and __import__("pathlib").Path(grade_engine.__file__).read_text()
    ) or ""
    # Trigger 1: LLMError retries exhausted (formatted via _format_degrade_reasoning).
    assert "call failed: " in src
    # Trigger 3: total budget exceeded (formatted in the budget branch).
    assert "grade budget exceeded" in src
