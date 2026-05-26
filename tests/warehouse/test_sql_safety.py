"""Tests for ``signalforge.warehouse._sql_safety`` validators."""

from __future__ import annotations

import pytest

from signalforge.warehouse._sql_safety import validate_snowflake_account
from signalforge.warehouse.errors import InvalidIdentifierError


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
