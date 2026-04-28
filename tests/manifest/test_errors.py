"""Unit tests for the manifest errors module (DEC-013, DEC-014)."""

from __future__ import annotations

import pytest

from signalforge.manifest.errors import (
    ManifestError,
    ManifestNotFoundError,
    ModelDisabledError,
    ModelMissingSqlError,
    ModelNotFoundError,
    ModelPathOutsideProjectError,
    UnsupportedManifestVersionError,
)

ALL_ERROR_CLASSES: tuple[type[ManifestError], ...] = (
    ManifestNotFoundError,
    UnsupportedManifestVersionError,
    ModelNotFoundError,
    ModelDisabledError,
    ModelPathOutsideProjectError,
    ModelMissingSqlError,
)


@pytest.mark.unit
def test_hierarchy_root() -> None:
    """ManifestError subclasses Exception; every other class inherits from it."""
    assert issubclass(ManifestError, Exception)
    for cls in ALL_ERROR_CLASSES:
        assert issubclass(cls, ManifestError), f"{cls.__name__} must inherit from ManifestError"


@pytest.mark.unit
def test_model_disabled_is_model_not_found() -> None:
    """ModelDisabledError is a subclass of ModelNotFoundError so callers
    can catch both with `except ModelNotFoundError`."""
    assert issubclass(ModelDisabledError, ModelNotFoundError)


@pytest.mark.unit
def test_default_remediations_present() -> None:
    """Every concrete error class declares a non-empty default_remediation."""
    for cls in ALL_ERROR_CLASSES:
        remediation = cls.default_remediation
        assert isinstance(remediation, str)
        assert remediation.strip(), f"{cls.__name__}.default_remediation must be non-empty"


@pytest.mark.unit
def test_str_includes_message_and_remediation() -> None:
    """__str__ renders both the constructor message and the default remediation."""
    rendered = str(ModelMissingSqlError("raw_code empty"))
    assert "raw_code empty" in rendered
    assert "dbt parse" in rendered
    assert "Remediation" in rendered


@pytest.mark.unit
def test_remediation_override() -> None:
    """Explicit remediation= overrides the class default in __str__."""
    err = ModelNotFoundError("missing", remediation="custom hint")
    rendered = str(err)
    assert "custom hint" in rendered
    assert ModelNotFoundError.default_remediation not in rendered


@pytest.mark.unit
def test_model_not_found_caught_by_manifest_error_base() -> None:
    """A ManifestError except-block catches a ModelNotFoundError instance."""
    caught: ManifestError | None = None
    try:
        raise ModelNotFoundError("nope")
    except ManifestError as exc:
        caught = exc
    assert caught is not None
    assert isinstance(caught, ModelNotFoundError)


@pytest.mark.unit
def test_model_disabled_error_caught_by_model_not_found_handler() -> None:
    """except ModelNotFoundError catches a raised ModelDisabledError —
    this fails if the inheritance chain is reordered (e.g. ModelDisabledError
    is accidentally made a sibling rather than a subclass)."""
    caught: ModelNotFoundError | None = None
    try:
        raise ModelDisabledError("disabled in dbt config")
    except ModelNotFoundError as exc:
        caught = exc
    assert caught is not None
    assert isinstance(caught, ModelDisabledError)
    # And the rendered message still carries the disabled-specific remediation,
    # not ModelNotFoundError's default — proves default_remediation is read off
    # the actual class, not the parent.
    assert ModelDisabledError.default_remediation in str(caught)
