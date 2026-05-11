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
    # EstimateNotSupportedError → 19.
    assert len(errors_module.__all__) == 19, (
        "DEC-026 enumerates 15 typed subclasses + 1 base; #22 US-001 "
        "adds 2 more (MaterialisationFailed/NotSupported); #36 US-002 "
        "adds EstimateNotSupportedError. Update tests and __all__ "
        "together if this changes."
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
