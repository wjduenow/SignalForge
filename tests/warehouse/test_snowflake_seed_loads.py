"""In-process loads-only smoke for the TPCH Snowflake e2e seed (issue #124, US-003).

Validates that the committed, hand-crafted
``tests/fixtures/snowflake/target/manifest.json`` loads cleanly via
:func:`signalforge.manifest.load` with NO network access and NO environment
variables — so it runs in the DEFAULT pytest suite (no marker, no gate). The
gated full-pipeline live e2e (US-005) exercises the same fixture end-to-end
against live Snowflake + Anthropic; this test is the cheap, always-on guard
that the seed stays valid for the loader.

Traces to ``.claude/rules/testing-signal.md`` § "Hand-crafted manifest seed
when workers can't run live tooling" (DEC-004 of issue #10, generalised): the
seed is committed because Ralph workers / CI cannot reach live Snowflake, and a
loads-only test ships in the same commit.
"""

from __future__ import annotations

from pathlib import Path

from signalforge.manifest import load
from signalforge.manifest.models import Manifest

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "snowflake"

_MODEL_UID = "model.signalforge_test_tpch.stg_tpch_customers"


def test_tpch_manifest_loads_via_signalforge() -> None:
    """The committed TPCH seed loads and resolves the staging model."""
    manifest = load(_FIXTURE_DIR)
    assert isinstance(manifest, Manifest)

    model = manifest.get_model(_MODEL_UID)
    assert model.name == "stg_tpch_customers"
    assert model.unique_id == _MODEL_UID
    assert model.package_name == "signalforge_test_tpch"
    assert model.original_file_path == "models/staging/stg_tpch_customers.sql"

    # Loader strips empty raw_code → None; reaching this line means raw_code
    # survived parsing and the source ref is present.
    assert model.raw_code is not None
    assert "customer_id" in model.raw_code


def test_tpch_seed_targets_tpch_sf1_customer() -> None:
    """The model resolves to SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER.

    The model's ``alias`` is overridden to ``customer`` so ``resolve_this()``
    points the prune stage straight at the source table without a ``dbt run``.
    """
    manifest = load(_FIXTURE_DIR)
    model = manifest.get_model(_MODEL_UID)
    assert model.database == "SNOWFLAKE_SAMPLE_DATA"
    assert model.schema_ == "TPCH_SF1"
    assert model.alias == "customer"
    assert model.resolve_this().qualified_name == "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.customer"


def test_tpch_seed_carries_engineered_always_pass_columns() -> None:
    """The engineered always-pass columns are present for the US-005 drop signal.

    ``region`` is a string literal (`'us' AS region`) and ``acctbal_safe`` is a
    ``COALESCE``-guarded column. A drafted ``not_null`` on either is
    mathematically always-pass, giving the live e2e a deterministic prune drop
    (mirrors the Austin bikeshare engineered-determinism pattern, issue #10).
    """
    manifest = load(_FIXTURE_DIR)
    model = manifest.get_model(_MODEL_UID)

    assert "region" in model.columns
    assert "acctbal_safe" in model.columns
    # The literal + COALESCE survive into raw_code so prune compiles always-pass.
    assert model.raw_code is not None
    assert "'us' AS region" in model.raw_code
    assert "COALESCE(c_acctbal, 0) AS acctbal_safe" in model.raw_code


def test_tpch_seed_has_exactly_one_enabled_model() -> None:
    """The fixture has exactly one enabled model — the staging view."""
    manifest = load(_FIXTURE_DIR)
    models = list(manifest.iter_models())
    assert len(models) == 1
    assert models[0].name == "stg_tpch_customers"
