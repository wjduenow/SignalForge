"""Public API contract tests for :mod:`signalforge.manifest` (DEC-017).

These tests guard the package's public surface against accidental drift —
both shrinkage (a documented name disappears) and growth (an internal helper
silently becomes part of the public namespace).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

import signalforge.manifest as sf_manifest

DOCUMENTED_MINIMUM = {
    "load",
    "Manifest",
    "Model",
    "ManifestError",
    "ManifestNotFoundError",
    "UnsupportedManifestVersionError",
    "ModelNotFoundError",
    "ModelDisabledError",
    "ModelPathOutsideProjectError",
    "ModelMissingSqlError",
}


@pytest.mark.unit
def test_all_names_importable() -> None:
    """Every name listed in ``__all__`` must be a real attribute on the package."""
    for name in sf_manifest.__all__:
        assert hasattr(sf_manifest, name), (
            f"signalforge.manifest.__all__ lists {name!r} but it is not bound on the package"
        )


@pytest.mark.unit
def test_all_set_matches_documented_minimum() -> None:
    """``__all__`` must include the documented names; extra error classes are fine."""
    assert set(sf_manifest.__all__) >= DOCUMENTED_MINIMUM


@pytest.mark.unit
def test_load_is_callable() -> None:
    assert callable(sf_manifest.load)


@pytest.mark.unit
def test_manifest_and_model_are_pydantic() -> None:
    assert issubclass(sf_manifest.Manifest, BaseModel)
    assert issubclass(sf_manifest.Model, BaseModel)


@pytest.mark.unit
def test_error_hierarchy_intact_via_public_api() -> None:
    """Re-exports must preserve the inheritance graph in :mod:`errors`."""
    from signalforge.manifest import ManifestError, ModelDisabledError

    assert issubclass(ModelDisabledError, ManifestError)


@pytest.mark.unit
def test_internal_helpers_not_promoted() -> None:
    """Loader internals must not leak onto the package's top-level namespace."""
    assert not hasattr(sf_manifest, "_canonicalise_path")
    assert not hasattr(sf_manifest, "_detect_version")


@pytest.mark.unit
def test_loader_module_still_reachable_dotted() -> None:
    """Internals stay reachable via the submodule path — just not as bare attributes."""
    from signalforge.manifest.loader import _canonicalise_path, _detect_version

    assert callable(_canonicalise_path)
    assert callable(_detect_version)
