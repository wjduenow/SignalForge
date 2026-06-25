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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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


def test_snowflake_secrets_excluded_from_repr() -> None:
    """`repr()` / `str()` must NOT leak credential material — `password`,
    `private_key_passphrase`, `private_key_path` carry `repr=False` so a
    debug print, log line, or exception context can't expose them."""
    target = DbtProfileTarget.model_validate(
        _snowflake_target(
            password="hunter2-secret",
            private_key_path="/keys/rsa_key.p8",
            private_key_passphrase="topsecret-pp",
            authenticator="snowflake",
        )
    )
    rendered = repr(target)

    assert "hunter2-secret" not in rendered
    assert "topsecret-pp" not in rendered
    assert "/keys/rsa_key.p8" not in rendered
    # The values are still accessible via attribute — only the repr is redacted.
    assert target.password == "hunter2-secret"
    assert target.private_key_passphrase == "topsecret-pp"
    # A non-secret identifying field still renders (sanity: repr isn't empty).
    assert "xy12345.us-east-1" in rendered


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

    with pytest.raises(IncompleteProfileError) as excinfo:
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
    with pytest.raises(UnsupportedAuthMethodError) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(authenticator=authenticator))
    assert authenticator in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("warehouse", "wh;DROP"),
        ("database", "db-with-dash"),
        ("schema", "sch;ema"),
        ("role", "r;DROP"),
    ],
)
def test_snowflake_bad_identifier_raises(field: str, bad_value: str) -> None:
    """Bad warehouse / database / schema / role identifiers → InvalidIdentifierError."""
    with pytest.raises(InvalidIdentifierError) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(**{field: bad_value}))
    assert bad_value in str(excinfo.value)


def test_snowflake_bad_account_raises() -> None:
    """A garbage account locator (embedded quote) → InvalidIdentifierError."""
    with pytest.raises(InvalidIdentifierError) as excinfo:
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
# 6d. Databricks profile parsing + cross-field validator (US-002, #222)
# ---------------------------------------------------------------------------


def _databricks_target(**overrides: object) -> dict[str, object]:
    """A representative valid ``type: databricks`` PAT target dict."""
    base: dict[str, object] = {
        "type": "databricks",
        "host": "dbc-ab12cd34.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123def456",
        "token": "dapi-secret-token",
        "catalog": "analytics",
        "schema": "public",
        "threads": 4,
    }
    base.update(overrides)
    return base


def test_databricks_target_parses_full() -> None:
    """A representative Databricks PAT target parses; every new field populates."""
    target = DbtProfileTarget.model_validate(_databricks_target())

    assert target.type == "databricks"
    assert target.host == "dbc-ab12cd34.cloud.databricks.com"
    assert target.http_path == "/sql/1.0/warehouses/abc123def456"
    assert target.token == "dapi-secret-token"
    assert target.catalog == "analytics"
    # Databricks's `schema:` key continues to populate `dataset` (alias).
    assert target.dataset == "public"
    assert target.threads == 4


def test_databricks_oauth_target_parses() -> None:
    """An OAuth-M2M target (auth_type=oauth + client_id + client_secret, NO
    token) parses cleanly."""
    target_dict = _databricks_target(
        auth_type="oauth",
        client_id="oauth-client-id",
        client_secret="oauth-client-secret",
    )
    del target_dict["token"]
    target = DbtProfileTarget.model_validate(target_dict)

    assert target.auth_type == "oauth"
    assert target.client_id == "oauth-client-id"
    assert target.client_secret == "oauth-client-secret"
    assert target.token is None


def test_databricks_secrets_excluded_from_repr() -> None:
    """`repr()` / `str()` must NOT leak `token` (PAT) or `client_secret`
    (OAuth-M2M) — both carry `repr=False`."""
    target = DbtProfileTarget.model_validate(
        _databricks_target(
            token="dapi-supersecret",
            auth_type="oauth",
            client_id="visible-client-id",
            client_secret="oauth-supersecret",
        )
    )
    rendered = repr(target)

    assert "dapi-supersecret" not in rendered
    assert "oauth-supersecret" not in rendered
    # Values are still accessible via attribute — only the repr is redacted.
    assert target.token == "dapi-supersecret"
    assert target.client_secret == "oauth-supersecret"
    # A non-secret identifying field still renders (sanity: repr isn't empty).
    assert "dbc-ab12cd34.cloud.databricks.com" in rendered
    # client_id is NOT a secret and should render.
    assert "visible-client-id" in rendered


def test_databricks_missing_http_path_raises() -> None:
    """Missing `http_path` → IncompleteProfileError naming the key."""
    target = _databricks_target()
    del target["http_path"]

    with pytest.raises(IncompleteProfileError) as excinfo:
        DbtProfileTarget.model_validate(target)
    assert "http_path" in str(excinfo.value)


def test_databricks_pat_missing_token_raises() -> None:
    """A default-PAT target (no auth_type) missing `token` → IncompleteProfileError."""
    target = _databricks_target()
    del target["token"]

    with pytest.raises(IncompleteProfileError) as excinfo:
        DbtProfileTarget.model_validate(target)
    assert "token" in str(excinfo.value)


def test_databricks_oauth_missing_client_creds_raises() -> None:
    """`auth_type: oauth` missing client_id / client_secret →
    IncompleteProfileError listing BOTH (collect-all)."""
    target = _databricks_target(auth_type="oauth")
    del target["token"]  # token is irrelevant under oauth

    with pytest.raises(IncompleteProfileError) as excinfo:
        DbtProfileTarget.model_validate(target)
    msg = str(excinfo.value)
    assert "client_id" in msg
    assert "client_secret" in msg


def test_databricks_unknown_auth_type_raises() -> None:
    """An unsupported `auth_type` (e.g. azure-ad) → UnsupportedAuthMethodError,
    NOT a confusing missing-key error."""
    with pytest.raises(UnsupportedAuthMethodError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(auth_type="azure-ad"))
    assert "azure-ad" in str(excinfo.value)


def test_databricks_foreign_bigquery_field_rejected() -> None:
    """A BigQuery-only field (location) on a databricks target → ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(location="US"))
    assert "location" in str(excinfo.value)


def test_databricks_foreign_snowflake_field_rejected() -> None:
    """A Snowflake-only field (account) on a databricks target → ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(account="xy12345"))
    assert "account" in str(excinfo.value)


def test_bigquery_foreign_databricks_field_rejected() -> None:
    """A Databricks-only field (host) on a bigquery target → ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        DbtProfileTarget.model_validate(
            {
                "type": "bigquery",
                "method": "oauth",
                "project": "p",
                "schema": "d",
                "host": "dbc-ab12.cloud.databricks.com",
            }
        )
    assert "host" in str(excinfo.value)


def test_snowflake_foreign_databricks_field_rejected() -> None:
    """A Databricks-only field (host) on a snowflake target → ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        DbtProfileTarget.model_validate(_snowflake_target(host="dbc-ab12.cloud.databricks.com"))
    assert "host" in str(excinfo.value)


@pytest.mark.parametrize("bad_catalog", ["a-b", "a;b"])
def test_databricks_bad_catalog_raises(bad_catalog: str) -> None:
    """A catalog name outside the strict SQL-identifier grammar →
    InvalidIdentifierError (it becomes SQL via `USE CATALOG`)."""
    with pytest.raises(InvalidIdentifierError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(catalog=bad_catalog))
    assert bad_catalog in str(excinfo.value)


def test_databricks_bad_schema_raises() -> None:
    """A schema name outside the strict SQL-identifier grammar → InvalidIdentifierError."""
    with pytest.raises(InvalidIdentifierError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(schema="sch;ema"))
    assert "sch;ema" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_host",
    ["https://dbc-ab12.cloud.databricks.com", "has space.com", "host;DROP"],
)
def test_databricks_bad_host_raises(bad_host: str) -> None:
    """A garbage / scheme-prefixed host → InvalidIdentifierError."""
    with pytest.raises(InvalidIdentifierError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(host=bad_host))
    # The offending value must reach the message (no field-name fallback —
    # "host" is unconditionally present, which would make the check vacuous).
    assert bad_host in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_path",
    ["sql/1.0/warehouses/abc", "/sql/1.0;DROP", "/path with space"],
)
def test_databricks_bad_http_path_raises(bad_path: str) -> None:
    """A path missing the leading slash or carrying garbage → InvalidIdentifierError."""
    with pytest.raises(InvalidIdentifierError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(http_path=bad_path))
    # The offending value must reach the message (no field-name fallback).
    assert bad_path in str(excinfo.value)


def test_databricks_explicit_pat_auth_type_parses() -> None:
    """An explicit `auth_type: pat` (with token) parses — `pat` is in the
    supported set, not just the omitted-auth_type default. Guards against a
    regression dropping `pat` from `_DATABRICKS_SUPPORTED_AUTH`."""
    target = DbtProfileTarget.model_validate(_databricks_target(auth_type="pat"))
    assert target.auth_type == "pat"
    assert target.token == "dapi-secret-token"


def test_databricks_short_catalog_accepted() -> None:
    """A short Unity Catalog name like `main` (4 chars) parses — `catalog`
    uses `validate_identifier` (no length bound), NOT the 6-30-char
    `validate_project_id`. Locks in the behaviour the rule file warns about
    (the TableRef.project length gotcha does NOT reach the profile layer)."""
    target = DbtProfileTarget.model_validate(_databricks_target(catalog="main"))
    assert target.catalog == "main"


def test_databricks_empty_token_rejected() -> None:
    """An empty-string `token` (e.g. an unset `env_var(..., '')`) is treated as
    MISSING → IncompleteProfileError, not silently accepted. The credential
    fields are not shape-validated, so the required-key check must catch ''."""
    with pytest.raises(IncompleteProfileError) as excinfo:
        DbtProfileTarget.model_validate(_databricks_target(token=""))
    assert "token" in str(excinfo.value)


def test_databricks_oauth_empty_client_creds_rejected() -> None:
    """Empty-string OAuth credentials are treated as MISSING (collect-all)."""
    target = _databricks_target(auth_type="oauth", client_id="", client_secret="")
    del target["token"]
    with pytest.raises(IncompleteProfileError) as excinfo:
        DbtProfileTarget.model_validate(target)
    msg = str(excinfo.value)
    assert "client_id" in msg
    assert "client_secret" in msg


def test_databricks_accepts_connection_knobs() -> None:
    """Common dbt-databricks connection knobs (connect_retries /
    connect_timeout / connect_max_idle) are accepted-but-unused — a real
    operator profile carrying them parses rather than tripping extra="forbid"."""
    target = DbtProfileTarget.model_validate(
        _databricks_target(connect_retries=3, connect_timeout=30, connect_max_idle=60)
    )
    assert target.connect_retries == 3
    assert target.connect_timeout == 30
    assert target.connect_max_idle == 60


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


class StrictSnowflakeModel(BaseModel):
    """Test-only mirror of dbt-snowflake's commonly-documented target fields.

    ``extra="forbid"`` so adding a new field to the Snowflake drift fixture
    without updating BOTH this model and (if SignalForge needs it) the
    production :class:`DbtProfileTarget` trips the test loudly — the same
    forward-compat compensation the BigQuery :class:`StrictModel` provides
    (DEC-017).

    The field list mirrors ``tests/fixtures/profiles/dbt_snowflake_drift_v1_x.yml``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str
    account: str | None = None
    user: str | None = None
    role: str | None = None
    database: str | None = None
    warehouse: str | None = None
    # `schema` shadows Pydantic's deprecated BaseModel.schema(); mirror the
    # production model's approach and alias the dbt `schema:` key to a
    # differently-named attribute (safety-layer.md § field-name shadow).
    dataset: str | None = Field(default=None, alias="schema")
    threads: int | None = None
    password: str | None = None
    private_key_path: str | None = None
    private_key_passphrase: str | None = None
    authenticator: str | None = None
    client_session_keep_alive: bool | None = None
    query_tag: str | None = None
    connect_retries: int | None = None
    connect_timeout: int | None = None
    retry_on_database_errors: bool | None = None
    retry_all: bool | None = None
    reuse_connections: bool | None = None


def test_drift_detector_snowflake_extra_forbid() -> None:
    """The Snowflake drift fixture validates against StrictSnowflakeModel —
    bumping fixture fields without updating StrictSnowflakeModel/DbtProfileTarget
    fails this test loudly (DEC-017)."""
    with (FIXTURES / "dbt_snowflake_drift_v1_x.yml").open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    target_dict = raw["signalforge_test"]["outputs"]["dev"]

    model = StrictSnowflakeModel.model_validate(target_dict)

    # Sanity-check a couple of fields so this catches at least one corruption
    # mode (not just structural validation).
    assert model.type == "snowflake"
    assert model.account == "xy12345.us-east-1"
    assert model.warehouse == "TRANSFORMING"


def test_load_profile_parses_snowflake_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_profile`` parses a ``type: snowflake`` target end-to-end: the
    Snowflake-shaped fields populate, and ``schema:`` hydrates ``dataset`` via
    the alias (#120, US-005)."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    shutil.copy(FIXTURES / "snowflake_password.yml", project_dir / "profiles.yml")

    target = load_profile(project_dir)

    assert target.type == "snowflake"
    assert target.account == "xy12345.us-east-1"
    assert target.user == "svc_user"
    assert target.warehouse == "TRANSFORMING"
    assert target.database == "ANALYTICS"
    # Snowflake's `schema:` key hydrates `dataset` via the alias.
    assert target.dataset == "PUBLIC"


class StrictDatabricksModel(BaseModel):
    """Test-only mirror of dbt-databricks's commonly-documented target fields.

    ``extra="forbid"`` so adding a new field to the Databricks drift fixture
    without updating BOTH this model and (if SignalForge needs it) the
    production :class:`DbtProfileTarget` trips the test loudly — the same
    forward-compat compensation the BigQuery :class:`StrictModel` and Snowflake
    :class:`StrictSnowflakeModel` provide (DEC-017).

    The field list mirrors ``tests/fixtures/profiles/dbt_databricks_drift_v1_x.yml``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str
    host: str | None = None
    http_path: str | None = None
    catalog: str | None = None
    # `schema` shadows Pydantic's deprecated BaseModel.schema(); mirror the
    # production model's approach and alias the dbt `schema:` key to a
    # differently-named attribute (safety-layer.md § field-name shadow).
    dataset: str | None = Field(default=None, alias="schema")
    threads: int | None = None
    token: str | None = None
    auth_type: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    connect_retries: int | None = None
    connect_timeout: int | None = None
    connect_max_idle: int | None = None
    retry_all: bool | None = None
    session_properties: dict[str, object] | None = None


def test_drift_detector_databricks_extra_forbid() -> None:
    """The Databricks drift fixture validates against StrictDatabricksModel —
    bumping fixture fields without updating StrictDatabricksModel/DbtProfileTarget
    fails this test loudly (DEC-017)."""
    with (FIXTURES / "dbt_databricks_drift_v1_x.yml").open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    target_dict = raw["signalforge_test"]["outputs"]["dev"]

    model = StrictDatabricksModel.model_validate(target_dict)

    # Sanity-check a couple of fields so this catches at least one corruption
    # mode (not just structural validation).
    assert model.type == "databricks"
    assert model.host == "dbc-ab12cd34.cloud.databricks.com"
    assert model.catalog == "analytics"


def test_load_profile_parses_databricks_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_profile`` parses a ``type: databricks`` target end-to-end: the
    Databricks-shaped fields populate, and ``schema:`` hydrates ``dataset`` via
    the alias (#222, US-003)."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir)
    shutil.copy(FIXTURES / "databricks_pat.yml", project_dir / "profiles.yml")

    target = load_profile(project_dir)

    assert target.type == "databricks"
    assert target.host == "dbc-ab12cd34.cloud.databricks.com"
    assert target.http_path == "/sql/1.0/warehouses/abc123def456"
    assert target.catalog == "analytics"
    # Databricks's `schema:` key hydrates `dataset` via the alias.
    assert target.dataset == "public"


def test_module_uses_warehouse_logger() -> None:
    """DEC-027: every module in ``signalforge.warehouse.*`` uses the
    ``signalforge.warehouse`` logger, not a dunder-name logger.
    """
    assert profiles_module._LOGGER.name == "signalforge.warehouse"
