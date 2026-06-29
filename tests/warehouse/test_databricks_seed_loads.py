"""In-process loads-only smoke for the nyctaxi Databricks e2e seed (issue #226, US-002).

Validates that the committed, hand-crafted
``tests/fixtures/databricks/target/manifest.json`` loads cleanly via
:func:`signalforge.manifest.load` with NO network access and NO environment
variables — so it runs in the DEFAULT pytest suite (no marker, no gate). The
gated full-pipeline live e2e exercises the same fixture end-to-end against live
Databricks + Anthropic; this test is the cheap, always-on guard that the seed
stays valid for the loader.

Traces to ``.claude/rules/testing-signal.md`` § "Hand-crafted manifest seed
when workers can't run live tooling" (DEC-004 of issue #10, generalised): the
seed is committed because Ralph workers / CI cannot reach live Databricks, and a
loads-only test ships in the same commit. Mirrors the Snowflake seed loads-only
test (``test_snowflake_seed_loads.py``).
"""

from __future__ import annotations

from pathlib import Path

from signalforge.manifest import Manifest, load

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "databricks"

_MODEL_UID = "model.signalforge_test_nyctaxi.stg_nyctaxi_trips"


def test_nyctaxi_manifest_loads_via_signalforge() -> None:
    """The committed nyctaxi seed loads and resolves the staging model."""
    manifest = load(_FIXTURE_DIR)
    assert isinstance(manifest, Manifest)

    model = manifest.get_model(_MODEL_UID)
    assert model.name == "stg_nyctaxi_trips"
    assert model.unique_id == _MODEL_UID
    assert model.package_name == "signalforge_test_nyctaxi"
    assert model.original_file_path == "models/staging/stg_nyctaxi_trips.sql"

    # Loader strips empty raw_code → None; reaching this line means raw_code
    # survived parsing and the source ref is present.
    assert model.raw_code is not None
    assert "tpep_pickup_datetime" in model.raw_code


def test_nyctaxi_seed_targets_samples_nyctaxi_trips() -> None:
    """The model resolves to samples.nyctaxi.trips (Unity Catalog sample).

    The model's ``alias`` is overridden to ``trips`` so ``resolve_this()``
    points the prune stage straight at the read-only Unity Catalog sample table
    without a ``dbt run``.
    """
    manifest = load(_FIXTURE_DIR)
    model = manifest.get_model(_MODEL_UID)
    assert model.database == "samples"
    assert model.schema_ == "nyctaxi"
    assert model.alias == "trips"
    assert model.resolve_this().qualified_name == "samples.nyctaxi.trips"


def test_nyctaxi_seed_carries_natural_not_null_always_pass_column() -> None:
    """The natural NOT NULL pickup timestamp is present for the e2e drop signal.

    Under ``oneshot`` sampling prune queries the SOURCE table directly, so the
    declared columns must be REAL nyctaxi columns (a renamed/engineered column
    would compile to an "invalid identifier" and route to kept-without-evidence,
    never always-passes). The drop signal therefore relies on
    ``tpep_pickup_datetime`` — the pickup timestamp, naturally NOT NULL — so a
    drafted ``not_null`` on it returns zero failing rows and prunes as
    always-passes (mirrors the Austin bikeshare natural-NOT-NULL pattern).
    """
    manifest = load(_FIXTURE_DIR)
    model = manifest.get_model(_MODEL_UID)

    # All six REAL, unrenamed nyctaxi columns — no engineered literals.
    assert "tpep_pickup_datetime" in model.columns
    expected_columns = {
        "tpep_pickup_datetime",
        "tpep_dropoff_datetime",
        "trip_distance",
        "fare_amount",
        "pickup_zip",
        "dropoff_zip",
    }
    assert expected_columns <= set(model.columns)
    # No engineered/renamed columns leaked in.
    assert {"region", "fare_safe", "trip_id"}.isdisjoint(model.columns)

    # tpep_pickup_datetime survives into raw_code (the always-passes target);
    # no engineered literal columns.
    assert model.raw_code is not None
    assert "tpep_pickup_datetime" in model.raw_code
    assert "COALESCE" not in model.raw_code
    assert " AS region" not in model.raw_code


def test_nyctaxi_seed_has_exactly_one_enabled_model() -> None:
    """The fixture has exactly one enabled model — the staging view."""
    manifest = load(_FIXTURE_DIR)
    models = list(manifest.iter_models())
    assert len(models) == 1
    assert models[0].name == "stg_nyctaxi_trips"
