"""Tests for :mod:`signalforge.diff._artifact_id` (US-006 of issue #8).

The diff layer's ``artifact_id_for`` formatter and the grade layer's
``_artifact_id_for`` formatter join grade-sidecar JSON to the rendered
diff via the ``(run_id, artifact_id, criterion_id)`` triple. A single
dotted-path disagreement would silently drop affected grade rows from
the join.

After issue #42 the implementation lives in
:mod:`signalforge._common.artifact_id` and both layers re-export the
same function objects. The cross-stage parity test now asserts function
**identity** (``is`` equality) rather than byte-equal output — a
divergence is impossible by construction.

The cross-stage import from :mod:`signalforge.grade.engine` is the
**single allowed cross-stage seam** between ``signalforge.diff`` and
``signalforge.grade``; production diff code must NOT import from
``signalforge.grade`` at runtime.
"""

from __future__ import annotations

import pytest

from signalforge._common import artifact_id as _common_artifact_id

# Cross-stage parity test seam — the only place under signalforge.diff
# that imports from signalforge.grade.
from signalforge.diff._artifact_id import (
    _model_test_args_hash,
    artifact_id_for,
    compute_args_hashes,
)
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestAcceptedValues,
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestUnique,
)
from signalforge.grade.engine import (
    _artifact_id_for as _grade_artifact_id_for,
)
from signalforge.grade.engine import (
    _model_test_args_hash as _grade_model_test_args_hash,
)
from signalforge.grade.engine import (
    _test_args_hashes as _grade_test_args_hashes,
)

# ---------------------------------------------------------------------------
# Six dotted-path shapes (DEC-009)
# ---------------------------------------------------------------------------


def test_shape_column_description() -> None:
    """``column.<col>.description`` shape."""
    assert (
        artifact_id_for(scope="column", column_name="email", field="description")
        == "column.email.description"
    )


def test_shape_column_rationale() -> None:
    """``column.<col>.rationale`` shape."""
    assert (
        artifact_id_for(scope="column", column_name="email", field="rationale")
        == "column.email.rationale"
    )


def test_shape_model_description() -> None:
    """``model.description`` shape."""
    assert artifact_id_for(scope="model", field="description") == "model.description"


def test_shape_model_rationale() -> None:
    """``model.rationale`` shape."""
    assert artifact_id_for(scope="model", field="rationale") == "model.rationale"


def test_shape_test_column_no_collision() -> None:
    """``test.column.<col>.<type>`` (4-part) when no collision."""
    nn = CandidateTestNotNull(column="user_id")
    assert (
        artifact_id_for(scope="column", column_name="user_id", test=nn)
        == "test.column.user_id.not_null"
    )


def test_shape_test_column_with_args_hash() -> None:
    """``test.column.<col>.<type>.<args_hash>`` (5-part) when colliding."""
    av = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    assert (
        artifact_id_for(scope="column", column_name="status", test=av, args_hash="abcd1234")
        == "test.column.status.accepted_values.abcd1234"
    )


def test_shape_test_model_no_collision() -> None:
    """``test.model.<type>`` (3-part) when no collision."""
    uq = CandidateTestUnique(column="email")
    assert artifact_id_for(scope="model", test=uq) == "test.model.unique"


def test_shape_test_model_with_args_hash() -> None:
    """``test.model.<type>.<args_hash>`` (4-part) when colliding."""
    uq = CandidateTestUnique(column="email")
    assert (
        artifact_id_for(scope="model", test=uq, args_hash="deadbeef")
        == "test.model.unique.deadbeef"
    )


# ---------------------------------------------------------------------------
# Collision-disambiguation rule
# ---------------------------------------------------------------------------


def test_collision_two_accepted_values_same_column_different_values() -> None:
    """Two ``accepted_values`` tests on the same column with different
    ``values`` lists produce different 8-hex args_hash suffixes.

    Without disambiguation, both would render as
    ``test.column.status.accepted_values`` and the
    ``(run_id, artifact_id, criterion_id)`` triple would collide in the
    diff-side join.
    """
    av1 = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    av2 = CandidateTestAcceptedValues(column="status", values=("c", "d"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(
            CandidateColumn(
                name="status",
                description="status",
                tests=(av1, av2),
            ),
        ),
        tests=(),
    )
    hashes = compute_args_hashes(candidate)
    h1 = hashes[id(av1)]
    h2 = hashes[id(av2)]
    assert h1 is not None
    assert h2 is not None
    assert h1 != h2
    # Each is exactly 8 hex chars (no ordinal disambiguator on distinct args).
    assert len(h1) == 8
    assert len(h2) == 8
    assert all(c in "0123456789abcdef" for c in h1)
    assert all(c in "0123456789abcdef" for c in h2)

    aid1 = artifact_id_for(scope="column", column_name="status", test=av1, args_hash=h1)
    aid2 = artifact_id_for(scope="column", column_name="status", test=av2, args_hash=h2)
    assert aid1 != aid2
    assert aid1.startswith("test.column.status.accepted_values.")
    assert aid2.startswith("test.column.status.accepted_values.")


def test_collision_two_accepted_values_model_level() -> None:
    """Two model-level ``accepted_values`` tests on different columns
    collide on ``test.type`` and get distinct hash suffixes."""
    av1 = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    av2 = CandidateTestAcceptedValues(column="region", values=("us", "eu"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="status", description="x"),),
        tests=(av1, av2),
    )
    hashes = compute_args_hashes(candidate)
    assert hashes[id(av1)] != hashes[id(av2)]
    assert hashes[id(av1)] is not None
    assert hashes[id(av2)] is not None


def test_no_collision_assigns_none() -> None:
    """A test whose ``test.type`` is unique within scope gets ``None``
    so the bare 4-part artifact_id is used."""
    nn = CandidateTestNotNull(column="user_id")
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="user_id", description="pk", tests=(nn,)),),
        tests=(),
    )
    hashes = compute_args_hashes(candidate)
    assert hashes[id(nn)] is None


def test_exact_duplicate_gets_ordinal_suffix() -> None:
    """Two tests with identical type AND args produce the same blake2b-4
    hash; the second occurrence gets a ``:1`` ordinal suffix."""
    nn1 = CandidateTestNotNull(column="user_id")
    nn2 = CandidateTestNotNull(column="user_id")
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="user_id", description="pk", tests=(nn1, nn2)),),
        tests=(),
    )
    hashes = compute_args_hashes(candidate)
    h1 = hashes[id(nn1)]
    h2 = hashes[id(nn2)]
    assert h1 is not None
    assert h2 is not None
    # First keeps the bare hash; second gets the :1 ordinal.
    assert ":" not in h1
    assert h2 == f"{h1}:1"


def test_model_and_column_scope_do_not_collide() -> None:
    """A model-level test and a column-scope test of the same type don't
    collide because the artifact_id prefix differs (``test.model.`` vs
    ``test.column.``). Each scope's count is independent."""
    nn_col = CandidateTestNotNull(column="user_id")
    nn_model = CandidateTestNotNull(column="user_id")
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="user_id", description="pk", tests=(nn_col,)),),
        tests=(nn_model,),
    )
    hashes = compute_args_hashes(candidate)
    assert hashes[id(nn_col)] is None
    assert hashes[id(nn_model)] is None


# ---------------------------------------------------------------------------
# custom_sql variant (US-009) — args-hash domain + dotted-path shapes
# ---------------------------------------------------------------------------


def test_custom_sql_distinct_sql_distinct_hash() -> None:
    """Two ``custom_sql`` tests with different SQL hash differently."""
    cs1 = CandidateTestCustomSQL(sql="SELECT 1 WHERE a > 0", column="a")
    cs2 = CandidateTestCustomSQL(sql="SELECT 1 WHERE a < 0", column="a")
    h1 = _model_test_args_hash(cs1)
    h2 = _model_test_args_hash(cs2)
    assert h1 != h2
    assert len(h1) == 8
    assert all(c in "0123456789abcdef" for c in h1)


def test_custom_sql_identical_sql_same_hash() -> None:
    """Two ``custom_sql`` tests with identical SQL + column collide
    deterministically (same blake2b-4 hash)."""
    cs1 = CandidateTestCustomSQL(sql="SELECT 1 WHERE total < 0", column="total")
    cs2 = CandidateTestCustomSQL(sql="SELECT 1 WHERE total < 0", column="total")
    assert _model_test_args_hash(cs1) == _model_test_args_hash(cs2)


def test_custom_sql_column_distinguishes_hash() -> None:
    """A column-scoped and a model-level ``custom_sql`` with the same SQL
    hash apart because ``column`` (``None`` vs a string) is in the
    hash domain."""
    cs_col = CandidateTestCustomSQL(sql="SELECT 1 WHERE x", column="x")
    cs_model = CandidateTestCustomSQL(sql="SELECT 1 WHERE x", column=None)
    assert _model_test_args_hash(cs_col) != _model_test_args_hash(cs_model)


def test_custom_sql_shape_column_scope() -> None:
    """``test.column.<col>.custom_sql`` shape (column-scoped)."""
    cs = CandidateTestCustomSQL(sql="SELECT 1 WHERE x < 0", column="x")
    assert artifact_id_for(scope="column", column_name="x", test=cs) == "test.column.x.custom_sql"


def test_custom_sql_shape_model_level() -> None:
    """``test.model.custom_sql`` shape (model-level, column=None)."""
    cs = CandidateTestCustomSQL(sql="SELECT 1 WHERE total != net + tax", column=None)
    assert artifact_id_for(scope="model", test=cs) == "test.model.custom_sql"


def test_custom_sql_shape_with_args_hash() -> None:
    """``test.column.<col>.custom_sql.<args_hash>`` (5-part) on collision."""
    cs = CandidateTestCustomSQL(sql="SELECT 1 WHERE x < 0", column="x")
    assert (
        artifact_id_for(scope="column", column_name="x", test=cs, args_hash="abcd1234")
        == "test.column.x.custom_sql.abcd1234"
    )


def test_custom_sql_collision_distinct_suffixes() -> None:
    """Two column-scoped ``custom_sql`` tests with different SQL collide on
    ``test.type`` and get distinct hash suffixes via
    :func:`compute_args_hashes`."""
    cs1 = CandidateTestCustomSQL(sql="SELECT 1 WHERE x < 0", column="x")
    cs2 = CandidateTestCustomSQL(sql="SELECT 1 WHERE x > 100", column="x")
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="x", description="x", tests=(cs1, cs2)),),
        tests=(),
    )
    hashes = compute_args_hashes(candidate)
    assert hashes[id(cs1)] is not None
    assert hashes[id(cs2)] is not None
    assert hashes[id(cs1)] != hashes[id(cs2)]


def test_custom_sql_exact_duplicate_gets_ordinal_suffix() -> None:
    """Two model-level ``custom_sql`` tests with identical SQL get the
    same base hash; the second occurrence gets a ``:1`` ordinal suffix."""
    cs1 = CandidateTestCustomSQL(sql="SELECT 1 WHERE bad", column=None)
    cs2 = CandidateTestCustomSQL(sql="SELECT 1 WHERE bad", column=None)
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(CandidateColumn(name="x", description="x"),),
        tests=(cs1, cs2),
    )
    hashes = compute_args_hashes(candidate)
    h1 = hashes[id(cs1)]
    h2 = hashes[id(cs2)]
    assert h1 is not None
    assert h2 is not None
    assert ":" not in h1
    assert h2 == f"{h1}:1"


def test_custom_sql_cross_stage_parity() -> None:
    """The grade engine's hash + formatter agree byte-for-byte on the
    ``custom_sql`` variant (defence-in-depth alongside the identity test)."""
    cs_col = CandidateTestCustomSQL(sql="SELECT 1 WHERE x < 0", column="x")
    cs_model = CandidateTestCustomSQL(sql="SELECT 1 WHERE total != net", column=None)
    assert _model_test_args_hash(cs_col) == _grade_model_test_args_hash(cs_col)
    assert _model_test_args_hash(cs_model) == _grade_model_test_args_hash(cs_model)
    assert artifact_id_for(scope="column", column_name="x", test=cs_col) == (
        _grade_artifact_id_for(scope="column", column_name="x", test=cs_col)
    )
    assert artifact_id_for(scope="model", test=cs_model) == (
        _grade_artifact_id_for(scope="model", test=cs_model)
    )


# ---------------------------------------------------------------------------
# Programming-error fail-loud
# ---------------------------------------------------------------------------


def test_column_scope_test_without_column_name_raises() -> None:
    nn = CandidateTestNotNull(column="x")
    with pytest.raises(ValueError, match="column_name"):
        artifact_id_for(scope="column", test=nn)


def test_column_scope_text_without_field_raises() -> None:
    with pytest.raises(ValueError, match="column_name . field"):
        artifact_id_for(scope="column", column_name="x")


def test_model_scope_text_without_field_raises() -> None:
    with pytest.raises(ValueError, match="field"):
        artifact_id_for(scope="model")


def test_test_and_field_combination_raises() -> None:
    """Passing both ``test`` and ``field`` is an incompatible combination
    (the formatter would silently ignore ``field`` and emit a test-shaped
    artifact_id). Hoisting to the shared seam (issue #42) added a hard
    guard against that footgun."""
    nn = CandidateTestNotNull(column="x")
    with pytest.raises(ValueError, match="field is only valid for text artifacts"):
        artifact_id_for(scope="column", column_name="x", test=nn, field="description")


def test_args_hash_without_test_raises() -> None:
    """``args_hash`` only makes sense alongside a ``test``; without one
    it would be silently dropped on the text-artifact branch."""
    with pytest.raises(ValueError, match="args_hash is only valid when test is provided"):
        artifact_id_for(scope="model", field="description", args_hash="deadbeef")


def test_model_scope_with_column_name_raises() -> None:
    """Model-scope artifacts cannot carry a ``column_name`` — the
    formatter would silently ignore it and emit the model-shaped
    artifact_id."""
    with pytest.raises(ValueError, match="model scope must not receive column_name"):
        artifact_id_for(scope="model", field="description", column_name="x")


# ---------------------------------------------------------------------------
# Cross-stage parity — identity equality (the load-bearing rule for US-006,
# tightened by issue #42 from byte-equal output to same-object identity).
# ---------------------------------------------------------------------------


def test_cross_stage_parity_is_function_identity() -> None:
    """The diff layer and grade layer expose the SAME function objects.

    After issue #42 the formatter + ``compute_args_hashes`` live in
    :mod:`signalforge._common.artifact_id`; both layers re-export.
    Asserting ``is`` equality makes a future drift impossible by
    construction — there is no second copy that could diverge.
    """
    assert artifact_id_for is _grade_artifact_id_for
    assert artifact_id_for is _common_artifact_id.artifact_id_for
    assert _model_test_args_hash is _grade_model_test_args_hash
    assert _model_test_args_hash is _common_artifact_id.model_test_args_hash
    assert compute_args_hashes is _grade_test_args_hashes
    assert compute_args_hashes is _common_artifact_id.compute_args_hashes


def test_cross_stage_parity_with_grade_engine() -> None:
    """Byte-equal output vs :func:`signalforge.grade.engine._artifact_id_for`.

    Kept as defence-in-depth after issue #42 promoted the parity
    contract to function-identity equality
    (:func:`test_cross_stage_parity_is_function_identity`). Representative
    inputs cover all six shapes plus the args_hash variants. Any
    divergence here would mean the identity assertion failed silently.
    """
    nn = CandidateTestNotNull(column="user_id")
    uq = CandidateTestUnique(column="email")
    av = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    rel = CandidateTestRelationships(column="account_id", to="ref('accounts')", field="id")

    cases: list[dict[str, object]] = [
        # column desc / rationale
        {"scope": "column", "column_name": "email", "field": "description"},
        {"scope": "column", "column_name": "email", "field": "rationale"},
        # model desc / rationale
        {"scope": "model", "field": "description"},
        {"scope": "model", "field": "rationale"},
        # column-scope test, no args_hash
        {"scope": "column", "column_name": "user_id", "test": nn},
        # column-scope test, with args_hash
        {
            "scope": "column",
            "column_name": "status",
            "test": av,
            "args_hash": "deadbeef",
        },
        # model-scope test, no args_hash
        {"scope": "model", "test": uq},
        # model-scope test, with args_hash
        {"scope": "model", "test": uq, "args_hash": "abcd1234"},
        # relationships variant
        {"scope": "column", "column_name": "account_id", "test": rel},
    ]
    for kwargs in cases:
        diff_aid = artifact_id_for(**kwargs)  # type: ignore[arg-type]
        grade_aid = _grade_artifact_id_for(**kwargs)  # type: ignore[arg-type]
        assert diff_aid == grade_aid, (
            f"Cross-stage parity break for {kwargs!r}: diff={diff_aid!r} grade={grade_aid!r}"
        )


def test_cross_stage_parity_model_test_args_hash() -> None:
    """The 8-hex blake2b-4 hash matches the grade engine's helper byte-
    for-byte across every CandidateTest variant."""
    tests: tuple[
        CandidateTestNotNull
        | CandidateTestUnique
        | CandidateTestAcceptedValues
        | CandidateTestRelationships,
        ...,
    ] = (
        CandidateTestNotNull(column="user_id"),
        CandidateTestUnique(column="email"),
        CandidateTestAcceptedValues(column="status", values=("c", "a", "b")),
        CandidateTestRelationships(column="account_id", to="ref('accounts')", field="id"),
    )
    for test in tests:
        assert _model_test_args_hash(test) == _grade_model_test_args_hash(test), (
            f"Hash divergence for {type(test).__name__}"
        )


def test_cross_stage_parity_compute_args_hashes() -> None:
    """``compute_args_hashes`` returns byte-equal results vs
    :func:`signalforge.grade.engine._test_args_hashes` across a
    candidate carrying both column-scope and model-scope collisions
    plus an exact duplicate to exercise the ordinal-suffix path."""
    av1 = CandidateTestAcceptedValues(column="status", values=("a", "b"))
    av2 = CandidateTestAcceptedValues(column="status", values=("c", "d"))
    nn1 = CandidateTestNotNull(column="user_id")
    nn2 = CandidateTestNotNull(column="user_id")  # exact duplicate of nn1
    av_m1 = CandidateTestAcceptedValues(column="region", values=("us", "eu"))
    av_m2 = CandidateTestAcceptedValues(column="tier", values=("gold", "silver"))
    candidate = CandidateSchema(
        name="m",
        description="d",
        columns=(
            CandidateColumn(
                name="status",
                description="status",
                tests=(av1, av2),
            ),
            CandidateColumn(
                name="user_id",
                description="pk",
                tests=(nn1, nn2),
            ),
        ),
        tests=(av_m1, av_m2),
    )
    diff_hashes = compute_args_hashes(candidate)
    grade_hashes = _grade_test_args_hashes(candidate)
    # Same key set (id-keyed; both functions iterate the same candidate).
    assert set(diff_hashes.keys()) == set(grade_hashes.keys())
    for key in diff_hashes:
        assert diff_hashes[key] == grade_hashes[key], f"Hash divergence at id={key}"
