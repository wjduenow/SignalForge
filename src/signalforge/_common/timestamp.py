"""Canonical ISO-8601 UTC timestamp helper for audit/sidecar writers.

Issue #56 — every fail-closed audit writer (``safety``, ``draft``,
``prune``, ``grade``) emits a ``timestamp`` field, and a downstream
consumer correlating records across stages by timestamp benefits from
one byte-shape across writers.

The helper accepts a tz-aware :class:`datetime.datetime` and returns
``YYYY-MM-DDTHH:MM:SS.ffffffZ`` — microsecond precision, literal ``Z``
suffix, no offset form. Naive inputs raise :class:`ValueError`; passing
a non-UTC zone normalises to UTC first.

Pydantic models with a ``timestamp: datetime`` field use this helper
via ``@field_serializer("timestamp")`` so ``model_dump_json`` emits the
canonical shape; the writer's on-disk bytes match across stages.
"""

from __future__ import annotations

from datetime import UTC, datetime

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def iso8601_z(dt: datetime) -> str:
    """Render ``dt`` as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC, microseconds, Z).

    Args:
        dt: a tz-aware :class:`datetime.datetime`. Non-UTC zones are
            converted to UTC; naive datetimes raise :class:`ValueError`
            (we refuse to guess the zone).

    Returns:
        The canonical ISO-8601 string. Always 27 characters
        (``2026-05-11T12:00:00.123456Z``).

    Raises:
        ValueError: if ``dt`` is naive (``tzinfo is None``).
    """
    if dt.tzinfo is None:
        raise ValueError("iso8601_z requires a tz-aware datetime; got naive input")
    return dt.astimezone(UTC).strftime(_FORMAT)
