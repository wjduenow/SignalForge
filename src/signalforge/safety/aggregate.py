"""Aggregate-stats wrapper for the safety layer (US-009).

Public surface is one function — :func:`aggregate_columns` — that classifies
each requested column via :func:`signalforge.safety.redact._classify_column`
and, for non-redacted columns, fetches per-column stats from the warehouse
adapter. Redacted columns yield ``None`` (keyed by the hashed placeholder
name) and a :class:`signalforge.safety.models.RedactionRecord`.

Design commitments operationalised here:

* **DEC-008** — All ``adapter.column_stats`` calls execute inside a single
  ``with adapter:`` block so the BigQuery adapter can batch them into one
  query rather than one round-trip per column.
* **DEC-010** — Only redacted columns are renamed to their hashed
  placeholder in the returned dict; non-redacted columns keep their real
  names so callers can still address them by the names the dbt model
  declares.
"""

from __future__ import annotations

from collections.abc import Iterable

from signalforge.manifest.models import Model
from signalforge.safety.errors import ColumnNotInModelError
from signalforge.safety.models import RedactionRecord
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.redact import _classify_column
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import ColumnStats, TableRef


def aggregate_columns(
    adapter: WarehouseAdapter,
    model: Model,
    columns: Iterable[str],
    policy: SafetyPolicy,
) -> tuple[dict[str, ColumnStats | None], tuple[RedactionRecord, ...]]:
    """Collect column-level stats for the requested columns.

    Each requested column is classified up front via
    :func:`_classify_column`. Redacted columns yield ``None`` in the returned
    dict (keyed by the **hashed** placeholder name, per DEC-010) and one
    :class:`RedactionRecord` in the redactions tuple. Non-redacted columns
    invoke ``adapter.column_stats`` inside a single ``with adapter:`` block
    so the adapter can batch the calls into one query (DEC-008); the
    resulting :class:`ColumnStats` is stored under the column's **real**
    name.

    Empty ``columns`` yields ``({}, ())`` without ever opening the adapter
    context.

    Raises:
        ColumnNotInModelError: ``columns`` references a name not declared on
            ``model.columns``.
        ManifestProjectNotFoundError / ManifestSchemaNotFoundError: the
            model is missing the ``database`` / ``schema`` fields needed to
            build a :class:`TableRef`.

    Returns:
        ``(stats_by_name, redactions)``. ``stats_by_name`` keys are hashed
        placeholders for redacted columns and real names otherwise;
        ``redactions`` lists every redacted column in request order.
    """
    requested = list(columns)
    if not requested:
        return {}, ()

    # Build a name -> Column lookup so the per-column classifier call is O(1).
    column_lookup = {c.name: c for c in model.columns_list}

    # Classify all columns up front so we know which to fetch *before*
    # opening the adapter context — DEC-008 batches stats calls, but there's
    # no point opening the context at all if every requested column is
    # redacted.
    classifications: dict[str, RedactionRecord | None] = {}
    for name in requested:
        col_obj = column_lookup.get(name)
        if col_obj is None:
            raise ColumnNotInModelError(
                model_unique_id=model.unique_id,
                column_name=name,
            )
        classifications[name] = _classify_column(col_obj, model, policy)

    stats: dict[str, ColumnStats | None] = {}
    redactions: list[RedactionRecord] = []

    needs_warehouse = any(rec is None or not rec.redacted for rec in classifications.values())

    if needs_warehouse:
        # Resolve the table reference once. ``TableRef.from_model`` raises
        # typed errors when database/schema are missing — those propagate.
        table = TableRef.from_model(model)
        with adapter:
            for name in requested:
                rec = classifications[name]
                if rec is not None and rec.redacted:
                    redactions.append(rec)
                    stats[rec.hashed_name] = None
                else:
                    stats[name] = adapter.column_stats(table, name)
    else:
        # Every requested column is redacted — never touch the warehouse.
        for name in requested:
            rec = classifications[name]
            assert rec is not None and rec.redacted  # guarded by needs_warehouse
            redactions.append(rec)
            stats[rec.hashed_name] = None

    return stats, tuple(redactions)


__all__ = ["aggregate_columns"]
