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
    IncompleteProfileError,
    InvalidIdentifierError,
    ProfileEnvVarUnsetError,
    ProfileNotFoundError,
    ProfileTargetNotFoundError,
    UnsupportedAuthMethodError,
)
from signalforge.warehouse.profiles import DbtProfileTarget, load_profile

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
# 6b. env_var() macro rendering (issue #47 — supports init-demo profiles.yml)
# ---------------------------------------------------------------------------


def _write_env_var_profile(project_dir: Path, env_var_expr: str) -> None:
    """Helper: write a minimal profile that references `env_var(...)`."""
    (project_dir / "profiles.yml").write_text(
        "signalforge_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        f'      project: "{{{{ {env_var_expr} }}}}"\n'
        "      dataset: austin_bikeshare\n"
        "      location: US\n",
        encoding="utf-8",
    )


def test_load_profile_renders_env_var_macro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env_var('NAME')`` resolves to the environment value at load time.

    Mirrors the dbt convention so the bundled ``init-demo`` profile
    (which uses ``{{ env_var('GOOGLE_CLOUD_PROJECT') }}``) works without
    profile edits when the operator has the env var set.
    """
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")
    monkeypatch.setenv("MY_BILLING_PROJECT", "billing-prod-42")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    _write_env_var_profile(project_dir, "env_var('MY_BILLING_PROJECT')")

    target = load_profile(project_dir)
    assert target.project == "billing-prod-42"


def test_load_profile_env_var_with_default_uses_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env_var('NAME', 'default')`` falls back to the literal default
    when NAME is unset (dbt convention)."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")
    monkeypatch.delenv("UNSET_BILLING_PROJECT", raising=False)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    _write_env_var_profile(project_dir, "env_var('UNSET_BILLING_PROJECT', 'fallback-project')")

    target = load_profile(project_dir)
    assert target.project == "fallback-project"


def test_load_profile_env_var_unset_no_default_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env_var('NAME')`` with no default and NAME unset raises
    :class:`ProfileEnvVarUnsetError` — dbt's documented behaviour.

    This is the load-bearing test for init-demo's UX: the first-run
    operator who forgets to ``export GOOGLE_CLOUD_PROJECT`` before
    ``signalforge lint`` / ``generate`` gets a clear typed error pointing
    at the missing env var, not a downstream BigQuery rejection of the
    literal jinja string.
    """
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")
    monkeypatch.delenv("DEFINITELY_NOT_SET_47", raising=False)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    _write_env_var_profile(project_dir, "env_var('DEFINITELY_NOT_SET_47')")

    with pytest.raises(ProfileEnvVarUnsetError) as excinfo:
        load_profile(project_dir)
    assert excinfo.value.var_name == "DEFINITELY_NOT_SET_47"
    rendered = str(excinfo.value)
    assert "DEFINITELY_NOT_SET_47" in rendered
    assert "↳ Remediation:" in rendered


def test_load_profile_env_var_preserves_yaml_quoting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quoted ``"{{ env_var('NAME') }}"`` substitutes the value while
    preserving the surrounding YAML string context — the rendered
    ``project`` field is a plain string, not a parsed int / bool."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")
    # Use a numeric-looking value to verify YAML doesn't coerce to int.
    monkeypatch.setenv("NUMERIC_PROJECT", "12345")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    _write_env_var_profile(project_dir, "env_var('NUMERIC_PROJECT')")

    target = load_profile(project_dir)
    assert target.project == "12345"
    assert isinstance(target.project, str)


# ---------------------------------------------------------------------------
# 6c. Snowflake profile parsing + cross-field validator (US-003, #120)
# ---------------------------------------------------------------------------


def _snowflake_target(**overrides: object) -> dict[str, object]:
    """A representative valid ``type: snowflake`` target dict."""
    base: dict[str, object] = {
        "type": "snowflake",
        "account": "xy12345.us-east-1",
        "user": "svc_signalforge",
        "role": "TRANSFORMER",
        "warehouse": "ANALYTICS_WH",
        "database": "ANALYTICS",
        "schema": "public",
        "threads": 4,
        "password": "s3cr3t",
    }
    base.update(overrides)
    return base


def test_snowflake_target_parses_full() -> None:
    """A representative Snowflake target parses; every new field populates."""
    target = DbtProfileTarget.model_validate(_snowflake_target())

    assert target.type == "snowflake"
    assert target.account == "xy12345.us-east-1"
    assert target.user == "svc_signalforge"
    assert target.role == "TRANSFORMER"
    assert target.warehouse == "ANALYTICS_WH"
    assert target.database == "ANALYTICS"
    # Snowflake's `schema:` key continues to populate `dataset` (alias).
    assert target.dataset == "public"
    assert target.threads == 4
    assert target.password == "s3cr3t"


@pytest.mark.parametrize(
    ("drop", "expected_missing"),
    [
        (["account"], "account"),
        (["user"], "user"),
        (["warehouse"], "warehouse"),
        (["account", "user", "warehouse"], "warehouse"),
    ],
)
def test_snowflake_missing_required_keys_raises(drop: list[str], expected_missing: str) -> None:
    """Missing account / user / warehouse → IncompleteProfileError naming the key(s)."""
    target = _snowflake_target()
    for key in drop:
        del target[key]

    with pytest.raises((IncompleteProfileError, ValidationError)) as excinfo:
        DbtProfileTarget.model_validate(target)
    assert expected_missing in str(excinfo.value)


def test_snowflake_authenticator_externalbrowser_parses() -> None:
    """`authenticator: externalbrowser` is an accepted SSO method."""
    # password is optional once SSO is in play.
    target_dict = _snowflake_target(authenticator="externalbrowser")
    del target_dict["password"]
    target = DbtProfileTarget.model_validate(target_dict)
    assert target.authenticator == "externalbrowser"


@pytest.mark.parametrize("authenticator", ["oauth", "username_password_mfa"])
def test_snowflake_deferred_authenticator_raises(authenticator: str) -> None:
    """Deferred auth methods → UnsupportedAuthMethodError (deferred-auth remediation)."""
    with pytest.raises((UnsupportedAuthMethodError, ValidationError)) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(authenticator=authenticator))
    assert authenticator in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("warehouse", "wh;DROP"),
        ("database", "db-with-dash"),
        ("schema", "sch;ema"),
    ],
)
def test_snowflake_bad_identifier_raises(field: str, bad_value: str) -> None:
    """Bad warehouse / database / schema identifiers → InvalidIdentifierError."""
    with pytest.raises((InvalidIdentifierError, ValidationError)) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(**{field: bad_value}))
    assert bad_value in str(excinfo.value)


def test_snowflake_bad_account_raises() -> None:
    """A garbage account locator (embedded quote) → InvalidIdentifierError."""
    with pytest.raises((InvalidIdentifierError, ValidationError)) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(account="a'b"))
    assert "a'b" in str(excinfo.value) or "account" in str(excinfo.value)


def test_snowflake_good_account_parses() -> None:
    """A region-suffixed legacy locator parses cleanly."""
    target = DbtProfileTarget.model_validate(_snowflake_target(account="xy12345.us-east-1"))
    assert target.account == "xy12345.us-east-1"


def test_snowflake_foreign_bigquery_field_rejected() -> None:
    """A BigQuery-only field (location) on a snowflake target → ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(location="US"))
    assert "location" in str(excinfo.value)


def test_snowflake_foreign_max_bytes_billed_rejected() -> None:
    """`maximum_bytes_billed` (BigQuery-only) on a snowflake target → ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(maximum_bytes_billed=1_000_000))
    assert "maximum_bytes_billed" in str(excinfo.value)


def test_bigquery_foreign_snowflake_field_rejected() -> None:
    """A Snowflake-only field (account) on a bigquery target → ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        DbtProfileTarget.model_validate(
            {
                "type": "bigquery",
                "method": "oauth",
                "project": "p",
                "schema": "d",
                "account": "xy12345",
            }
        )
    assert "account" in str(excinfo.value)


def test_bigquery_with_threads_parses() -> None:
    """A BigQuery profile carrying `threads: 4` now parses (previously tripped forbid)."""
    target = DbtProfileTarget.model_validate(
        {
            "type": "bigquery",
            "method": "oauth",
            "project": "p",
            "schema": "d",
            "threads": 4,
        }
    )
    assert target.threads == 4
    assert target.type == "bigquery"


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
