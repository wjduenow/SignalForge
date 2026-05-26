"""Unit tests for the warehouse errors module (DEC-026, DEC-022).

Mirrors :mod:`tests.manifest.test_errors`. Every test is capable of failing:
no ``assert True``-shaped placeholders (``testing-signal.md``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.warehouse import errors as errors_module
from signalforge.warehouse.errors import (
    BytesBilledExceededError,
    ColumnNotFoundError,
    EstimateUnavailableError,
    IncompleteProfileError,
    InvalidIdentifierError,
    ManifestProjectNotFoundError,
    ManifestSchemaNotFoundError,
    MaterialisationFailedError,
    MaterialisationNotSupportedError,
    ProfileNotFoundError,
    ProfileTargetNotFoundError,
    QuerySyntaxError,
    SamplingError,
    SamplingRequiresPartitionFilterError,
    TableNotFoundError,
    UnknownTableSizeError,
    UnsupportedAuthMethodError,
    UnsupportedProfileTypeError,
    WarehouseAuthError,
    WarehouseError,
)

# Constructor kwargs for each subclass — keeps the parametrised test below
# honest as new subclasses get added (the missing entry breaks at collection
# rather than silently skipping).
_CONSTRUCT_KWARGS: dict[str, dict[str, object]] = {
    "WarehouseError": {"message": "generic warehouse failure"},
    "WarehouseAuthError": {"message": "auth failed"},
    "UnsupportedProfileTypeError": {"profile_type": "snowflake"},
    "UnsupportedAuthMethodError": {"method": "service-account"},
    "ProfileEnvVarUnsetError": {
        "var_name": "MY_BILLING_PROJECT",
        "profiles_path": Path("/etc/dbt/profiles.yml"),
    },
    "ProfileNotFoundError": {"searched_paths": [Path("/etc/dbt/profiles.yml")]},
    "ProfileTargetNotFoundError": {"profile_name": "myproj", "target": "prod"},
    "ManifestProjectNotFoundError": {"model_unique_id": "model.proj.foo"},
    "ManifestSchemaNotFoundError": {"model_unique_id": "model.proj.foo"},
    "InvalidIdentifierError": {"field": "dataset", "value": "ok"},
    "BytesBilledExceededError": {"job_id": "j1", "bytes_billed": 200, "limit": 100},
    "TableNotFoundError": {"table": "p.d.t"},
    "ColumnNotFoundError": {"table": "p.d.t", "column": "c"},
    "QuerySyntaxError": {"detail": "syntax err"},
    "SamplingError": {"message": "generic sampling failure"},
    "SamplingRequiresPartitionFilterError": {"table": "p.d.t", "num_rows": 200_000_000},
    "UnknownTableSizeError": {"table": "p.d.t"},
    "MaterialisationFailedError": {"message": "BigQuery refused the CTAS"},
    "MaterialisationNotSupportedError": {"adapter_name": "SnowflakeAdapter"},
    "EstimateNotSupportedError": {"adapter_name": "SnowflakeAdapter"},
    "EstimateUnavailableError": {"detail": "EXPLAIN plan lacked GlobalStats"},
    "IncompleteProfileError": {
        "profile_type": "snowflake",
        "missing": ["account", "warehouse"],
    },
}


@pytest.mark.unit
def test_warehouse_error_renders_remediation_marker() -> None:
    """The base ``__str__`` includes both the message and the
    ``↳ Remediation:`` marker line."""
    rendered = str(WarehouseError("boom", remediation="fix it"))
    assert "boom" in rendered
    assert "↳ Remediation: fix it" in rendered


@pytest.mark.unit
def test_all_is_sorted() -> None:
    """``__all__`` is alphabetically sorted — keeps the package re-export
    surface (US-011) deterministic and the diff churn low when classes are
    added."""
    assert errors_module.__all__ == sorted(errors_module.__all__)


@pytest.mark.unit
def test_each_subclass_has_default_remediation() -> None:
    """Every class in ``__all__`` declares a non-empty ``default_remediation``.

    Iterating ``__all__`` (rather than a hand-curated tuple) is what catches
    a future contributor who adds a class but forgets the remediation.
    """
    # DEC-026 enumerates 15 typed subclasses (WarehouseAuthError through
    # UnknownTableSizeError) plus the WarehouseError base = 16 classes;
    # issue #22 (US-001) adds MaterialisationFailedError and
    # MaterialisationNotSupportedError → 18; issue #36 (US-002) adds
    # EstimateNotSupportedError → 19; issue #47 adds
    # ProfileEnvVarUnsetError (supports init-demo profile env_var
    # rendering) → 20.
    assert len(errors_module.__all__) == 22, (
        "DEC-026 enumerates 15 typed subclasses + 1 base; #22 US-001 "
        "adds 2 more (MaterialisationFailed/NotSupported); #36 US-002 "
        "adds EstimateNotSupportedError; #47 QG pass-3 adds "
        "ProfileEnvVarUnsetError; #120 US-002 adds IncompleteProfileError; "
        "#130 US-001 adds EstimateUnavailableError. "
        "Update tests and __all__ together if this changes."
    )
    for name in errors_module.__all__:
        cls = getattr(errors_module, name)
        assert issubclass(cls, WarehouseError), f"{name} must subclass WarehouseError"
        remediation = cls.default_remediation
        assert isinstance(remediation, str)
        assert remediation.strip(), f"{name}.default_remediation must be non-empty"


@pytest.mark.unit
@pytest.mark.error
def test_invalid_identifier_quotes_value_via_repr() -> None:
    """Adversarial input is rendered through ``repr()`` (DEC-022) so that
    embedded quotes and control characters are escaped/quoted in the
    rendered message — log viewers and stack traces never see raw input."""
    adversarial = "foo'; DROP TABLE bar; --"
    err = InvalidIdentifierError(field="dataset", value=adversarial)
    rendered = str(err)
    # ``repr()`` of the value MUST appear verbatim somewhere in the message;
    # raw unquoted form must NOT.
    assert repr(adversarial) in rendered
    # And the field name is also repr-quoted, defending against log injection
    # via crafted field names too.
    assert repr("dataset") in rendered
    # Sanity: attributes are preserved for programmatic access.
    assert err.field == "dataset"
    assert err.value == adversarial


@pytest.mark.unit
@pytest.mark.error
def test_sampling_subclass_caught_by_parent() -> None:
    """``except SamplingError`` catches both sampling subclasses — proves
    the inheritance chain is what DEC-026 specifies, not a sibling layout."""
    caught_a: SamplingError | None = None
    try:
        raise SamplingRequiresPartitionFilterError(table="p.d.t", num_rows=200_000_000)
    except SamplingError as exc:
        caught_a = exc
    assert isinstance(caught_a, SamplingRequiresPartitionFilterError)

    caught_b: SamplingError | None = None
    try:
        raise UnknownTableSizeError(table="p.d.t")
    except SamplingError as exc:
        caught_b = exc
    assert isinstance(caught_b, UnknownTableSizeError)


@pytest.mark.unit
@pytest.mark.error
def test_profile_target_caught_by_profile_not_found() -> None:
    """``except ProfileNotFoundError`` catches a raised
    :class:`ProfileTargetNotFoundError`. Keeps the "I just want to know the
    profile didn't load" caller path simple."""
    caught: ProfileNotFoundError | None = None
    try:
        raise ProfileTargetNotFoundError(profile_name="myproj", target="prod")
    except ProfileNotFoundError as exc:
        caught = exc
    assert isinstance(caught, ProfileTargetNotFoundError)
    # The discriminating fields survive the catch.
    assert caught.profile_name == "myproj"
    assert caught.target == "prod"


@pytest.mark.unit
def test_bytes_billed_exceeded_carries_fields() -> None:
    """Discriminating fields (``job_id``, ``bytes_billed``, ``limit``) are
    accessible attributes on the instance — ops docs (DEC-027) need this to
    cross-link to the BigQuery job history."""
    err = BytesBilledExceededError(
        job_id="abc",
        bytes_billed=200_000_000,
        limit=100_000_000,
    )
    assert err.job_id == "abc"
    assert err.bytes_billed == 200_000_000
    assert err.limit == 100_000_000
    rendered = str(err)
    assert "100000000" in rendered
    assert "200000000" in rendered


@pytest.mark.unit
def test_every_subclass_constructible_and_renders() -> None:
    """Smoke-test the constructor signature and ``__str__`` for every class
    in ``__all__``. The ``_CONSTRUCT_KWARGS`` table is intentionally a tight
    coupling: adding a new subclass without an entry breaks this test."""
    missing = set(errors_module.__all__) - set(_CONSTRUCT_KWARGS)
    assert not missing, f"_CONSTRUCT_KWARGS missing entries: {sorted(missing)}"
    for name in errors_module.__all__:
        cls = getattr(errors_module, name)
        kwargs = _CONSTRUCT_KWARGS[name]
        instance = cls(**kwargs)
        rendered = str(instance)
        assert "↳ Remediation:" in rendered, f"{name} did not render remediation"


@pytest.mark.unit
@pytest.mark.error
def test_remediation_override_per_instance() -> None:
    """Explicit ``remediation=`` overrides the class default, mirroring the
    manifest-layer contract."""
    err = TableNotFoundError(table="p.d.t", remediation="custom hint")
    rendered = str(err)
    assert "custom hint" in rendered
    assert TableNotFoundError.default_remediation not in rendered


@pytest.mark.unit
def test_unsupported_profile_type_quotes_value() -> None:
    """DEC-022 applies to every error that embeds user-supplied strings."""
    err = UnsupportedProfileTypeError(profile_type="snow\nflake")
    rendered = str(err)
    # repr() of a string with a newline contains the escape \n, not a real
    # newline within the quoted literal.
    assert repr("snow\nflake") in rendered
    assert err.profile_type == "snow\nflake"


@pytest.mark.unit
def test_unsupported_auth_method_quotes_value() -> None:
    err = UnsupportedAuthMethodError(method="service-account-json")
    assert repr("service-account-json") in str(err)
    assert err.method == "service-account-json"


@pytest.mark.unit
def test_manifest_project_and_schema_carry_unique_id() -> None:
    p = ManifestProjectNotFoundError(model_unique_id="model.proj.foo")
    s = ManifestSchemaNotFoundError(model_unique_id="model.proj.foo")
    assert p.model_unique_id == "model.proj.foo"
    assert s.model_unique_id == "model.proj.foo"
    assert repr("model.proj.foo") in str(p)
    assert repr("model.proj.foo") in str(s)


@pytest.mark.unit
def test_profile_not_found_renders_searched_paths() -> None:
    paths = [Path("/etc/dbt/profiles.yml"), Path("/home/u/.dbt/profiles.yml")]
    err = ProfileNotFoundError(searched_paths=paths)
    rendered = str(err)
    for p in paths:
        # Each path is repr-quoted (DEC-022 — defends against crafted paths
        # in dev environments).
        assert repr(str(p)) in rendered
    assert err.searched_paths == paths


@pytest.mark.unit
def test_warehouse_auth_error_default_remediation_mentions_gcloud() -> None:
    """Failing this test means the remediation lost its actionable hint —
    a regression that erodes the explainable-diffs commitment."""
    rendered = str(WarehouseAuthError("ADC missing"))
    assert "gcloud auth application-default login" in rendered


@pytest.mark.unit
def test_query_syntax_and_column_not_found_carry_fields() -> None:
    q = QuerySyntaxError(detail="unexpected token")
    assert q.detail == "unexpected token"
    assert repr("unexpected token") in str(q)

    c = ColumnNotFoundError(table="p.d.t", column="missing_col")
    assert c.table == "p.d.t"
    assert c.column == "missing_col"
    rendered = str(c)
    assert repr("missing_col") in rendered
    assert repr("p.d.t") in rendered


@pytest.mark.unit
def test_materialisation_failed_error_str_format() -> None:
    """``MaterialisationFailedError`` renders the message + the
    ``↳ Remediation:`` line via the layer-base pattern (DEC-008 of
    plans/super/22-temp-table-sample.md). The class wraps any SDK /
    network / quota failure during the materialisation query — the
    operator gets a typed, remediation-bearing exception instead of a
    raw `google.api_core.exceptions.BadRequest`."""
    err = MaterialisationFailedError("CTAS rejected by BigQuery")
    rendered = str(err)
    assert "CTAS rejected by BigQuery" in rendered
    assert "↳ Remediation:" in rendered
    # The default remediation is non-empty and points the operator at a
    # recovery path.
    assert MaterialisationFailedError.default_remediation.strip()


@pytest.mark.unit
def test_materialisation_not_supported_error_carries_dec006_remediation() -> None:
    """``MaterialisationNotSupportedError`` renders the DEC-006 verbatim
    remediation string. This is the ABC default-impl raise — every
    non-BigQuery adapter inherits it, and the remediation tells the
    operator how to fall back via ``signalforge.yml``."""
    expected_remediation = (
        "Set 'prune.sample_strategy: oneshot' in signalforge.yml to fall "
        "back to per-test sampling, or wait for v0.3 multi-warehouse "
        "materialisation support."
    )
    err = MaterialisationNotSupportedError(adapter_name="SnowflakeAdapter")
    rendered = str(err)
    assert expected_remediation in rendered
    # The class-level constant is the source of truth.
    assert MaterialisationNotSupportedError.default_remediation == expected_remediation


@pytest.mark.unit
def test_both_new_errors_inherit_from_warehouse_error() -> None:
    """Both ``MaterialisationFailedError`` and
    ``MaterialisationNotSupportedError`` subclass :class:`WarehouseError`
    so the CLI's tier-3 ``WarehouseError`` MRO walk in
    ``_EXCEPTION_TO_EXIT_CODE`` covers them via inheritance (DEC-008 of
    plans/super/22-temp-table-sample.md). A future regression that
    drops the warehouse base would silently route these into tier-1
    (panic-path); the isinstance assertion is the gate."""
    failed = MaterialisationFailedError("boom")
    not_supported = MaterialisationNotSupportedError(adapter_name="X")
    assert isinstance(failed, WarehouseError)
    assert isinstance(not_supported, WarehouseError)


@pytest.mark.unit
@pytest.mark.error
def test_incomplete_profile_error_collects_all_missing_keys() -> None:
    """``IncompleteProfileError`` names the profile type, lists EVERY missing
    key (collect-all, #120 US-002 / DEC-004), and renders the standard
    ``↳ Remediation:`` line. ``.profile_type`` and ``.missing`` round-trip as
    attributes for programmatic access; ``.missing`` is a copied list, not the
    caller's reference."""
    missing = ["account", "warehouse"]
    err = IncompleteProfileError(profile_type="snowflake", missing=missing)
    rendered = str(err)
    # Every missing key appears (repr-quoted per DEC-022).
    assert repr("account") in rendered
    assert repr("warehouse") in rendered
    # The profile type is named and repr-quoted.
    assert repr("snowflake") in rendered
    assert "↳ Remediation:" in rendered
    # Attributes round-trip.
    assert err.profile_type == "snowflake"
    assert err.missing == ["account", "warehouse"]
    # The stored list is a copy — mutating the caller's list must not bleed
    # through.
    missing.append("role")
    assert err.missing == ["account", "warehouse"]


@pytest.mark.unit
def test_estimate_unavailable_error_subclasses_warehouse_error() -> None:
    """``EstimateUnavailableError`` subclasses :class:`WarehouseError` so the
    CLI's tier-3 ``WarehouseError`` MRO walk in ``_EXCEPTION_TO_EXIT_CODE``
    covers it via inheritance (DEC-003 of issue #130). A regression dropping
    the base would silently route it into the panic-path tier; the
    ``isinstance`` assertion is the gate."""
    err = EstimateUnavailableError(detail="EXPLAIN plan lacked GlobalStats")
    assert isinstance(err, WarehouseError)


@pytest.mark.unit
def test_estimate_unavailable_error_str_renders_message_and_remediation() -> None:
    """``__str__`` renders the diagnostic message AND the
    ``↳ Remediation:`` line via the layer-base pattern. The ``detail`` field
    is repr-quoted (DEC-022) so adversarial planner output can't smuggle a
    raw newline / control char into a log viewer; the field also round-trips
    as an attribute for programmatic access."""
    detail = "GlobalStats\nmissing"
    err = EstimateUnavailableError(detail=detail)
    rendered = str(err)
    # repr() of the value appears verbatim; raw multi-line form does not.
    assert repr(detail) in rendered
    assert "Query-bytes estimate unavailable:" in rendered
    assert "↳ Remediation:" in rendered
    assert err.detail == detail


@pytest.mark.unit
def test_estimate_unavailable_error_remediation_locked_verbatim() -> None:
    """DEC-003 of issue #130's plan — the default remediation is locked
    byte-for-byte. Changing this text is a contract break: the CLI's
    ``--estimate`` degrade path surfaces it to operators, and downstream
    tooling / CI parsers may key on it."""
    expected = (
        "The query plan carried no parseable byte estimate; the run falls "
        "back to a price-only cost preview. EXPLAIN figures are planner "
        "estimates and may be absent for some query shapes — re-run "
        "without --estimate to skip the preview entirely."
    )
    assert EstimateUnavailableError.default_remediation == expected


@pytest.mark.unit
def test_estimate_unavailable_distinct_from_not_supported() -> None:
    """The two estimation errors are sibling concretes, not parent/child:
    ``EstimateUnavailableError`` ("ran but no figure") must NOT be an
    instance of ``EstimateNotSupportedError`` ("does no estimation at all").
    Conflating them would let the engine misreport which degrade fired."""
    from signalforge.warehouse.errors import EstimateNotSupportedError

    unavailable = EstimateUnavailableError(detail="x")
    assert not isinstance(unavailable, EstimateNotSupportedError)
