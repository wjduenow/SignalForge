"""Thin wrapper around ``google.cloud.bigquery`` to contain pyright noise.

US-008 confines every ``# pyright: ignore[...]`` comment that the
``google-cloud-bigquery`` SDK provokes to this module so the rest of the
warehouse subpackage stays type-clean. The shim exposes a duck-typed
:class:`_BQClientProtocol` matching both the real ``bigquery.Client`` and
:class:`tests.warehouse._fake.FakeBigQueryClient` (US-007), so the adapter
can call the same method signatures regardless of how it was constructed.

Three responsibilities:

* :func:`make_real_client` — lazily import ``google.cloud.bigquery`` and
  build a :class:`bigquery.Client` from ``(project, location)``; translates
  ADC failures into :class:`WarehouseAuthError`.
* :func:`map_bq_exception` — translate ``google.api_core.exceptions``
  flavours into the typed :class:`WarehouseError` subclasses.
* :func:`row_to_dict` — convert a ``bigquery.Row`` (or :class:`FakeRow`)
  to a plain ``dict`` so the adapter never returns a Google-internal type.

Observability discipline (DEC-027): no logger calls in this shim. Logging
lives in the adapter where the stage label is known.
"""

from __future__ import annotations

from typing import Any, Protocol


class _BQClientProtocol(Protocol):
    """Duck-typed surface common to ``bigquery.Client`` and ``FakeBigQueryClient``.

    Both production (``google.cloud.bigquery.Client``) and test
    (``tests.warehouse._fake.FakeBigQueryClient``) clients satisfy this
    protocol, so the adapter calls the same methods regardless of which
    one was injected. The protocol is intentionally narrow — only the
    surface the adapter actually consumes.
    """

    project: str

    def query(self, sql: str, job_config: Any = None) -> Any: ...

    def get_table(self, ref: Any) -> Any: ...

    def list_rows(self, ref: Any, max_results: int | None = None) -> Any: ...


def make_real_client(
    project: str | None, location: str | None
) -> _BQClientProtocol:  # pragma: no cover - exercised by integration tests only
    """Construct a real ``google.cloud.bigquery.Client``.

    ADC handles auth (DEC-019). If ADC is missing or the refresh fails,
    the underlying ``DefaultCredentialsError`` / ``RefreshError`` is
    translated into :class:`WarehouseAuthError` so the caller catches a
    SignalForge-typed exception.
    """
    from google.auth.exceptions import (  # type: ignore[import-not-found]
        DefaultCredentialsError,
        RefreshError,
    )
    from google.cloud import bigquery  # type: ignore[import-not-found]

    from signalforge.warehouse.errors import WarehouseAuthError

    try:
        return bigquery.Client(  # type: ignore[no-any-return]
            project=project,
            location=location,
        )
    except (DefaultCredentialsError, RefreshError) as exc:
        raise WarehouseAuthError(message=str(exc)) from exc


def make_query_job_config(*, max_bytes_billed: int, stage: str, version: str | None = None) -> Any:
    """Build a ``QueryJobConfig`` with DEC-015 defaults.

    ``use_query_cache=False`` is non-negotiable — Architectural Commitment
    #5 (explainable diffs) requires reproducibility (same input → same
    prune decision). ``labels`` carry SignalForge stage + version so v0.2
    can attribute cost via ``INFORMATION_SCHEMA.JOBS_BY_PROJECT``.

    BigQuery labels must be lowercase and may not contain ``.`` — we
    translate the package version (e.g. ``"0.1.0.dev0"``) by replacing
    every ``.`` with ``_``. When ``version`` is omitted, the helper
    resolves the running ``signalforge.__version__`` so the unit test
    can call this function without an integration-tier ``version=`` kwarg.
    """
    from google.cloud import bigquery  # type: ignore[import-not-found]

    if version is None:
        from signalforge import __version__ as _pkg_version

        version = _pkg_version

    return bigquery.QueryJobConfig(  # type: ignore[no-any-return]
        use_query_cache=False,
        maximum_bytes_billed=max_bytes_billed,
        labels={
            "signalforge_stage": stage,
            "signalforge_version": version.replace(".", "_"),
        },
    )


def map_bq_exception(exc: Exception, *, context: dict[str, Any] | None = None) -> Exception:
    """Translate ``google.api_core.exceptions`` into typed warehouse errors.

    Returns the *new* exception so the caller can ``raise mapped from exc``.
    Returns ``exc`` unchanged when no specific mapping fits — the caller
    should re-raise the original in that case rather than swallow it.

    The optional ``context`` kwarg carries adapter-side state that the
    raw Google exception doesn't expose — currently
    ``max_bytes_billed`` so :class:`BytesBilledExceededError` can render
    the actual configured cap (the alternative was a misleading
    ``limit=0`` placeholder).

    The ``google.api_core`` import is lazy so this module can be imported
    in test environments where the SDK is shimmed (or when a test injects
    a non-Google exception that we just pass through).
    """
    try:
        from google.api_core import exceptions as gae  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - google-api-core is in install_requires
        return exc

    from signalforge.warehouse.errors import (
        BytesBilledExceededError,
        ColumnNotFoundError,
        QuerySyntaxError,
        TableNotFoundError,
        WarehouseAuthError,
    )

    if isinstance(exc, gae.Forbidden):
        return WarehouseAuthError(message=str(exc))
    if isinstance(exc, gae.NotFound):
        # Real BigQuery surfaces missing columns as ``BadRequest`` /
        # "Unrecognized name", not ``NotFound`` — so every ``NotFound``
        # we see in production maps to a missing table.
        return TableNotFoundError(table=str(exc))
    if isinstance(exc, gae.BadRequest):
        msg = str(exc).lower()
        if "exceeded limit" in msg or "maximum bytes billed" in msg:
            limit = (
                int(context["max_bytes_billed"])
                if context is not None and "max_bytes_billed" in context
                else 0
            )
            return BytesBilledExceededError(job_id=None, bytes_billed=None, limit=limit)
        if "unrecognized name" in msg or ("name" in msg and "not found" in msg):
            return ColumnNotFoundError(table="?", column=str(exc))
        # All other BadRequests bucket into "your SQL is malformed" — DEC-007
        # callers can distinguish via attribute access on the typed error.
        return QuerySyntaxError(detail=str(exc))
    return exc


def row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a ``bigquery.Row`` (or :class:`FakeRow`) to a plain dict.

    Real ``bigquery.Row`` exposes ``items()``; the test fake also exposes
    ``items()``. Falling back to ``values`` (then to ``dict(row)``) keeps
    the shim resilient against both surfaces and the accidental dict-like
    that may appear in unit tests.
    """
    if hasattr(row, "items"):
        return dict(row.items())
    if hasattr(row, "values") and not callable(row.values):
        return dict(row.values)
    return dict(row)


__all__ = [
    "_BQClientProtocol",
    "make_query_job_config",
    "make_real_client",
    "map_bq_exception",
    "row_to_dict",
]
