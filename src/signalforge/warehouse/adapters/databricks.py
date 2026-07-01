"""Databricks adapter — deterministic sampling surface (issue #224; epic #219).

Implements the warehouse-agnostic seam — Architectural Commitment #3 of
``CLAUDE.md`` — through a *fourth* concrete adapter code path (after BigQuery,
the Postgres stub, and the Snowflake adapter), mirroring the Snowflake adapter
(#119 skeleton, #122 sampling) closely. The #221 skeleton wired the ABC +
factory seam and #223 landed the prune-compiler dialect; #224 (this surface)
lands the first real warehouse I/O. Live validity against a Databricks SQL
warehouse is certified in #226.

Surface:

* :meth:`__init__` captures connection params (``host`` / ``http_path`` /
  ``token`` / ``catalog`` / ``schema``) plus the forward-compat OAuth-M2M auth
  params (``auth_type`` / ``client_id`` / ``client_secret``). No connection is
  opened (mirror Snowflake DEC-001 of #122); #224 consumes these when opening
  one.
* :meth:`__repr__` renders ONLY non-credential identifying fields — ``host`` /
  ``http_path`` / ``catalog`` — so a debug-print or log line never leaks
  ``token`` / ``client_secret`` (the repr-redaction rule).
* :meth:`__init__` accepts an injectable ``connection`` (mirror Snowflake
  DEC-001 of #122), lazily built via :func:`_databricks_client.make_real_client`
  on first :meth:`_get_connection`; the Databricks connection embodies the
  session that scopes temp objects.
* :meth:`__enter__` returns ``self``; :meth:`__exit__` runs a fail-soft
  :meth:`_cleanup_active_session` that closes the live connection and
  swallows-and-warns on failure. With no opened connection
  (``_active_session is None``) the ``with adapter:`` block is a clean no-op.
* :meth:`dialect` returns the :data:`DATABRICKS_DIALECT` constant.
* :meth:`sample_rows` is implemented (#224 US-003; sample shape corrected by
  #226) — deterministic hash-mod sampling (the projection-subquery shape: the
  masked ``xxhash64`` whole-row hash is computed in an inner projection alias,
  ``SELECT * EXCEPT (_sf_sample_hash) FROM (... AS _sf_sample_hash)``, because
  Spark rejects ``struct(*)`` in a Sort node) sized from :meth:`get_row_count`
  (``SELECT COUNT(*)``), with the fail-loud sizing the Snowflake / BigQuery
  adapters share (:class:`UnknownTableSizeError` /
  :class:`SamplingRequiresPartitionFilterError`).
* :meth:`get_row_count` is implemented (#224 US-003) — overrides the ABC degrade
  with a ``SELECT COUNT(*)`` (``DESCRIBE DETAIL`` has no reliable ``numRows``;
  COUNT is metadata-cheap on Delta), returning ``None`` on a
  :class:`WarehouseError` so the shared sizing pathway decides.
* :meth:`materialise_sample` is implemented (#224 US-004; revised live by #226)
  — overrides the ABC degrade with a qualified ``CREATE OR REPLACE TABLE
  <cat>.<sch>._sf_sample_<run_id> AS <deterministic sample body>`` (``run_id``
  from the shared :mod:`signalforge.warehouse._sample_id` recipe), pinning the
  connection so a follow-up :meth:`run_test_sql` reaches the table. A real table
  (NOT ``TEMPORARY TABLE``) because Databricks rejects a qualified temp name;
  it is dropped at the session-cleanup boundary (see :meth:`materialise_sample`).
* :meth:`run_test_sql` is implemented (#224 US-004) — overrides the
  :class:`NotImplementedError` stub: wraps a candidate failing-rows SELECT in a
  ``COUNT(*)`` aggregate (plus a per-row ``to_json(struct(*))`` LIMIT capture
  query when ``capture_failures > 0``) and returns a typed :class:`TestResult`.
* :meth:`column_stats` is implemented (#224 US-005, DEC-011) — overrides the
  :class:`NotImplementedError` stub with a single aggregate query (count /
  distinct / nulls / min / max / data_type). Databricks ships this AHEAD of
  Snowflake (which stubs it); the Snowflake-parity decision is GitHub issue #258.
* :meth:`estimate_query_bytes` is implemented (#225, US-002) — it runs
  ``EXPLAIN COST <sql>`` and parses the MAX Spark CBO
  ``Statistics(sizeInBytes=...)`` across plan nodes via
  :func:`_parse_explain_cost_bytes`, overriding the ABC
  ``EstimateNotSupportedError`` degrade. Live validity is certified in #226.
* :meth:`run_stats_query` inherits the ABC typed degrade
  (``StatsQueryNotSupportedError``) — a clean, operator-actionable signal (out
  of scope for #225).
* :meth:`WarehouseAdapter.from_profile` dispatches ``profile.type ==
  "databricks"`` here so an operator with a Databricks profile sees a typed
  "v0.x pending" ``NotImplementedError`` rather than the v0.1
  :class:`UnsupportedProfileTypeError`.

The ``databricks-sql-connector`` import stays confined to
:mod:`signalforge.warehouse.adapters._databricks_client` (the one-shim-per-vendor
SDK seam); this module opens connections only through that shim's
``make_real_client`` and never imports the connector directly.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from signalforge.warehouse._sample_id import _compute_run_id, _hash_session_id
from signalforge.warehouse._sample_sql import render_sample_select
from signalforge.warehouse._sql_safety import validate_identifier, validate_test_sql
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import (
    EstimateUnavailableError,
    MaterialisationFailedError,
    SamplingRequiresPartitionFilterError,
    UnknownTableSizeError,
    WarehouseError,
)
from signalforge.warehouse.models import (
    DATABRICKS_DIALECT,
    ColumnStats,
    Dialect,
    TableRef,
    TestResult,
)

if TYPE_CHECKING:
    from signalforge.warehouse.adapters._databricks_client import (
        _DatabricksClientProtocol,
        _DatabricksCursorProtocol,
    )
    from signalforge.warehouse.models import PartitionFilter


_LOGGER = logging.getLogger("signalforge.warehouse")

# Mirror of :data:`signalforge.warehouse.adapters.bigquery._LARGE_TABLE_THRESHOLD`
# (100M). Re-declared (not imported) so this module never pulls in the BigQuery
# adapter — the value is the load-bearing contract: identical sizing behaviour
# across vendors (mirrors the Snowflake adapter's same re-declaration).
_LARGE_TABLE_THRESHOLD: int = 100_000_000

# Spark's ``spark.sql.defaultSizeInBytes`` (= ``Long.MaxValue`` ≈ ``8.0 EiB``)
# is the size a plan node carries when it has NO cost-based-optimizer statistics
# (DEC-004 of issue #225). ``8 * 1024**6`` == ``2**63`` == ``9223372036854775808``;
# Spark's ``Utils.bytesToString`` formats both ``Long.MaxValue`` and ``2**63`` as
# ``8.0 EiB`` (it rounds the EiB division to one decimal), so a parsed ``8.0 EiB``
# converts to exactly this sentinel and trips the ``>=`` check below. Reporting a
# ~9-exabyte "cost" would conflate "no table statistics" with "a genuinely huge
# scan" — so a max at-or-above this value routes to ``EstimateUnavailableError``.
_SPARK_DEFAULT_SIZE_SENTINEL_BYTES: int = 8 * 1024**6

# 1024-based binary unit → power-of-1024 exponent. ``EXPLAIN COST`` renders
# ``Statistics(sizeInBytes=<num> <unit>)`` with these human-readable units.
_SIZE_UNIT_POWERS: dict[str, int] = {
    "B": 0,
    "KiB": 1,
    "MiB": 2,
    "GiB": 3,
    "TiB": 4,
    "PiB": 5,
    "EiB": 6,
}

# Match every ``sizeInBytes=<num> <unit>`` occurrence in the plan text. ``<num>``
# accepts an integer (``512``), a decimal (``12.3``), or scientific notation
# (``5.0E+2`` / ``5.00E+5``); ``<unit>`` is one of the binary units above. The
# multi-char units are listed before the bare ``B`` so the alternation prefers the
# longest match (regex alternation is ordered left-to-right at each position).
_SIZE_IN_BYTES_RE = re.compile(
    r"sizeInBytes=\s*"
    r"(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*"
    r"(?P<unit>KiB|MiB|GiB|TiB|PiB|EiB|B)"
)

# Spark ``typeof()`` returns LOWERCASE DDL type strings. Mirrors BigQuery's
# ``_is_complex_type`` (DEC-016 of the ``ColumnStats`` contract): the scalar
# complex types where ``MIN``/``MAX`` is not meaningful, plus the parametric
# ones detected by their type-name prefix. Spark has no ``GEOGRAPHY`` and
# renders JSON as ``string`` — so the sets differ from BigQuery's.
_COMPLEX_SPARK_TYPES: frozenset[str] = frozenset({"binary", "variant"})
"""Scalar complex Spark types where ``MIN``/``MAX`` is omitted (R1/DEC-001)."""

_PARAMETRIC_COMPLEX_SPARK_PREFIXES: frozenset[str] = frozenset({"array", "struct", "map"})
"""Parametric complex Spark types (``array<…>`` / ``struct<…>`` / ``map<…>``)."""


def _escape_spark_string_literal(value: str) -> str:
    """Escape the INNER content of a Spark/Databricks single-quoted string literal.

    Returns the escaped body WITHOUT the surrounding single quotes (the caller
    wraps it in ``'…'``). Spark-correct escaping (R3/DEC-005 of issue #227), NOT
    the BigQuery :func:`escape_bq_string_literal` the partition-filter renderer
    used to borrow:

    * ``\\`` → ``\\\\`` — under Spark's default ``escapedStringLiterals=false`` the
      backslash IS an escape character, so a literal backslash must be doubled.
      Done FIRST, so the quote-doubling below is not itself re-escaped.
    * ``'`` → ``''`` — Spark-idiomatic quote doubling; safe for the quote char in
      BOTH ``escapedStringLiterals`` modes.

    No ``SET spark.sql.…`` conf is issued anywhere — the escape is correct for
    the default session mode.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


def _is_complex_spark_type(type_str: str) -> bool:
    """Return True for Spark types where ``MIN``/``MAX`` is not meaningful.

    Mirrors :func:`signalforge.warehouse.adapters.bigquery._is_complex_type`
    with Databricks/Spark semantics (R1/DEC-001 of issue #227). Handles both
    the scalar complex types (``binary``, ``variant``) and the parametric ones
    (``array<…>``, ``struct<…>``, ``map<…>``); the prefix split keeps the check
    resilient against arbitrary nested-type bodies. Spark ``typeof()`` returns
    lowercase DDL strings, so the comparison is lower-folded defensively.

    An empty string (``data_type == ""`` — the empty-table case) is NOT complex,
    so ``MIN``/``MAX`` are preserved for it.
    """
    lowered = type_str.strip().lower()
    if lowered in _COMPLEX_SPARK_TYPES:
        return True
    head = lowered.split("<", 1)[0]
    return head in _PARAMETRIC_COMPLEX_SPARK_PREFIXES


def _parse_explain_cost_bytes(cell: object) -> int:
    """Extract the planner's estimated-bytes figure from a Spark/Databricks
    ``EXPLAIN COST <sql>`` result cell (DEC-002 / DEC-003 / DEC-004 / DEC-005 /
    DEC-006 / DEC-007 of issue #225).

    ``EXPLAIN COST <sql>`` returns a single cell carrying the multi-line text of
    the optimized logical plan. Each plan node is annotated with cost-based
    statistics of the form ``Statistics(sizeInBytes=<num> <unit>[, rowCount=...])``
    — ``<unit>`` is a 1024-based binary unit (``B`` / ``KiB`` / ``MiB`` / ``GiB`` /
    ``TiB`` / ``PiB`` / ``EiB``) and ``<num>`` may be an integer, decimal, or
    scientific notation. This is the Databricks analogue of Snowflake's
    ``GlobalStats.bytesAssigned`` (:func:`_parse_explain_json_bytes`) — but Spark
    emits plan *text*, not JSON.

    The function takes the MAX ``sizeInBytes`` across all plan nodes (DEC-003).
    The maximum is almost always the leaf table scan — the "bytes scanned" cost
    proxy, the closest analogue to BigQuery's ``total_bytes_processed``. The root
    (top of the optimized logical plan) reflects *output* size, which understates
    scan cost (tiny for a ``SELECT COUNT(*)``); leaf-node string matching is
    fragile. The no-stats sentinel (below) propagates from a stats-less leaf up
    through its ancestors, so ``max == sentinel`` cleanly signals "the scan has no
    statistics."

    Pure: no connection, no warehouse call, no logging.

    Every failure to extract a usable byte count raises
    :class:`EstimateUnavailableError` with an operator-useful ``detail`` (DEC-006 —
    the existing error is reused; never fabricate a ``0``, which would silently
    report a ``$0`` cost on a future plan-shape change). Five failure shapes route
    here:

    * ``cell`` is not a ``str`` (e.g. ``None`` or a ``list`` — a defensive guard
      for a malformed connector return).
    * No ``Statistics(sizeInBytes=...)`` matches at all (DEC-005 — a plan-shape
      change across Databricks runtime versions, or a metadata-only query).
    * A parsed value is negative or non-finite (a pathological scientific-notation
      overflow such as ``1E+400`` → ``inf``).
    * The computed max is at-or-above the Spark ``8.0 EiB`` no-stats sentinel
      (DEC-004) — the plan node had no CBO statistics; the ``detail`` names
      ``ANALYZE TABLE`` as the remediation.

    :param cell: the ``EXPLAIN COST`` result cell — the optimized-logical-plan
        text ``str``.
    :returns: the maximum ``sizeInBytes`` across all plan nodes, in bytes —
        a non-negative ``int`` strictly below the no-stats sentinel.
    :raises EstimateUnavailableError: on a non-``str`` cell, a plan carrying no
        ``sizeInBytes`` statistics, a negative/non-finite parsed value, or a
        max at-or-above the Spark default-size (no-stats) sentinel.
    """
    if not isinstance(cell, str):
        raise EstimateUnavailableError(
            detail=f"EXPLAIN COST cell was not plan text (got {type(cell).__name__})"
        )

    max_bytes: int | None = None
    for match in _SIZE_IN_BYTES_RE.finditer(cell):
        raw_num = match.group("num")
        value = float(raw_num)
        # The ``num`` group cannot capture a leading ``-``, so ``value < 0`` is
        # defensive (unreachable via the public parser); ``isfinite`` IS reached
        # by an overflowing scientific literal (e.g. ``1E+400`` → ``inf``).
        if not math.isfinite(value) or value < 0:
            raise EstimateUnavailableError(
                detail=f"EXPLAIN COST plan carried a non-finite or negative sizeInBytes ({raw_num})"
            )
        node_bytes = int(value * 1024 ** _SIZE_UNIT_POWERS[match.group("unit")])
        if max_bytes is None or node_bytes > max_bytes:
            max_bytes = node_bytes

    if max_bytes is None:
        raise EstimateUnavailableError(detail="EXPLAIN COST plan carried no sizeInBytes statistics")

    if max_bytes >= _SPARK_DEFAULT_SIZE_SENTINEL_BYTES:
        raise EstimateUnavailableError(
            detail=(
                "EXPLAIN COST plan reported the Spark default size (no table "
                "statistics; run ANALYZE TABLE <table> COMPUTE STATISTICS)"
            )
        )

    return max_bytes


class DatabricksAdapter(WarehouseAdapter):
    """:class:`WarehouseAdapter` for Databricks SQL profiles.

    Issue #224 (US-003) lands the first real warehouse I/O: :meth:`sample_rows`
    (deterministic projection-subquery hash-mod — #226), :meth:`get_row_count`
    (``SELECT COUNT(*)``), and the shared fail-loud :meth:`_resolve_sample_bucket`
    sizing, all on a connection wired via :meth:`_get_connection` with a fail-soft
    ``__exit__`` cleanup. US-004 adds :meth:`materialise_sample` (qualified
    ``CREATE OR REPLACE TABLE``, dropped at cleanup — #226) and
    :meth:`run_test_sql` (``COUNT(*)``
    failing-rows wrap + per-row ``to_json`` capture). US-005 adds
    :meth:`column_stats` (single aggregate-only profiling query) — shipped AHEAD
    of Snowflake, whose parity is tracked as issue #258. :meth:`estimate_query_bytes`
    is implemented (#225) via ``EXPLAIN COST`` (parse Spark CBO ``sizeInBytes``);
    :meth:`run_stats_query` inherits its typed ``StatsQueryNotSupportedError``
    degrade (out of scope for #225).
    """

    def __init__(
        self,
        *,
        connection: _DatabricksClientProtocol | None = None,
        host: str | None = None,
        http_path: str | None = None,
        token: str | None = None,
        catalog: str | None = None,
        schema: str | None = None,
        auth_type: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        # Injectable connection seam (mirrors Snowflake DEC-001 of #122 /
        # BigQuery's ``client=``). ``None`` triggers a lazy
        # ``make_real_client(...)`` build on first :meth:`_get_connection`; tests
        # inject a fake. No connection is opened here.
        self._connection = connection
        self._host = host
        self._http_path = http_path
        self._token = token
        self._catalog = catalog
        self._schema = schema
        # Forward-compat OAuth-M2M auth params (epic open-decision #3 — PAT only
        # for v0.x). Captured so a later child can open an M2M connection without
        # a constructor-signature change; the skeleton's ``make_real_client`` uses
        # PAT (``token``) only.
        self._auth_type = auth_type
        self._client_id = client_id
        self._client_secret = client_secret

        # The Databricks connection embodies the session that scopes temp
        # objects (mirrors Snowflake DEC-002 of #122). Set on the first
        # :meth:`_get_connection`; reset to ``None`` in
        # :meth:`_cleanup_active_session` so a second ``__exit__`` is a no-op.
        self._active_session: _DatabricksClientProtocol | None = None

        # Materialised-sample tables created on the active session (issue #226).
        # Databricks rejects a qualified ``CREATE TEMPORARY TABLE`` name
        # (``[TEMP_TABLE_CREATION_REQUIRES_SINGLE_PART_NAME]``), so
        # :meth:`materialise_sample` creates a real ``CREATE OR REPLACE TABLE``
        # colocated with the source instead — which does NOT auto-reap with the
        # session. Each created ref is tracked here and explicitly
        # ``DROP TABLE IF EXISTS``-ed (fail-soft) in
        # :meth:`_cleanup_active_session` before the connection closes.
        self._materialised_tables: list[TableRef] = []

    def __repr__(self) -> str:
        # Render ONLY non-credential identifying fields. NEVER token, schema, or
        # the OAuth-M2M ``client_id`` / ``client_secret``.
        return (
            f"<DatabricksAdapter host={self._host!r} "
            f"http_path={self._http_path!r} catalog={self._catalog!r}>"
        )

    def _get_connection(self) -> _DatabricksClientProtocol:
        """Return the live Databricks connection (lazy build; mirrors Snowflake).

        Lazily builds the connection via
        :func:`signalforge.warehouse.adapters._databricks_client.make_real_client`
        from the stored PAT params on first use, caching it on
        ``self._connection``. Records the connection as ``self._active_session``
        on first open so the ``__exit__`` cleanup boundary has something to tear
        down. The SDK shim import is lazy (inside this body) so importing the
        adapter never requires ``databricks-sql-connector`` (it ships only under
        the ``[databricks]`` extra). Not exercised at the skeleton stage (every
        op raises / degrades before reaching here) but present so #224 builds the
        sampling surface on top without restructuring.
        """
        if self._connection is None:
            from signalforge.warehouse.adapters._databricks_client import make_real_client

            self._connection = make_real_client(
                host=self._host or "",
                http_path=self._http_path or "",
                token=self._token or "",
            )
        if self._active_session is None:
            self._active_session = self._connection
        return self._connection

    def __enter__(self) -> WarehouseAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Best-effort, fail-soft session cleanup (mirrors Snowflake #122
        # DEC-003). Closing the connection ends the Databricks session. Failure
        # is swallowed-and-warned; state always resets so a subsequent
        # ``__exit__`` is a no-op.
        self._cleanup_active_session()

    @staticmethod
    def _read_session_id(conn: _DatabricksClientProtocol) -> str | None:
        """Best-effort read of the connection's opaque session id.

        Read defensively — a minimal fake exposes a plain ``session_id``
        attribute (mirroring how the Snowflake adapter reads ``conn.session_id``),
        while the real ``databricks.sql`` ``Connection`` exposes
        ``get_session_id_hex()`` instead. Try the attribute first, fall back to
        the getter (wrapped so a broken connection can't raise out of the
        cleanup boundary). Returns ``None`` when neither surfaces a value.
        """
        raw = getattr(conn, "session_id", None)
        if raw is not None:
            return str(raw)
        getter = getattr(conn, "get_session_id_hex", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # noqa: BLE001 - cleanup boundary must never raise
                return None
            return None if value is None else str(value)
        return None

    def _cleanup_active_session(self) -> None:
        """Best-effort, fail-soft session cleanup (cleanup-boundary fail-soft).

        Splits out from :meth:`__exit__` so the test surface can exercise the
        cleanup path without entering an actual ``with`` block. Idempotent:
        returns immediately when ``self._active_session`` is ``None`` (mirrors
        the Snowflake / BigQuery cleanup-boundary fail-soft pattern).
        """
        conn = self._active_session
        if conn is None:
            return
        # The hashed form is used on the happy path (redaction); the raw form is
        # the deliberate narrow exception in the cleanup-failure WARNING only
        # (mirrors Snowflake #122 DEC-014).
        raw_session_id = self._read_session_id(conn)
        try:
            # Issue #226: drop materialised-sample tables (real
            # ``CREATE OR REPLACE TABLE``s that do NOT auto-reap with the
            # session) BEFORE closing the connection. Fail-soft PER table — a
            # drop failure must neither abort cleanup nor mask the close; it
            # emits ONE operator-actionable WARNING naming the exact manual
            # ``DROP`` command (a real table IS reachable outside the session,
            # unlike a session-temp object, so a manual command exists here).
            for ref in self._materialised_tables:
                try:
                    drop_cursor = conn.cursor()
                    try:
                        drop_cursor.execute(f"DROP TABLE IF EXISTS {self._quote(ref)}")
                    finally:
                        drop_cursor.close()
                except Exception as exc:  # noqa: BLE001 - cleanup-boundary swallows all
                    # Echo the EXACT executed (per-component backtick-quoted)
                    # form so the manual command is copy-paste-safe for catalogs
                    # / schemas that require quoting.
                    _LOGGER.warning(
                        "Databricks materialised-sample cleanup failed; drop it "
                        "manually:\n"
                        "  DROP TABLE IF EXISTS %s\n"
                        "  Reason: %s",
                        self._quote(ref),
                        type(exc).__name__,
                    )
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001 - cleanup-boundary swallows all
                # Cleanup-boundary fail-soft (mirrors Snowflake #122 DEC-014):
                # swallow the failure and emit ONE operator-actionable WARNING.
                # The materialised-sample tables (real CREATE OR REPLACE TABLEs)
                # were already dropped in the loop above — or, if a per-table drop
                # failed, named in a preceding WARNING with a manual DROP command
                # — so this close-failure path concerns only the SESSION itself,
                # which Databricks reaps server-side when the SQL warehouse drops
                # the idle connection. The raw ``session_id`` is the deliberate
                # narrow exception to the redaction rule so the operator can
                # correlate the orphaned session in Databricks' query history; the
                # WARNING quotes NO client-side ``auto-expire in <N>s`` countdown
                # (the reap is server-side and not locally computable).
                # ``--quiet`` does NOT suppress this WARNING (it floors at
                # WARNING). Lazy-format ``%s`` for ANSI safety.
                _LOGGER.warning(
                    "Databricks session cleanup (connection close) failed; the "
                    "session will be reaped server-side when the SQL warehouse "
                    "drops the idle connection. Any materialised-sample tables "
                    "were already dropped above (or named in a preceding WARNING "
                    "with a manual DROP command).\n"
                    "  Session ID: %s\n"
                    "  Reason: %s",
                    raw_session_id,
                    type(exc).__name__,
                )
            else:
                # Happy path — redacted INFO log. The raw ``session_id`` never
                # leaves the adapter; only the hash correlates records. Lazy-format
                # JSON for ANSI safety (warehouse-layer convention).
                payload: dict[str, str] = {}
                if raw_session_id is not None:
                    payload["session_id_hash"] = _hash_session_id(raw_session_id)
                _LOGGER.info("session closed: %s", json.dumps(payload))
        finally:
            # Reset only the session-tracking state — NOT ``self._connection``.
            # Idempotency comes from the ``_active_session is None`` early-return;
            # nulling ``self._connection`` would route a re-entry back through
            # the lazy-build branch and silently discard a test-injected fake
            # (mirrors Snowflake / BigQuery cleanup).
            self._active_session = None
            # The materialised tables were dropped above (or WARNING-ed);
            # clear the list so a second ``__exit__`` is a no-op (issue #226).
            self._materialised_tables = []

    def dialect(self) -> Dialect:
        return DATABRICKS_DIALECT

    # ------------------------------------------------------------------
    # get_row_count + sizing + sample_rows — DEC-002 / DEC-003 / DEC-008
    # of issue #224.
    # ------------------------------------------------------------------

    @staticmethod
    def _fold(identifier: str) -> str:
        """Case-fold one identifier per :attr:`Dialect.identifier_case` (DEC-008).

        Mirrors the prune compiler's ``_fold_identifier`` (and the Snowflake
        adapter's ``_fold``) so the adapter and the compiler quote identifiers
        byte-identically — load-bearing for the ``materialised`` strategy (the
        temp table the adapter CREATEs and the compiler REFERENCEs must fold to
        the same case, since quoted identifiers are case-sensitive). Databricks
        folds unquoted identifiers to **lowercase** (Unity Catalog), the
        opposite of Snowflake's ``"upper"``. Folds an already-validated ASCII
        identifier (validated on :class:`TableRef` / :class:`PartitionFilter`
        construction), so it cannot introduce a quote-breaking character.
        """
        case = DATABRICKS_DIALECT.identifier_case
        if case == "upper":
            return identifier.upper()
        if case == "lower":
            return identifier.lower()
        return identifier

    def _quote_identifier(self, identifier: str) -> str:
        """Fold-then-quote ONE identifier with the dialect's backtick (DEC-008)."""
        qc = DATABRICKS_DIALECT.quote_char
        return f"{qc}{self._fold(identifier)}{qc}"

    def _quote(self, ref: TableRef) -> str:
        """Render a fully-qualified Databricks table identifier (DEC-008).

        Unity Catalog three-part names are quoted **per component**
        (`` `catalog`.`schema`.`table` ``) because
        :attr:`Dialect.quote_qualified_per_component` is ``True`` — a single
        backtick-quoted string spanning dots would read as ONE literal
        identifier named ``catalog.schema.table``. Two-part `` `schema`.`table` ``
        when ``project`` is ``None``.

        **Fold-then-quote, identical to the prune compiler's ``_quote`` /
        ``_fold_identifier``.** Each component is case-folded
        (``identifier_case='lower'`` for Databricks) BEFORE the backticks are
        added, so a conventionally-cased manifest identifier resolves against the
        real Unity Catalog object and the temp table the adapter CREATEs in
        :meth:`materialise_sample` matches the name the compiler REFERENCEs (the
        #124 lesson — CREATE-vs-REFERENCE must never diverge).
        """
        components = (
            [ref.dataset, ref.name] if ref.project is None else [ref.project, ref.dataset, ref.name]
        )
        return ".".join(self._quote_identifier(c) for c in components)

    def _render_partition_filter(self, pf: PartitionFilter) -> str:
        """Render a :class:`PartitionFilter` to a Databricks SQL fragment (DEC-008).

        ``datetime`` → ``TIMESTAMP '…'``; ``date`` → ``DATE '…'`` (via the
        dialect literal templates — Spark typed-literal form); ``str`` is escaped
        via :func:`_escape_spark_string_literal` — a Databricks-local, Spark-correct
        escape (backslash doubling under the default ``escapedStringLiterals=false``
        plus quote doubling), NOT the BigQuery :func:`escape_bq_string_literal` —
        for safe inclusion inside a single-quoted literal. The column name is
        fold-then-quoted (per-component backtick) and already validated on
        :class:`PartitionFilter` construction.

        Mirrors the Snowflake adapter's ``_render_partition_filter`` and the
        prune compiler's ``_render_partition_filter(pf, dialect)`` — the
        partition predicate this method renders matches what the compiler emits
        for the deterministic sample CTE.
        """
        # ``datetime`` is a subclass of ``date``, so check it first.
        if isinstance(pf.value, datetime):
            rendered = DATABRICKS_DIALECT.timestamp_literal_template.format(
                value=pf.value.isoformat()
            )
        elif isinstance(pf.value, date):
            rendered = DATABRICKS_DIALECT.date_literal_template.format(value=pf.value.isoformat())
        else:
            rendered = f"'{_escape_spark_string_literal(str(pf.value))}'"
        return f"{self._quote_identifier(pf.column)} {pf.op} {rendered}"

    def _execute(self, sql: str, *, table: TableRef | None = None) -> list[Any]:
        """Run ``sql`` on the connection's cursor, returning ``fetchall()``.

        Any SDK exception is routed through
        :func:`signalforge.warehouse.adapters._databricks_client.map_databricks_exception`
        (DEC-009): a mapped typed error is re-raised ``from`` the original; an
        unchanged passthrough re-raises the original. ``table`` (when supplied)
        gives the mapper the ``table`` identifier for the Table/Column arms.
        """
        from signalforge.warehouse.adapters._databricks_client import map_databricks_exception

        context = {"table": table.qualified_name} if table is not None else None
        cursor = self._get_connection().cursor()
        try:
            cursor.execute(sql)
            return list(cursor.fetchall())
        except Exception as exc:
            mapped = map_databricks_exception(exc, context=context)
            if mapped is exc:
                raise
            raise mapped from exc
        finally:
            # Release the server-side cursor handle on both the success and
            # failure paths so repeated queries on the long-lived connection
            # don't leak cursors.
            cursor.close()

    def _execute_to_dicts(self, sql: str, *, table: TableRef | None = None) -> list[dict[str, Any]]:
        """Run ``sql`` and shape tuple ``fetchall()`` rows into dicts (DEC-002).

        Reads ``cursor.description`` (DB-API: each descriptor's ``[0]`` is the
        column name) so the adapter builds ``dict`` rows from tuple results
        without depending on a dict-cursor. SDK errors route through
        :func:`map_databricks_exception` (DEC-009).
        """
        from signalforge.warehouse.adapters._databricks_client import map_databricks_exception

        context = {"table": table.qualified_name} if table is not None else None
        cursor = self._get_connection().cursor()
        try:
            try:
                cursor.execute(sql)
                rows = list(cursor.fetchall())
            except Exception as exc:
                mapped = map_databricks_exception(exc, context=context)
                if mapped is exc:
                    raise
                raise mapped from exc
            # _rows_to_dicts reads cursor.description, so shape the rows BEFORE
            # the finally closes the cursor.
            return self._rows_to_dicts(cursor, rows)
        finally:
            cursor.close()

    @staticmethod
    def _rows_to_dicts(cursor: _DatabricksCursorProtocol, rows: list[Any]) -> list[dict[str, Any]]:
        """Build dict rows from tuple ``fetchall()`` results via ``description``.

        Each DB-API descriptor's element ``[0]`` is the column name. A row that
        is already a mapping passes through unchanged (defensive against a
        dict-cursor-style connection). Mirrors the Snowflake adapter's
        ``_rows_to_dicts``.
        """
        description = cursor.description
        column_names = [desc[0] for desc in description] if description else []
        result: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                result.append(dict(row))
            else:
                result.append(dict(zip(column_names, row, strict=False)))
        return result

    def get_row_count(self, table: TableRef) -> int | None:
        """Return the row count for ``table`` via ``SELECT COUNT(*)``, or ``None``
        when it cannot be determined (DEC-003).

        Overrides the ABC default (which raises
        :class:`RowCountNotSupportedError`). This is the seam
        :func:`signalforge.prune.engine._resolve_sample_bucket` calls to size the
        deterministic-sample bucket under ``prune.scope: sample``.

        **Why ``COUNT(*)`` and not metadata** (DEC-003): ``DESCRIBE DETAIL`` has
        no reliable top-level ``numRows`` (it lives in the Delta ``statistics``
        map, populated only after ``ANALYZE TABLE COMPUTE STATISTICS`` →
        commonly NULL/stale), and ``information_schema.tables`` carries no
        ``row_count``. ``SELECT COUNT(*)`` is the reliable source — metadata-only
        / cheap on Delta tables.

        Any :class:`WarehouseError` (a non-countable target, a mapped SDK error)
        returns ``None`` so the shared :meth:`_resolve_sample_bucket` fail-loud
        sizing decides what to do (unknown + no filter → fail loud; unknown +
        filter → ``bucket=1000``). A non-:class:`WarehouseError` (an unmapped
        transient blip) propagates unchanged.
        """
        sql = f"SELECT COUNT(*) AS row_count FROM {self._quote(table)}"
        try:
            rows = self._execute(sql, table=table)
        except WarehouseError:
            return None
        if not rows:
            return None
        first = rows[0]
        if isinstance(first, dict):
            value = next(iter(first.values()), None)
        elif isinstance(first, (list, tuple)):
            value = first[0]
        else:
            value = first
        if value is None:
            return None
        return int(value)

    def _resolve_sample_bucket(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None,
    ) -> int:
        """Size the deterministic-sample bucket via the fail-loud sizing pathway
        (DEC-003), mirroring the Snowflake / BigQuery adapters exactly:

        * row count unknown + no ``partition_filter`` →
          :class:`UnknownTableSizeError`.
        * row count unknown + ``partition_filter`` present → ``bucket = 1000``
          (DEBUG-logged fallback).
        * row count ``>= _LARGE_TABLE_THRESHOLD`` + no ``partition_filter`` →
          :class:`SamplingRequiresPartitionFilterError`.
        * else → ``bucket = max(num_rows // n, 1)``.

        Row count comes from :meth:`get_row_count` (``SELECT COUNT(*)``), which
        returns ``None`` on a :class:`WarehouseError`; ``None`` and ``0`` route
        through the same unknown-size pathway (mirrors BigQuery's
        ``num_rows == 0`` branch).
        """
        num_rows = self.get_row_count(table)
        if num_rows is None or num_rows == 0:
            if partition_filter is None:
                raise UnknownTableSizeError(table=table.qualified_name)
            _LOGGER.debug(
                "Sampling table with unknown num_rows; using bucket=1000 (table=%s)",
                table.qualified_name,
            )
            return 1000
        if num_rows >= _LARGE_TABLE_THRESHOLD and partition_filter is None:
            raise SamplingRequiresPartitionFilterError(
                table=table.qualified_name, num_rows=num_rows
            )
        return max(num_rows // n, 1)

    def sample_rows(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
    ) -> list[dict[str, Any]]:
        """Sample up to ``n`` rows deterministically (DEC-002 / DEC-003 / DEC-008).

        Algorithm (mirrors the Snowflake / BigQuery ``sample_rows``,
        Databricks-flavoured):

        1. Reject ``n <= 0`` with :class:`ValueError` before any warehouse
           contact.
        2. Size the bucket via :meth:`_resolve_sample_bucket` (``SELECT
           COUNT(*)`` + the shared fail-loud sizing).
        3. Emit the deterministic sample SELECT via the shared
           :func:`signalforge.warehouse._sample_sql.render_sample_select` helper
           (``order_by_hash=True``). For Databricks
           (``sample_hash_in_projection=True``, #226) this is the
           **projection-subquery** shape — Spark rejects ``struct(*)`` in a Sort
           node (``[INVALID_USAGE_OF_STAR_OR_REGEX] Invalid usage of '*' in
           Sort``), so the masked ``xxhash64`` hash is computed once in an inner
           projection alias and the outer ``WHERE``/``ORDER BY`` reference it::

               SELECT * EXCEPT (_sf_sample_hash) FROM
               (SELECT t.*, (xxhash64(to_json(struct(*))) & 9223372036854775807)
                       AS _sf_sample_hash FROM `cat`.`sch`.`tbl` AS t)
               WHERE MOD(_sf_sample_hash, <bucket>) < 1
                 [AND <partition_filter>]
               ORDER BY _sf_sample_hash
               LIMIT n

        The hash-mod approach is deterministic across runs (same input → same
        prune decision) and works on views / CTEs where ``TABLESAMPLE`` does not.
        The hash expression and sample SHAPE are read from
        :data:`DATABRICKS_DIALECT` (NEVER hard-coded) so the adapter's sample SQL
        is byte-consistent with the prune compiler's sample CTE (Architectural
        Commitment #5). The ``ORDER BY`` makes ``LIMIT`` truncation deterministic.
        The partition filter is rendered by the adapter's own
        :meth:`_render_partition_filter` and passed to the helper as
        ``extra_where``.
        """
        if n <= 0:
            raise ValueError(f"sample_rows requires n > 0; got n={n}")

        bucket = self._resolve_sample_bucket(table, n, partition_filter=partition_filter)

        quoted = self._quote(table)
        extra_where = (
            self._render_partition_filter(partition_filter)
            if partition_filter is not None
            else None
        )
        sql = render_sample_select(
            quoted,
            dialect=DATABRICKS_DIALECT,
            sample_bucket=bucket,
            sample_size=n,
            extra_where=extra_where,
            order_by_hash=True,
        )

        return self._execute_to_dicts(sql, table=table)

    # ------------------------------------------------------------------
    # materialise_sample — DEC-004 / DEC-006 / DEC-008 of issue #224.
    # ------------------------------------------------------------------

    def materialise_sample(
        self,
        table: TableRef,
        n: int,
        *,
        partition_filter: PartitionFilter | None = None,
        ttl_seconds: int = 3600,
    ) -> TableRef:
        """Materialise a deterministic sample into a Databricks table
        (``CREATE OR REPLACE TABLE`` colocated with the source); return a
        :class:`TableRef` pointing at it (DEC-004; revised live by issue #226 —
        see the note below on why a real table, not ``TEMPORARY TABLE``).

        Overrides the ABC default (which raises
        :class:`MaterialisationNotSupportedError`). Mirrors the Snowflake
        adapter's :meth:`materialise_sample` (issue #122) verbatim, Databricks-
        flavoured:

        DEC-008 — the ``run_id`` reuses the shared
        :func:`signalforge.warehouse._sample_id._compute_run_id` recipe so the
        temp-table name ``_sf_sample_<run_id>`` is byte-identical to the
        BigQuery / Snowflake adapters' for the same ``(table, n,
        partition_filter)`` tuple under the same ``signalforge.__version__``.

        DEC-006 — the materialised table is created on the live connection.
        The connection is pinned as ``self._active_session`` (via
        :meth:`_get_connection`) so a subsequent :meth:`run_test_sql` on the
        same connection reaches it. (It is a real ``CREATE OR REPLACE TABLE``,
        NOT a session-local temp — see the note below — so it persists until the
        explicit ``DROP`` at cleanup rather than auto-reaping with the session.)

        DEC-004 — the deterministic sample SELECT body is built by the shared
        :func:`signalforge.warehouse._sample_sql.render_sample_select` helper
        (``order_by_hash=True``), which for Databricks
        (``sample_hash_in_projection=True``, #226) emits the projection-subquery
        shape (Spark rejects ``struct(*)`` in a Sort node, so the hash is
        computed in an inner projection alias referenced by ``WHERE``/``ORDER
        BY``). The hash expression and shape are read from :data:`DATABRICKS_DIALECT`
        (NOT hard-coded) so the CTAS bytes stay consistent with
        :meth:`sample_rows` and the prune compiler's sample CTE (Architectural
        Commitment #5). ``partition_filter`` lands ONCE here, in the CTAS
        ``WHERE`` (rendered by :meth:`_render_partition_filter`, passed to the
        helper as ``extra_where``).

        The materialised table is colocated with the SOURCE (created as
        ``<source catalog>.<source schema>._sf_sample_<run_id>``, per-component
        backtick-quoted + fold-to-lower, identical to how the compiler will
        REFERENCE it) and the returned :class:`TableRef` is fully-qualified via
        the source catalog / schema.

        .. note::

            **Certified live against Databricks Free Edition (issue #226).** The
            live run proved Databricks REJECTS a qualified ``CREATE TEMPORARY
            TABLE <cat>.<sch>.<temp>`` name
            (``[TEMP_TABLE_CREATION_REQUIRES_SINGLE_PART_NAME]``), and a
            :class:`TableRef` cannot express a bare single-part name (``dataset``
            is required, so the compiler always emits a qualified reference).
            The shipped behaviour is therefore the documented fallback (plan
            DEC-004): a real ``CREATE OR REPLACE TABLE`` colocated with the
            source — reachable by its qualified name from a follow-up
            :meth:`run_test_sql` on the same connection, and explicitly
            ``DROP TABLE IF EXISTS``-ed (fail-soft) in
            :meth:`_cleanup_active_session`, since a real table does NOT
            auto-reap with the session. The ``databricks-sql-connector`` DOES
            persist the session across queries (also certified live).

        Args:
            table: Source production table to sample from.
            n: Target sample size; bucket sizing mirrors :meth:`sample_rows`
                (deterministic hash-mod, fail-loud size guards).
            partition_filter: Optional :class:`PartitionFilter` applied ONCE
                inside the CTAS ``WHERE`` clause.
            ttl_seconds: accepted for ABC parity but IGNORED by Databricks —
                there is no client-side TTL knob. The materialised table is
                dropped explicitly at the session-cleanup boundary
                (:meth:`_cleanup_active_session`); if that drop fails, the
                cleanup WARNING names the exact manual ``DROP`` command (issue
                #226 — a real table is reachable outside the session).

        Returns:
            :class:`TableRef` with ``project=table.project``,
            ``dataset=table.dataset``, ``name="_sf_sample_<run_id>"`` — the
            materialised table (a real ``CREATE OR REPLACE TABLE``, NOT
            session-scoped; dropped explicitly at cleanup), fully-qualified via
            the source catalog / schema.

        Raises:
            ValueError: ``n <= 0``.
            MaterialisationFailedError: any SDK / network / quota failure during
                the CTAS (wraps the original via ``cause=``).
            UnknownTableSizeError, SamplingRequiresPartitionFilterError:
                propagated from :meth:`_resolve_sample_bucket` (the shared
                fail-loud sizing contract).
        """
        if n <= 0:
            raise ValueError(f"materialise_sample requires n > 0; got n={n}")

        # DEC-008 — byte-identical recipe to BigQuery / Snowflake (shared helper).
        run_id = _compute_run_id(table=table, n=n, partition_filter=partition_filter)
        temp_name = f"_sf_sample_{run_id}"
        # The temp-table identifier MUST pass validate_identifier before
        # quoting. blake2b-8 lowercase hex is alphanumeric so the regex always
        # passes; the explicit call documents the contract and catches any
        # future drift in _compute_run_id.
        validate_identifier("temp_table_name", temp_name)

        # Shared fail-loud sizing pathway (same as sample_rows; DEC-003).
        bucket = self._resolve_sample_bucket(table, n, partition_filter=partition_filter)

        quoted_source = self._quote(table)
        # The materialised table is colocated with the source (DEC-004): same
        # catalog / schema, per-component fold-then-quote, deterministic name.
        temp_ref = TableRef(project=table.project, dataset=table.dataset, name=temp_name)
        quoted_temp = self._quote(temp_ref)

        extra_where = (
            self._render_partition_filter(partition_filter)
            if partition_filter is not None
            else None
        )
        # Projection-subquery sample body (#226, sample_hash_in_projection=True):
        # the masked xxhash64 whole-row hash is computed once in an inner
        # projection alias and referenced from WHERE / ORDER BY — Spark rejects
        # struct(*) in a Sort node. Read from the dialect, not hard-coded.
        select_body = render_sample_select(
            quoted_source,
            dialect=DATABRICKS_DIALECT,
            sample_bucket=bucket,
            sample_size=n,
            extra_where=extra_where,
            order_by_hash=True,
        )
        # Issue #226 (live-cert): Databricks rejects a *qualified* temp-table
        # name in ``CREATE TEMPORARY TABLE <cat>.<sch>.<temp>``
        # (``[TEMP_TABLE_CREATION_REQUIRES_SINGLE_PART_NAME]``), and a TableRef
        # cannot express a bare single-part name (``dataset`` is required), so we
        # materialise into a real ``CREATE OR REPLACE TABLE`` colocated with the
        # source (the documented fallback). It is reachable by the qualified name
        # from a follow-up ``run_test_sql`` on the same connection, and is
        # explicitly dropped in :meth:`_cleanup_active_session` (it does NOT
        # auto-reap with the session like a true TEMP TABLE would).
        sql = f"CREATE OR REPLACE TABLE {quoted_temp} AS {select_body}"

        # Track the table BEFORE issuing the CTAS (issue #226): a real
        # CREATE OR REPLACE TABLE does NOT auto-reap, and ``cursor.execute`` can
        # raise AFTER the server has committed the table (e.g. a client
        # read-timeout / network blip over the Thrift-HTTP transport once the
        # Delta CTAS has landed). Tracking after a successful execute would leak
        # that committed table with no DROP and no operator WARNING. Tracking
        # first closes the window — ``DROP TABLE IF EXISTS`` at cleanup is a
        # harmless no-op when the table was never created.
        #
        # Concurrency caveat (v0.x known limitation): ``temp_name`` is the SHARED
        # deterministic ``_compute_run_id`` recipe, so two concurrent SignalForge
        # runs against the same ``(table, n, partition_filter)`` on the same
        # catalog collide on this name. Unlike BigQuery (``_SESSION`` dataset) and
        # Snowflake (``CREATE TEMPORARY TABLE``), a Databricks materialised sample
        # is a REAL globally-visible table, so one run's cleanup ``DROP`` can
        # remove a concurrent run's sample mid-prune. The failure is SAFE — the
        # affected ``run_test_sql`` hits table-not-found → ``WarehouseError`` →
        # the conservative ``kept-without-evidence`` degrade (the content is
        # identical anyway: ``CREATE OR REPLACE`` with the same deterministic
        # SELECT yields the same rows). A per-session suffix would avoid the
        # collision but break the load-bearing ``compiled_sql`` audit
        # reproducibility invariant (#22: same inputs → byte-stable temp name
        # across runs), so it is deliberately NOT applied; operators running
        # concurrent Databricks prunes against the same model should serialise or
        # vary ``prune.sample_size``. See ``docs/warehouse-adapter-ops.md``.
        if temp_ref not in self._materialised_tables:
            self._materialised_tables.append(temp_ref)

        # Open / reuse the connection (also sets self._active_session) so the
        # follow-up run_test_sql reaches the materialised table (DEC-006).
        conn = self._get_connection()
        from signalforge.warehouse.adapters._databricks_client import map_databricks_exception

        cursor = conn.cursor()
        try:
            cursor.execute(sql)
        except Exception as exc:
            # DEC-009 / DEC-004 — route the SDK failure through the Databricks
            # exception mapper first, then wrap in the typed
            # MaterialisationFailedError (mirrors Snowflake / BigQuery). The
            # raise-from chain preserves the original SDK exception via
            # __cause__.
            mapped = map_databricks_exception(exc, context={"table": table.qualified_name})
            cause: BaseException = mapped if mapped is not exc else exc
            raise MaterialisationFailedError(
                message=f"sample materialisation failed for {table.qualified_name}: {cause}",
                cause=cause,
            ) from exc
        finally:
            # Release the cursor handle (the materialised table lives in the
            # source schema, not on the cursor, so it stays reachable).
            cursor.close()

        # INFO log uses the HASHED session id, never the raw value. Lazy-format
        # JSON for ANSI safety (warehouse-layer convention).
        raw_session_id = self._read_session_id(conn)
        payload: dict[str, Any] = {
            "table": table.qualified_name,
            "sample_rows": n,
            "run_id": run_id,
        }
        if raw_session_id is not None:
            payload["session_id_hash"] = _hash_session_id(raw_session_id)
        _LOGGER.info("materialised sample: %s", json.dumps(payload))

        return temp_ref

    def column_stats(self, table: TableRef, column: str) -> ColumnStats:
        """Return an aggregate profile for one column (DEC-011 of issue #224).

        Overrides the v0.x ``NotImplementedError`` stub. Databricks implements
        ``column_stats`` AHEAD of Snowflake (which stubs it); the Snowflake-parity
        decision is tracked as GitHub issue #258.

        A SINGLE aggregate query over the fold-then-quoted table computes the
        :class:`ColumnStats` contract for the fold-then-quoted column — mirroring
        :meth:`BigQueryAdapter.column_stats`'s semantics in Spark SQL:

        * ``count`` — ``COUNT(<col>)`` (NON-null count, matching BigQuery).
        * ``distinct`` — ``COUNT(DISTINCT <col>)``.
        * ``nulls`` — ``COUNT_IF(<col> IS NULL)`` (Spark's ``COUNTIF`` analogue).
        * ``min`` / ``max`` — ``MIN(<col>)`` / ``MAX(<col>)``, then **nulled out
          for complex Spark types** (R1/DEC-001 of issue #227). The aggregate
          emits ``MIN``/``MAX`` unconditionally (``data_type`` is derived inline
          via ``typeof`` in the same single round-trip, so the type isn't known
          before the query is built), but AFTER the row returns a pure
          post-process sets ``min = max = None`` when :func:`_is_complex_spark_type`
          matches the returned ``data_type`` — honouring the same
          :class:`ColumnStats` DEC-016 contract BigQuery does (skip MIN/MAX for
          ARRAY / STRUCT / MAP / BINARY / VARIANT). No extra query / round-trip.
          Scalar columns keep the byte-identical pass-through path; the
          empty-table case (``data_type == ""``) is not complex, so its MIN/MAX
          are preserved.
        * ``data_type`` — ``MAX(typeof(<col>))`` (Spark's DDL type string; an
          empty table yields ``NULL`` → coerced to ``""``, matching BigQuery's
          "type unknown → empty string" precedent).

        Unlike BigQuery (DEC-008/DEC-025), there is NO context-manager batching:
        the call runs its own single round-trip on
        :meth:`_get_connection`'s connection, mirroring this adapter's
        :meth:`sample_rows` / :meth:`run_test_sql` / :meth:`get_row_count`. The
        column is validated by
        :func:`signalforge.warehouse._sql_safety.validate_identifier` before it
        reaches the SQL string (DEC-013); SDK errors route through
        :func:`map_databricks_exception` (via :meth:`_execute_to_dicts`).

        .. note::

            Real-Spark ``column_stats`` on a **scalar** column is **certified
            live (#226)** — ``tests/warehouse/test_databricks_prune_live.py``
            asserts ``count`` / ``distinct`` / ``nulls`` / ``data_type`` against
            the real rig. Complex-type ``MIN`` / ``MAX`` handling
            (R1/DEC-001 of issue #227: nulled out for array / struct / map /
            binary / variant, matching the BigQuery :class:`ColumnStats`
            DEC-016 contract) is a pure post-process pinned by offline unit
            tests; the live pass exercised only scalar columns, so the
            scalar-column path is the supported, live-certified surface for
            v0.x.
        """
        validate_identifier("column", column)

        quoted_col = self._quote_identifier(column)
        sql = (
            f"SELECT COUNT({quoted_col}) AS non_null_count, "
            f"COUNT(DISTINCT {quoted_col}) AS distinct_count, "
            f"COUNT_IF({quoted_col} IS NULL) AS null_count, "
            # MIN/MAX are emitted unconditionally, then nulled out for complex
            # Spark types (array/struct/map/binary/variant) in a pure
            # post-process after the row returns (R1/DEC-001; see docstring).
            f"MIN({quoted_col}) AS min_value, "
            f"MAX({quoted_col}) AS max_value, "
            f"MAX(typeof({quoted_col})) AS data_type "
            f"FROM {self._quote(table)}"
        )

        rows = self._execute_to_dicts(sql, table=table)
        if not rows:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError(f"column_stats aggregate returned no rows for table {table}")

        # Databricks folds the unquoted aliases to lower, but resolve
        # case-insensitively so a fake / dict-cursor that preserved case works too.
        lowered = {str(k).lower(): v for k, v in rows[0].items()}
        raw_type = lowered.get("data_type")
        data_type = str(raw_type) if raw_type is not None else ""

        # R1/DEC-001 (issue #227): honour the ColumnStats DEC-016 contract —
        # MIN/MAX is not meaningful on complex Spark types (array/struct/map/
        # binary/variant), so null them out. This is a PURE POST-PROCESS on the
        # already-fetched ``data_type`` (no extra query / round-trip). The
        # empty-table case (data_type == "") is NOT complex, so its MIN/MAX are
        # preserved. Scalar columns keep the byte-identical pass-through path.
        min_value = lowered.get("min_value")
        max_value = lowered.get("max_value")
        if _is_complex_spark_type(data_type):
            min_value = None
            max_value = None

        return ColumnStats(
            count=int(lowered["non_null_count"]),
            distinct=int(lowered["distinct_count"]),
            nulls=int(lowered["null_count"]),
            min=min_value,
            max=max_value,
            data_type=data_type,
        )

    # ------------------------------------------------------------------
    # run_test_sql — DEC-007 of issue #224.
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_failure_row(row: dict[str, Any]) -> dict[str, Any]:
        """Parse one captured failing row (DEC-007).

        Each captured row carries a single ``to_json(struct(*))`` column whose
        value is a JSON **string** (Spark's ``to_json`` returns a string per
        row — NOT a VARIANT / array like Snowflake's ``OBJECT_CONSTRUCT``, so
        each value is ``json.loads``-ed individually, never a single outer
        decode). A connection that already vended a parsed mapping (a fake / a
        dict-cursor) passes through via :func:`dict`.
        """
        value: Any = next(iter(row.values()), None)
        parsed: Any = json.loads(value) if isinstance(value, str) else value
        return dict(parsed)

    def run_test_sql(self, sql: str, *, capture_failures: int = 0) -> TestResult:
        """Run a candidate failing-rows SELECT and return a typed
        :class:`TestResult` (DEC-007).

        Overrides the :class:`NotImplementedError` stub. The candidate is
        sanity-checked by
        :func:`signalforge.warehouse._sql_safety.validate_test_sql` (no ``;``,
        no ``--`` comments, balanced parens) before wrapping.

        The failing-row ``COUNT(*)`` is always computed via
        ``SELECT COUNT(*) AS failures FROM (<sql>) AS t``. When
        ``capture_failures > 0`` a SECOND query captures up to
        ``capture_failures`` example failing rows via Spark's
        ``SELECT to_json(struct(*)) AS failure_row FROM (<sql>) AS s LIMIT
        <capture_failures>`` — per-row ``to_json`` (NOT ``collect_list`` →
        ``ARRAY<STRING>``), so each row's JSON string is ``json.loads``-ed
        individually (DEC-007). Columns resolve case-insensitively via
        ``cursor.description`` (Databricks folds unquoted aliases to lower).

        Both queries execute on ``self._active_session`` (the connection
        :meth:`_get_connection` returns / a prior :meth:`materialise_sample`
        pinned), so a materialised temp table is reachable. SDK errors route
        through :func:`map_databricks_exception` (via :meth:`_execute_to_dicts`);
        ``row_schema`` is ``None`` in v0.x (mirrors BigQuery / Snowflake).

        .. note::

            The COUNT(*) failing-rows wrap is **certified live (#226)** (the
            prune / e2e live tests run it against the real rig). The
            ``to_json(struct(*))`` per-row CAPTURE branch (and its JSON-string
            marshalling assumption) was NOT exercised by the live pass — the
            engineered always-pass tests return 0 failing rows, so the capture
            branch never fires — and remains shape-only (fake + sqlglot parse).
        """
        validate_test_sql(sql)

        count_rows = self._execute_to_dicts(f"SELECT COUNT(*) AS failures FROM ({sql}) AS t")
        if not count_rows:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError("run_test_sql COUNT wrapper returned no rows")
        # Databricks folds the unquoted ``failures`` alias to lower, but resolve
        # case-insensitively so a fake / dict-cursor that preserved case works too.
        lowered = {str(k).lower(): v for k, v in count_rows[0].items()}
        failure_count = int(lowered["failures"])

        sample_failures: list[dict[str, Any]] | None
        if capture_failures > 0:
            capture_rows = self._execute_to_dicts(
                f"SELECT to_json(struct(*)) AS failure_row "
                f"FROM ({sql}) AS s LIMIT {capture_failures}"
            )
            sample_failures = [self._parse_failure_row(r) for r in capture_rows]
        else:
            sample_failures = None

        return TestResult(
            passed=(failure_count == 0),
            failure_count=failure_count,
            sample_failures=sample_failures,
            row_schema=None,
        )

    # ------------------------------------------------------------------
    # estimate_query_bytes — DEC-002 / DEC-008 / DEC-009 / DEC-012 of #225.
    # ------------------------------------------------------------------

    def _execute_scalar(self, sql: str) -> Any:
        """Run ``sql`` and return the first row's first cell (DEC-009).

        A no-:class:`TableRef`-in-scope sibling of :meth:`_execute` — the
        ``--estimate`` path has only the caller-supplied SQL, no table context.
        Keeps ONE cursor-handling path per operation while passing an empty
        ``context`` to :func:`map_databricks_exception`: a mapped typed error is
        re-raised ``from`` the original; an unchanged passthrough re-raises the
        original.

        Closes the cursor in a ``finally`` on both the success and failure paths
        (the #224 cursor-leak convention) so repeated estimate calls on the
        long-lived connection don't leak server-side handles.

        Returns ``None`` when the query produced no rows (the caller decides
        whether that is a degrade — :meth:`estimate_query_bytes` treats an empty
        result as an unparseable estimate).
        """
        from signalforge.warehouse.adapters._databricks_client import map_databricks_exception

        cursor = self._get_connection().cursor()
        try:
            try:
                cursor.execute(sql)
                rows = list(cursor.fetchall())
            except Exception as exc:
                mapped = map_databricks_exception(exc, context={})
                if mapped is exc:
                    raise
                raise mapped from exc
        finally:
            cursor.close()
        if not rows:
            return None
        first = rows[0]
        # Normalise a row to its first cell. A dict-cursor-style connection hands
        # back mapping rows (e.g. {"plan": "<text>"}); returning the whole dict
        # would feed the ROW (not the plan text) to the parser and trip a false
        # degrade, so extract the first value for mappings too.
        if isinstance(first, dict):
            return next(iter(first.values()), None)
        return first[0] if isinstance(first, (list, tuple)) else first

    def estimate_query_bytes(self, sql: str) -> int:
        """Estimate bytes Databricks/Spark would scan for ``sql`` via
        ``EXPLAIN COST`` (DEC-002 / DEC-008 / DEC-009 / DEC-012 of issue #225).

        Overrides the ABC default (which raises
        :class:`EstimateNotSupportedError`). Mirrors
        :meth:`SnowflakeAdapter.estimate_query_bytes`'s shape:

        1. Validate the caller-supplied SQL via
           :func:`signalforge.warehouse._sql_safety.validate_test_sql` FIRST
           (no ``;``, no ``--`` comments, balanced parens). The ``EXPLAIN COST ``
           prefix is trusted constant text prepended AFTER validation (DEC-008),
           so it never trips the user-SQL rejects.
        2. Run ``EXPLAIN COST <validated-sql>`` through the shared
           cursor-handling helper (:meth:`_execute_scalar`); SDK failures route
           through :func:`map_databricks_exception` (DEC-009). The ``--estimate``
           engine catches the mapped :class:`WarehouseError` as a supplementary
           failure and degrades to a price-only preview.
        3. Hand the single-row / single-cell plan-text result to the pure
           :func:`_parse_explain_cost_bytes` parser, which reads the MAX
           ``Statistics(sizeInBytes=...)`` across plan nodes (DEC-002 / DEC-003).

        An empty result (no rows) is an unparseable estimate →
        :class:`EstimateUnavailableError` (NEVER a fabricated number / ``0``).

        .. note::

            ``EXPLAIN COST`` against a live Databricks SQL warehouse is
            **certified live (#226)** — ``tests/warehouse/test_databricks_estimate_live.py``
            runs it against the real rig and asserts a positive int. ``sizeInBytes``
            *accuracy* still depends on CBO / ``ANALYZE TABLE`` stats freshness
            (planner-estimate caveat).
        """
        validate_test_sql(sql)

        cell = self._execute_scalar(f"EXPLAIN COST {sql}")
        if cell is None:
            raise EstimateUnavailableError(detail="EXPLAIN COST returned no rows")
        return _parse_explain_cost_bytes(cell)


__all__ = ["DATABRICKS_DIALECT", "DatabricksAdapter"]
