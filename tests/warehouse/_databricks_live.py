"""Shared gating + adapter-builder helpers for the Databricks live tests (#226).

The Databricks adapter's offline surface is already pinned against hand-rolled
fakes + an ungated ``sqlglot`` parse-guard (``tests/warehouse/_fake_databricks.py``,
``test_databricks_adapter.py``, ``test_databricks_sql_parse.py``,
``test_databricks_estimate.py``). Those certify *shape*, NOT that a live
Databricks SQL warehouse actually accepts the emitted SQL (the #121/#124/#171
lesson — a snapshot pins invalid SQL byte-for-byte). This module is the
belt-and-suspenders other half: the **shared** gate + live-adapter builder that
the ``@pytest.mark.databricks`` live tests (#226 US-003 estimate, US-004 prune,
US-005 CLI e2e) all import, so the gating logic lives in exactly one place
instead of being duplicated across each live test (the way the two Snowflake
live files each re-declare their gate).

Belt-and-suspenders gating (``.claude/rules/testing-signal.md`` § "End-to-end
gated tests"):

1. ``@pytest.mark.databricks`` on the test function — registered in
   ``pyproject.toml`` ``[tool.pytest.ini_options].markers`` and deselected by
   the default ``addopts`` (``-m '... and not databricks'``), so the default
   ``pytest`` run never collects a live test.
2. A runtime :func:`skip_reason` — when a maintainer runs
   ``pytest -m databricks`` but lacks credentials, each missing prerequisite
   surfaces as its own distinct skip-with-reason rather than a confusing
   connection error.

The required env vars mirror the ``pyproject.toml`` ``databricks`` marker
description and the repo-root ``.env`` contract:

* ``SF_RUN_DATABRICKS`` — the project-wide opt-in for "this test talks to a real
  Databricks SQL warehouse" (mirrors ``SF_RUN_BQ`` / ``SF_RUN_SNOWFLAKE``).
* ``DATABRICKS_SERVER_HOSTNAME`` / ``DATABRICKS_HTTP_PATH`` /
  ``DATABRICKS_TOKEN`` — the minimal PAT-auth connection triple consumed by
  :func:`signalforge.warehouse.adapters._databricks_client.make_real_client`
  (via ``host`` / ``http_path`` / ``token``).

``ANTHROPIC_API_KEY`` is deliberately NOT part of the shared base gate — only
the full-pipeline CLI e2e (US-005) needs an LLM key. That test layers its own
extra check on top of :func:`skip_reason` (see ``extra_required`` below) rather
than bloating the warehouse-only gate.

Run via the maintainer-only invocation (``--no-cov`` because
``--cov-fail-under`` in ``addopts`` would fail a marker-specific run)::

    export SF_RUN_DATABRICKS=1
    export DATABRICKS_SERVER_HOSTNAME=<workspace-host>
    export DATABRICKS_HTTP_PATH=<sql-warehouse-http-path>
    export DATABRICKS_TOKEN=<personal-access-token>
    uv run pytest -m databricks --no-cov

Traces to: plans/super/226-* (#226 US-003) ; epic #219.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from signalforge.warehouse import DatabricksAdapter

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The PAT-auth connection env vars ``make_real_client`` needs. The names mirror
# the ``databricks`` marker description in ``pyproject.toml`` and the repo-root
# ``.env``; they map to the adapter's ``host`` / ``http_path`` / ``token`` kwargs.
_REQUIRED_CONN_VARS = (
    "DATABRICKS_SERVER_HOSTNAME",
    "DATABRICKS_HTTP_PATH",
    "DATABRICKS_TOKEN",
)


def databricks_runs_enabled() -> bool:
    """``SF_RUN_DATABRICKS`` is set to a truthy value.

    The Databricks analogue of the ``SF_RUN_BQ`` / ``SF_RUN_SNOWFLAKE`` opt-in;
    accepts ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive).
    """
    return os.environ.get("SF_RUN_DATABRICKS", "").lower() in _TRUTHY


def skip_reason(*, extra_required: Sequence[str] = ()) -> str | None:
    """Return a skip-reason string if any required prerequisite is missing.

    Returns ``None`` only when the opt-in flag AND every required env var is
    present — the caller then proceeds to make a real Databricks call. Each
    missing prerequisite yields its own distinct reason so a maintainer running
    ``pytest -m databricks`` sees exactly what to set.

    Args:
        extra_required: additional env-var names a specific live test needs
            beyond the warehouse-connection base (e.g. ``("ANTHROPIC_API_KEY",)``
            for the full-pipeline CLI e2e). Checked after the connection vars so
            the operator fixes the connection first.
    """
    if not databricks_runs_enabled():
        return "SF_RUN_DATABRICKS=1 required (live test talks to a real Databricks SQL warehouse)"
    for var in _REQUIRED_CONN_VARS:
        if not os.environ.get(var):
            return f"{var} required (Databricks PAT-auth connection parameter for the live call)"
    for var in extra_required:
        if not os.environ.get(var):
            return f"{var} required (extra prerequisite for this live test)"
    return None


def build_live_adapter() -> DatabricksAdapter:
    """Construct a real :class:`DatabricksAdapter` from the live env vars.

    Reads the PAT-auth connection triple
    (``DATABRICKS_SERVER_HOSTNAME`` / ``DATABRICKS_HTTP_PATH`` /
    ``DATABRICKS_TOKEN``) plus optional ``DATABRICKS_CATALOG`` /
    ``DATABRICKS_SCHEMA`` context, mirroring how
    :meth:`WarehouseAdapter.from_profile` wires a parsed Databricks profile.

    Callers MUST guard with :func:`skip_reason` first — this reads the required
    vars with ``os.environ[...]`` and will ``KeyError`` if they are absent.
    """
    return DatabricksAdapter(
        host=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        token=os.environ["DATABRICKS_TOKEN"],
        # Optional context — passed through when set so a maintainer can point
        # the live call at a specific Unity Catalog catalog / schema.
        catalog=os.environ.get("DATABRICKS_CATALOG"),
        schema=os.environ.get("DATABRICKS_SCHEMA"),
    )
