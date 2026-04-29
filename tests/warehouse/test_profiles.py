"""Tests for the dbt profiles loader (US-005, DEC-009/017/022/023).

Every test is capable of failing on a real regression — no `assert True`-shaped
placeholders (``testing-signal.md``). The drift-detector test
(:func:`test_drift_detector_extra_forbid`) operationalises DEC-017's
forward-compat strategy: the production model is ``extra="forbid"``, and a
test-only ``StrictModel`` mirrors *every* documented field of dbt-bigquery
1.9 against the drift fixture so a future field bump fails loudly.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from signalforge.warehouse import profiles as profiles_module
from signalforge.warehouse.errors import (
    ProfileNotFoundError,
    ProfileTargetNotFoundError,
    UnsupportedAuthMethodError,
)
from signalforge.warehouse.profiles import load_profile

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "profiles"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_dbt_project(project_dir: Path, profile_name: str = "signalforge_test") -> None:
    """Write a minimal dbt_project.yml so load_profile can resolve the profile name."""
    (project_dir / "dbt_project.yml").write_text(
        f"name: signalforge_test\nversion: '1.0.0'\nconfig-version: 2\nprofile: {profile_name}\n",
        encoding="utf-8",
    )


def _clear_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ``DBT_PROFILES_DIR`` so tests that should not resolve via env are honest."""
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)


# ---------------------------------------------------------------------------
# 1. Resolution-path tests
# ---------------------------------------------------------------------------


def test_load_profile_resolves_dbt_profiles_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DBT_PROFILES_DIR` env var is the highest-priority resolution path."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)

    env_dir = tmp_path / "env_profiles"
    env_dir.mkdir()
    shutil.copy(FIXTURES / "bigquery_oauth.yml", env_dir / "profiles.yml")

    monkeypatch.setenv("DBT_PROFILES_DIR", str(env_dir))
    # Isolate $HOME so any developer ~/.dbt/profiles.yml does not leak in.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    target = load_profile(project_dir)

    assert target.project == "my-gcp-project"
    assert target.dataset == "analytics"
    assert target.method == "oauth"


def test_load_profile_resolves_project_root_profiles_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no env var is set, `<project_dir>/profiles.yml` is used."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    shutil.copy(FIXTURES / "bigquery_oauth.yml", project_dir / "profiles.yml")

    target = load_profile(project_dir)

    assert target.project == "my-gcp-project"
    assert target.location == "US"


def test_load_profile_resolves_home_dot_dbt_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/.dbt/profiles.yml` is the last-resort fallback."""
    _clear_profile_env(monkeypatch)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)

    fake_home = tmp_path / "fake_home"
    (fake_home / ".dbt").mkdir(parents=True)
    shutil.copy(FIXTURES / "bigquery_oauth.yml", fake_home / ".dbt" / "profiles.yml")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    target = load_profile(project_dir)

    assert target.project == "my-gcp-project"


# ---------------------------------------------------------------------------
# 2. Target selection tests
# ---------------------------------------------------------------------------


def test_load_profile_target_arg_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing `target="prod"` overrides the profile's default `target: dev` field."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    shutil.copy(FIXTURES / "multi_target.yml", project_dir / "profiles.yml")

    target = load_profile(project_dir, target="prod")

    assert target.project == "my-gcp-project-prod"
    assert target.dataset == "analytics_prod"


def test_load_profile_missing_target_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profile's `target:` field names a missing output → ProfileTargetNotFoundError."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    shutil.copy(FIXTURES / "missing_target.yml", project_dir / "profiles.yml")

    with pytest.raises(ProfileTargetNotFoundError) as excinfo:
        load_profile(project_dir)
    assert excinfo.value.target == "dev"
    assert excinfo.value.profile_name == "signalforge_test"
    # Remediation must list the actually-available targets so users can
    # fix the profile without opening the YAML (Copilot review feedback).
    assert excinfo.value.available, "available targets list should be non-empty"
    for available_target in excinfo.value.available:
        assert available_target in str(excinfo.value)
    # And the searched_paths must point at the real profiles.yml file,
    # not at the profile *name* placeholder.
    assert excinfo.value.profiles_path is not None
    assert excinfo.value.profiles_path.name == "profiles.yml"
    assert excinfo.value.searched_paths == [excinfo.value.profiles_path]


# ---------------------------------------------------------------------------
# 3. Not-found / unsupported error paths
# ---------------------------------------------------------------------------


def test_load_profile_no_profiles_yml_anywhere_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var, no project-root file, no ~/.dbt → ProfileNotFoundError listing all three."""
    _clear_profile_env(monkeypatch)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()  # exists but no .dbt subdir
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    with pytest.raises(ProfileNotFoundError) as excinfo:
        load_profile(project_dir)

    assert excinfo.value.searched_paths, (
        "ProfileNotFoundError must surface the searched paths in its remediation"
    )
    # The remediation is what users actually read on the CLI.
    assert "profiles.yml" in str(excinfo.value)


def test_load_profile_unsupported_method_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`method: service-account` triggers UnsupportedAuthMethodError via the field validator."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    shutil.copy(FIXTURES / "bigquery_service_account.yml", project_dir / "profiles.yml")

    # Pydantic wraps validator errors in ValidationError; the typed error is
    # available as the underlying cause. Both are valid for callers to catch
    # — the test asserts the message-side signal.
    with pytest.raises((UnsupportedAuthMethodError, ValidationError)) as excinfo:
        load_profile(project_dir)
    msg = str(excinfo.value)
    assert "service-account" in msg


def test_load_profile_unknown_field_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`extra="forbid"` rejects unknown keys (DEC-017)."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    (project_dir / "profiles.yml").write_text(
        "signalforge_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        "      project: p\n"
        "      dataset: d\n"
        "      bogus_field: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        load_profile(project_dir)
    assert "bogus_field" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. Field-level behaviour
# ---------------------------------------------------------------------------


def test_load_profile_dataset_alias_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`schema:` in YAML hydrates the `dataset` field (populate_by_name=True)."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    (project_dir / "profiles.yml").write_text(
        "signalforge_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        "      project: p\n"
        "      schema: analytics\n",
        encoding="utf-8",
    )

    target = load_profile(project_dir)

    assert target.dataset == "analytics"


# ---------------------------------------------------------------------------
# 5. Symlink hardening (DEC-017)
# ---------------------------------------------------------------------------


def test_load_profile_symlink_to_outside_project_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `profiles.yml` symlink pointing outside `project_dir` is rejected."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)

    outside = tmp_path / "outside_profiles.yml"
    shutil.copy(FIXTURES / "bigquery_oauth.yml", outside)

    try:
        os.symlink(outside, project_dir / "profiles.yml")
    except OSError:
        pytest.skip("symlinks unsupported")

    with pytest.raises(ProfileNotFoundError):
        load_profile(project_dir)


# ---------------------------------------------------------------------------
# 6. Soft warning at 1 MB (DEC-023)
# ---------------------------------------------------------------------------


def test_load_profile_warns_on_large_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A profile larger than `_PROFILES_YAML_WARN_AT` logs a WARNING (DEC-023)."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")
    # Patch the threshold down so the small fixture trips it.
    monkeypatch.setattr("signalforge.warehouse.profiles._PROFILES_YAML_WARN_AT", 100)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    shutil.copy(FIXTURES / "bigquery_oauth.yml", project_dir / "profiles.yml")

    with caplog.at_level(logging.WARNING, logger="signalforge.warehouse"):
        load_profile(project_dir)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(r.name == "signalforge.warehouse" for r in warnings), (
        f"expected at least one WARNING from signalforge.warehouse logger; "
        f"got {[(r.name, r.levelno, r.getMessage()) for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# 7. Drift detector (DEC-017)
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    """Test-only mirror of dbt-bigquery 1.9's full ``oauth`` field set.

    ``extra="forbid"`` so adding a new top-level field to the drift fixture
    without updating BOTH this model and (if needed) the production
    :class:`DbtProfileTarget` trips the test loudly. This is the
    forward-compat compensation for DEC-017's ``extra="forbid"`` stance on
    the production model.

    The field list mirrors ``tests/fixtures/profiles/dbt_bigquery_drift_v1_9.yml``.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    method: str | None = None
    project: str | None = None
    dataset: str | None = None
    threads: int | None = None
    location: str | None = None
    priority: str | None = None
    maximum_bytes_billed: int | None = None
    timeout_seconds: int | None = None
    retries: int | None = None
    keyfile: str | None = None
    impersonate_service_account: str | None = None
    gcs_bucket: str | None = None
    dataproc_region: str | None = None
    compute_region: str | None = None
    oauth_redirect_uri: str | None = None


def test_drift_detector_extra_forbid() -> None:
    """The drift fixture validates against StrictModel — bumping fixture fields
    without updating StrictModel/DbtProfileTarget will fail this test loudly.
    """
    with (FIXTURES / "dbt_bigquery_drift_v1_9.yml").open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    target_dict = raw["signalforge_test"]["outputs"]["dev"]

    model = StrictModel.model_validate(target_dict)

    # Sanity-check a couple of fields so this test catches at least one
    # corruption mode (not just structural validation).
    assert model.type == "bigquery"
    assert model.method == "oauth"
    assert model.maximum_bytes_billed == 1000000000


def test_module_uses_warehouse_logger() -> None:
    """DEC-027: every module in ``signalforge.warehouse.*`` uses the
    ``signalforge.warehouse`` logger, not a dunder-name logger.
    """
    assert profiles_module._LOGGER.name == "signalforge.warehouse"
