"""Tests for the manifest Pydantic models (DEC-001, DEC-011, DEC-016, DEC-017).

These tests cover three layers:

1. Pure-data unit tests on hand-crafted dicts, exercising validators and
   default-factory wiring without touching the filesystem.
2. An integration test parametrised over the four committed small-project
   fixtures (``manifest_v9.json`` … ``manifest_v12.json``), proving the
   model hierarchy round-trips real manifests across dbt 1.5 → 1.11.
3. A drift detector that builds an ``extra="forbid"`` subclass of ``Model``
   and asserts a representative real-manifest node round-trips after we
   strip down to the supported keys — and explodes if an unknown key is
   added. This is the canary for "Model.fields silently expanded without
   updating ``extra`` semantics."
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict, ValidationError

from signalforge.manifest.models import (
    Column,
    Config,
    DependsOn,
    GenericTest,
    Manifest,
    Model,
    Ref,
    Source,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_small" / "target"
GENERIC_TEST_NODES = (
    Path(__file__).parent.parent / "fixtures" / "manifest" / "generic_test_nodes.json"
)
SUPPORTED_VERSIONS = (9, 10, 11, 12)

# Hand-authored fixture keys (see tests/fixtures/manifest/generic_test_nodes.json).
_V10_ATTACHED_KEY = (
    "test.my_pkg.dbt_expectations_expect_column_values_to_be_between_my_model_amount__100__0"
    ".abc12345de"
)
_V9_NO_ATTACHED_KEY = (
    "test.my_pkg.relationships_child_model_customer_id__id__ref_dim_customers_.def67890ab"
)


def _load_generic_test_nodes() -> dict[str, Any]:
    """Load the hand-authored ``resource_type == "test"`` node fragments."""
    raw: dict[str, Any] = json.loads(GENERIC_TEST_NODES.read_text())
    nodes: dict[str, Any] = raw["nodes"]
    return nodes


def _load_fixture(version: int) -> dict[str, Any]:
    """Load ``manifest_v{version}.json`` and filter ``nodes`` to model entries.

    ``Manifest.nodes`` is typed ``dict[str, Model]`` — and ``Model.unique_id``
    requires the ``"model."`` prefix — so the loader (and these tests) must
    drop seeds/tests/snapshots/analyses *before* validating. The small
    project happens to contain only model nodes, but the filter is the
    contract every caller is expected to honour.
    """
    path = FIXTURE_DIR / f"manifest_v{version}.json"
    raw: dict[str, Any] = json.loads(path.read_text())
    raw["nodes"] = {
        unique_id: node
        for unique_id, node in raw.get("nodes", {}).items()
        if unique_id.startswith("model.")
    }
    return raw


def _minimal_model_dict(**overrides: Any) -> dict[str, Any]:
    """Build a minimal-but-valid Model payload, allowing per-test overrides."""
    base: dict[str, Any] = {
        "unique_id": "model.my_pkg.my_model",
        "name": "my_model",
        "resource_type": "model",
        "package_name": "my_pkg",
        "original_file_path": "models/my_model.sql",
        "path": "my_model.sql",
        "schema": "analytics",
        "raw_code": "select 1 as id",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_model_validate_v12_node() -> None:
    """A minimal-but-realistic dict satisfies ``Model.model_validate``."""
    payload = _minimal_model_dict()
    model = Model.model_validate(payload)
    assert model.unique_id == "model.my_pkg.my_model"
    assert model.name == "my_model"
    assert model.schema_ == "analytics"
    # Defaults wired correctly.
    assert isinstance(model.config, Config)
    assert isinstance(model.depends_on, DependsOn)
    assert model.columns == {}
    assert model.refs == []


@pytest.mark.unit
def test_model_unique_id_validator_rejects_bad_prefix() -> None:
    """``unique_id`` must start with ``model.`` — anything else is a hard error."""
    bad = _minimal_model_dict(unique_id="seed.my_pkg.my_seed")
    with pytest.raises(ValidationError):
        Model.model_validate(bad)


@pytest.mark.unit
def test_model_raw_code_strips_to_none() -> None:
    """Whitespace-only ``raw_code`` collapses to ``None`` (DEC-016)."""
    for blank in ("", "   ", "\n\t  \n"):
        m = Model.model_validate(_minimal_model_dict(raw_code=blank))
        assert m.raw_code is None
    # Non-blank still strips surrounding whitespace.
    m = Model.model_validate(_minimal_model_dict(raw_code="  select 1\n"))
    assert m.raw_code == "select 1"


@pytest.mark.unit
def test_model_columns_list_property() -> None:
    """``columns_list`` returns the dict's values in insertion order."""
    payload = _minimal_model_dict(
        columns={
            "id": {"name": "id", "data_type": "INT64"},
            "name": {"name": "name", "data_type": "STRING"},
            "created_at": {"name": "created_at", "data_type": "TIMESTAMP"},
        }
    )
    model = Model.model_validate(payload)
    names = [c.name for c in model.columns_list]
    assert names == ["id", "name", "created_at"]
    # Each value is a Column instance, not a raw dict.
    assert all(isinstance(c, Column) for c in model.columns_list)


@pytest.mark.unit
def test_model_config_tags_and_top_level_tags_are_independent() -> None:
    """Top-level ``Model.tags`` and ``Model.config.tags`` are separate fields."""
    payload = _minimal_model_dict(tags=["a"], config={"tags": ["b"], "materialized": "view"})
    model = Model.model_validate(payload)
    assert model.tags == ["a"]
    assert model.config.tags == ["b"]
    assert model.config.materialized == "view"


@pytest.mark.unit
@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_manifest_validates_each_schema_version(version: int) -> None:
    """The four committed v9–v12 small manifests round-trip through ``Manifest``."""
    raw = _load_fixture(version)
    manifest = Manifest.model_validate(raw)
    # Top-level shape.
    assert isinstance(manifest, Manifest)
    assert manifest.metadata["dbt_schema_version"].endswith(f"/v{version}.json")
    # At least one model in nodes (the small project ships dim_users, stg_users,
    # and one more) — and at least one model in disabled (stg_orders).
    assert len(manifest.nodes) >= 1
    assert all(isinstance(m, Model) for m in manifest.nodes.values())
    assert len(manifest.disabled) >= 1
    for disabled_list in manifest.disabled.values():
        assert disabled_list, "disabled values are lists with at least one entry"
        assert all(isinstance(m, Model) for m in disabled_list)


@pytest.mark.unit
def test_drift_detector_extra_forbid() -> None:
    """Drift sentinel — see module docstring.

    Build a ``StrictModel(Model)`` with ``extra="forbid"`` and feed it a real
    v12 node trimmed down to the keys ``Model`` declares. Round-trip succeeds.
    Then poke an unknown key in and assert ``ValidationError`` — proving the
    base ``Model``'s field set still matches what the loader trims to.
    """

    class StrictModel(Model):
        model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    raw = _load_fixture(12)
    # Pick any model node.
    node = next(iter(raw["nodes"].values()))

    # Map of model field-name → on-disk alias. Anything ``Model`` declares,
    # by either alias or python name, is allowed through to StrictModel.
    allowed_keys: set[str] = set()
    for field_name, field_info in Model.model_fields.items():
        allowed_keys.add(field_name)
        if field_info.alias is not None:
            allowed_keys.add(field_info.alias)

    trimmed = {k: v for k, v in node.items() if k in allowed_keys}
    # Coerce ``primary_key: None`` → ``[]`` on the way in: ``extra="forbid"``
    # does not bypass the base class's ``mode="before"`` validators, but the
    # parent's coercion still runs, so this works as-is.
    strict = StrictModel.model_validate(trimmed)
    assert strict.unique_id.startswith("model.")

    # Now add an unknown key and assert it's rejected.
    poisoned = dict(trimmed)
    poisoned["definitely_not_a_real_dbt_field"] = "boom"
    with pytest.raises(ValidationError):
        StrictModel.model_validate(poisoned)


@pytest.mark.unit
def test_source_validate_and_relation_name() -> None:
    """A real ``manifest.sources`` entry round-trips; ``relation_name`` derives.

    The physical table name is ``identifier`` when present, else falls back
    to the source-table ``name`` (DEC-005 of #116).
    """
    raw = _load_fixture(12)
    # Select the source deterministically by its known key rather than
    # relying on dict iteration order (P5 fix).
    src_dict = raw["sources"]["source.signalforge_test_small.raw.users"]
    src = Source.model_validate(src_dict)
    assert src.unique_id.startswith("source.")
    assert src.source_name == "raw"
    assert src.name == "users"
    assert src.schema_ == "raw"  # aliased from ``schema``
    # ``relation_name`` is ``identifier`` when present, else ``name``. The
    # fixture carries ``identifier == "users"`` so it equals the identifier.
    # Parenthesised so the assertion actually tests the fallback rather than
    # being short-circuited to the always-truthy ``src.name`` (P5 fix).
    assert src.identifier == "users"
    assert src.relation_name == (src.identifier or src.name)
    assert src.relation_name == "users"

    # identifier-absent fallback to name.
    no_ident = Source.model_validate(
        {
            "unique_id": "source.pkg.s.t",
            "source_name": "s",
            "name": "t",
            "resource_type": "source",
            "database": "db",
            "schema": "sch",
        }
    )
    assert no_ident.relation_name == "t"


@pytest.mark.unit
def test_source_drift_detector_extra_forbid() -> None:
    """Drift sentinel for :class:`Source` — mirrors the ``Model`` detector.

    Build a ``StrictSource(Source)`` with ``extra="forbid"``, feed it a real
    v12 source entry trimmed to the declared keys (round-trip succeeds), then
    poison an unknown key and assert ``ValidationError``.
    """

    class StrictSource(Source):
        model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    raw = _load_fixture(12)
    node = next(iter(raw["sources"].values()))

    allowed_keys: set[str] = set()
    for field_name, field_info in Source.model_fields.items():
        allowed_keys.add(field_name)
        if field_info.alias is not None:
            allowed_keys.add(field_info.alias)

    trimmed = {k: v for k, v in node.items() if k in allowed_keys}
    strict = StrictSource.model_validate(trimmed)
    assert strict.unique_id.startswith("source.")

    poisoned = dict(trimmed)
    poisoned["definitely_not_a_real_dbt_field"] = "boom"
    with pytest.raises(ValidationError):
        StrictSource.model_validate(poisoned)


@pytest.mark.unit
def test_ref_normalised_dict_shape() -> None:
    """The dict-shaped ref (dbt 1.5+) round-trips with explicit nulls.

    Pre-1.5 string-shaped refs are out of SignalForge's supported range
    (v0.1 supports manifest schemas v9–v12 = dbt 1.5–1.11) and are not
    handled by this model.
    """
    ref = Ref.model_validate({"name": "stg_users", "package": None, "version": None})
    assert ref.name == "stg_users"
    assert ref.package is None
    assert ref.version is None

    # Versioned ref — int form.
    ref_v = Ref.model_validate({"name": "dim_users", "package": None, "version": 2})
    assert ref_v.version == 2


# ---------------------------------------------------------------------------
# GenericTest + Manifest.tests (#154 US-001 — DEC-008, DEC-009)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generic_test_validate_v10_attached_node() -> None:
    """A v10+ generic-test node (with ``attached_node`` + populated
    ``compiled_code``) round-trips through ``GenericTest`` with every declared
    field populated and dbt's extra keys dropped via ``extra="ignore"``."""
    node = _load_generic_test_nodes()[_V10_ATTACHED_KEY]
    gt = GenericTest.model_validate(node)
    assert gt.unique_id == _V10_ATTACHED_KEY
    assert gt.compiled_code is not None and "validation_errors" in gt.compiled_code
    assert gt.column_name == "amount"
    assert gt.attached_node == "model.my_pkg.my_model"
    assert gt.file_key_name == "models.my_model"
    assert gt.depends_on.nodes == ["model.my_pkg.my_model"]
    # test_metadata is the macro identity.
    assert gt.test_metadata is not None
    assert gt.test_metadata.name == "expect_column_values_to_be_between"
    assert gt.test_metadata.namespace == "dbt_expectations"
    assert gt.test_metadata.kwargs["min_value"] == 0
    assert gt.test_metadata.kwargs["max_value"] == 100


@pytest.mark.unit
def test_generic_test_null_compiled_code_tolerated_silently() -> None:
    """A parse-only manifest carries ``compiled_code: null``; the reader
    tolerates it silently (mirrors ``Column.data_type = None``, #159).

    The v9 fixture node also omits ``attached_node`` entirely — the field
    defaults to ``None`` without failing validation."""
    node = _load_generic_test_nodes()[_V9_NO_ATTACHED_KEY]
    gt = GenericTest.model_validate(node)
    assert gt.compiled_code is None
    assert gt.attached_node is None  # absent key → default None (v9 shape)
    assert gt.file_key_name == "models.child_model"
    # Multi-model depends_on preserved (relationships test).
    assert gt.depends_on.nodes == [
        "model.my_pkg.dim_customers",
        "model.my_pkg.child_model",
    ]


@pytest.mark.unit
def test_manifest_tests_defaults_empty() -> None:
    """``Manifest.tests`` defaults to an empty dict — no existing
    ``Manifest(...)`` construction site (which omits ``tests``) breaks."""
    manifest = Manifest(metadata={}, nodes={})
    assert manifest.tests == {}


@pytest.mark.unit
def test_manifest_carries_generic_tests() -> None:
    """A ``Manifest`` constructed with ``tests`` surfaces typed ``GenericTest``
    values keyed by unique_id — parallel to ``nodes``/``sources``."""
    nodes = _load_generic_test_nodes()
    manifest = Manifest.model_validate({"metadata": {}, "nodes": {}, "tests": nodes})
    assert set(manifest.tests) == {_V10_ATTACHED_KEY, _V9_NO_ATTACHED_KEY}
    assert all(isinstance(t, GenericTest) for t in manifest.tests.values())
    # ``nodes`` stays model-only: test nodes never leak into it.
    assert manifest.nodes == {}


@pytest.mark.unit
def test_generic_test_drift_detector_extra_forbid() -> None:
    """Drift sentinel for :class:`GenericTest` — mirrors the ``Model`` /
    ``Source`` detectors. Both the v10-attached AND v9-no-attached shapes,
    trimmed to declared keys, round-trip through an ``extra="forbid"`` mirror;
    a poisoned unknown key is rejected."""

    class StrictGenericTest(GenericTest):
        model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    allowed_keys: set[str] = set()
    for field_name, field_info in GenericTest.model_fields.items():
        allowed_keys.add(field_name)
        if field_info.alias is not None:
            allowed_keys.add(field_info.alias)

    nodes = _load_generic_test_nodes()
    # Cover BOTH association shapes (v10+ attached_node present, v9 absent).
    for key in (_V10_ATTACHED_KEY, _V9_NO_ATTACHED_KEY):
        node = nodes[key]
        trimmed = {k: v for k, v in node.items() if k in allowed_keys}
        strict = StrictGenericTest.model_validate(trimmed)
        assert strict.unique_id == key

        poisoned = dict(trimmed)
        poisoned["definitely_not_a_real_dbt_field"] = "boom"
        with pytest.raises(ValidationError):
            StrictGenericTest.model_validate(poisoned)
