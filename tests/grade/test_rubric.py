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
        "test, or already dropped by the prune layer as always-passing?"
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
_DEFAULT_RUBRIC_GOLDEN_HASH = "280aa6db7fde2b24"


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
