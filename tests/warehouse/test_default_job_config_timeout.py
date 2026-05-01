"""``timeout_ms`` plumbing on ``_make_query_job_config`` / ``_default_job_config``.

Issue #6 US-002 (DEC-013, AR-B2) extends the single-seam ``QueryJobConfig``
factory with a ``timeout_ms`` kwarg threaded into
``QueryJobConfig.job_timeout_ms``. The prune layer (#6 US-009) is the
target caller; the warehouse adapter's own ``sample_rows`` /
``column_stats`` / ``run_test_sql`` paths leave it ``None`` so existing
behaviour stays unchanged.

The BigQuery SDK stringifies the value (the underlying property returns
``str | None``); these tests assert against the SDK-observable shape
rather than a hard-coded ``int`` so a future SDK upgrade that keeps the
value as ``int`` doesn't silently flip the contract.
"""

from __future__ import annotations

from signalforge.warehouse import BigQueryAdapter
from signalforge.warehouse.adapters._client import _make_query_job_config
from tests.warehouse._fake import FakeBigQueryClient


def test_make_query_job_config_no_timeout_leaves_unset() -> None:
    """Default ``timeout_ms=None`` keeps ``job_timeout_ms`` unset.

    Existing call sites (``sample_rows`` / ``column_stats`` /
    ``run_test_sql``) supply no ``timeout_ms``; their behaviour must
    not change. The BigQuery SDK exposes an unset timeout as ``None``.
    """
    cfg = _make_query_job_config(max_bytes_billed=100_000_000, stage="warehouse_test")
    assert cfg.job_timeout_ms is None


def test_make_query_job_config_explicit_timeout_propagates() -> None:
    """An explicit ``timeout_ms=`` propagates to ``job_timeout_ms``.

    BigQuery's ``QueryJobConfig.job_timeout_ms`` setter coerces the
    supplied ``int`` to its on-the-wire ``str`` form; assert against
    both shapes so a future SDK upgrade that keeps the value as ``int``
    doesn't silently flip the contract.
    """
    cfg = _make_query_job_config(
        max_bytes_billed=100_000_000,
        stage="warehouse_test",
        timeout_ms=30_000,
    )
    assert cfg.job_timeout_ms in (30_000, "30000")


def test_default_job_config_threads_timeout_through() -> None:
    """``BigQueryAdapter._default_job_config(timeout_ms=…)`` reaches
    ``job_timeout_ms`` through the single seam.

    The injected ``FakeBigQueryClient`` keeps the test offline; only
    the constructed ``QueryJobConfig`` is inspected.
    """
    fake = FakeBigQueryClient(project="fake_project")
    adapter = BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fake,
    )
    cfg = adapter._default_job_config(stage="warehouse_test", timeout_ms=15_000)
    assert cfg.job_timeout_ms in (15_000, "15000")


def test_existing_callers_unchanged_no_timeout_set() -> None:
    """Regression guard: existing internal call sites pass no
    ``timeout_ms`` and must continue to produce a config with no
    ``job_timeout_ms``.

    A failure here would mean the new kwarg leaked a default into the
    existing ``sample_rows`` / ``column_stats`` / ``run_test_sql``
    paths.
    """
    fake = FakeBigQueryClient(project="fake_project")
    adapter = BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fake,
    )
    cfg = adapter._default_job_config(stage="warehouse_sample")
    assert cfg.job_timeout_ms is None
