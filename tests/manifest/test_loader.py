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
import os
import shutil
import sys
from pathlib import Path

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
from signalforge.manifest.loader import (
    MAX_MANIFEST_BYTES,
    _canonicalise_path,
    _detect_version,
    load,
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
