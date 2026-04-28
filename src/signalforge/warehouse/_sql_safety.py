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
# end with a hyphen. Legacy "domain-scoped" project IDs of the shape
# ``example.com:my-project`` are also accepted via the optional
# ``<org>:<project>`` prefix. The strict identifier form is also accepted
# as a fallback so existing test fixtures (``fake_project``) and uppercase
# legacy IDs remain valid.
_PROJECT_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9-]{4,28}[A-Za-z0-9]"
    r"(?::[A-Za-z][A-Za-z0-9_-]*)?$|^[A-Za-z_][A-Za-z0-9_]*$"
)


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
    must not end with a hyphen, with an optional legacy domain-scoped
    ``<org>:<project>`` prefix. The strict ``[A-Za-z_][A-Za-z0-9_]*``
    identifier form is also accepted as a fallback so existing fixtures
    using underscored fake IDs continue to validate.

    Adversarial inputs (whitespace, quoting, SQL fragments) are rejected.
    """
    from signalforge.warehouse.errors import InvalidIdentifierError

    if not _PROJECT_RE.fullmatch(value):
        raise InvalidIdentifierError(field=field, value=value)


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
    """Remove ``'...'`` and ``"..."`` string literals so token checks can ignore them.

    Naive — handles ``''`` and ``""`` as escapes. Sufficient for the
    DEC-013 "cheap rejects, not full SQL parser" contract.
    """
    out: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch in ("'", '"'):
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
