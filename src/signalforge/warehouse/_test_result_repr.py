"""Compact, type-aware row rendering for TestResult.explanation() (DEC-020)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

_MAX_VALUE_LEN = 40


def compact_repr(row: dict[str, Any], schema: list[tuple[str, str]] | None = None) -> str:
    """Render one BQ row as a short, SQL-safe-looking string.

    Used by ``TestResult.explanation()`` to produce the example fragment
    in the deterministic "why" string. Truncates each value at 40 chars.
    When ``schema`` is provided, TIMESTAMP/DATETIME columns render as
    ``TIMESTAMP('...')`` / ``DATETIME('...')`` so a reader can paste the
    fragment into a WHERE clause.
    """
    schema_map = dict(schema) if schema else {}
    parts: list[str] = []
    for k, v in row.items():
        ty = schema_map.get(k, "")
        parts.append(f"{k}={_render_value(v, ty)}")
    return ", ".join(parts)


def _render_value(v: Any, bq_type: str) -> str:
    from signalforge.warehouse._sql_safety import escape_bq_string_literal

    if v is None:
        return "NULL"
    bq_type_upper = bq_type.upper()
    if bq_type_upper in ("TIMESTAMP", "DATETIME") and isinstance(v, (datetime, str)):
        return f"{bq_type_upper}('{escape_bq_string_literal(_truncate(str(v)))}')"
    if bq_type_upper == "DATE" and isinstance(v, (date, str)):
        return f"DATE('{escape_bq_string_literal(_truncate(str(v)))}')"
    if isinstance(v, str):
        return f"'{escape_bq_string_literal(_truncate(v))}'"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return _truncate(repr(v))


def _truncate(s: str) -> str:
    if len(s) <= _MAX_VALUE_LEN:
        return s
    return s[: _MAX_VALUE_LEN - 3] + "..."
