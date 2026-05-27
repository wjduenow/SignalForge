"""Tests for ``signalforge generate --estimate`` (US-005 of issue #36).

Pins the load-bearing properties of the CLI's ``--estimate`` short-circuit:

* AC-4 of the ticket: ``len(fake.messages._create_calls) == 0`` AND
  ``len(fake.messages._count_calls) >= 1``. Zero billable LLM calls.
* Happy-path exits 0 and prints the rendered estimate to stdout.
* Mutex with ``--write`` and ``--dry-run`` — argparse rejects → exit 2.
* Missing ``ANTHROPIC_API_KEY`` (or any :class:`LLMAuthError` from
  ``count_tokens``) surfaces as tier 3.
* Warehouse-bytes failure degrades to ``<unavailable: ...>`` (DEC-005)
  and exits 0; the LLM-cost half of the report still computes.
* DEC-016: no traceback ever leaks to stderr on any path.

The tests use a real :class:`FakeAnthropicClient` and a real
:class:`BigQueryAdapter`-with-:class:`FakeBigQueryClient` so the engine
runs end-to-end. The CLI's stage entry points
(``manifest.load`` / ``warehouse.load_profile`` /
``_make_warehouse_adapter`` / ``AnthropicProvider.make_client``) are patched
so no real disk / network is touched, but ``signalforge.cli._estimate``
itself is NOT patched — that's the whole point of the AC-4 contract.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from signalforge.cli import main
from signalforge.llm.errors import LLMAuthError
from signalforge.warehouse import (
    BigQueryAdapter,
    SnowflakeAdapter,
    WarehouseAdapter,
    WarehouseAuthError,
)
from tests.cli._factories import make_fake_dbt_project, make_manifest, make_model
from tests.llm._fake import FakeAnthropicClient, FakeCountTokensResponse
from tests.warehouse._fake import FakeBigQueryClient
from tests.warehouse._fake_snowflake import FakeSnowflakeConnection

_SNOWFLAKE_FIXTURES: Path = (
    Path(__file__).resolve().parents[1] / "fixtures" / "warehouse" / "snowflake"
)


def _load_snowflake_fixture(name: str) -> str:
    return (_SNOWFLAKE_FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Patch helper — install fakes for the estimate short-circuit path
# ---------------------------------------------------------------------------


def _install_estimate_patches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_anthropic: FakeAnthropicClient | None = None,
    fake_bq: FakeBigQueryClient | None = None,
    adapter: WarehouseAdapter | None = None,
) -> tuple[FakeAnthropicClient, FakeBigQueryClient]:
    """Patch the four CLI seams that the ``--estimate`` short-circuit
    consumes (manifest load, warehouse profile load, warehouse adapter
    factory, and the provider's ``make_client`` — the LLM-client seam after
    issue #135 DEC-006) with explicit fakes.

    Returns the ``(fake_anthropic, fake_bq)`` pair so the caller can
    queue ``expect_count_tokens`` / ``expect_dry_run`` expectations.

    By default ``_make_warehouse_adapter`` returns a :class:`BigQueryAdapter`
    backed by the returned ``FakeBigQueryClient``. Pass ``adapter=<obj>`` to
    substitute a different adapter (e.g. a :class:`SnowflakeAdapter` wired to a
    :class:`FakeSnowflakeConnection`) so a test can exercise the Snowflake
    EXPLAIN-based estimate path (#130 US-003) — happy or degrade. When
    ``adapter`` is supplied, ``_make_warehouse_adapter`` returns it instead
    of the BigQuery one, so no ``expect_dry_run`` expectation is consumed.
    """
    from signalforge.cli import generate as gen_mod

    model = make_model()
    manifest = make_manifest(model)

    fa = fake_anthropic if fake_anthropic is not None else FakeAnthropicClient(project="fake")
    fb = fake_bq if fake_bq is not None else FakeBigQueryClient(project="fake_project")

    resolved_adapter: WarehouseAdapter = (
        adapter
        if adapter is not None
        else BigQueryAdapter(
            project="fake_project",
            location="US",
            max_bytes_billed=100_000_000,
            client=fb,
        )
    )

    monkeypatch.setattr(gen_mod.manifest_module, "load", MagicMock(return_value=manifest))
    monkeypatch.setattr(
        gen_mod.warehouse_module, "load_profile", MagicMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        gen_mod, "_make_warehouse_adapter", MagicMock(return_value=resolved_adapter)
    )
    # DEC-006 of #135 — the ``--estimate`` short-circuit builds its concrete
    # client via ``provider_for(draft_config.provider).make_client()`` (default
    # provider "anthropic"), so patch the AnthropicProvider's ``make_client`` to
    # hand back the FakeAnthropicClient rather than a CLI helper.
    from signalforge.llm.providers import AnthropicProvider

    monkeypatch.setattr(AnthropicProvider, "make_client", lambda self: fa)

    return fa, fb


def _queue_default_count_tokens(fake: FakeAnthropicClient) -> None:
    """Queue one drafter ``count_tokens`` plus one per rubric criterion."""
    from signalforge.grade.rubric import DEFAULT_RUBRIC

    fake.expect_count_tokens(
        matching=lambda kwargs: True,
        returns=FakeCountTokensResponse(input_tokens=1000),
    )
    for _ in range(len(DEFAULT_RUBRIC)):
        fake.expect_count_tokens(
            matching=lambda kwargs: True,
            returns=FakeCountTokensResponse(input_tokens=500),
        )


# ---------------------------------------------------------------------------
# AC-4 — zero messages.create calls
# ---------------------------------------------------------------------------


def test_generate_estimate_zero_messages_create_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-4 of issue #36: the ``--estimate`` short-circuit never issues a
    billable ``messages.create`` call. At least one ``count_tokens`` is
    issued (drafter + per-criterion).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    fa, fb = _install_estimate_patches(monkeypatch)
    _queue_default_count_tokens(fa)
    fb.expect_dry_run(sql_matching=r"SELECT", returns_bytes=10_000)

    code = main(["generate", "--estimate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert len(fa.create_calls) == 0
    assert len(fa.count_calls) >= 1


# ---------------------------------------------------------------------------
# Happy-path behaviour
# ---------------------------------------------------------------------------


def test_generate_estimate_exits_zero_on_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signalforge generate --estimate <model>`` exits 0 on the happy path."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    fa, fb = _install_estimate_patches(monkeypatch)
    _queue_default_count_tokens(fa)
    fb.expect_dry_run(sql_matching=r"SELECT", returns_bytes=2048)

    code = main(["generate", "--estimate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "Traceback" not in captured.err


def test_generate_estimate_prints_rendered_estimate_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The renderer's section headers land on stdout for the happy path."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    fa, fb = _install_estimate_patches(monkeypatch)
    _queue_default_count_tokens(fa)
    fb.expect_dry_run(sql_matching=r"SELECT", returns_bytes=10_000)

    code = main(["generate", "--estimate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "Estimated draft cost:" in captured.out
    assert "Total estimated LLM cost:" in captured.out


# ---------------------------------------------------------------------------
# Argparse mutex
# ---------------------------------------------------------------------------


def test_generate_estimate_mutex_with_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--estimate --write`` is rejected by the argparse mutex → exit 2."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    code = main(["generate", "--estimate", "--write", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 2
    err_low = captured.err.lower()
    assert "--estimate" in err_low or "--write" in err_low or "not allowed" in err_low
    assert "Traceback" not in captured.err


def test_generate_estimate_mutex_with_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--estimate --dry-run`` is rejected by the argparse mutex → exit 2."""
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    code = main(["generate", "--estimate", "--dry-run", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 2
    err_low = captured.err.lower()
    assert "--estimate" in err_low or "--dry-run" in err_low or "not allowed" in err_low
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Error propagation — LLM auth (tier 3)
# ---------------------------------------------------------------------------


def test_generate_estimate_missing_anthropic_api_key_exits_tier_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ``LLMAuthError`` raised from the engine's ``count_tokens`` call
    propagates through the existing ``cmd_generate`` panic boundary and
    maps to tier 3 via ``_EXCEPTION_TO_EXIT_CODE``.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    fa, fb = _install_estimate_patches(monkeypatch)
    # First (and only) count_tokens raises LLMAuthError — mirrors the
    # "no ANTHROPIC_API_KEY" production failure mode.
    fa.expect_count_tokens(
        matching=lambda kwargs: True,
        returns=LLMAuthError("authentication failed"),
    )

    code = main(["generate", "--estimate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 3, f"stderr={captured.err}"
    assert "ERROR" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# DEC-005 — warehouse degrade on supplementary stage failure
# ---------------------------------------------------------------------------


def test_generate_estimate_warehouse_failure_degrades_to_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DEC-005: a ``WarehouseError`` from ``estimate_query_bytes`` is
    captured into ``warehouse_unavailable_reason`` by the engine; the
    CLI prints ``<unavailable: WarehouseAuthError>`` and exits 0.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    fa, fb = _install_estimate_patches(monkeypatch)
    _queue_default_count_tokens(fa)
    fb.expect_dry_run(
        sql_matching=r"SELECT", returns_bytes=WarehouseAuthError("credentials invalid")
    )

    code = main(["generate", "--estimate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "<unavailable: WarehouseAuthError>" in captured.out
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# DEC-016 — no traceback ever leaks on any path
# ---------------------------------------------------------------------------


def test_generate_estimate_no_traceback_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pins DEC-016 of ``cli-layer.md``: no traceback escapes the handler
    boundary on ANY path tested in this file. Re-asserts the property
    across the happy path, both mutex paths, the auth-error path, and
    the warehouse-degrade path within a single test for clarity.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)

    # 1. Happy path.
    fa, fb = _install_estimate_patches(monkeypatch)
    _queue_default_count_tokens(fa)
    fb.expect_dry_run(sql_matching=r"SELECT", returns_bytes=1024)
    main(["generate", "--estimate", "model.shop.customers"])
    assert "Traceback" not in capsys.readouterr().err

    # 2. Mutex with --write.
    main(["generate", "--estimate", "--write", "model.shop.customers"])
    assert "Traceback" not in capsys.readouterr().err

    # 3. Mutex with --dry-run.
    main(["generate", "--estimate", "--dry-run", "model.shop.customers"])
    assert "Traceback" not in capsys.readouterr().err

    # 4. LLM auth error.
    fa, fb = _install_estimate_patches(monkeypatch)
    fa.expect_count_tokens(
        matching=lambda kwargs: True, returns=LLMAuthError("authentication failed")
    )
    main(["generate", "--estimate", "model.shop.customers"])
    assert "Traceback" not in capsys.readouterr().err

    # 5. Warehouse degrade.
    fa, fb = _install_estimate_patches(monkeypatch)
    _queue_default_count_tokens(fa)
    fb.expect_dry_run(sql_matching=r"SELECT", returns_bytes=WarehouseAuthError("creds"))
    main(["generate", "--estimate", "model.shop.customers"])
    assert "Traceback" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# #130 US-003 — Snowflake adapter reports a real EXPLAIN-based estimate
# ---------------------------------------------------------------------------


def test_generate_estimate_snowflake_adapter_reports_real_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#130 US-003: the full ``--estimate`` short-circuit reports a REAL
    warehouse-bytes estimate when the adapter is a :class:`SnowflakeAdapter`
    wired to a fake connection returning a captured EXPLAIN cell.

    The adapter now runs ``EXPLAIN USING JSON`` and parses
    ``GlobalStats.bytesAssigned`` (replacing the #123 behaviour where it
    inherited the ABC default and raised ``EstimateNotSupportedError``). The
    command exits 0, prints a real estimate (no ``<unavailable: ...>``), and no
    traceback leaks.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    fake_conn = FakeSnowflakeConnection()
    fake_conn.expect_execute(
        matching=r"^EXPLAIN USING JSON ",
        returns=[(_load_snowflake_fixture("explain_using_json_sample.json"),)],
    )
    fa, _fb = _install_estimate_patches(monkeypatch, adapter=SnowflakeAdapter(connection=fake_conn))
    _queue_default_count_tokens(fa)

    code = main(["generate", "--estimate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "<unavailable:" not in captured.out
    assert "Total estimated warehouse: <unknown>" not in captured.out
    assert "Total estimated warehouse:" in captured.out
    assert "Traceback" not in captured.err
    fake_conn.assert_all_expectations_met()
    fa.assert_all_expectations_met()


def test_generate_estimate_snowflake_adapter_degrades_to_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#130 DEC-007: the full ``--estimate`` short-circuit degrades cleanly
    when the Snowflake EXPLAIN plan carries no parseable byte stat.

    A :class:`SnowflakeAdapter` wired to a fake connection returning a no-stat
    EXPLAIN cell raises :class:`EstimateUnavailableError` (a ``WarehouseError``
    subclass) — distinct from the retired ``EstimateNotSupportedError`` of
    #123. The estimate engine's DEC-005 conservative-bias degrade captures that
    into ``warehouse_unavailable_reason``; the renderer prints
    ``<unavailable: EstimateUnavailableError>`` and the command exits 0 — the
    LLM-cost half of the report still computes via the ``count_tokens`` calls.
    DEC-016: no traceback leaks.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    fake_conn = FakeSnowflakeConnection()
    fake_conn.expect_execute(
        matching=r"^EXPLAIN USING JSON ",
        returns=[(_load_snowflake_fixture("explain_using_json_no_stats.json"),)],
    )
    fa, _fb = _install_estimate_patches(monkeypatch, adapter=SnowflakeAdapter(connection=fake_conn))
    _queue_default_count_tokens(fa)

    code = main(["generate", "--estimate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 0, f"stderr={captured.err}"
    assert "<unavailable: EstimateUnavailableError>" in captured.out
    assert "Traceback" not in captured.err
    # Strictness: pin exact count_tokens consumption so a drift to fewer
    # calls (which would leave queued expectations unconsumed) fails loud.
    fake_conn.assert_all_expectations_met()
    fa.assert_all_expectations_met()


def test_from_profile_dispatches_snowflake_to_snowflake_adapter() -> None:
    """Pins the dispatch wiring that makes the test above meaningful:
    ``WarehouseAdapter.from_profile(<snowflake profile>)`` returns a
    :class:`SnowflakeAdapter` (issue #123 secondary assertion).

    Builds the ``DbtProfileTarget`` from the committed
    ``snowflake_password.yml`` fixture's ``dev`` output block — the same
    fields ``load_profile`` would hydrate — then exercises the
    ``profile.type == "snowflake"`` factory branch directly. Constructing
    the target from the fixture (rather than driving the full
    ``load_profile`` dbt_project.yml / profiles.yml search path) keeps the
    dispatch assertion decoupled from the profile-resolution plumbing
    already pinned in ``tests/warehouse/test_profiles.py``.
    """
    import yaml

    from signalforge.warehouse import DbtProfileTarget

    fixture = (
        Path(__file__).resolve().parents[1] / "fixtures" / "profiles" / "snowflake_password.yml"
    )
    raw = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    output = raw["signalforge_test"]["outputs"]["dev"]
    # The fixture's password is a dbt env_var() template; supply a literal so
    # the typed model validates without dbt-style rendering.
    output = {**output, "password": "s3cret"}

    profile = DbtProfileTarget.model_validate(output)
    adapter = WarehouseAdapter.from_profile(profile)

    assert isinstance(adapter, SnowflakeAdapter)
