"""Shared cross-adapter deterministic sample-id seam (DEC-008 of issue #122).

These three helpers derive the deterministic ``run_id`` that names a
materialised sample temp table (``_sf_sample_<run_id>``) and the redacted
session-id hash that the adapter logs emit. They live here — not on any one
adapter — so the BigQuery and Snowflake adapters produce **byte-identical**
``run_id`` values for the same ``(table, n, partition_filter)`` tuple under the
same ``signalforge.__version__``. The recipe bytes (blake2b digest sizes, NUL
separators, canonical JSON encoding) are the contract; relocating the helpers
out of :mod:`signalforge.warehouse.adapters.bigquery` keeps that contract in one
place and avoids a cross-adapter import (which would risk pulling a vendor SDK
into the wrong adapter's import path).

Originally lived in :mod:`signalforge.warehouse.adapters.bigquery` (DEC-001 /
DEC-003 of issue #22). Relocated VERBATIM here by issue #122 — the recipe is
unchanged, so every existing BigQuery materialise-sample snapshot stays
byte-identical.
"""

from __future__ import annotations

import json
from datetime import date
from hashlib import blake2b

from signalforge.warehouse.models import PartitionFilter, TableRef


def _hash_session_id(session_id: str) -> str:
    """
    Produce a short redacted identifier for a session id.
    
    Parameters:
        session_id (str): Original session identifier to redact.
    
    Returns:
        str: An 8-character hexadecimal string representing the redacted session id.
    """
    return blake2b(session_id.encode("utf-8"), digest_size=4).hexdigest()


def _canonical_partition_filter(pf: PartitionFilter | None) -> str:
    """
    Render a PartitionFilter to a canonical JSON string for deterministic run-id hashing.
    
    If `pf` is None, returns the literal string "null". When `pf` is provided, the filter is encoded as JSON with keys "column", "op", and "value"; if `pf.value` is a date or datetime it is rendered using `isoformat()`, otherwise `str()` is used. The JSON uses stable key ordering and compact separators to ensure byte-for-byte determinism.
    
    Parameters:
        pf (PartitionFilter | None): The partition filter to canonicalize, or `None`.
    
    Returns:
        str: The canonical JSON string for the filter, or the literal "null".
    """
    if pf is None:
        return "null"
    # ``datetime`` is a ``date`` subclass; isinstance(..., date) covers both,
    # and ``.isoformat()`` produces the canonical text for either type so a
    # ternary keeps the rendering rule on one line.
    rendered_value = pf.value.isoformat() if isinstance(pf.value, date) else str(pf.value)
    return json.dumps(
        {"column": pf.column, "op": pf.op, "value": rendered_value},
        sort_keys=True,
        separators=(",", ":"),
    )


def _compute_run_id(
    *,
    table: TableRef,
    n: int,
    partition_filter: PartitionFilter | None,
) -> str:
    """DEC-001 of #22 — derive the deterministic ``run_id`` for the
    temp-table name ``_sf_sample_<run_id>``.

    Inputs (joined with NUL separator to prevent concatenation
    collisions, mirrors the grader's ``criterion_prompt_hash`` recipe):

    * ``table.qualified_name`` — the source table's three-part
      ``<project>.<dataset>.<name>`` (or two-part when project is
      None). Identifies the sample's source uniquely.
    * ``signalforge.__version__`` — bumps when the codebase changes,
      so a SignalForge upgrade invalidates any cached materialised
      sample (compiled SQL drift across versions stays observable).
    * ``str(n)`` — sample size; different ``n`` values produce
      distinct temp tables.
    * :func:`_canonical_partition_filter` — stable JSON encoding so
      two callers building the same filter produce byte-equal input.

    Output: 16 hex chars (``blake2b(..., digest_size=8).hexdigest()``).
    Lowercase alphanumeric, so the resulting ``_sf_sample_<run_id>``
    passes :func:`validate_identifier` without further coercion.
    """
    from signalforge import __version__

    payload = "\x00".join(
        [
            table.qualified_name,
            __version__,
            str(n),
            _canonical_partition_filter(partition_filter),
        ]
    ).encode("utf-8")
    return blake2b(payload, digest_size=8).hexdigest()


__all__ = [
    "_canonical_partition_filter",
    "_compute_run_id",
    "_hash_session_id",
]
