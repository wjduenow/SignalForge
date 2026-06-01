"""Mechanic exhaustiveness gate — 6-site dispatch routing test (#170 US-010).

A *targeted* dispatch-site routing test (NOT a full AST scan — DEC-009
of #170): constructs ONE minimal instance of every variant in the
:data:`signalforge.draft.models.CandidateTest` discriminated union and
asserts each routes through every one of the **6 production dispatch
sites** without raising. The 6 sites are enumerated in
``.claude/rules/business-rule-tests.md`` § "The 6 production dispatch
sites" and the issue plan's DEC-009.

Why a targeted routing test and not a full AST scan? Adding the next
test variant (an 8th member of the union, after #170 lands
``unique_combination`` as the 7th) is a multi-arm change touching:

1. :func:`signalforge.prune.compiler._compile_test`
2. :func:`signalforge._common.artifact_id.model_test_args_hash`
3. :func:`signalforge.diff._emitter._render_test`
4. :func:`signalforge.ingest.parser._parse_named_test`
5. :func:`signalforge.draft.parser._validate_anchor_contract`
6. :func:`signalforge.ingest.anchor.validate_anchor_contract`

A missing arm at any of the first three (which dispatch on
``isinstance``) is a latent **runtime crash** (``NotImplementedError``
or ``ValueError`` in the exhaustive ``raise``), NOT a type error — the
discriminated union is open at runtime. Sites 5 and 6 (which dispatch
on ``test.type``) silently mis-route a model-level-only variant onto
the generic ``column not in model_columns`` arm, producing a spurious
anchor violation. Site 4 is the EXTERNAL macro-recognition path; it is
exercised separately for the two variants with an external dbt macro
form (``row_count_between`` → ``dbt_expectations.expect_table_row_count_to_be_between``;
``unique_combination`` → ``dbt_utils.unique_combination_of_columns``).

A targeted routing test parametrised over every union member catches
each shape at test time, NOT at runtime against a real model. Cheap;
load-bearing for the next variant after #170 (every future variant
auto-grows the parametrize when added to the union and forces the
contributor to land all 6 arms — or fail this test loud).

Cardinality pin: a separate test asserts the union holds exactly eight
members (the v0.3 count after #171's ``row_count_anomaly_by_period``
landed via US-003). When the next variant lands, the contributor MUST
bump the constant in lockstep with the union — this is the "tripwire"
that turns a missing arm into a test failure rather than a runtime
crash on the operator's machine.

Parallel-bead scaffolding (#171 US-003 lesson): #171's variant landed
in a scaffolding bead (US-003) that explicitly does NOT extend the six
dispatch arms — those land in their own beads (US-004/005/006/007/008
+ the diff-emitter arm). Until those land, the per-site routing tests
would crash on the new variant; :data:`_VARIANTS_PENDING_DISPATCH_ARMS`
is a **per-site dict** (site number → frozenset of pending variants),
and each per-site parametrize iterates the matching ``_VARIANTS_FOR_SITE_N``
constant (computed once at import via :func:`_variants_for_site`).
Per-site granularity lets a single dispatch-arm bead land (e.g. site
2 in #171 US-004) without blocking the other sites' beads — once
US-004 merges, site 2's pending set is empty and the variant flows
through site 2's parametrize, while sites 1/3/5/6 stay exempted
until their own beads land. The cardinality + factory-coverage +
macro-yaml-coverage tests still iterate the FULL :data:`_VARIANTS`
set so the tripwire stays loud.
"""

from __future__ import annotations

from typing import get_args

import pytest

from signalforge._common.artifact_id import model_test_args_hash
from signalforge.diff._emitter import _SKIP, _render_test
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTest,
    CandidateTestAcceptedValues,
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestRowCountAnomalyByPeriod,
    CandidateTestRowCountBetween,
    CandidateTestUnique,
    CandidateTestUniqueCombination,
)
from signalforge.draft.parser import _validate_anchor_contract
from signalforge.ingest.anchor import validate_anchor_contract
from signalforge.ingest.errors import IngestAnchorContractError
from signalforge.ingest.models import SkippedTest
from signalforge.ingest.parser import _parse_named_test
from signalforge.manifest.models import Column, Manifest, Model, Source
from signalforge.prune.compiler import (
    _compile_test,
    _InvalidIdentifier,
    _RequiresFutureData,
)
from signalforge.warehouse.models import BIGQUERY_DIALECT, TableRef

# ---------------------------------------------------------------------------
# Cardinality pin — the union holds exactly eight members as of #171.
# ---------------------------------------------------------------------------

#: Expected number of variants in the :data:`CandidateTest` discriminated
#: union. Bumped 6 → 7 in #170 (``unique_combination`` landed), 7 → 8 in
#: #171 (``row_count_anomaly_by_period`` landed via US-003). When the
#: next variant lands, this constant MUST move in lockstep — the
#: cardinality test below is the tripwire that forces every contributor
#: adding the next variant to acknowledge the 6-site dispatch obligation.
_EXPECTED_VARIANT_COUNT: int = 8

#: Variants pending dispatch-arm work in their own beads, keyed by
#: production dispatch site number (1–6 per
#: ``.claude/rules/business-rule-tests.md`` § "The 6 production dispatch
#: sites"). #171's US-003 lands the variant class + union + drift
#: mirror + fixture + factory arm, but the six per-site dispatch arms
#: land in separate beads (US-004 site 2, US-006 site 5, US-007 site
#: 6, US-008 site 1, US-014 site 3; site 4 is N/A — no external macro
#: form). Until each per-site arm lands, that site's routing test
#: would crash on the new variant; the per-site dict excludes the
#: variant from THAT site's parametrize only, so once a dispatch-arm
#: bead lands the matching entry is removed (leaving the variant
#: exercised on that site) while the still-pending sites keep their
#: exemption.
#:
#: Each per-site set MUST shrink in lockstep with the matching
#: dispatch-arm bead landing — i.e. once US-004/006/007/008/014 are
#: merged, every set should be empty and every variant must round-trip
#: every dispatch site.
#:
#: As of US-007 + US-008 (this merge): sites 1 (prune compiler), 2
#: (``_common.artifact_id``), and 6 (``ingest.anchor``) are landed for
#: ``CandidateTestRowCountAnomalyByPeriod`` — all three removed from
#: their pending sets. Sites 3 (diff emitter) and 5 (drafter anchor)
#: are still pending in sibling beads.
_VARIANTS_PENDING_DISPATCH_ARMS: dict[int, frozenset[type]] = {
    1: frozenset(),  # US-008 — landed
    2: frozenset(),  # US-004 — landed
    3: frozenset({CandidateTestRowCountAnomalyByPeriod}),  # US-014 (diff emitter)
    4: frozenset(),  # N/A — variant has no external dbt-macro form
    5: frozenset({CandidateTestRowCountAnomalyByPeriod}),  # US-006 (draft anchor)
    6: frozenset(),  # US-007 — landed
}


def _variants_for_site(site_num: int) -> tuple[type, ...]:
    """Return the per-site parametrize set for the given dispatch site.

    Filters :data:`_VARIANTS` by removing variants listed in that
    site's :data:`_VARIANTS_PENDING_DISPATCH_ARMS` entry. Once every
    pending arm has landed, the per-site sets are empty and this
    function returns :data:`_VARIANTS` verbatim for every site.
    """
    pending = _VARIANTS_PENDING_DISPATCH_ARMS.get(site_num, frozenset())
    return tuple(v for v in _VARIANTS if v not in pending)


def _candidate_test_variants() -> tuple[type, ...]:
    """Reflect the concrete classes from the :data:`CandidateTest` union.

    The :data:`CandidateTest` alias is
    ``Annotated[A | B | C | …, Field(discriminator="type")]``;
    :func:`typing.get_args` peels the ``Annotated`` wrapper, returning
    ``(<union>, <field_info>)``. Calling :func:`get_args` again on the
    union yields the concrete classes (eight as of #171). Reflecting
    (rather than hardcoding the tuple) is load-bearing: adding the next
    variant to the union auto-grows the parametrize without a test edit,
    so a contributor cannot add a variant + miss a dispatch site by
    editing only one place.
    """
    annotated_args = get_args(CandidateTest)
    # annotated_args[0] is the unwrapped X | Y | Z union; get_args on it
    # yields the concrete classes.
    return get_args(annotated_args[0])


# Cached at import time so the parametrize IDs are stable.
_VARIANTS: tuple[type, ...] = _candidate_test_variants()

#: Per-site parametrize sets. Each site's tuple equals :data:`_VARIANTS`
#: once that site's entry in :data:`_VARIANTS_PENDING_DISPATCH_ARMS`
#: is empty. Held as module-level constants (not recomputed inside the
#: parametrize decorator) so the parametrize IDs are stable across
#: collection.
_VARIANTS_FOR_SITE_1: tuple[type, ...] = _variants_for_site(1)
_VARIANTS_FOR_SITE_2: tuple[type, ...] = _variants_for_site(2)
_VARIANTS_FOR_SITE_3: tuple[type, ...] = _variants_for_site(3)
_VARIANTS_FOR_SITE_4: tuple[type, ...] = _variants_for_site(4)
_VARIANTS_FOR_SITE_5: tuple[type, ...] = _variants_for_site(5)
_VARIANTS_FOR_SITE_6: tuple[type, ...] = _variants_for_site(6)


# ---------------------------------------------------------------------------
# Test-instance factory — minimal valid instances, one per variant.
# ---------------------------------------------------------------------------


def _make_instance(variant_cls: type) -> CandidateTest:
    """Return a minimal valid instance of ``variant_cls``.

    Every column-scoped variant uses ``column="customer_id"`` so the
    anchor-contract dispatch sites can validate against a single model
    fixture. Model-level-only variants (``custom_sql`` with
    ``column=None``, ``row_count_between``, ``unique_combination``) use
    Pydantic's hard-coded ``column = None``.

    ``unique_combination`` uses ``("customer_id", "order_id")`` — both
    must appear on the model fixture below.
    """
    if variant_cls is CandidateTestNotNull:
        return CandidateTestNotNull(column="customer_id")
    if variant_cls is CandidateTestUnique:
        return CandidateTestUnique(column="customer_id")
    if variant_cls is CandidateTestAcceptedValues:
        return CandidateTestAcceptedValues(column="customer_id", values=("a", "b"))
    if variant_cls is CandidateTestRelationships:
        return CandidateTestRelationships(column="customer_id", to="customers", field="id")
    if variant_cls is CandidateTestCustomSQL:
        # Column-scoped form. ``CandidateTestCustomSQL.column`` is
        # ``str | None`` (the variant supports both column-scoped and
        # model-level shapes), but the factory deliberately uses the
        # column-scoped form here:
        #
        # * Sites 1, 2, 3 route on ``isinstance`` and accept either
        #   shape (no behavioural difference).
        # * Sites 5, 6 dispatch on ``test.type``; the column-scoped
        #   form lands in the CandidateColumn-loop arm (where the
        #   parent-column-equality exemption for custom_sql lives —
        #   ``llm-drafter.md`` § "Whole-draft fail-loud anchor
        #   contract"), which is structurally distinct from the
        #   model-level loop arm.
        # * The model-level loop exemption for ``custom_sql`` is NOT
        #   currently present in :mod:`signalforge.ingest.anchor` (site
        #   6) — only ``row_count_between`` and ``unique_combination``
        #   are exempted. In practice ``read_schema`` does not produce
        #   model-level ``custom_sql`` from operator YAML (the macro
        #   recognition arm in :func:`_parse_named_test` does not
        #   recognise it), and :func:`read_test_files` does not call
        #   the anchor validator at all (see the docstring on
        #   ``read_test_files``), so the gap is benign today — but a
        #   future code path that synthesises model-level ``custom_sql``
        #   and feeds it through site 6 would surface a spurious
        #   "references nonexistent column None" violation. Documented
        #   here so the next variant author knows the exemption arms
        #   on site 6 are NOT exhaustive over the model-level-capable
        #   variants.
        return CandidateTestCustomSQL(column="customer_id", sql="SELECT 1 WHERE 1=0")
    if variant_cls is CandidateTestRowCountBetween:
        return CandidateTestRowCountBetween(minimum=0, maximum=1_000_000)
    if variant_cls is CandidateTestUniqueCombination:
        return CandidateTestUniqueCombination(columns=("customer_id", "order_id"))
    if variant_cls is CandidateTestRowCountAnomalyByPeriod:
        # Defaults across every non-required field per DEC-007;
        # ``date_column`` must be a real model column for the future
        # site-5/6 anchor arms to pass. The ``orders`` fixture below
        # adds ``ordered_at`` as the date_column carrier.
        return CandidateTestRowCountAnomalyByPeriod(date_column="ordered_at")
    raise AssertionError(
        f"_make_instance has no arm for variant {variant_cls.__name__}. "
        "A new CandidateTest variant landed without extending this factory; "
        "add an arm here and re-run the dispatch routing test."
    )


# ---------------------------------------------------------------------------
# Manifest fixtures — single ``orders`` model + a ``customers`` parent so
# ``relationships`` can resolve. The ``orders`` model carries every column
# any variant references (``customer_id``, ``order_id``).
# ---------------------------------------------------------------------------


def _make_orders_model() -> Model:
    return Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={
            "customer_id": Column(name="customer_id"),
            "order_id": Column(name="order_id"),
            "ordered_at": Column(name="ordered_at"),
        },
        raw_code="select 1",
    )


def _make_customers_model() -> Model:
    return Model(
        unique_id="model.shop.customers",
        name="customers",
        resource_type="model",
        package_name="shop",
        original_file_path="models/customers.sql",
        path="customers.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1",
    )


def _make_manifest() -> Manifest:
    """Manifest carrying ``orders`` + ``customers`` so ``relationships``
    resolves to a real parent. ``custom_sql``'s ``{{ this }}`` resolution
    walks ``orders``; a sibling ``raw.events`` source covers the
    ``source()`` resolution path on site 1 if a future variant needs it.
    """
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={
            "model.shop.orders": _make_orders_model(),
            "model.shop.customers": _make_customers_model(),
        },
        sources={
            "source.shop.raw.events": Source(
                unique_id="source.shop.raw.events",
                source_name="raw",
                name="events",
                resource_type="source",
                database="fake_project",
                schema="raw_dataset",  # type: ignore[call-arg]
                identifier="events",
            )
        },
    )


def _make_orders_table_ref() -> TableRef:
    return TableRef(project="fake_project", dataset="dataset", name="orders")


# Closed mapping from variant → external dbt-macro YAML form (for site 4).
# A ``None`` value means "this variant has no external macro recognition arm;
# site 4 is N/A". The two entries below mirror the two external recognition
# arms ``_parse_named_test`` ships (#169 + #170).
_EXTERNAL_MACRO_YAML: dict[type, tuple[str, dict] | None] = {
    CandidateTestNotNull: None,
    CandidateTestUnique: None,
    CandidateTestAcceptedValues: None,
    CandidateTestRelationships: None,
    CandidateTestCustomSQL: None,
    CandidateTestRowCountBetween: (
        "dbt_expectations.expect_table_row_count_to_be_between",
        {"min_value": 0, "max_value": 1_000_000},
    ),
    CandidateTestUniqueCombination: (
        "dbt_utils.unique_combination_of_columns",
        {"combination_of_columns": ["customer_id", "order_id"]},
    ),
    # ``row_count_anomaly_by_period`` has no external dbt-macro
    # recognition form today (#171 does not ship one; the variant is
    # SignalForge-internal). Set to ``None`` so site 4 is N/A for this
    # variant — matches the convention for the four built-in tests +
    # ``custom_sql``.
    CandidateTestRowCountAnomalyByPeriod: None,
}


# ---------------------------------------------------------------------------
# The cardinality tripwire — guards against silent variant addition.
# ---------------------------------------------------------------------------


def test_candidate_test_union_has_exactly_eight_variants() -> None:
    """The :data:`CandidateTest` union holds exactly eight variants as of
    #171. When the next variant lands, this test must be updated in
    lockstep with the union — the failure here is the explicit signal
    that the contributor needs to:

    1. Bump :data:`_EXPECTED_VARIANT_COUNT`.
    2. Extend :func:`_make_instance` with an arm for the new variant.
    3. Extend :data:`_EXTERNAL_MACRO_YAML` (``None`` if no macro form).
    4. Verify each of the 6 production dispatch sites carries the new
       arm (the parametrised routing test below catches a missing arm
       on every site that ``_make_instance`` produces an instance for).

    Mirrors the ``test_scan_7_discovers_every_per_stage_errors_module``
    pattern in ``tests/test_audit_completeness.py``: a count-only
    sanity test paired with the structural test it guards.
    """
    assert len(_VARIANTS) == _EXPECTED_VARIANT_COUNT, (
        f"CandidateTest union size changed from {_EXPECTED_VARIANT_COUNT} to "
        f"{len(_VARIANTS)}. Update _EXPECTED_VARIANT_COUNT in lockstep AND "
        "verify every one of the 6 production dispatch sites in "
        ".claude/rules/business-rule-tests.md § 'The 6 production dispatch "
        "sites' has an arm for the new variant. _make_instance and "
        "_EXTERNAL_MACRO_YAML need an arm too — the routing test below "
        "exercises only variants the factory knows about."
    )


def test_make_instance_covers_every_variant() -> None:
    """:func:`_make_instance` must have an arm for every union member.
    Without this test, a future contributor could add an 8th variant,
    bump :data:`_EXPECTED_VARIANT_COUNT`, and forget the factory — the
    routing test would then skip the new variant silently.
    """
    for variant_cls in _VARIANTS:
        instance = _make_instance(variant_cls)
        assert isinstance(instance, variant_cls), (
            f"_make_instance({variant_cls.__name__}) returned "
            f"{type(instance).__name__}; factory arm is mis-wired"
        )


def test_external_macro_yaml_covers_every_variant() -> None:
    """Every union member must appear in :data:`_EXTERNAL_MACRO_YAML`
    (value ``None`` if no external dbt-macro recognition form).
    """
    missing = [v.__name__ for v in _VARIANTS if v not in _EXTERNAL_MACRO_YAML]
    assert not missing, (
        f"_EXTERNAL_MACRO_YAML is missing arms for: {missing}. "
        "Add an entry (None if the variant has no external macro form)."
    )


# ---------------------------------------------------------------------------
# Site 1 — prune compiler dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant_cls", _VARIANTS_FOR_SITE_1, ids=lambda v: v.__name__)
def test_site_1_prune_compiler_dispatches_every_variant(variant_cls: type) -> None:
    """:func:`signalforge.prune.compiler._compile_test` must have an
    arm for every variant. A missing arm hits the closing
    ``NotImplementedError`` in the dispatcher (an exhaustive-dispatch
    runtime crash that no static type-check catches because the union
    is open at runtime).

    The compiler is allowed to return either a SQL ``str`` (the happy
    path) OR a conservative-bias sentinel (:class:`_RequiresFutureData`
    when ``relationships`` parent is missing from the manifest, or
    :class:`_InvalidIdentifier` when SQL-safety rejects an identifier).
    Both sentinels are valid dispatcher outputs — they are NOT raises.
    """
    test = _make_instance(variant_cls)
    manifest = _make_manifest()
    model = manifest.nodes["model.shop.orders"]

    result = _compile_test(
        test,
        _make_orders_table_ref(),
        BIGQUERY_DIALECT,
        manifest,
        model=model,
    )

    assert isinstance(result, (str, _RequiresFutureData, _InvalidIdentifier)), (
        f"site 1 — {variant_cls.__name__}: _compile_test returned "
        f"{type(result).__name__}; expected str | _RequiresFutureData | _InvalidIdentifier. "
        "A new variant landed without an arm in _compile_test "
        "(signalforge.prune.compiler)."
    )


# ---------------------------------------------------------------------------
# Site 2 — artifact_id hash dispatcher.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant_cls", _VARIANTS_FOR_SITE_2, ids=lambda v: v.__name__)
def test_site_2_artifact_id_hash_dispatches_every_variant(variant_cls: type) -> None:
    """:func:`signalforge._common.artifact_id.model_test_args_hash` must
    have an arm for every variant. A missing arm hits the explicit
    ``ValueError`` in the exhaustive ``else`` branch.

    The function returns an 8-hex ``blake2b-4`` digest (the existing
    contract — see the docstring in ``_common.artifact_id``; the audit
    corpus's other hashes use blake2b-8 / 16-hex, but this one is
    deliberately shorter because it appears as an
    ``artifact_id``-suffix collision-disambiguator, not a reproducibility
    fingerprint).
    """
    test = _make_instance(variant_cls)

    digest = model_test_args_hash(test)

    assert isinstance(digest, str), (
        f"site 2 — {variant_cls.__name__}: model_test_args_hash returned "
        f"{type(digest).__name__}; expected str."
    )
    assert len(digest) == 8, (
        f"site 2 — {variant_cls.__name__}: hash length is {len(digest)}; "
        "expected 8 (blake2b-4 — the artifact_id collision-disambiguator format)."
    )
    int(digest, 16)  # raises if non-hex — covers the all-ASCII-hex invariant


# ---------------------------------------------------------------------------
# Site 3 — diff emitter dispatcher.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant_cls", _VARIANTS_FOR_SITE_3, ids=lambda v: v.__name__)
def test_site_3_diff_emitter_dispatches_every_variant(variant_cls: type) -> None:
    """:func:`signalforge.diff._emitter._render_test` must have an arm
    for every variant. A missing arm hits the explicit ``ValueError``
    in the exhaustive ``raise`` at the bottom of the function.

    Accepted return shapes:

    * ``str`` — bare type name for ``not_null`` / ``unique``.
    * ``dict`` — single-key YAML fragment for parameterised tests
      (including the ``dbt_expectations.*`` / ``dbt_utils.*`` macro
      blocks from #169 / #170).
    * :data:`signalforge.diff._emitter._SKIP` — sentinel for
      ``custom_sql`` (singular tests ship as standalone ``.sql`` files
      via :func:`emit_proposed_test_files`, NOT YAML blocks).
    """
    test = _make_instance(variant_cls)

    result = _render_test(test)

    assert isinstance(result, (str, dict)) or result is _SKIP, (
        f"site 3 — {variant_cls.__name__}: _render_test returned "
        f"{type(result).__name__}; expected str | dict | _SKIP."
    )


# ---------------------------------------------------------------------------
# Site 4 — ingest parser (external dbt-macro recognition).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant_cls", _VARIANTS_FOR_SITE_4, ids=lambda v: v.__name__)
def test_site_4_ingest_parser_dispatches_external_macro_variants(
    variant_cls: type,
) -> None:
    """:func:`signalforge.ingest.parser._parse_named_test` is the
    EXTERNAL macro-recognition dispatch site. Variants without an
    external dbt-macro form (``not_null``, ``unique``,
    ``accepted_values``, ``relationships``, ``custom_sql`` — read from
    external ``tests/*.sql`` singular files, not from ``schema.yml`` —
    have :data:`_EXTERNAL_MACRO_YAML` set to ``None`` and this test
    is a no-op for those variants.

    For ``row_count_between`` (#169 — ``dbt_expectations.expect_table_row_count_to_be_between``)
    and ``unique_combination`` (#170 —
    ``dbt_utils.unique_combination_of_columns``), the parser must
    recognise the macro name and return the matching
    :class:`CandidateTest` subclass (a :class:`SkippedTest` would
    indicate the parser routed to the generic
    ``custom-or-generic-test`` skip arm — a silent regression).
    """
    macro = _EXTERNAL_MACRO_YAML[variant_cls]
    if macro is None:
        pytest.skip(
            f"{variant_cls.__name__} has no external dbt-macro recognition "
            "form (site 4 is N/A — value is None in _EXTERNAL_MACRO_YAML)."
        )

    macro_name, macro_body = macro

    result = _parse_named_test(macro_name, body=macro_body, column=None)

    assert isinstance(result, variant_cls), (
        f"site 4 — {variant_cls.__name__}: _parse_named_test({macro_name!r}, "
        f"body={macro_body!r}) returned {type(result).__name__}; expected "
        f"{variant_cls.__name__}. The parser is mis-routing the external "
        "macro to the custom-or-generic-test skip arm OR a different "
        "variant — verify _parse_named_test has a recognition arm for "
        "this macro name."
    )
    # Explicit negative: must NOT be a SkippedTest (the silent-routing
    # failure mode).
    assert not isinstance(result, SkippedTest), (
        f"site 4 — {variant_cls.__name__}: _parse_named_test routed to "
        f"SkippedTest({result.reason!r}); the recognition arm is missing."
    )


# ---------------------------------------------------------------------------
# Site 5 — drafter anchor-contract dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant_cls", _VARIANTS_FOR_SITE_5, ids=lambda v: v.__name__)
def test_site_5_draft_anchor_dispatches_every_variant(variant_cls: type) -> None:
    """:func:`signalforge.draft.parser._validate_anchor_contract` must
    have a dispatch arm for every variant — model-level-only variants
    (``custom_sql`` model-level form, ``row_count_between``,
    ``unique_combination``) need an explicit special-case BEFORE the
    generic ``test.column not in model_columns`` fallthrough, else
    ``column=None`` produces a spurious "references nonexistent column
    None" violation.

    The test produces a minimal valid candidate and asserts the
    validator returns an empty violations tuple. A non-empty result
    signals that the variant fell through to the generic arm.
    """
    test = _make_instance(variant_cls)
    model_columns = frozenset({"customer_id", "order_id"})

    if test.column is not None:
        # Column-scoped variant: file under a CandidateColumn so the
        # parent-column-equality check passes. ``not_null`` / ``unique``
        # / ``accepted_values`` / ``relationships`` all land here.
        candidate = CandidateSchema(
            name="orders",
            description="orders model",
            columns=(
                CandidateColumn(
                    name="customer_id",
                    description="customer id",
                    tests=(test,),
                ),
                CandidateColumn(name="order_id", description="order id"),
            ),
            tests=(),
        )
    else:
        # Model-level variant (custom_sql / row_count_between /
        # unique_combination): attach as a model-level test.
        candidate = CandidateSchema(
            name="orders",
            description="orders model",
            columns=(
                CandidateColumn(name="customer_id", description="customer id"),
                CandidateColumn(name="order_id", description="order id"),
            ),
            tests=(test,),
        )

    violations = _validate_anchor_contract(candidate, model_columns)

    assert violations == (), (
        f"site 5 — {variant_cls.__name__}: _validate_anchor_contract "
        f"produced violations on a valid candidate: {list(violations)}. "
        "The variant likely fell through to the generic column-existence "
        "check despite being model-level-only (column=None). Verify the "
        "validator has an explicit elif test.type == "
        f"{test.type!r} branch ahead of the generic fallthrough."
    )


def test_site_5_draft_anchor_raises_on_real_violation_for_unique_combination() -> None:
    """Self-check that site 5 actually fails loud when a real anchor
    violation is present — guards against a tautological green
    ``test_site_5_draft_anchor_dispatches_every_variant`` arm that
    silently masks dispatch bugs.

    Plants a ``unique_combination`` referencing a column NOT on the
    model and asserts the validator surfaces a violation. Without this
    test, a refactor that broke the column-membership check on the
    ``unique_combination`` arm (and silently returned ``()``) would
    pass the routing test trivially.
    """
    test = CandidateTestUniqueCombination(columns=("customer_id", "phantom_col"))
    model_columns = frozenset({"customer_id", "order_id"})
    candidate = CandidateSchema(
        name="orders",
        description="orders model",
        columns=(
            CandidateColumn(name="customer_id", description="customer id"),
            CandidateColumn(name="order_id", description="order id"),
        ),
        tests=(test,),
    )

    violations = _validate_anchor_contract(candidate, model_columns)

    assert any("phantom_col" in v for v in violations), (
        "site 5 self-check: planted unique_combination violation "
        "(missing column 'phantom_col') was NOT surfaced by "
        "_validate_anchor_contract. The dispatch arm is mis-wired and "
        "the routing test would now silently pass on a broken validator."
    )


# ---------------------------------------------------------------------------
# Site 6 — ingest anchor-contract dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant_cls", _VARIANTS_FOR_SITE_6, ids=lambda v: v.__name__)
def test_site_6_ingest_anchor_dispatches_every_variant(variant_cls: type) -> None:
    """:func:`signalforge.ingest.anchor.validate_anchor_contract` must
    have an exemption arm for every model-level-only variant — the
    same shape as site 5. The model-level loop iterates ``candidate.tests``
    and asserts ``test.column in model_columns``; for a variant where
    ``column=None``, the check fires a spurious "model-level test
    references nonexistent column None" violation without the
    early-continue exemption.

    Asserts no :class:`IngestAnchorContractError` raises for any
    valid candidate of any variant.
    """
    test = _make_instance(variant_cls)
    model_columns = frozenset({"customer_id", "order_id"})

    if test.column is not None:
        candidate = CandidateSchema(
            name="orders",
            description="orders model",
            columns=(
                CandidateColumn(
                    name="customer_id",
                    description="customer id",
                    tests=(test,),
                ),
                CandidateColumn(name="order_id", description="order id"),
            ),
            tests=(),
        )
    else:
        candidate = CandidateSchema(
            name="orders",
            description="orders model",
            columns=(
                CandidateColumn(name="customer_id", description="customer id"),
                CandidateColumn(name="order_id", description="order id"),
            ),
            tests=(test,),
        )

    # validate_anchor_contract returns None on success; raises on
    # violation. We pin "no raise" as the contract.
    try:
        validate_anchor_contract(candidate, model_columns)
    except IngestAnchorContractError as exc:  # pragma: no cover — pin the failure path
        pytest.fail(
            f"site 6 — {variant_cls.__name__}: ingest "
            f"validate_anchor_contract raised IngestAnchorContractError "
            f"on a valid candidate: {list(exc.violations)}. "
            "The variant likely fell through to the generic "
            "'model-level test references nonexistent column None' arm "
            "despite being model-level-only. Verify the validator has an "
            f"explicit `if test.type == {test.type!r}: continue` ahead "
            "of the column-membership check."
        )


def test_site_6_ingest_anchor_raises_on_real_violation_for_unique_combination() -> None:
    """Self-check parallel to site 5's self-check: confirms site 6
    actually fails loud when a real violation is present. The drafter-
    side anchor validator (site 5) already has its own column-existence
    check for ``unique_combination.columns`` (US-006 in #170); the
    ingest-side validator (site 6) does NOT need to duplicate it —
    ``signalforge.ingest.anchor.validate_anchor_contract`` only
    exempts model-level-only variants from the generic ``test.column``
    check. To pin a true site-6 failure we need a different shape: a
    :class:`CandidateColumn` whose ``name`` is absent from the model.
    """
    candidate = CandidateSchema(
        name="orders",
        description="orders model",
        columns=(
            # Phantom column-name: site 6's CandidateColumn.name
            # membership check should reject it.
            CandidateColumn(name="phantom_col", description="phantom"),
        ),
        tests=(),
    )
    model_columns = frozenset({"customer_id", "order_id"})

    with pytest.raises(IngestAnchorContractError) as excinfo:
        validate_anchor_contract(candidate, model_columns)
    assert any("phantom_col" in v for v in excinfo.value.violations), (
        "site 6 self-check: planted CandidateColumn violation was NOT "
        "surfaced as an IngestAnchorContractError on 'phantom_col'."
    )


# ---------------------------------------------------------------------------
# Cross-site invariant — every variant of the union is exercised on
# every site that the factory produces an instance for. The previous
# tests cover this implicitly; this final test pins the count so a
# parametrize miscount surfaces explicitly.
# ---------------------------------------------------------------------------


def test_every_variant_is_exercised_on_every_in_scope_dispatch_site() -> None:
    """Cross-check that every union variant appears in the parametrize
    ID space. A future contributor that adds a variant but forgets to
    update :func:`_make_instance` would cause :func:`_make_instance` to
    raise ``AssertionError`` on the new variant — but only on a parametrize
    iteration. This sanity test makes the count assertion explicit so a
    parametrize collection bug (variant skipped because of an
    ``ids=`` collision or similar) surfaces before the per-site tests
    even run.
    """
    assert len(_VARIANTS) == _EXPECTED_VARIANT_COUNT
    # Sites 1, 2, 3, 5, 6 run on every variant whose dispatch arm for
    # THAT site has landed (per-site filter via _VARIANTS_FOR_SITE_N
    # backed by :data:`_VARIANTS_PENDING_DISPATCH_ARMS`); site 4 skips
    # for variants without external macro recognition. The cross-product
    # varies with the per-site pending sets (#171 US-003 lands one
    # pending variant; US-004 empties site 2's set; sibling beads
    # empty the other sites' sets as they merge). We don't pin that
    # exact number (the parametrize collection mechanics are pytest's
    # job) — what we pin is that every variant in the FULL union has a
    # factory arm AND an _EXTERNAL_MACRO_YAML entry, so no variant is
    # silently skipped from the cross-coverage tracking.
    factory_covered = {v for v in _VARIANTS if _make_instance(v) is not None}
    assert factory_covered == set(_VARIANTS), (
        f"_make_instance does not cover every variant. Missing: {set(_VARIANTS) - factory_covered}"
    )
    macro_covered = {v for v in _VARIANTS if v in _EXTERNAL_MACRO_YAML}
    assert macro_covered == set(_VARIANTS), (
        f"_EXTERNAL_MACRO_YAML does not cover every variant. Missing: "
        f"{set(_VARIANTS) - macro_covered}"
    )
