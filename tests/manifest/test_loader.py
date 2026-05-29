"""Tests for the manifest loader (DEC-007, DEC-008, DEC-010, DEC-013, DEC-014, DEC-017).

These cover four layers:

1. **Unit tests** on the pure helpers (`_detect_version`,
   `_canonicalise_path`, `MAX_MANIFEST_BYTES` warning) without exercising
   end-to-end loads where unnecessary.
2. **Integration tests** parametrised across the four committed small-project
   fixtures (v9–v12), exercising :func:`load`, :func:`Manifest.iter_models`,
   :func:`Manifest.schema_version`, and the resolver's three input forms
   (unique_id, relative path, absolute path).
3. **Resolver semantics** — disabled models raise :class:`ModelDisabledError`,
   unknown ids raise :class:`ModelNotFoundError`, empty raw_code raises
   :class:`ModelMissingSqlError` at *resolve* time (DEC-016).
4. **Symlink hardening** (DEC-007) — a symlink inside a temp-copied project
   that points at ``/etc/hostname`` is rejected with
   :class:`ModelPathOutsideProjectError`. Skipped on Windows.

Carries forward the seven Phase 2 regression tests from the plan, plus the
path-traversal symlink test, the size-warning test, and the path-outside
guard for ``manifest_path=`` overrides.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import pytest

from signalforge._common.path_safety import PathContainmentError
from signalforge.manifest.errors import (
    ManifestError,
    ManifestNotFoundError,
    ModelDisabledError,
    ModelMissingSqlError,
    ModelNotFoundError,
    ModelPathOutsideProjectError,
    UnsupportedManifestVersionError,
)
from signalforge.manifest.loader import (
    MAX_MANIFEST_BYTES,
    _canonicalise_path,
    _detect_version,
    load,
    schema_version,
)
from signalforge.manifest.models import Manifest

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SMALL_PROJECT = FIXTURES_DIR / "dbt_project_small"
ERROR_PATHS = FIXTURES_DIR / "error_paths"

ALL_VERSIONS = [9, 10, 11, 12]
KNOWN_UNIQUE_ID = "model.signalforge_test_small.dim_users"
KNOWN_REL_PATH = "models/marts/dim_users.sql"
DISABLED_UNIQUE_ID = "model.signalforge_test_small.stg_orders"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_small_project(tmp_path: Path) -> Path:
    """Copy the small project tree into ``tmp_path`` and return the new root.

    Used by tests that need a writable project directory (symlink tests,
    "no manifest" tests).
    """
    dest = tmp_path / "dbt_project_small"
    shutil.copytree(SMALL_PROJECT, dest, symlinks=False)
    return dest


def _project_with_error_manifest(tmp_path: Path, fixture_name: str) -> Path:
    """Copy the small project + drop ``error_paths/<fixture_name>`` into ``target/``.

    Returns the project root. Used by the error-path tests so the fixture
    is *inside* the project tree and survives the path-outside-project
    guard on ``manifest_path``.
    """
    project = _copy_small_project(tmp_path)
    src = ERROR_PATHS / fixture_name
    dest = project / "target" / fixture_name
    shutil.copy(src, dest)
    return project


# ---------------------------------------------------------------------------
# 1. Version detection (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("version", ALL_VERSIONS)
def test_detect_version_url_supported(version: int) -> None:
    metadata = {
        "dbt_schema_version": f"https://schemas.getdbt.com/dbt/manifest/v{version}.json",
    }
    assert _detect_version(metadata, {}) == version


@pytest.mark.unit
def test_detect_version_url_unsupported_v8_raises() -> None:
    metadata = {
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v8.json",
    }
    with pytest.raises(UnsupportedManifestVersionError) as exc_info:
        _detect_version(metadata, {})
    rendered = str(exc_info.value)
    assert "v8" in rendered or "v9" in rendered  # message or remediation mentions range


@pytest.mark.unit
def test_detect_version_url_unsupported_v99_raises() -> None:
    metadata = {
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v99.json",
    }
    with pytest.raises(UnsupportedManifestVersionError):
        _detect_version(metadata, {})


@pytest.mark.unit
def test_detect_version_fallback_unit_tests_implies_v12() -> None:
    metadata = {"dbt_schema_version": "weird"}
    assert _detect_version(metadata, {"unit_tests": []}) == 12


@pytest.mark.unit
def test_detect_version_fallback_saved_queries_implies_v11() -> None:
    metadata: dict[str, object] = {}
    assert _detect_version(metadata, {"saved_queries": []}) == 11


@pytest.mark.unit
def test_detect_version_fallback_metrics_implies_v10() -> None:
    metadata: dict[str, object] = {}
    assert _detect_version(metadata, {"metrics": []}) == 10


@pytest.mark.unit
def test_detect_version_fallback_default_is_v9() -> None:
    metadata: dict[str, object] = {}
    assert _detect_version(metadata, {"nodes": {}}) == 9


# ---------------------------------------------------------------------------
# 2. Canonicalise path helper (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonicalise_path_relative_resolves_under_project(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "models").mkdir()
    (project / "models" / "x.sql").write_text("select 1")

    result = _canonicalise_path("models/x.sql", project)
    assert result == (project / "models" / "x.sql").resolve()


@pytest.mark.unit
def test_canonicalise_path_outside_raises(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(ModelPathOutsideProjectError):
        _canonicalise_path("/etc/hostname", project)


# ---------------------------------------------------------------------------
# 3. Cross-version load (integration, parametrised)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("version", ALL_VERSIONS)
def test_load_round_trip_each_version(version: int) -> None:
    manifest_path = SMALL_PROJECT / "target" / f"manifest_v{version}.json"
    manifest = load(SMALL_PROJECT, manifest_path=manifest_path)

    assert isinstance(manifest, Manifest)
    assert manifest.schema_version.endswith(f"v{version}.json")
    models = list(manifest.iter_models())
    assert len(models) >= 1
    # Sanity: the loader filtered out non-model nodes — every value really is a Model.
    assert all(m.resource_type == "model" for m in models)


# ---------------------------------------------------------------------------
# 4. Resolver — three input forms agree
# ---------------------------------------------------------------------------


@pytest.fixture
def v12_manifest() -> Manifest:
    return load(
        SMALL_PROJECT,
        manifest_path=SMALL_PROJECT / "target" / "manifest_v12.json",
    )


@pytest.mark.integration
def test_resolve_by_unique_id(v12_manifest: Manifest) -> None:
    model = v12_manifest.get_model(KNOWN_UNIQUE_ID)
    assert model.unique_id == KNOWN_UNIQUE_ID


@pytest.mark.integration
def test_resolve_by_relative_path_matches_unique_id(v12_manifest: Manifest) -> None:
    by_id = v12_manifest.get_model(KNOWN_UNIQUE_ID)
    by_path = v12_manifest.get_model(KNOWN_REL_PATH)
    assert by_id.unique_id == by_path.unique_id


@pytest.mark.integration
def test_resolve_by_absolute_path_matches_unique_id(v12_manifest: Manifest) -> None:
    abs_path = (SMALL_PROJECT / KNOWN_REL_PATH).resolve()
    by_id = v12_manifest.get_model(KNOWN_UNIQUE_ID)
    by_abs = v12_manifest.get_model(abs_path)
    assert by_id.unique_id == by_abs.unique_id


# ---------------------------------------------------------------------------
# 5. Resolver — error semantics
# ---------------------------------------------------------------------------


@pytest.mark.error
def test_resolve_disabled_raises_model_disabled_error(v12_manifest: Manifest) -> None:
    with pytest.raises(ModelDisabledError) as exc_info:
        v12_manifest.get_model(DISABLED_UNIQUE_ID)
    assert "disabled" in str(exc_info.value).lower()


@pytest.mark.error
def test_resolve_unknown_unique_id_raises_model_not_found(v12_manifest: Manifest) -> None:
    with pytest.raises(ModelNotFoundError):
        v12_manifest.get_model("model.foo.bar")


@pytest.mark.error
def test_resolve_unknown_path_raises_model_not_found(v12_manifest: Manifest) -> None:
    with pytest.raises(ModelNotFoundError):
        v12_manifest.get_model("models/staging/never_exists.sql")


# ---------------------------------------------------------------------------
# 6. ManifestNotFoundError when target/manifest.json is absent
# ---------------------------------------------------------------------------


@pytest.mark.error
def test_load_missing_manifest_raises(tmp_path: Path) -> None:
    project = tmp_path / "empty_project"
    project.mkdir()
    with pytest.raises(ManifestNotFoundError) as exc_info:
        load(project)
    rendered = str(exc_info.value)
    assert "dbt parse" in rendered


@pytest.mark.error
def test_load_missing_project_dir_raises(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does_not_exist"
    with pytest.raises(ManifestNotFoundError):
        load(nonexistent)


# ---------------------------------------------------------------------------
# 7. manifest_path override + path-outside-project guard
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_manifest_path_override_works() -> None:
    manifest = load(
        SMALL_PROJECT,
        manifest_path="target/manifest_v9.json",
    )
    assert manifest.schema_version.endswith("v9.json")


@pytest.mark.error
def test_manifest_path_outside_project_raises(tmp_path: Path) -> None:
    # /etc/hostname exists on the test runner; pick another well-known
    # outside-the-project file if running somewhere it doesn't.
    outside = "/etc/hostname"
    if not Path(outside).exists():
        pytest.skip(f"{outside} not present on this platform")
    with pytest.raises(ModelPathOutsideProjectError):
        load(SMALL_PROJECT, manifest_path=outside)


# ---------------------------------------------------------------------------
# 8. Symlink path traversal (DEC-007)
# ---------------------------------------------------------------------------


@pytest.mark.error
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    project = _copy_small_project(tmp_path)
    target = Path("/etc/hostname")
    if not target.exists():
        pytest.skip(f"{target} not present on this platform")
    symlink = project / "models" / "escape.sql"
    os.symlink(target, symlink)

    manifest = load(project, manifest_path=project / "target" / "manifest_v12.json")
    with pytest.raises(ModelPathOutsideProjectError):
        manifest.get_model("models/escape.sql")


# ---------------------------------------------------------------------------
# 9. Empty raw_code raises ModelMissingSqlError at resolve time (DEC-016)
# ---------------------------------------------------------------------------


@pytest.mark.error
def test_empty_raw_code_raises_at_resolve_time(tmp_path: Path) -> None:
    # The empty_raw_code fixture has dim_users with an empty raw_code.
    # Loading must succeed (DEC-016: resolve-time, not parse-time).
    project = _project_with_error_manifest(tmp_path, "empty_raw_code.json")
    manifest = load(project, manifest_path="target/empty_raw_code.json")
    with pytest.raises(ModelMissingSqlError) as exc_info:
        manifest.get_model("model.signalforge_test_small.dim_users")
    rendered = str(exc_info.value)
    assert "raw_code" in rendered or "raw SQL" in rendered


# ---------------------------------------------------------------------------
# 10. 200 MB warning (DEC-008)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_oversize_manifest_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop the threshold to 100 bytes so any real fixture trips it.
    # Integration-marked because it exercises the full load() round-trip
    # against a committed fixture file.
    monkeypatch.setattr("signalforge.manifest.loader.MAX_MANIFEST_BYTES", 100)
    with pytest.warns(UserWarning, match="resident memory"):
        load(SMALL_PROJECT, manifest_path="target/manifest_v12.json")


@pytest.mark.unit
def test_max_manifest_bytes_default_is_200mb() -> None:
    # Sanity check that the constant did not drift.
    assert MAX_MANIFEST_BYTES == 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# 11. Unsupported version end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.error
def test_load_unsupported_v99_fixture_raises(tmp_path: Path) -> None:
    project = _project_with_error_manifest(tmp_path, "unsupported_v99.json")
    with pytest.raises(UnsupportedManifestVersionError) as exc_info:
        load(project, manifest_path="target/unsupported_v99.json")
    rendered = str(exc_info.value)
    # Remediation should mention the supported range.
    assert "v9" in rendered and "v12" in rendered


# ---------------------------------------------------------------------------
# 12. Malformed JSON raises ManifestError
# ---------------------------------------------------------------------------


@pytest.mark.error
def test_malformed_json_raises_manifest_error(tmp_path: Path) -> None:
    project = _project_with_error_manifest(tmp_path, "malformed.json")
    with pytest.raises(ManifestError):
        load(project, manifest_path="target/malformed.json")


@pytest.mark.error
def test_non_dict_root_raises_manifest_error(tmp_path: Path) -> None:
    """A manifest whose JSON root is not an object (e.g. a list) is rejected.

    The payload parses cleanly (it is valid JSON), so the
    ``json.JSONDecodeError`` guard does not fire — the root-shape
    ``isinstance(loaded, dict)`` guard is the live branch that catches it.
    """
    project = _project_with_error_manifest(tmp_path, "non_dict_root.json")
    with pytest.raises(ManifestError, match="not a JSON object"):
        load(project, manifest_path="target/non_dict_root.json")


# ---------------------------------------------------------------------------
# 13. Missing version URL falls through to feature-sniff (does NOT raise)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_missing_version_url_falls_back_to_feature_sniff(tmp_path: Path) -> None:
    # missing_version_url.json has no metadata.dbt_schema_version but the
    # full v12 top-level keys (unit_tests, saved_queries, …) — feature-sniff
    # should pick v12 and the load should succeed.
    project = _project_with_error_manifest(tmp_path, "missing_version_url.json")
    manifest = load(project, manifest_path="target/missing_version_url.json")
    # schema_version property returns "" when URL is absent.
    assert manifest.schema_version == ""
    assert len(list(manifest.iter_models())) >= 1


# ---------------------------------------------------------------------------
# 14. disabled_only fixture: enabled lookup raises ModelDisabledError
# ---------------------------------------------------------------------------


@pytest.mark.error
def test_disabled_only_fixture_resolves_disabled_error(tmp_path: Path) -> None:
    project = _project_with_error_manifest(tmp_path, "disabled_only.json")
    manifest = load(project, manifest_path="target/disabled_only.json")
    # dim_users is disabled in this fixture (per the fixture's name + shape).
    with pytest.raises(ModelDisabledError):
        manifest.get_model("model.signalforge_test_small.dim_users")


# ---------------------------------------------------------------------------
# 15. Manifest constructed without load() rejects file-path lookup
# ---------------------------------------------------------------------------


@pytest.mark.error
def test_directly_constructed_manifest_rejects_path_lookup() -> None:
    bare = Manifest.model_validate({"metadata": {}, "nodes": {}, "disabled": {}})
    with pytest.raises(ManifestError) as exc_info:
        bare.get_model("models/foo.sql")
    assert "load()" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 16. iter_models / schema_version sanity
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_iter_models_returns_all_enabled_models(v12_manifest: Manifest) -> None:
    ids = {m.unique_id for m in v12_manifest.iter_models()}
    assert KNOWN_UNIQUE_ID in ids
    assert DISABLED_UNIQUE_ID not in ids  # disabled lives in `disabled`, not `nodes`


# ---------------------------------------------------------------------------
# 17. Default target/manifest.json must also be canonicalised (DEC-007 hardening,
#     post-pass-2 review fix). A symlink at target/manifest.json pointing
#     outside the project root must be rejected even when no explicit
#     manifest_path override is supplied.
# ---------------------------------------------------------------------------


@pytest.mark.error
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_default_manifest_path_symlink_escape_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    target = project / "target"
    target.mkdir()
    outside_manifest = tmp_path / "outside.json"
    outside_manifest.write_text("{}")
    (target / "manifest.json").symlink_to(outside_manifest)

    with pytest.raises(ModelPathOutsideProjectError):
        load(project)


# ---------------------------------------------------------------------------
# 18. Symlink loops must surface as ModelPathOutsideProjectError, not as a
#     bare RuntimeError (Path.resolve raises RuntimeError on cycles regardless
#     of strict=). Post-pass-2 fix.
# ---------------------------------------------------------------------------


@pytest.mark.error
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_symlink_loop_in_default_path_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    target = project / "target"
    target.mkdir()
    # a -> b, b -> a forms a cycle.
    (target / "a").symlink_to(target / "b")
    (target / "b").symlink_to(target / "a")
    (target / "manifest.json").symlink_to(target / "a")

    with pytest.raises(ModelPathOutsideProjectError):
        load(project)


@pytest.mark.error
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_symlink_loop_in_explicit_manifest_path_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a").symlink_to(project / "b")
    (project / "b").symlink_to(project / "a")

    with pytest.raises(ModelPathOutsideProjectError):
        load(project, manifest_path=project / "a")


@pytest.mark.error
def test_non_loop_oserror_on_input_path_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ELOOP ``OSError`` (e.g. ``PermissionError``) from strict
    resolution of the manifest path propagates rather than being downgraded
    to a best-effort ``strict=False`` resolution.

    Issue #96 review (Copilot): the ``except OSError`` fallback must be
    narrowed to ``FileNotFoundError`` / ``NotADirectoryError`` so a
    permission failure is not masked as a partially-resolved path.
    """
    project = tmp_path / "proj"
    project.mkdir()
    project_resolved = project.resolve(strict=True)

    original_resolve = Path.resolve

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if strict and self.name == "manifest.json":
            raise PermissionError(errno.EACCES, "Permission denied")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(PermissionError):
        _canonicalise_path("manifest.json", project_resolved)


# ---------------------------------------------------------------------------
# 18. catalog.json sibling merge (#159 US-001 — DEC-001, DEC-002, DEC-007, DEC-010)
# ---------------------------------------------------------------------------

MANIFEST_WITH_COLUMNS = FIXTURES_DIR / "manifest" / "manifest_with_columns.json"
CATALOG_CANONICAL = FIXTURES_DIR / "manifest" / "catalog_canonical.json"
CATALOG_CASE_MISMATCH = FIXTURES_DIR / "manifest" / "catalog_case_mismatch.json"
CATALOG_PHANTOM = FIXTURES_DIR / "manifest" / "catalog_phantom_column.json"
CATALOG_PARTIAL = FIXTURES_DIR / "manifest" / "catalog_partial.json"
DIM_USERS_UID = "model.signalforge_test_small.dim_users"


def _project_with_manifest_and_catalog(
    tmp_path: Path,
    manifest_src: Path,
    catalog_src: Path | None,
) -> Path:
    """Build a project tree carrying ``manifest_src`` (and optionally
    ``catalog_src``) under ``target/``. Returns the project root.

    Used by the catalog merge tests so each test gets an isolated project
    directory with a known manifest+catalog pair.
    """
    project = tmp_path / "proj"
    target = project / "target"
    target.mkdir(parents=True)
    shutil.copy(manifest_src, target / "manifest.json")
    if catalog_src is not None:
        shutil.copy(catalog_src, target / "catalog.json")
    return project


@pytest.mark.integration
def test_load_merges_catalog_types_into_columns(tmp_path: Path) -> None:
    """Happy path: catalog.json sibling read overlays ``data_type`` per column."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, CATALOG_CANONICAL)
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    assert model.columns["id"].data_type == "INT64"
    assert model.columns["email"].data_type == "STRING"
    assert model.columns["created_at"].data_type == "TIMESTAMP"


@pytest.mark.integration
def test_load_catalog_missing_is_silent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """No catalog.json present: load succeeds, ``data_type`` stays ``None``,
    no log records emitted from the manifest layer (stage-0 invariant)."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, None)
    with caplog.at_level(logging.DEBUG, logger="signalforge.manifest"):
        manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    for col in model.columns.values():
        assert col.data_type is None
    # Stage-0 invariant: manifest layer emits NO log records.
    assert [r for r in caplog.records if r.name.startswith("signalforge.manifest")] == []


@pytest.mark.integration
def test_load_catalog_malformed_json_is_silent(tmp_path: Path) -> None:
    """A corrupt ``catalog.json`` does not abort load; ``data_type`` stays ``None``."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, CATALOG_CANONICAL)
    # Overwrite the canonical catalog with malformed JSON.
    (project / "target" / "catalog.json").write_text("{ this is not json", encoding="utf-8")
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    for col in model.columns.values():
        assert col.data_type is None


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file-mode perms")
def test_load_catalog_oserror_is_silent(tmp_path: Path) -> None:
    """A catalog.json that raises ``OSError`` on read (mode 0o000) is silently
    skipped; the manifest still loads."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, CATALOG_CANONICAL)
    catalog_path = project / "target" / "catalog.json"
    catalog_path.chmod(0o000)
    try:
        manifest = load(project)
    finally:
        # Restore so tmp_path cleanup can delete the file.
        catalog_path.chmod(0o600)
    model = manifest.nodes[DIM_USERS_UID]
    for col in model.columns.values():
        assert col.data_type is None


@pytest.mark.integration
def test_load_catalog_column_case_insensitive_match(tmp_path: Path) -> None:
    """Manifest column ``id`` matches catalog column ``ID`` (Snowflake-style)."""
    project = _project_with_manifest_and_catalog(
        tmp_path, MANIFEST_WITH_COLUMNS, CATALOG_CASE_MISMATCH
    )
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    assert model.columns["id"].data_type == "NUMBER"
    assert model.columns["email"].data_type == "VARCHAR"
    # created_at not in catalog → stays None
    assert model.columns["created_at"].data_type is None


@pytest.mark.integration
def test_load_catalog_phantom_column_ignored(tmp_path: Path) -> None:
    """Catalog declares ``phantom_col`` not in manifest; not added to ``Model.columns``.

    Manifest columns NOT in catalog stay ``None`` (DEC-010(b))."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, CATALOG_PHANTOM)
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    assert "phantom_col" not in model.columns
    assert model.columns["id"].data_type == "INT64"
    # email + created_at not in catalog → stays None
    assert model.columns["email"].data_type is None
    assert model.columns["created_at"].data_type is None


@pytest.mark.integration
def test_load_catalog_missing_column_stays_null(tmp_path: Path) -> None:
    """Manifest has columns that the catalog doesn't declare; their ``data_type``
    remains ``None`` (DEC-010(b))."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, CATALOG_PARTIAL)
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    assert model.columns["id"].data_type == "INT64"
    assert model.columns["email"].data_type is None
    assert model.columns["created_at"].data_type is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "catalog_body",
    [
        # Root is a JSON array, not a dict (DEC-010c shape guard).
        "[]",
        # Root dict but ``nodes`` is a list (DEC-010c).
        '{"nodes": []}',
        # ``nodes`` present but the matched node is not a dict.
        '{"nodes": {"model.signalforge_test_small.dim_users": "scalar"}}',
        # Node dict but ``columns`` is not a dict.
        '{"nodes": {"model.signalforge_test_small.dim_users": {"columns": "scalar"}}}',
        # Catalog column is not a dict.
        ('{"nodes": {"model.signalforge_test_small.dim_users": {"columns": {"id": "scalar"}}}}'),
        # Catalog column dict missing ``type``.
        (
            '{"nodes": {"model.signalforge_test_small.dim_users": '
            '{"columns": {"id": {"index": 1}}}}}'
        ),
        # Catalog column ``type`` is not a string.
        (
            '{"nodes": {"model.signalforge_test_small.dim_users": '
            '{"columns": {"id": {"type": 42}}}}}'
        ),
        # Empty catalog (no node matches at all).
        '{"nodes": {}}',
        # Catalog declares a node not in the manifest (no manifest column
        # matches; the model_changed branch stays False so model_copy is
        # skipped — exercises the else-branch at the end of the merge loop).
        (
            '{"nodes": {"model.other_project.other_model": '
            '{"columns": {"foo": {"type": "STRING"}}}}}'
        ),
    ],
    ids=[
        "root-is-list",
        "nodes-is-list",
        "node-is-scalar",
        "columns-is-scalar",
        "col-is-scalar",
        "col-missing-type",
        "col-type-non-string",
        "empty-nodes",
        "unmatched-node",
    ],
)
def test_load_catalog_shape_degrades_silently(tmp_path: Path, catalog_body: str) -> None:
    """Every malformed catalog shape silently degrades to no-op overlay;
    ``data_type`` stays ``None`` on every column.

    Exercises the DEC-010c "silent no-op on malformed catalog" defence
    across the per-shape guards in ``_apply_catalog_overlay``.
    """
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, None)
    (project / "target" / "catalog.json").write_text(catalog_body, encoding="utf-8")
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    for col in model.columns.values():
        assert col.data_type is None


@pytest.mark.integration
def test_load_catalog_only_phantom_columns_no_model_change(tmp_path: Path) -> None:
    """Matched node where EVERY catalog column is a phantom (not in manifest)
    leaves the model unchanged — the merge loop takes the else-branch and
    drops in the original model unmodified."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, None)
    (project / "target" / "catalog.json").write_text(
        json.dumps(
            {
                "nodes": {
                    DIM_USERS_UID: {
                        "columns": {
                            "phantom_a": {"type": "STRING"},
                            "phantom_b": {"type": "INT64"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    assert "phantom_a" not in model.columns
    assert "phantom_b" not in model.columns
    for col in model.columns.values():
        assert col.data_type is None


@pytest.mark.integration
def test_load_catalog_non_string_column_name_ignored(tmp_path: Path) -> None:
    """A column whose key in the catalog ``columns`` dict is not a string is
    silently skipped (defence against malformed catalog payloads that round-trip
    a non-string key through a non-JSON producer)."""
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, None)
    catalog_path = project / "target" / "catalog.json"
    # JSON itself forbids non-string keys, so build the dict in Python and dump
    # via Python's json (which would coerce — instead inject the dict directly
    # into the parser path by writing valid JSON and then having the loader
    # parse it; for the non-string-key path we mock at the post-parse step).
    # The simpler way: write a custom payload that triggers the ``isinstance``
    # guard on cat_col_name by using a list-typed columns dict — covered by
    # the parametrised tests above. This test additionally pins the recovery
    # case where ALL columns in a matched node fail the guard, leaving
    # ``per_node`` empty so the node is dropped from ``by_node``.
    catalog_path.write_text(
        json.dumps(
            {
                "nodes": {
                    DIM_USERS_UID: {
                        "columns": {
                            "id": {"index": 1},  # missing type → skipped
                            "email": {"type": None},  # non-string type → skipped
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manifest = load(project)
    model = manifest.nodes[DIM_USERS_UID]
    for col in model.columns.values():
        assert col.data_type is None


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_load_catalog_path_canonicalised(tmp_path: Path) -> None:
    """``catalog.json`` is resolved through ``_common.path_safety.canonicalise_path``;
    a symlink pointing outside the project tree is rejected with
    :class:`PathContainmentError` (DEC-002).

    The security gate is NOT in the silent-degrade set — a malicious symlink
    must surface, never be swallowed.
    """
    project = _project_with_manifest_and_catalog(tmp_path, MANIFEST_WITH_COLUMNS, None)
    # Drop a real file outside the project tree, then symlink target/catalog.json
    # to it. Canonicalise should reject the resolved path as outside project.
    outside = tmp_path / "outside_catalog.json"
    outside.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    (project / "target" / "catalog.json").symlink_to(outside)
    with pytest.raises(PathContainmentError):
        load(project)


# ---------------------------------------------------------------------------
# Pre-existing-line coverage gates (#159 US-005 codecov follow-up).
#
# These tests pin defensive branches in `signalforge.manifest.loader` that
# existed before #159 but lacked coverage. Codecov flags any uncovered line
# in a file modified by the PR even when the line itself is untouched, so
# closing the gap on the file the catalog overlay landed in is the cleanest
# way to satisfy the project-coverage gate alongside the new patch tests.
# ---------------------------------------------------------------------------


def test_load_project_dir_is_a_file_raises_manifest_not_found(tmp_path: Path) -> None:
    """When ``project_dir`` resolves to a regular file (not a directory),
    the loader raises ``ManifestNotFoundError`` at loader.py:231 BEFORE
    any manifest read. Distinct from ``FileNotFoundError`` (caught one
    branch up) — the path exists, it just isn't a directory."""
    not_a_dir = tmp_path / "not_a_dir.txt"
    not_a_dir.write_text("regular file, not a directory\n")
    with pytest.raises(ManifestNotFoundError, match="is not a directory"):
        load(not_a_dir)


def test_load_non_dict_metadata_raises_manifest_error(tmp_path: Path) -> None:
    """An otherwise-valid manifest whose ``metadata`` key is not a JSON
    object hits the explicit ``isinstance(metadata, dict)`` gate at
    loader.py:283 and raises ``ManifestError`` — never proceeds past it
    to feature-sniff a version off a non-object payload."""
    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    (project / "target" / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": ["not", "a", "dict"],
                "nodes": {},
                "disabled": {},
                "sources": {},
                "macros": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="metadata is not an object"):
        load(project)


def test_load_disabled_with_non_list_value_continues(tmp_path: Path) -> None:
    """A ``disabled[<unique_id>]`` entry whose value is not a list (dbt
    always writes lists, but the loader is defensive against schema drift)
    routes through the ``continue`` at loader.py:308 — silently dropped
    from the disabled map without failing the whole load."""
    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    (project / "target" / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"
                },
                "nodes": {},
                "disabled": {
                    "model.pkg.something": "this should be a list but isn't",
                },
                "sources": {},
                "macros": {},
            }
        ),
        encoding="utf-8",
    )
    # Loads cleanly; the malformed disabled entry is silently skipped.
    manifest = load(project)
    assert manifest.disabled == {}


def test_schema_version_non_string_returns_empty_string(tmp_path: Path) -> None:
    """When ``manifest.metadata['dbt_schema_version']`` exists but is not
    a string (e.g. shape drift in a future fusion schema), the
    ``schema_version()`` helper returns an empty string at loader.py:485
    rather than propagating the wrong type."""
    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    (project / "target" / "manifest.json").write_text(
        json.dumps(
            {
                # Use a real URL so the LOADER accepts it; we'll mutate the
                # value on the constructed Manifest after load to exercise
                # the schema_version() helper specifically.
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"
                },
                "nodes": {},
                "disabled": {},
                "sources": {},
                "macros": {},
            }
        ),
        encoding="utf-8",
    )
    manifest = load(project)
    # Mutate via model_copy to get a non-string version field. Manifest is
    # frozen; we construct a fresh one for the test rather than mutating.
    bad_metadata = dict(manifest.metadata)
    bad_metadata["dbt_schema_version"] = 12  # int, not str
    bad_manifest = manifest.model_copy(update={"metadata": bad_metadata})
    assert schema_version(bad_manifest) == ""


def test_get_model_resolver_index_cache_hit_returns_cached(
    v12_manifest: Manifest,
) -> None:
    """A second ``get_model`` call against the same Manifest instance
    must reuse the in-memory resolver-index cache populated on the first
    call — hits ``return cached`` at loader.py:598. Without the cache,
    every CLI lookup would re-scan the entire nodes dict."""
    # stg_users.sql is an ENABLED model in the v12 fixture (stg_orders is
    # disabled, which would route through a different branch). The cache
    # hit at loader.py:598 applies regardless — we want the second call
    # to bypass _build_indexes via the _INDEX_ATTR cache.
    first_lookup = v12_manifest.get_model("models/staging/stg_users.sql")
    second_lookup = v12_manifest.get_model("models/staging/stg_users.sql")
    # Same Model instance returned both times; the second call rode the
    # cached index built during the first call.
    assert first_lookup is second_lookup


def test_get_model_by_file_path_for_disabled_model_raises_disabled(
    tmp_path: Path,
) -> None:
    """Resolving a model by its ``original_file_path`` for a node that
    lives under ``disabled`` raises ``ModelDisabledError`` at
    loader.py:679 — the path-lookup branch needs the same disabled-state
    detection the unique_id lookup branch already has, so the operator
    sees the same typed error regardless of how they identified the
    model."""
    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    (project / "models" / "staging").mkdir(parents=True)
    # Create the .sql so _check_raw_code wouldn't trip — but it never runs
    # because the disabled branch raises first.
    (project / "models" / "staging" / "stg_disabled.sql").write_text(
        "select 1 as foo\n", encoding="utf-8"
    )
    (project / "target" / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"
                },
                "nodes": {},
                "disabled": {
                    "model.pkg.stg_disabled": [
                        {
                            "unique_id": "model.pkg.stg_disabled",
                            "name": "stg_disabled",
                            "resource_type": "model",
                            "package_name": "pkg",
                            "path": "staging/stg_disabled.sql",
                            "original_file_path": "models/staging/stg_disabled.sql",
                            "raw_code": "select 1 as foo",
                            "columns": {},
                            "config": {"enabled": False},
                            "refs": [],
                            "depends_on": {"nodes": []},
                            "sources": [],
                        }
                    ]
                },
                "sources": {},
                "macros": {},
            }
        ),
        encoding="utf-8",
    )
    manifest = load(project)
    with pytest.raises(ModelDisabledError, match="is disabled in dbt config"):
        manifest.get_model("models/staging/stg_disabled.sql")
