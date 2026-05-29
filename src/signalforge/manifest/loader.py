"""Manifest loader: file IO + version detection + symlink-hardened path resolution.

Implements DEC-007, DEC-008, DEC-010, DEC-013, DEC-014, DEC-017.

Public surface
--------------

* :func:`load` — read ``target/manifest.json`` (or an explicit override) into
  a :class:`signalforge.manifest.models.Manifest`. Filters non-model nodes
  (DEC-017) before constructing.
* :func:`get_model`, :func:`iter_models`, :func:`schema_version` — free
  functions exposed as thin method wrappers on :class:`Manifest`. Wrappers
  use a deferred import to avoid circular imports between ``models.py`` and
  this module (Option B from the US-005 design notes).

Design notes
------------

* **Symlink hardening (DEC-007).** :func:`_canonicalise_path` always resolves
  symlinks (``Path.resolve``) before checking ``is_relative_to`` against the
  resolved ``project_dir``. A symlink that escapes the project tree (e.g.
  ``models/escape.sql -> /etc/hostname``) is rejected with
  :class:`ModelPathOutsideProjectError`.
* **Soft 200 MB warning (DEC-008).** :data:`MAX_MANIFEST_BYTES` is a soft
  threshold — exceeding it emits a :class:`UserWarning` (with a memory
  estimate) but does not abort. The 3x factor reflects the rough ratio
  between on-disk JSON and resident Pydantic objects observed during US-002
  research.
* **`manifest_path` override (DEC-010).** Callers can point at any file
  inside ``project_dir``. Paths outside the project — even absolute paths —
  are rejected. We deliberately reuse :class:`ModelPathOutsideProjectError`
  for manifest paths too: the invariant ("the loader must not read files
  outside the project tree") is identical, and a single error class keeps
  the catch surface small for callers.
* **Lazy resolver indexes.** :func:`get_model` lazily builds and caches
  path-based indexes (e.g. ``original_file_path -> Model``) on the
  :class:`Manifest` instance via :func:`object.__setattr__` — the
  standard Pydantic v2 escape hatch for frozen models. Unique-id lookup
  uses ``manifest.nodes`` directly rather than a separate cached
  ``unique_id -> Model`` index. The loader also stashes the resolved
  ``project_dir`` on the manifest the same way so file-path lookups can
  canonicalise inputs without the caller passing it again.
"""

from __future__ import annotations

import errno
import json
import os
import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from signalforge._common.path_safety import canonicalise_path
from signalforge.manifest.errors import (
    AmbiguousRefError,
    ManifestError,
    ManifestNotFoundError,
    ModelDisabledError,
    ModelMissingSqlError,
    ModelNotFoundError,
    ModelPathOutsideProjectError,
    RefNotFoundError,
    SourceNotFoundError,
    UnsupportedManifestVersionError,
)
from signalforge.manifest.models import Column, Manifest, Model

if TYPE_CHECKING:
    from signalforge.warehouse.models import TableRef

MAX_MANIFEST_BYTES = 200 * 1024 * 1024
"""Soft warning threshold (DEC-008). Above this, :func:`load` emits a
:class:`UserWarning` and continues. The constant is module-level so tests
can monkeypatch it down to a few bytes to exercise the warning path
without committing a 200 MB fixture."""

_SUPPORTED_VERSIONS = frozenset({9, 10, 11, 12})
"""Manifest schema versions SignalForge claims support for (dbt 1.5 → 1.11)."""

_VERSION_URL_RE = re.compile(
    r"https?://schemas\.getdbt\.com/dbt/manifest/v(\d+)\.json", re.IGNORECASE
)
"""Matches the canonical ``metadata.dbt_schema_version`` URL shape and
captures the integer version (e.g. ``v12`` → ``12``)."""

_INDEX_ATTR = "_sf_indexes"
_PROJECT_DIR_ATTR = "_sf_project_dir"


# ---------------------------------------------------------------------------
# Path canonicalisation
# ---------------------------------------------------------------------------


def _canonicalise_path(input_path: Path | str, project_dir: Path) -> Path:
    """Resolve ``input_path`` relative to ``project_dir`` and reject escapes.

    DEC-007. Always calls :meth:`Path.resolve` to follow symlinks; rejects
    paths whose resolved form is not under the resolved ``project_dir``.

    Raises :class:`ModelPathOutsideProjectError` if the resolved path
    escapes the project tree, or if either path contains a symlink loop.
    Symlink-cycle detection spans Python versions: <= 3.12 raises
    :class:`RuntimeError` from ``Path.resolve`` regardless of ``strict=``;
    >= 3.13 (gh-108958) raises ``OSError(errno.ELOOP)`` under ``strict=True``
    and stops resolving silently under ``strict=False``, so the input path is
    resolved ``strict=True`` first (falling back to ``strict=False`` only for
    a genuinely missing target).
    """
    p = Path(input_path)
    # The sole caller (`load`) passes an already-resolved `project_dir`, so a
    # cycle / missing-dir cannot surface here in the real flow — these arms are
    # defensive for direct callers and mirror `signalforge._common.path_safety`
    # (which is exercised directly by its own test suite). Excluded from
    # coverage to avoid a spurious gap on the pre-resolved path.
    try:  # pragma: no cover
        project_resolved = project_dir.resolve(strict=True)
    except RuntimeError as exc:  # pragma: no cover - defensive (pre-resolved)
        raise ModelPathOutsideProjectError(
            f"project_dir contains a symlink loop: {project_dir}",
        ) from exc
    except OSError as exc:  # pragma: no cover - defensive (pre-resolved)
        if exc.errno == errno.ELOOP:  # Python >= 3.13 symlink cycle (gh-108958)
            raise ModelPathOutsideProjectError(
                f"project_dir contains a symlink loop: {project_dir}",
            ) from exc
        raise
    if not p.is_absolute():
        p = project_resolved / p
    # Resolve strict=True first so a symlink cycle raises on every supported
    # Python (<= 3.12 RuntimeError; >= 3.13 OSError(ELOOP)). A missing target
    # falls back to strict=False best-effort — under 3.13, strict=False
    # silently stops at the loop and would otherwise slip past containment.
    try:
        resolved = p.resolve(strict=True)
    except RuntimeError as exc:  # pragma: no cover - <=3.12 cycle signal
        raise ModelPathOutsideProjectError(
            f"Path contains a symlink loop: {p}",
        ) from exc
    except (FileNotFoundError, NotADirectoryError):
        # Target does not exist yet — fall back to best-effort resolution.
        # Narrow to these two so a PermissionError / other OSError surfaces
        # instead of being masked as a partially-resolved path.
        resolved = p.resolve(strict=False)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # Python >= 3.13 symlink cycle (gh-108958)
            raise ModelPathOutsideProjectError(
                f"Path contains a symlink loop: {p}",
            ) from exc
        raise
    if not resolved.is_relative_to(project_resolved):
        raise ModelPathOutsideProjectError(
            f"Path {resolved} escapes project_dir {project_resolved}.",
        )
    return resolved


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def _detect_version(metadata: dict[str, Any], raw_manifest: dict[str, Any]) -> int:
    """Return the manifest schema version (9..12) or raise.

    Primary signal is ``metadata.dbt_schema_version`` parsed via the
    canonical URL regex. If the URL is absent or unparseable, fall back to
    feature-sniffing on the raw top-level keys (``unit_tests`` → 12,
    ``saved_queries`` → 11, ``semantic_models`` / ``metrics`` → 10, else 9).

    Raises :class:`UnsupportedManifestVersionError` when the URL parses but
    the captured version is outside :data:`_SUPPORTED_VERSIONS`.
    """
    url = metadata.get("dbt_schema_version", "")
    if isinstance(url, str) and url:
        match = _VERSION_URL_RE.search(url)
        if match is not None:
            version = int(match.group(1))
            if version not in _SUPPORTED_VERSIONS:
                raise UnsupportedManifestVersionError(
                    f"Manifest schema version v{version} is not supported "
                    f"(found in metadata.dbt_schema_version: {url!r}).",
                )
            return version

    # Fallback: feature-sniff. The order matters — newer features are checked first.
    if "unit_tests" in raw_manifest:
        return 12
    if "saved_queries" in raw_manifest:
        return 11
    if "semantic_models" in raw_manifest or "metrics" in raw_manifest:
        return 10
    return 9


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def load(
    project_dir: Path | str,
    manifest_path: Path | str | None = None,
) -> Manifest:
    """Load a dbt manifest into a :class:`Manifest`.

    ``project_dir`` is the dbt project root (the directory containing
    ``dbt_project.yml``). It must exist; otherwise :class:`ManifestNotFoundError`
    is raised.

    ``manifest_path`` defaults to ``project_dir / "target" / "manifest.json"``.
    If supplied as a relative path, it is resolved against ``project_dir``.
    Absolute paths are accepted but must canonicalise to a location under
    ``project_dir`` — symlink escapes are rejected with
    :class:`ModelPathOutsideProjectError` per DEC-007.

    Files larger than :data:`MAX_MANIFEST_BYTES` emit a :class:`UserWarning`
    and continue (DEC-008).
    """
    project = Path(project_dir)
    try:
        project_resolved = project.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ManifestNotFoundError(
            f"project_dir does not exist: {project}",
        ) from exc
    if not project_resolved.is_dir():
        raise ManifestNotFoundError(
            f"project_dir is not a directory: {project_resolved}",
        )

    # Always go through _canonicalise_path — even the default
    # target/manifest.json could be a symlink (or sit inside a symlinked
    # target/) pointing outside the project root. DEC-007 hardening must
    # apply uniformly to both default and override paths.
    default_path = Path("target") / "manifest.json"
    resolved_manifest = _canonicalise_path(
        manifest_path if manifest_path is not None else default_path,
        project_resolved,
    )

    if not resolved_manifest.exists() or not resolved_manifest.is_file():
        raise ManifestNotFoundError(
            f"Manifest file not found: {resolved_manifest}",
        )

    size_bytes = os.path.getsize(resolved_manifest)
    if size_bytes > MAX_MANIFEST_BYTES:
        size_mb = size_bytes / (1024 * 1024)
        warnings.warn(
            f"Manifest is {size_mb:.1f} MB; expect ~{3 * size_mb:.0f} MB "
            f"resident memory in Python objects",
            UserWarning,
            stacklevel=2,
        )

    try:
        with resolved_manifest.open("r", encoding="utf-8") as fh:
            # `json.load` returns `Any`; keep it `Any` (not `dict[str, Any]`)
            # so the root-shape guard below stays live — annotating it as a
            # dict up front makes pyright treat the isinstance check as dead.
            loaded: Any = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"Manifest is not valid JSON: {resolved_manifest} ({exc})",
            remediation="Re-run `dbt parse` — the manifest file is corrupt or truncated.",
        ) from exc

    if not isinstance(loaded, dict):
        raise ManifestError(
            f"Manifest root is not a JSON object: {resolved_manifest}",
            remediation=(
                "Re-run `dbt parse` — manifest.json must be a JSON object at the top level."
            ),
        )
    raw: dict[str, Any] = loaded

    metadata = raw.get("metadata", {}) or {}
    if not isinstance(metadata, dict):
        raise ManifestError(
            f"manifest.metadata is not an object in {resolved_manifest}",
            remediation="Re-run `dbt parse`.",
        )

    # Validates supported version — raises UnsupportedManifestVersionError
    # when the URL says v8/v20/etc. We don't currently *use* the returned
    # int (the loader is otherwise version-agnostic thanks to extra=ignore),
    # but the call must happen so unsupported manifests fail loudly.
    _detect_version(metadata, raw)

    raw_nodes = raw.get("nodes", {}) or {}
    raw_disabled = raw.get("disabled", {}) or {}
    raw_sources = raw.get("sources", {}) or {}

    # DEC-017: Manifest.nodes is dict[str, Model] of *only* model resources.
    filtered_nodes: dict[str, Any] = {
        k: v
        for k, v in raw_nodes.items()
        if isinstance(v, dict) and v.get("resource_type") == "model"
    }

    filtered_disabled: dict[str, list[Any]] = {}
    for k, v in raw_disabled.items():
        if not isinstance(v, list):
            continue
        model_entries = [
            entry
            for entry in v
            if isinstance(entry, dict) and entry.get("resource_type") == "model"
        ]
        if model_entries:
            filtered_disabled[k] = model_entries

    # DEC-005 of #116: Manifest.sources is dict[str, Source] of *only* source
    # resources. dbt only writes ``resource_type == "source"`` entries under
    # the top-level ``sources`` key, but filter defensively for the same reason
    # nodes are filtered (forward-compat across schema versions).
    filtered_sources: dict[str, Any] = {
        k: v
        for k, v in raw_sources.items()
        if isinstance(v, dict) and v.get("resource_type") == "source"
    }

    manifest = Manifest.model_validate(
        {
            "metadata": metadata,
            "nodes": filtered_nodes,
            "disabled": filtered_disabled,
            "sources": filtered_sources,
        }
    )

    # Stash the resolved project_dir so file-path get_model() calls can
    # canonicalise inputs. Frozen-model escape hatch (Pydantic v2 idiom).
    object.__setattr__(manifest, _PROJECT_DIR_ATTR, project_resolved)

    # Overlay column ``data_type`` values from a sibling ``catalog.json``
    # when present (issue #159 — DEC-001, DEC-002, DEC-007, DEC-010).
    manifest = _apply_catalog_overlay(manifest, resolved_manifest, project_resolved)

    return manifest


# ---------------------------------------------------------------------------
# catalog.json sibling merge (issue #159 — DEC-001, DEC-002, DEC-007, DEC-010)
# ---------------------------------------------------------------------------


def _apply_catalog_overlay(
    manifest: Manifest,
    resolved_manifest: Path,
    project_resolved: Path,
) -> Manifest:
    """Merge column ``data_type`` values from a sibling ``catalog.json``.

    Issue #159 — DEC-001/002/007/010. The catalog file is looked up at
    ``<resolved_manifest>.parent / "catalog.json"`` (sibling to the manifest;
    mirrors how dbt itself locates the file). When present, its per-node
    ``columns[*].type`` entries are merged into the in-memory
    :class:`Column.data_type` fields using case-insensitive column-name
    matching (Snowflake catalog.json uppercases identifiers; BigQuery
    preserves case — DEC-007).

    Silent degradation (DEC-010):

    * Catalog file absent → no-op (most common case for projects not running
      ``dbt docs generate``).
    * Catalog JSON malformed / file unreadable → silent skip.
    * Catalog node not in manifest → silently ignored.
    * Catalog column not in the matching manifest model → silently dropped
      (never adds a phantom column to ``Model.columns``).
    * Manifest column not in catalog → keeps ``data_type = None``.

    Path canonicalisation **does** apply: a ``catalog.json`` symlink that
    escapes the project tree raises :class:`PathContainmentError` from the
    common path-safety helper. The silent-degrade set covers I/O / parse
    failures, NOT a malicious path manipulation.

    No logging is emitted (stage-0 invariant per
    ``.claude/rules/manifest-readers.md``).

    Returns either the original ``manifest`` (when nothing to merge) or a
    new :class:`Manifest` whose ``nodes`` dict carries the overlaid
    :class:`Column` / :class:`Model` instances built via
    :meth:`pydantic.BaseModel.model_copy` (the frozen-model pattern).
    """
    catalog_path = resolved_manifest.parent / "catalog.json"
    # Canonicalise the catalog path — security gate; a symlink escape raises
    # PathContainmentError, which we deliberately do NOT swallow.
    resolved_catalog = canonicalise_path(catalog_path, project_resolved)

    if not resolved_catalog.exists() or not resolved_catalog.is_file():
        return manifest

    # Read + parse. Any I/O or JSON failure → silent no-op (DEC-010c).
    try:
        with resolved_catalog.open("r", encoding="utf-8") as fh:
            catalog_loaded: Any = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return manifest

    if not isinstance(catalog_loaded, dict):
        return manifest

    catalog_nodes = catalog_loaded.get("nodes")
    if not isinstance(catalog_nodes, dict):
        return manifest

    # Build {unique_id -> {lower(col_name) -> type}} once.
    by_node: dict[str, dict[str, str]] = {}
    for unique_id, node in catalog_nodes.items():
        if not isinstance(node, dict):
            continue
        cat_cols = node.get("columns")
        if not isinstance(cat_cols, dict):
            continue
        per_node: dict[str, str] = {}
        for cat_col_name, cat_col in cat_cols.items():
            # ``cat_col_name`` is a JSON object key, so always ``str`` — no
            # explicit type guard needed here (the JSON parser enforces it).
            if not isinstance(cat_col, dict):
                continue
            cat_type = cat_col.get("type")
            if not isinstance(cat_type, str):
                continue
            per_node[cat_col_name.lower()] = cat_type
        if per_node:
            by_node[unique_id] = per_node

    if not by_node:
        return manifest

    new_nodes: dict[str, Model] = {}
    changed = False
    for unique_id, model in manifest.nodes.items():
        types_for_node = by_node.get(unique_id)
        if types_for_node is None:
            new_nodes[unique_id] = model
            continue
        new_columns: dict[str, Column] = {}
        model_changed = False
        for col_name, column in model.columns.items():
            cat_type = types_for_node.get(col_name.lower())
            if cat_type is None:
                new_columns[col_name] = column
                continue
            # Frozen-model overlay per manifest-readers.md.
            new_columns[col_name] = column.model_copy(update={"data_type": cat_type})
            model_changed = True
        if model_changed:
            new_nodes[unique_id] = model.model_copy(update={"columns": new_columns})
            changed = True
        else:
            new_nodes[unique_id] = model

    if not changed:
        return manifest

    new_manifest = manifest.model_copy(update={"nodes": new_nodes})
    # Re-stash the resolved project_dir + drop any stale resolver-index cache —
    # model_copy creates a fresh instance; the indexes attached via
    # object.__setattr__ on the old instance are lost intentionally and will
    # be rebuilt lazily on first get_model() lookup against new_nodes.
    object.__setattr__(new_manifest, _PROJECT_DIR_ATTR, project_resolved)
    return new_manifest


# ---------------------------------------------------------------------------
# Resolver helpers (free functions; thin Manifest methods delegate to these)
# ---------------------------------------------------------------------------


def schema_version(manifest: Manifest) -> str:
    """Return the manifest's ``metadata.dbt_schema_version`` URL string.

    Returns an empty string if the URL is absent — feature-sniffing
    happened at load time, so by the time the manifest is constructed an
    absent URL is no longer a fatal condition.
    """
    value = manifest.metadata.get("dbt_schema_version", "")
    if not isinstance(value, str):
        return ""
    return value


def iter_models(manifest: Manifest) -> Iterator[Model]:
    """Iterate over the enabled (``resource_type == "model"``) nodes."""
    return iter(manifest.nodes.values())


# ---------------------------------------------------------------------------
# Jinja-ref relation resolution (DEC-005 of #116)
# ---------------------------------------------------------------------------


def resolve_ref(
    manifest: Manifest,
    name: str,
    *,
    package: str | None = None,
    version: int | str | None = None,
) -> TableRef:
    """Resolve a dbt ``ref(name)`` to a qualified-name ``TableRef``.

    Matches an enabled model by ``Model.name`` (the dbt ``ref()`` argument is
    the unversioned model name, not the ``unique_id``). When ``package`` is
    supplied (the two-arg ``ref('pkg', 'name')`` form), the match is further
    constrained by ``Model.package_name``. ``version`` is accepted for API
    parity with the dbt grammar but not used for matching in v0.1 — dbt's
    versioned models are out of the supported manifest range.

    Raises:
        :class:`RefNotFoundError`: no enabled model matches ``name`` (and
            ``package``, when supplied).
        :class:`AmbiguousRefError`: more than one enabled model matches and no
            ``package`` was given to disambiguate.

    The returned ``TableRef`` is built via
    :meth:`signalforge.warehouse.models.TableRef.from_model`, so a model
    missing ``database`` / ``schema_`` raises the warehouse-layer
    ``ManifestProjectNotFoundError`` / ``ManifestSchemaNotFoundError``.
    """
    from signalforge.warehouse.models import TableRef

    matches = [
        m
        for m in manifest.nodes.values()
        if m.name == name and (package is None or m.package_name == package)
    ]
    if not matches:
        qualified = f"ref('{package}', '{name}')" if package is not None else f"ref('{name}')"
        raise RefNotFoundError(
            f"{qualified} matched no enabled model in the manifest.",
        )
    if len(matches) > 1:
        candidates = ", ".join(sorted(repr(m.unique_id) for m in matches))
        raise AmbiguousRefError(
            f"ref('{name}') matched {len(matches)} enabled models: {candidates}.",
        )
    return TableRef.from_model(matches[0])


def resolve_source(
    manifest: Manifest,
    source_name: str,
    table_name: str,
) -> TableRef:
    """Resolve a dbt ``source(source_name, table_name)`` to a ``TableRef``.

    Matches a :class:`signalforge.manifest.models.Source` by
    ``(source_name, name)`` against the manifest's source registry, then builds
    a ``TableRef`` from the source's ``database`` (project), ``schema_``
    (dataset), and physical table name (``identifier`` or ``name``).

    Raises:
        :class:`SourceNotFoundError`: the ``(source_name, table_name)`` pair is
            absent from ``manifest.sources``.
    """
    from signalforge.warehouse.models import TableRef

    for src in manifest.sources.values():
        if src.source_name == source_name and src.name == table_name:
            # dbt always populates database/schema/identifier on a source, but
            # our read-back model types them nullable for forward-compat. A
            # None here means a malformed manifest — fail loud rather than let
            # TableRef raise an opaque validation error.
            relation = src.relation_name
            if src.schema_ is None or relation is None:
                raise SourceNotFoundError(
                    f"source('{source_name}', '{table_name}') is missing a "
                    f"schema or identifier in the manifest.",
                )
            return TableRef(
                project=src.database,
                dataset=src.schema_,
                name=relation,
            )
    raise SourceNotFoundError(
        f"source('{source_name}', '{table_name}') is not present in manifest.sources.",
    )


def _build_indexes(manifest: Manifest) -> dict[str, dict[str, Model]]:
    """Build (and cache) the resolver indexes.

    Returns a dict with two keys:

    * ``"by_path"`` — ``original_file_path -> Model`` (enabled nodes only).
    * ``"by_path_disabled"`` — ``original_file_path -> Model`` (first
      disabled entry per path; dbt allows multiple but the resolver
      surfaces a single :class:`ModelDisabledError` regardless).
    """
    cached = getattr(manifest, _INDEX_ATTR, None)
    if cached is not None:
        return cached

    by_path: dict[str, Model] = {}
    for model in manifest.nodes.values():
        by_path[model.original_file_path] = model

    by_path_disabled: dict[str, Model] = {}
    for entries in manifest.disabled.values():
        for model in entries:
            by_path_disabled.setdefault(model.original_file_path, model)

    indexes = {"by_path": by_path, "by_path_disabled": by_path_disabled}
    object.__setattr__(manifest, _INDEX_ATTR, indexes)
    return indexes


def _check_raw_code(model: Model) -> Model:
    """Raise :class:`ModelMissingSqlError` if the model has no raw SQL.

    Per DEC-016 this check fires at *resolve* time, not at parse time —
    parsing an empty-raw-code model is fine; using it isn't.
    """
    if model.raw_code is None or not model.raw_code.strip():
        raise ModelMissingSqlError(
            f"Model {model.unique_id!r} has no raw SQL (raw_code is empty/null).",
        )
    return model


def get_model(manifest: Manifest, key: str | Path) -> Model:
    """Resolve a model by ``unique_id`` or by file path.

    Strings starting with ``model.`` go through the unique_id branch; every
    other string and every :class:`Path` goes through the file-path branch.

    Raises:
        :class:`ModelNotFoundError`: key does not match any enabled or disabled model.
        :class:`ModelDisabledError`: key matches a model in ``manifest.disabled``.
        :class:`ModelPathOutsideProjectError`: file path escapes the project tree.
        :class:`ModelMissingSqlError`: resolved model has empty/null ``raw_code``.
        :class:`ManifestError`: ``manifest`` was constructed without going
            through :func:`load` and a file-path lookup was requested.
    """
    if isinstance(key, str) and key.startswith("model."):
        model = manifest.nodes.get(key)
        if model is not None:
            return _check_raw_code(model)
        if key in manifest.disabled:
            raise ModelDisabledError(
                f"Model {key!r} is disabled in dbt config.",
            )
        raise ModelNotFoundError(
            f"Model {key!r} is not present in manifest.nodes or manifest.disabled.",
        )

    # File-path branch. We need project_dir to canonicalise the input.
    project_dir = getattr(manifest, _PROJECT_DIR_ATTR, None)
    if project_dir is None:
        raise ManifestError(
            "Manifest was not constructed via signalforge.manifest.load(); "
            "file-path lookup is unavailable.",
            remediation=(
                "Use `signalforge.manifest.load()` to construct Manifest "
                "objects so file-path resolution works."
            ),
        )

    canonical = _canonicalise_path(key, project_dir)
    try:
        relative = canonical.relative_to(project_dir)
    except ValueError as exc:  # pragma: no cover - guarded by _canonicalise_path
        raise ModelPathOutsideProjectError(
            f"Path {canonical} escapes project_dir {project_dir}.",
        ) from exc
    rel_str = relative.as_posix()

    indexes = _build_indexes(manifest)
    model = indexes["by_path"].get(rel_str)
    if model is not None:
        return _check_raw_code(model)
    if rel_str in indexes["by_path_disabled"]:
        raise ModelDisabledError(
            f"Model at {rel_str!r} is disabled in dbt config.",
        )
    raise ModelNotFoundError(
        f"No model found at file path {rel_str!r}.",
    )
