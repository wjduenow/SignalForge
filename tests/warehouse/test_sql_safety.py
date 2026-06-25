"""Tests for ``signalforge.warehouse._sql_safety`` validators."""

from __future__ import annotations

import pytest

from signalforge.warehouse._sql_safety import (
    validate_catalog_or_project,
    validate_databricks_hostname,
    validate_databricks_http_path,
    validate_snowflake_account,
)
from signalforge.warehouse.errors import InvalidIdentifierError


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "main",  # short Unity Catalog catalog (4 chars — below the project floor)
        "workspace",  # default Unity Catalog catalog
        "db",  # short Snowflake database (2 chars)
        "my_fake_db",  # underscored identifier
        "my-gcp-proj-123",  # hyphenated GCP project id
        "fake_project",  # existing fixture-style id
        "PROD",  # uppercase identifier
    ],
)
def test_validate_catalog_or_project_accepts_identifiers_and_project_ids(value: str) -> None:
    """DEC-005: accepts EITHER a strict SQL identifier (short Unity Catalog
    catalogs / short Snowflake databases) OR a hyphen-permissive GCP project
    id — composing the two existing validators without weakening either."""
    # Does not raise.
    validate_catalog_or_project("project", value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "",
        "a b",
        "a'b",
        'a"b',
        "a;b",
        "a`b",
        "a\nb",
        "a\tb",
        "a\x00b",
        "bad project!",
        "-leading-hyphen",
    ],
)
def test_validate_catalog_or_project_rejects_injection_shapes(value: str) -> None:
    """Injection-shaped values fail BOTH composed validators and still raise:
    the relaxed project value stays identifier-shape-gated (no SQL-breaking
    chars admitted), so this opens no injection vector."""
    with pytest.raises(InvalidIdentifierError):
        validate_catalog_or_project("project", value)


@pytest.mark.unit
def test_validate_catalog_or_project_error_carries_field_and_repr_value() -> None:
    """The raised error names the field and renders the offending value via
    ``repr()`` so crafted input can't inject into logs."""
    adversarial = "a'; DROP TABLE bar; --"
    with pytest.raises(InvalidIdentifierError) as exc_info:
        validate_catalog_or_project("project", adversarial)
    err = exc_info.value
    assert err.field == "project"
    assert err.value == adversarial
    rendered = str(err)
    assert "project" in rendered
    assert repr(adversarial) in rendered


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "myorg-account1",
        "xy12345.us-east-1",
        "ab12345",
        "MY_ORG-acct.us_east_1",
    ],
)
def test_validate_snowflake_account_accepts_valid_locators(value: str) -> None:
    """Real Snowflake account locator shapes pass: org-account, region-suffixed
    legacy locator, and bare account identifier (all carry dots/hyphens the
    strict SQL-identifier rule would wrongly reject)."""
    # Does not raise.
    validate_snowflake_account("account", value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "",
        "a b",
        "a'b",
        'a"b',
        "a;b",
        "a`b",
        "a" * 300,
    ],
)
def test_validate_snowflake_account_rejects_garbage(value: str) -> None:
    """Empty, whitespace, quoting, SQL fragments, backticks, and over-long
    inputs all fail loud with InvalidIdentifierError.

    Note: ``a--b`` is intentionally NOT in this set. DEC-006 fixes the regex as
    ``^[A-Za-z0-9][A-Za-z0-9._-]{1,253}$`` and account identifiers are never
    interpolated into SQL, so a doubled hyphen is a legal locator shape, not the
    SQL line-comment token; the permissive regex correctly accepts it."""
    with pytest.raises(InvalidIdentifierError):
        validate_snowflake_account("account", value)


@pytest.mark.unit
def test_validate_snowflake_account_error_carries_field_and_repr_value() -> None:
    """The raised error names the field and renders the offending value via
    ``repr()`` (its ``_format_value``) so crafted input can't inject into logs."""
    adversarial = "a'; DROP TABLE bar; --"
    with pytest.raises(InvalidIdentifierError) as exc_info:
        validate_snowflake_account("account", adversarial)
    err = exc_info.value
    assert err.field == "account"
    assert err.value == adversarial
    rendered = str(err)
    assert "account" in rendered
    assert repr(adversarial) in rendered


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "dbc-ab12.cloud.databricks.com",
        "adb-123.4.azuredatabricks.net",
    ],
)
def test_validate_databricks_hostname_accepts_valid_hosts(value: str) -> None:
    """Real Databricks workspace hostnames pass: AWS and Azure forms (both carry
    dots and hyphens the strict SQL-identifier rule would wrongly reject)."""
    # Does not raise.
    validate_databricks_hostname("host", value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://dbc-ab12.cloud.databricks.com",
        "dbc ab12.cloud.databricks.com",
        "dbc'ab12.databricks.com",
        'dbc"ab12.databricks.com',
        "dbc;ab12.databricks.com",
        "dbc`ab12.databricks.com",
        "dbc\nab12.databricks.com",
    ],
)
def test_validate_databricks_hostname_rejects_garbage(value: str) -> None:
    """Empty, scheme prefix, whitespace, quoting, ``;``, backticks, and control
    characters all fail loud with InvalidIdentifierError. The scheme prefix is
    rejected because ``//`` and ``:`` fall outside the permissive alphabet."""
    with pytest.raises(InvalidIdentifierError):
        validate_databricks_hostname("host", value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "/sql/1.0/warehouses/abc123",
        "/sql/protocolv1/o/0/abc",
    ],
)
def test_validate_databricks_http_path_accepts_valid_paths(value: str) -> None:
    """Real Databricks HTTP paths pass: SQL warehouse and legacy protocol forms
    (both lead with a slash and carry slashes/dots the strict SQL-identifier
    rule would wrongly reject)."""
    # Does not raise.
    validate_databricks_http_path("http_path", value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "",
        "sql/1.0/warehouses/abc",
        "/sql/1.0 /warehouses/abc",
        "/sql/'warehouses/abc",
        '/sql/"warehouses/abc',
        "/sql/;warehouses/abc",
        "/sql/`warehouses/abc",
        "/sql/\nwarehouses/abc",
    ],
)
def test_validate_databricks_http_path_rejects_garbage(value: str) -> None:
    """Empty, missing-leading-slash, whitespace, quoting, ``;``, backticks, and
    control characters all fail loud with InvalidIdentifierError."""
    with pytest.raises(InvalidIdentifierError):
        validate_databricks_http_path("http_path", value)


@pytest.mark.unit
def test_validate_databricks_hostname_error_carries_field_and_repr_value() -> None:
    """The raised error names the field and renders the offending value via
    ``repr()`` so crafted host input can't inject into logs."""
    adversarial = "dbc'; DROP TABLE bar; --"
    with pytest.raises(InvalidIdentifierError) as exc_info:
        validate_databricks_hostname("host", adversarial)
    err = exc_info.value
    assert err.field == "host"
    assert err.value == adversarial
    rendered = str(err)
    assert "host" in rendered
    assert repr(adversarial) in rendered


@pytest.mark.unit
def test_validate_databricks_http_path_error_carries_field_and_repr_value() -> None:
    """The raised error names the field and renders the offending value via
    ``repr()`` so crafted path input can't inject into logs."""
    adversarial = "/sql/'; DROP TABLE bar; --"
    with pytest.raises(InvalidIdentifierError) as exc_info:
        validate_databricks_http_path("http_path", adversarial)
    err = exc_info.value
    assert err.field == "http_path"
    assert err.value == adversarial
    rendered = str(err)
    assert "http_path" in rendered
    assert repr(adversarial) in rendered
