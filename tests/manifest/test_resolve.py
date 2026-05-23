"""Tests for Jinja-ref relation resolution (DEC-005 of #116).

The manifest layer exposes three resolvers that map a dbt Jinja reference to a
qualified-name ``TableRef``, with NO Jinja engine:

* ``Manifest.resolve_ref(name)`` / ``resolve_source(s, t)`` — registry lookups.
* ``Model.resolve_this()`` — the model's own ``TableRef``.

Ambiguous and unknown refs/sources fail loud with typed errors. These tests
exercise the resolvers against the committed ``dbt_project_small`` manifest
(a source ``raw.users`` plus three inter-refing models) and against
hand-crafted multi-package manifests for the ambiguity path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from signalforge.manifest import (
    AmbiguousRefError,
    Manifest,
    Model,
    RefNotFoundError,
    SourceNotFoundError,
    load,
    resolve_ref,
    resolve_source,
)
from signalforge.warehouse.models import TableRef

PROJECT_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_small"
FIXTURE_DIR = PROJECT_DIR / "target"

# ``TableRef`` validates ``project`` against GCP's 6–30-char grammar, so the
# resolver tests build manifests with a BigQuery-valid project / dataset rather
# than the small fixture's short ``dev`` / ``main`` placeholders (those never
# flow through TableRef in the loader tests).
_PROJECT = "my_project_dev"
_DATASET = "analytics"


def _model_dict(unique_id: str, *, name: str, package: str) -> dict[str, Any]:
    return {
        "unique_id": unique_id,
        "name": name,
        "resource_type": "model",
        "package_name": package,
        "original_file_path": f"models/{name}.sql",
        "path": f"{name}.sql",
        "database": _PROJECT,
        "schema": _DATASET,
        "alias": name,
        "raw_code": "select 1 as id",
    }


def _load_manifest_dict(version: int = 12) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURE_DIR / f"manifest_v{version}.json").read_text())
    raw["nodes"] = {
        unique_id: node
        for unique_id, node in raw.get("nodes", {}).items()
        if unique_id.startswith("model.")
    }
    raw["sources"] = {
        unique_id: src
        for unique_id, src in raw.get("sources", {}).items()
        if src.get("resource_type") == "source"
    }
    return raw


def _manifest(version: int = 12) -> Manifest:
    """A manifest with three inter-refing models and one source.

    Built from scratch (not the small fixture) so ``database`` / ``schema``
    satisfy ``TableRef``'s GCP-project / BigQuery-dataset grammar.
    """
    return Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {
                "model.signalforge_test_small.stg_users": _model_dict(
                    "model.signalforge_test_small.stg_users",
                    name="stg_users",
                    package="signalforge_test_small",
                ),
                "model.signalforge_test_small.dim_users": _model_dict(
                    "model.signalforge_test_small.dim_users",
                    name="dim_users",
                    package="signalforge_test_small",
                ),
            },
            "sources": {
                "source.signalforge_test_small.raw.users": {
                    "unique_id": "source.signalforge_test_small.raw.users",
                    "source_name": "raw",
                    "name": "users",
                    "resource_type": "source",
                    "database": _PROJECT,
                    "schema": "raw",
                    "identifier": "users",
                }
            },
        }
    )


# ---------------------------------------------------------------------------
# Loader surfaces sources
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_surfaces_sources_registry() -> None:
    """``load()`` populates ``Manifest.sources`` with the project's sources."""
    manifest = load(PROJECT_DIR, manifest_path="target/manifest_v12.json")
    assert len(manifest.sources) == 1
    src = next(iter(manifest.sources.values()))
    assert src.source_name == "raw"
    assert src.name == "users"


@pytest.mark.unit
def test_manifest_with_no_sources_has_empty_registry() -> None:
    """A manifest dict without sources yields an empty (not missing) dict."""
    raw = _load_manifest_dict(12)
    raw.pop("sources", None)
    manifest = Manifest.model_validate(raw)
    assert manifest.sources == {}


# ---------------------------------------------------------------------------
# resolve_ref
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_ref_returns_qualified_tableref() -> None:
    """``ref('stg_users')`` resolves to the model's qualified ``TableRef``."""
    manifest = _manifest()
    ref = manifest.resolve_ref("stg_users")
    assert isinstance(ref, TableRef)
    assert ref.qualified_name == f"{_PROJECT}.{_DATASET}.stg_users"


@pytest.mark.unit
def test_resolve_ref_free_function_matches_method() -> None:
    """The free function and the method delegate to the same resolution."""
    manifest = _manifest()
    assert resolve_ref(manifest, "dim_users").qualified_name == f"{_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_resolve_ref_unknown_fails_loud() -> None:
    """An unknown ref name raises :class:`RefNotFoundError`."""
    manifest = _manifest()
    with pytest.raises(RefNotFoundError) as excinfo:
        manifest.resolve_ref("does_not_exist")
    assert "does_not_exist" in str(excinfo.value)
    # Remediation line is rendered.
    assert "Remediation:" in str(excinfo.value)


@pytest.mark.unit
def test_resolve_ref_with_package_disambiguates() -> None:
    """The two-arg ``ref('pkg', 'name')`` form constrains by package."""
    manifest = _manifest()
    ref = manifest.resolve_ref("stg_users", package="signalforge_test_small")
    assert ref.qualified_name == f"{_PROJECT}.{_DATASET}.stg_users"


@pytest.mark.unit
def test_resolve_ref_with_wrong_package_fails_loud() -> None:
    """A package mismatch is a not-found, not a silent fall-through."""
    manifest = _manifest()
    with pytest.raises(RefNotFoundError):
        manifest.resolve_ref("stg_users", package="some_other_pkg")


@pytest.mark.unit
def test_resolve_ref_ambiguous_fails_loud() -> None:
    """A model name in two packages with no ``package`` raises ambiguity."""
    manifest = Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {
                "model.pkg_a.shared": _model_dict(
                    "model.pkg_a.shared", name="shared", package="pkg_a"
                ),
                "model.pkg_b.shared": _model_dict(
                    "model.pkg_b.shared", name="shared", package="pkg_b"
                ),
            },
        }
    )
    with pytest.raises(AmbiguousRefError) as excinfo:
        manifest.resolve_ref("shared")
    msg = str(excinfo.value)
    assert "model.pkg_a.shared" in msg
    assert "model.pkg_b.shared" in msg


@pytest.mark.unit
def test_resolve_ref_ambiguous_disambiguated_by_package() -> None:
    """Supplying the package picks the right model out of the collision."""
    manifest = Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {
                "model.pkg_a.shared": _model_dict(
                    "model.pkg_a.shared", name="shared", package="pkg_a"
                ),
                "model.pkg_b.shared": _model_dict(
                    "model.pkg_b.shared", name="shared", package="pkg_b"
                ),
            },
        }
    )
    ref = manifest.resolve_ref("shared", package="pkg_b")
    assert ref.qualified_name == f"{_PROJECT}.{_DATASET}.shared"


# ---------------------------------------------------------------------------
# resolve_source
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_source_returns_qualified_tableref() -> None:
    """``source('raw', 'users')`` resolves to the source relation."""
    manifest = _manifest()
    ref = manifest.resolve_source("raw", "users")
    assert isinstance(ref, TableRef)
    assert ref.qualified_name == f"{_PROJECT}.raw.users"


@pytest.mark.unit
def test_resolve_source_free_function_matches_method() -> None:
    manifest = _manifest()
    assert resolve_source(manifest, "raw", "users").qualified_name == f"{_PROJECT}.raw.users"


@pytest.mark.unit
def test_resolve_source_unknown_fails_loud() -> None:
    """An unknown (source, table) pair raises :class:`SourceNotFoundError`."""
    manifest = _manifest()
    with pytest.raises(SourceNotFoundError) as excinfo:
        manifest.resolve_source("raw", "not_a_table")
    assert "not_a_table" in str(excinfo.value)
    assert "Remediation:" in str(excinfo.value)


@pytest.mark.unit
def test_resolve_source_uses_identifier_when_present() -> None:
    """The physical table name comes from ``identifier`` not ``name``."""
    manifest = Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {},
            "sources": {
                "source.pkg.ext.events": {
                    "unique_id": "source.pkg.ext.events",
                    "source_name": "ext",
                    "name": "events",
                    "resource_type": "source",
                    "database": _PROJECT,
                    "schema": "landing",
                    "identifier": "raw_events_v2",
                }
            },
        }
    )
    ref = manifest.resolve_source("ext", "events")
    assert ref.qualified_name == f"{_PROJECT}.landing.raw_events_v2"


# ---------------------------------------------------------------------------
# resolve_this
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_this_returns_models_own_tableref() -> None:
    """``Model.resolve_this()`` mirrors ``TableRef.from_model``."""
    manifest = _manifest()
    model = manifest.get_model("model.signalforge_test_small.stg_users")
    this = model.resolve_this()
    assert isinstance(this, TableRef)
    assert this.qualified_name == f"{_PROJECT}.{_DATASET}.stg_users"


@pytest.mark.unit
def test_resolve_this_uses_alias_over_name() -> None:
    """``resolve_this`` honours ``alias`` (the physical name), not ``name``."""
    model = Model.model_validate(
        {
            "unique_id": "model.pkg.logical",
            "name": "logical",
            "resource_type": "model",
            "package_name": "pkg",
            "original_file_path": "models/logical.sql",
            "path": "logical.sql",
            "database": _PROJECT,
            "schema": _DATASET,
            "alias": "physical_tbl",
            "raw_code": "select 1",
        }
    )
    assert model.resolve_this().qualified_name == f"{_PROJECT}.{_DATASET}.physical_tbl"
