"""Gated LIVE e2e: the real ``SignalForgeGenerateOperator`` via ``dag.test()``.

This is the canonical full-stack Airflow live test (issue #236 / DEC-001). It
builds an INLINE DAG with the real ``SignalForgeGenerateOperator``
(single ``--model``, not ``--select``) against the committed Austin bikeshare
**source-as-model** fixture and drives it end-to-end through Airflow's own
:meth:`airflow.models.dag.DAG.test` (available in airflow 2.10.4) — exercising
the operator's ``execute`` → exit-code → XCom contract through Airflow's task
runner, NOT just the pure callable.

Belt-and-suspenders gating (per ``testing-signal.md``):

1. ``pytestmark = pytest.mark.airflow`` — deselected by the default ``addopts``
   ``-m 'not ... and not airflow'`` so a plain ``uv run pytest`` never imports
   Airflow. Airflow is imported ONLY inside the test via
   ``pytest.importorskip("airflow")`` — there is NO module-top ``from airflow``
   import, so the module collects cleanly without Airflow installed.
2. Per-function external-service markers (``e2e`` / ``anthropic`` / ``bigquery``).
   NOT ``cli_subprocess`` — ``dag.test()`` runs the operator in-process.
3. A runtime ``_live_skip_reason()`` over the four live gates
   (``SF_RUN_AIRFLOW=1`` + ``ANTHROPIC_API_KEY`` + ``GOOGLE_CLOUD_PROJECT`` +
   ``SF_RUN_BQ=1``) — each missing var yields a distinct skip reason so the
   maintainer sees exactly what to set.

The live leg spends real Anthropic + BigQuery budget; it self-skips otherwise.
It is maintainer-run only (the gated ``airflow`` CI job self-skips without the
live env). Run it inside the constraints-pinned Airflow venv with creds::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project> ANTHROPIC_API_KEY=sk-...
    SF_RUN_AIRFLOW=1 SF_RUN_BQ=1 \
      PYTHONPATH="$PWD/src" /path/to/.venv-airflow/bin/python \
      -m pytest tests/airflow/test_e2e_generate_operator.py -m airflow --no-cov

See ``docs/airflow-ops.md`` / ``docs/research/airflow-test-environment.md``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.cli._e2e_helpers import copy_fixture_to_tmp

pytestmark = pytest.mark.airflow

_AIRFLOW_SKIP = "Apache Airflow not installed (run inside the constraints-pinned airflow venv)"

# The subprocess driver that actually builds + runs the DAG (see its module
# docstring for why ``dag.test()`` must run in a child process for AIRFLOW_HOME
# isolation). Must agree with the marker in ``_e2e_generate_driver.py``.
_DRIVER = Path(__file__).resolve().parent / "_e2e_generate_driver.py"
_RESULT_MARKER = "__SF_E2E_RESULT__"

# Reuse the committed Austin bikeshare fixture (source-as-model aliased to the
# public ``bigquery-public-data.austin_bikeshare.bikeshare_trips`` table). Its
# natural NOT NULL columns (``trip_id`` / ``start_time``) give the
# mathematically-guaranteed always-passes drop — see the assertion comment below.
_AUSTIN_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dbt_project_austin"


def _live_skip_reason() -> str | None:
    """Return a skip-reason string if any of the four live gates is unset.

    The four-var gate mirrors the existing live Airflow e2e: ``SF_RUN_AIRFLOW``
    (opt-in to the Airflow live leg), ``ANTHROPIC_API_KEY`` (the drafter +
    grader), ``GOOGLE_CLOUD_PROJECT`` (BigQuery billing project), and
    ``SF_RUN_BQ`` (opt-in to real warehouse spend). Each missing var surfaces
    in the reason so the maintainer sees exactly what to set.
    """
    missing = [
        v
        for v in ("SF_RUN_AIRFLOW", "ANTHROPIC_API_KEY", "GOOGLE_CLOUD_PROJECT", "SF_RUN_BQ")
        if not os.environ.get(v)
    ]
    return f"live Airflow e2e needs: {', '.join(missing)}" if missing else None


@pytest.mark.e2e
@pytest.mark.anthropic
@pytest.mark.bigquery
def test_generate_operator_runs_live_via_dag_test(tmp_path: Path) -> None:
    """LIVE: the real operator runs the full pipeline end-to-end via ``dag.test()``.

    Recipe (DEC-001): copy the Austin fixture to ``tmp_path`` (committed fixture
    untouched); point ``AIRFLOW_HOME`` at a per-run temp dir + migrate its
    metadata DB; build an inline single-``--model`` DAG with the real
    ``SignalForgeGenerateOperator`` and run ``dag.test()`` in a child process
    (``_e2e_generate_driver.py``, for AIRFLOW_HOME isolation); assert the task
    succeeded AND its XCom carries non-negative tier counts with at least one
    ``dropped`` (the always-passes drop).
    """
    # Belt-and-suspenders gate #1: skip cleanly when Airflow is absent (the
    # module-top import stays airflow-free so collection always succeeds).
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)
    # Belt-and-suspenders gate #2: skip cleanly when the live env is incomplete.
    if reason := _live_skip_reason():
        pytest.skip(reason)

    # tmp_path isolation (DEC-008): the audit JSONLs + the diff/grade sidecars
    # land under the per-run temp dir, never the committed fixture.
    project_dir = copy_fixture_to_tmp(_AUSTIN_FIXTURE, tmp_path)

    # ``dag.test()`` needs an initialised Airflow metadata DB, and Airflow binds
    # its ORM engine to ``$AIRFLOW_HOME/airflow.db`` at IMPORT time — setting
    # ``AIRFLOW_HOME`` after ``import airflow`` (e.g. once a sibling gated test
    # has already imported it in the same ``-m airflow`` session) does NOT
    # repoint the engine. So: (a) point ``AIRFLOW_HOME`` at a per-run temp dir,
    # (b) migrate that DB via the CLI, and (c) run the actual DAG in a CHILD
    # process (``_e2e_generate_driver.py``) whose environment carries
    # ``AIRFLOW_HOME`` BEFORE it imports Airflow — the only reliable way to keep
    # the metadata DB under tmp (never the maintainer's ~/airflow) without
    # mutating Airflow's global ORM state in the pytest worker.
    airflow_home = tmp_path / "af"
    airflow_home.mkdir()
    child_env = {**os.environ, "AIRFLOW_HOME": str(airflow_home)}
    subprocess.run(
        [sys.executable, "-m", "airflow", "db", "migrate"],
        env=child_env,
        check=True,
        capture_output=True,
    )

    # The committed ``profiles.yml`` pins ``project: bigquery-public-data`` for
    # the regen ``dbt parse``; at query time the BigQuery client bills
    # ``profile.project``, and the maintainer cannot bill ``bigquery-public-data``.
    # Rewrite the per-run profile to bill ``GOOGLE_CLOUD_PROJECT`` (the model's
    # own ``database``/``schema`` still resolve the SOURCE table unchanged).
    # ``maximum_bytes_billed: 1 GB`` lets the materialised-sample CTAS scan the
    # ~2.27M-row source (hash-mod sampling requires a full scan). Mirrors the
    # ``test_e2e_bigquery_smoke.py`` profile rewrite.
    billing_project = os.environ["GOOGLE_CLOUD_PROJECT"]
    (project_dir / "profiles.yml").write_text(
        "austin:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        f"      project: {billing_project}\n"
        "      dataset: austin_bikeshare\n"
        "      location: US\n"
        "      maximum_bytes_billed: 1000000000\n"
    )

    # Build + run the inline single-``--model`` DAG end-to-end via ``dag.test()``
    # in the child driver (its env carries the per-run ``AIRFLOW_HOME`` from the
    # start, so its in-process engine binds to the migrated tmp DB). The child
    # prints one ``__SF_E2E_RESULT__`` line with the task state + the operator's
    # XCom dict. Anthropic/BigQuery creds + ``PYTHONPATH`` ride through ``os.environ``.
    proc = subprocess.run(
        [sys.executable, str(_DRIVER), str(project_dir)],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"driver exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    result_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_RESULT_MARKER)]
    assert result_lines, (
        f"driver produced no {_RESULT_MARKER} line\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    result = json.loads(result_lines[-1][len(_RESULT_MARKER) :].strip())

    # The task must have completed successfully.
    assert result["state"] == "success", f"expected task success; got state={result['state']!r}"

    # The operator's return value (its ``to_xcom()`` dict), carried out of the child.
    xcom = result["xcom"]
    assert isinstance(xcom, dict), f"expected an XCom dict; got {type(xcom).__name__}"

    # Every tier-count key must be present and a non-negative int.
    for key in ("kept", "kept_uncertain", "dropped", "flagged"):
        assert key in xcom, f"XCom missing tier-count key {key!r}: {sorted(xcom)}"
        assert isinstance(xcom[key], int) and xcom[key] >= 0, (
            f"XCom[{key!r}] must be a non-negative int; got {xcom[key]!r}"
        )

    # ENGINEERED DETERMINISM: at least one ``dropped`` test is mathematically
    # guaranteed. The Austin model is source-as-model aliased to the public
    # ``bikeshare_trips`` table, whose ``trip_id`` / ``start_time`` columns have
    # zero NULL rows. The LLM reliably drafts ``not_null`` on those columns; a
    # ``not_null`` over a column with zero NULLs always passes on the sample, so
    # the prune engine drops it (``reason="always-passes"``). The full
    # warehouse-side drop-reason is pinned by ``test_e2e_bigquery_smoke.py``;
    # here we assert the count reaches XCom through the operator's task path.
    assert xcom["dropped"] >= 1, (
        f"expected at least one always-passes drop in XCom (guaranteed by "
        f"not_null on natural NOT NULL trip_id/start_time); got dropped={xcom['dropped']}"
    )
