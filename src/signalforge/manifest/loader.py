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
from typing import Any

from signalforge.manifest.errors import (
    ManifestError,
    ManifestNotFoundError,
    ModelDisabledError,
    ModelMissingSqlError,
    ModelNotFoundError,
    ModelPathOutsideProjectError,
    UnsupportedManifestVersionError,
)
from signalforge.manifest.models import Manifest, Model

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
    try:
        project_resolved = project_dir.resolve(strict=True)
    except RuntimeError as exc:  # Python <= 3.12 symlink cycle
        raise ModelPathOutsideProjectError(
            f"project_dir contains a symlink loop: {project_dir}",
        ) from exc
    except OSError as exc:  # Python >= 3.13 symlink cycle (gh-108958)
        if exc.errno == errno.ELOOP:
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
    except RuntimeError as exc:  # Python <= 3.12 symlink cycle
        raise ModelPathOutsideProjectError(
            f"Path contains a symlink loop: {p}",
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # Python >= 3.13 symlink cycle (gh-108958)
            raise ModelPathOutsideProjectError(
                f"Path contains a symlink loop: {p}",
            ) from exc
        resolved = p.resolve(strict=False)
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
            raw: dict[str, Any] = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"Manifest is not valid JSON: {resolved_manifest} ({exc})",
            remediation="Re-run `dbt parse` — the manifest file is corrupt or truncated.",
        ) from exc

    if not isinstance(raw, dict):
        raise ManifestError(
            f"Manifest root is not a JSON object: {resolved_manifest}",
            remediation=(
                "Re-run `dbt parse` — manifest.json must be a JSON object at the top level."
            ),
        )

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

    manifest = Manifest.model_validate(
        {
            "metadata": metadata,
            "nodes": filtered_nodes,
            "disabled": filtered_disabled,
        }
    )

    # Stash the resolved project_dir so file-path get_model() calls can
    # canonicalise inputs. Frozen-model escape hatch (Pydantic v2 idiom).
    object.__setattr__(manifest, _PROJECT_DIR_ATTR, project_resolved)

    return manifest


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
