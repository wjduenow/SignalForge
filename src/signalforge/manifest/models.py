"""Pydantic v2 models for dbt manifest entities (DEC-001, DEC-011, DEC-016, DEC-017).

This module defines a frozen, ``extra="ignore"`` BaseModel hierarchy that
mirrors the slice of dbt's ``target/manifest.json`` schema SignalForge needs.

Design commitments:

* **DEC-001** — Pydantic v2 models with ``extra="ignore"``: dbt's manifest
  contains far more fields than SignalForge cares about (unrendered_config,
  build_path, contract, docs, group_map, parent_map, semantic_models, …).
  Silently ignoring unknown keys keeps the loader resilient across the
  v9–v12 schema range.
* **DEC-011** — ``Config`` is a *narrow* projection of dbt's huge per-model
  ``config`` blob. Only ``materialized``, ``tags``, and ``meta`` are surfaced;
  the rest is dropped via ``extra="ignore"``. Loader code must not reach into
  raw config keys — extend ``Config`` here instead.
* **DEC-016** — Validators on ``Model.unique_id`` (must start with ``model.``)
  and ``Model.raw_code`` (whitespace-only collapses to ``None``) operationalise
  the loader's invariants at parse time, not at use time.
* **DEC-017** — ``populate_by_name=True`` lets callers construct ``Model``
  with either the dbt-on-disk alias (``schema``) or the Python field name
  (``schema_``). ``frozen=True`` makes the data immutable downstream.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from signalforge.warehouse.models import TableRef

_BASE_MODEL_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class Ref(BaseModel):
    """A dbt ``ref()`` target as recorded in ``manifest.nodes[*].refs``.

    The dict-shaped form is what dbt 1.5+ writes (manifest schemas v9–v12).
    Pre-1.5 manifests used a string-shaped ref; those versions are out of
    SignalForge's supported range and are not handled here.
    """

    model_config = _BASE_MODEL_CONFIG

    name: str
    package: str | None = None
    # ``version`` is ``int`` in some manifests and ``str`` in others, depending
    # on how the ref was written in the model SQL.
    version: int | str | None = None


class Source(BaseModel):
    """A dbt ``source`` table from ``manifest.sources`` (DEC-005 of #116).

    dbt records sources as ``database.schema.identifier`` relations keyed by
    ``unique_id`` (``source.<pkg>.<source_name>.<table_name>``). A model
    references one via ``{{ source('<source_name>', '<table_name>') }}`` and
    the manifest mirrors that pair into ``Model.sources`` as
    ``[source_name, table_name]``. The resolvable relation is built from
    ``database`` (BigQuery project), ``schema_`` (dataset), and ``identifier``
    (the physical table name — falls back to ``name`` when dbt omits it).

    Surfaced so :func:`signalforge.manifest.loader.resolve_source` can map a
    ``source(s, t)`` Jinja call to a qualified-name ``TableRef`` without a
    Jinja engine.
    """

    model_config = _BASE_MODEL_CONFIG

    unique_id: str
    source_name: str
    name: str
    resource_type: str
    database: str | None = None
    # ``schema`` collides with ``BaseModel.schema``; store as ``schema_`` and
    # alias to dbt's on-disk key (mirrors ``Model.schema_``).
    schema_: str | None = Field(default=None, alias="schema")
    # dbt sometimes omits ``identifier`` (defaults to the source-table name).
    identifier: str | None = None

    @property
    def relation_name(self) -> str | None:
        """The physical table name: ``identifier`` if set, else ``name``."""
        return self.identifier or self.name


class Column(BaseModel):
    """A column entry from ``manifest.nodes[*].columns``."""

    model_config = _BASE_MODEL_CONFIG

    name: str
    data_type: str | None = None
    description: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    constraints: list[Any] = Field(default_factory=list)


class Config(BaseModel):
    """Narrow projection of dbt's per-model ``config`` blob (DEC-011).

    Only the three fields SignalForge actually consumes are surfaced; every
    other key in dbt's huge config dict (incremental_strategy, persist_docs,
    pre/post-hook, grants, contract, …) is dropped via ``extra="ignore"``.
    """

    model_config = _BASE_MODEL_CONFIG

    materialized: str | None = None
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class DependsOn(BaseModel):
    """Mirror of dbt's ``depends_on`` shape: ``{"nodes": [...], "macros": [...]}``."""

    model_config = _BASE_MODEL_CONFIG

    nodes: list[str] = Field(default_factory=list)
    macros: list[str] = Field(default_factory=list)


class Model(BaseModel):
    """A single ``resource_type == "model"`` entry from the manifest.

    The loader is responsible for filtering ``manifest.nodes`` down to entries
    whose ``unique_id`` starts with ``model.`` *before* constructing this
    type; ``Manifest.nodes`` is typed ``dict[str, Model]`` and will reject
    seed/test/snapshot shapes if they sneak through.
    """

    model_config = _BASE_MODEL_CONFIG

    unique_id: str
    name: str
    resource_type: str
    package_name: str
    original_file_path: str
    path: str
    database: str | None = None
    # ``schema`` is reserved-ish in Pydantic v2 (collides with ``BaseModel.schema``),
    # so we store it as ``schema_`` and alias to dbt's on-disk key.
    schema_: str | None = Field(default=None, alias="schema")
    alias: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    config: Config = Field(default_factory=Config)
    columns: dict[str, Column] = Field(default_factory=dict)
    depends_on: DependsOn = Field(default_factory=DependsOn)
    refs: list[Ref] = Field(default_factory=list)
    # dbt records sources as ``[[source_name, table_name], ...]``.
    sources: list[list[str]] = Field(default_factory=list)
    raw_code: str | None = None
    language: str | None = None
    access: str | None = None
    version: int | str | None = None
    latest_version: int | str | None = None
    primary_key: list[str] = Field(default_factory=list)

    @field_validator("unique_id")
    @classmethod
    def _validate_unique_id(cls, v: str) -> str:
        if not v.startswith("model."):
            raise ValueError(f"unique_id must start with 'model.', got: {v!r}")
        return v

    @field_validator("raw_code")
    @classmethod
    def _strip_raw_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped if stripped else None

    @field_validator("primary_key", "tags", mode="before")
    @classmethod
    def _coerce_none_to_empty_list(cls, v: Any) -> Any:
        # dbt sometimes serialises absent list-valued fields as JSON ``null``
        # rather than omitting them; coerce so callers always see ``[]``.
        if v is None:
            return []
        return v

    @property
    def columns_list(self) -> list[Column]:
        """Ergonomic accessor: the values of ``columns`` in insertion order."""
        return list(self.columns.values())

    def resolve_this(self) -> TableRef:
        """Resolve the dbt ``{{ this }}`` reference to this model's ``TableRef``.

        Thin alias for :meth:`signalforge.warehouse.models.TableRef.from_model`
        (DEC-005 of #116) so a Jinja-ref resolver can map ``{{ this }}`` without
        reaching across layers. Raises the warehouse-layer
        ``ManifestProjectNotFoundError`` / ``ManifestSchemaNotFoundError`` when
        ``database`` / ``schema_`` is absent.
        """
        from signalforge.warehouse.models import TableRef

        return TableRef.from_model(self)


class Manifest(BaseModel):
    """Top-level manifest document.

    ``nodes`` only contains ``resource_type == "model"`` entries — the loader
    filters non-model nodes (seeds, tests, snapshots, analyses, operations)
    *before* constructing this object, since ``Model`` validates that
    ``unique_id`` starts with ``"model."``.

    ``disabled`` is dbt's parallel dict for models disabled via config; its
    values are *lists* of ``Model`` (usually one entry, but dbt allows
    multiple disabled definitions to coexist).

    Top-level keys SignalForge does not consume (``sources``, ``macros``,
    ``parent_map``, ``child_map``, ``unit_tests``, …) are dropped silently
    via ``extra="ignore"`` per DEC-017.
    """

    model_config = _BASE_MODEL_CONFIG

    metadata: dict[str, Any]
    nodes: dict[str, Model] = Field(default_factory=dict)
    disabled: dict[str, list[Model]] = Field(default_factory=dict)
    # dbt's parallel ``sources`` registry, keyed by source unique_id. The
    # loader filters ``resource_type == "source"`` before construction (DEC-005
    # of #116). Empty dict for projects with no declared sources.
    sources: dict[str, Source] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Thin method wrappers — delegate to free functions in ``loader.py``.
    # Deferred imports avoid the ``models <-> loader`` circular dep.
    # ------------------------------------------------------------------

    def get_model(self, key: str | Path) -> Model:
        """Resolve a model by ``unique_id`` (``"model.*"``) or by file path.

        Delegates to :func:`signalforge.manifest.loader.get_model`. See
        that function for the full error contract.
        """
        from signalforge.manifest.loader import get_model as _get

        return _get(self, key)

    def iter_models(self) -> Iterator[Model]:
        """Iterate over enabled (``resource_type == "model"``) nodes."""
        from signalforge.manifest.loader import iter_models as _iter

        return _iter(self)

    def resolve_ref(
        self,
        name: str,
        *,
        package: str | None = None,
        version: int | str | None = None,
    ) -> TableRef:
        """Resolve a dbt ``ref(name)`` to a qualified-name ``TableRef``.

        Delegates to :func:`signalforge.manifest.loader.resolve_ref`. Raises
        :class:`RefNotFoundError` (no enabled model named ``name``) or
        :class:`AmbiguousRefError` (multiple matches; disambiguate with
        ``package``).
        """
        from signalforge.manifest.loader import resolve_ref as _resolve

        return _resolve(self, name, package=package, version=version)

    def resolve_source(self, source_name: str, table_name: str) -> TableRef:
        """Resolve a dbt ``source(source_name, table_name)`` to a ``TableRef``.

        Delegates to :func:`signalforge.manifest.loader.resolve_source`. Raises
        :class:`SourceNotFoundError` when the ``(source_name, table_name)`` pair
        is absent from the manifest's source registry.
        """
        from signalforge.manifest.loader import resolve_source as _resolve

        return _resolve(self, source_name, table_name)

    @property
    def schema_version(self) -> str:
        """Return the manifest's ``metadata.dbt_schema_version`` URL string."""
        from signalforge.manifest.loader import schema_version as _v

        return _v(self)
