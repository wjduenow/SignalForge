"""Identifier + caller-SQL safety helpers (DEC-013).

Centralises the regex used by every public-API field that becomes part of a
BigQuery SQL string, so the rule lives in one place.
"""

from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""Strict identifier regex used for dataset, table, and column names — every
SQL identifier other than the GCP project ID. The unrelaxed form is the
right choice for these fields because BigQuery does not accept hyphens in
unquoted dataset/table/column names anyway."""

# Google's GCP project ID rules: 6-30 chars, must start with a lowercase
# letter, may contain lowercase letters, digits, or hyphens, and may not
# end with a hyphen. The strict identifier form is also accepted as a
# fallback (with the same 6-30 length bound) so existing test fixtures
# (``fake_project``) and uppercase legacy IDs remain valid.
#
# Legacy "domain-scoped" project IDs of the shape ``example.com:my-project``
# are NOT supported in v0.1: the regex would reject the dot, AND the
# ``_quote`` SQL renderer in ``adapters/bigquery.py`` does not split on the
# colon. Tracked as a v0.2 follow-up in ``docs/warehouse-adapter-ops.md``.
_PROJECT_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9-]{4,28}[A-Za-z0-9]$|^[A-Za-z_][A-Za-z0-9_]{4,28}[A-Za-z0-9_]$"
)

# Snowflake account identifiers are NEVER interpolated into SQL (they configure
# the connection, not a query), so this regex is deliberately *permissive*: it
# is log-injection hygiene + a fail-loud "obvious garbage" gate, NOT the strict
# SQL-identifier rule. It must accept the dots and hyphens that real account
# locators carry (org-account ``myorg-account1``, region-suffixed legacy locator
# ``xy12345.us-east-1``, bare ``ab12345``) while rejecting whitespace, quoting,
# SQL fragments, backticks, and empty/over-long input. Allowed alphabet:
# alphanumerics, dot, underscore, hyphen; must start alphanumeric; length 2-254.
_SF_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,253}$")


def validate_identifier(field: str, value: str) -> None:
    """Raise InvalidIdentifierError if value is not a valid SQL identifier.

    Used by TableRef (``dataset``, ``name``) and PartitionFilter at
    construction time, and by BigQueryAdapter.column_stats at entry.
    Rejects anything outside ``[A-Za-z_][A-Za-z0-9_]*`` — including
    backticks, whitespace, dots, quotes, and hyphens. SQL keywords are
    accepted (we don't keyword-check).

    GCP **project IDs** use a different, hyphen-permissive grammar; route
    those through :func:`validate_project_id` instead.
    """
    from signalforge.warehouse.errors import InvalidIdentifierError

    if not _IDENTIFIER_RE.fullmatch(value):
        raise InvalidIdentifierError(field=field, value=value)


def validate_project_id(field: str, value: str) -> None:
    """Raise InvalidIdentifierError if value is not a valid GCP project ID.

    GCP project IDs use a separate grammar from BigQuery's other
    identifiers: 6-30 chars, lowercase-letter start, hyphens permitted,
    must not end with a hyphen. A strict-identifier fallback (also
    bounded to 6-30 chars) is accepted so existing fixtures using
    underscored fake IDs continue to validate. Legacy domain-scoped IDs
    (``example.com:my-project``) are deferred to v0.2.

    Adversarial inputs (whitespace, quoting, SQL fragments) are rejected.
    """
    from signalforge.warehouse.errors import InvalidIdentifierError

    if not _PROJECT_RE.fullmatch(value):
        raise InvalidIdentifierError(field=field, value=value)


def validate_snowflake_account(field: str, value: str) -> None:
    """Raise InvalidIdentifierError if value is not a plausible Snowflake account.

    DEC-006. Snowflake account identifiers are **never** interpolated into SQL —
    they configure the connection, not a query — so this is deliberately a
    permissive validator: log-injection hygiene plus a fail-loud "obvious
    garbage" gate, NOT the strict SQL-identifier rule. Do NOT route account
    identifiers through :func:`validate_identifier`; its regex rejects the dots
    and hyphens that real account locators require.

    Accepts the shapes Snowflake actually uses:

    - org-account form: ``myorg-account1``
    - region-suffixed legacy locator: ``xy12345.us-east-1``
    - bare account locator: ``ab12345``
    - mixed underscores/hyphens/dots: ``MY_ORG-acct.us_east_1``

    Rejects empty input, whitespace, quoting (``'`` / ``"``), ``;``,
    backticks, control characters, and over-long (> 254 char) values.

    Note: a doubled hyphen (``--``) is **accepted** — hyphens are legal in
    account locators and the value never reaches SQL, so ``--`` is not the
    line-comment token here. The intent is to reject whitespace / quotes /
    backticks / ``;`` / control chars, not every SQL metacharacter.
    """
    from signalforge.warehouse.errors import InvalidIdentifierError

    if not _SF_ACCOUNT_RE.fullmatch(value):
        raise InvalidIdentifierError(field=field, value=value)


def escape_bq_string_literal(s: str) -> str:
    """Escape characters so the result is safe inside a BQ single-quoted literal.

    BigQuery's standard SQL treats ``\\`` as an escape character inside
    single-quoted strings AND forbids unescaped newlines/carriage
    returns. This helper handles both: backslash first (so the
    subsequent escape pass can't be undone), then quotes, then the
    common control chars. NUL is dropped because BQ rejects it
    outright. Shared by ``_render_partition_filter`` and
    ``_test_result_repr`` so the two stay in lockstep.
    """
    return (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\x00", "")
    )


# Lightweight SQL safety rejects for run_test_sql (DEC-013).
# Not a SQL parser — just catches the easy mistakes the LLM drafter could make.


def validate_test_sql(sql: str) -> None:
    """Reject obviously unsafe candidate-test SQL.

    Rejects: top-level ``;``, ``--`` outside string literals, ``/* */``
    comments, unbalanced parentheses. Single-statement SELECTs are fine.

    The full failure surface is documented as the contract: callers must
    supply a single SELECT returning rows.
    """
    from signalforge.warehouse.errors import QuerySyntaxError

    # Stripping out single-quoted string literals before checking for
    # comment / semicolon / paren tokens.
    sanitized = _strip_string_literals(sql)

    if ";" in sanitized:
        raise QuerySyntaxError(detail="run_test_sql input must be a single statement (no `;`)")
    if "--" in sanitized:
        raise QuerySyntaxError(detail="run_test_sql input must not contain `--` line comments")
    if "/*" in sanitized or "*/" in sanitized:
        raise QuerySyntaxError(detail="run_test_sql input must not contain `/* */` block comments")
    depth = 0
    for ch in sanitized:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise QuerySyntaxError(detail="run_test_sql input has unbalanced parentheses")
    if depth != 0:
        raise QuerySyntaxError(detail="run_test_sql input has unbalanced parentheses")


def _strip_string_literals(sql: str) -> str:
    """Remove ``'...'``, ``"..."`` and `` `...` `` quoted spans so token checks ignore them.

    Naive — handles ``''`` / ``""`` / `` `` `` doubled-quote escapes. Backtick
    spans are BigQuery quoted identifiers (e.g. `` `weird;name` ``); stripping
    them is what makes a stray ``'`` / ``"`` / ``--`` / ``;`` hidden inside a
    backtick-quoted identifier stop confusing the literal scanner — without it
    a backtick identifier containing a lone quote (`` `O'Brien` ``) would open
    a phantom single-quoted literal and swallow a real top-level ``;`` that
    should have been rejected (DEC-008 of #116). Sufficient for the DEC-013
    "cheap rejects, not full SQL parser" contract — NOT a SQL parser.
    """
    out: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)
