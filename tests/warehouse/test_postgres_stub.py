"""Tests for the PostgresAdapter v0.2 stub (issue #53).

The stub exists to validate Architectural Commitment #3 — "warehouse-agnostic
by design" — by forcing the ``WarehouseAdapter`` ABC + ``from_profile``
factory through a second concrete code path. Tests pin:

* The stub's :meth:`dialect` returns a Postgres-flavoured :class:`Dialect`
  with the load-bearing flags the prune compiler keys on (``quote_char``,
  ``identifier_case``, ``supports_qualify``).
* :meth:`WarehouseAdapter.from_profile` dispatches ``type: postgres`` to
  the stub.
* The three warehouse-operation methods (``sample_rows`` /
  ``column_stats`` / ``run_test_sql``) raise :class:`NotImplementedError`
  with a message naming the ticket (#53). ``__enter__`` / ``__exit__``
  are intentionally implemented as no-ops so the ``with adapter:``
  contract works without conditional logic at the call site.
* A fixture-shaped profile YAML round-trips through :func:`load_profile`
  and out to a constructed adapter without error — exercises the seam
  end-to-end without depending on real Postgres credentials.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from signalforge.warehouse.adapters.postgres import PostgresAdapter
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.models import POSTGRES_DIALECT, Dialect, TableRef
from signalforge.warehouse.profiles import DbtProfileTarget, load_profile

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "profiles"


def _write_dbt_project(project_dir: Path, profile_name: str) -> None:
    (project_dir / "dbt_project.yml").write_text(
        f"name: signalforge_test\nversion: '1.0.0'\nconfig-version: 2\nprofile: {profile_name}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Dialect contract
# ---------------------------------------------------------------------------


def test_postgres_dialect_values() -> None:
    """The Postgres :class:`Dialect` must carry the values the prune
    compiler keys on (``prune-engine.md`` DEC-025). Drift on any of
    these would change the compiled SQL bytes silently."""
    assert isinstance(POSTGRES_DIALECT, Dialect)
    assert POSTGRES_DIALECT.name == "postgres"
    assert POSTGRES_DIALECT.quote_char == '"'
    assert POSTGRES_DIALECT.identifier_case == "lower"
    assert POSTGRES_DIALECT.supports_qualify is False


def test_dialect_method_returns_postgres_dialect() -> None:
    """:meth:`PostgresAdapter.dialect` must return the module-level
    constant, not a freshly-constructed equivalent — callers may key on
    identity for cheap dispatch."""
    adapter = PostgresAdapter()
    assert adapter.dialect() is POSTGRES_DIALECT


# ---------------------------------------------------------------------------
# Stub methods raise NotImplementedError
# ---------------------------------------------------------------------------


def test_sample_rows_raises_not_implemented() -> None:
    """The v0.2 stub raises :class:`NotImplementedError` on
    :meth:`sample_rows` and names the ticket so the v0.2 implementation
    work has a single grep target."""
    adapter = PostgresAdapter()
    table = TableRef(project=None, dataset="public", name="t")

    with pytest.raises(NotImplementedError) as exc_info:
        adapter.sample_rows(table, 100)

    assert "issue #53" in str(exc_info.value)


def test_column_stats_raises_not_implemented() -> None:
    """:meth:`column_stats` is part of the v0.2 stub surface."""
    adapter = PostgresAdapter()
    table = TableRef(project=None, dataset="public", name="t")

    with pytest.raises(NotImplementedError) as exc_info:
        adapter.column_stats(table, "id")

    assert "issue #53" in str(exc_info.value)


def test_run_test_sql_raises_not_implemented() -> None:
    """:meth:`run_test_sql` is part of the v0.2 stub surface."""
    adapter = PostgresAdapter()

    with pytest.raises(NotImplementedError) as exc_info:
        adapter.run_test_sql("SELECT 1")

    assert "issue #53" in str(exc_info.value)


# ---------------------------------------------------------------------------
# from_profile dispatch
# ---------------------------------------------------------------------------


def test_from_profile_dispatches_postgres_to_stub() -> None:
    """The factory must route ``type: postgres`` to the stub adapter
    (NOT raise :class:`UnsupportedProfileTypeError`). Operator-facing
    signal: ``NotImplementedError("…issue #53…")`` instead of "type not
    supported in v0.1"."""
    profile = DbtProfileTarget.model_validate(
        {
            "type": "postgres",
            "project": "analytics_db",
            "schema": "public",
        }
    )

    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, PostgresAdapter)
    assert adapter._dbname == "analytics_db"
    assert adapter._schema == "public"


def test_from_profile_postgres_loads_from_fixture_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a Postgres ``profiles.yml`` round-trips through
    :func:`load_profile` and :meth:`WarehouseAdapter.from_profile` to a
    constructed stub adapter without error. Pins the seam across YAML
    parse, ``DbtProfileTarget`` validation, and factory dispatch."""
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_dbt_project(project_dir, profile_name="signalforge_test_postgres")
    shutil.copy(FIXTURES / "postgres.yml", project_dir / "profiles.yml")

    target = load_profile(project_dir)

    assert target.type == "postgres"
    assert target.project == "analytics_db"
    assert target.dataset == "public"

    adapter = WarehouseAdapter.from_profile(target)
    assert isinstance(adapter, PostgresAdapter)
    assert adapter._dbname == "analytics_db"
    assert adapter._schema == "public"


# ---------------------------------------------------------------------------
# Context-manager parity with the BigQuery adapter
# ---------------------------------------------------------------------------


def test_context_manager_is_a_no_op() -> None:
    """The stub honours the ABC's ``with adapter:`` contract (DEC-013 of
    issue #22 generalised) so callers can swap a Postgres profile in for
    a BigQuery one without conditional ``with`` logic. The stub's
    ``__exit__`` is a no-op; cleanup work lands when the v0.x
    implementation does."""
    with PostgresAdapter() as adapter:
        assert isinstance(adapter, PostgresAdapter)
        assert adapter.dialect() is POSTGRES_DIALECT
