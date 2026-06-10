"""In-process smoke test for the Austin BigQuery e2e fixture (issue #10, US-002).

Validates that the committed ``tests/fixtures/dbt_project_austin/target/manifest.json``
loads cleanly via :func:`signalforge.manifest.load` without requiring any
network access or environment variables. The full e2e BigQuery smoke (US-005)
exercises the fixture end-to-end against live BQ + Anthropic; this test is
the cheap, always-on guard that the manifest stays valid for the loader.

Traces to plans/super/10-e2e-bigquery-smoke.md DEC-004 (committed manifest)
and US-002 acceptance criterion: "validates against
signalforge.manifest.load(project_dir) — verified by an in-process unit test".
"""

from __future__ import annotations

from pathlib import Path

from signalforge.manifest import load
from signalforge.manifest.models import Manifest

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "dbt_project_austin"


def test_austin_manifest_loads_via_signalforge() -> None:
    """The committed Austin manifest loads and resolves the staging model."""
    manifest = load(_FIXTURE_DIR)
    assert isinstance(manifest, Manifest)

    model = manifest.get_model("model.signalforge_test_austin.stg_bikeshare_trips")
    assert model.name == "stg_bikeshare_trips"
    assert model.unique_id == "model.signalforge_test_austin.stg_bikeshare_trips"
    assert model.package_name == "signalforge_test_austin"
    assert model.original_file_path == "models/staging/stg_bikeshare_trips.sql"
    # Loader strips empty raw_code → None; resolver raises if missing. Reaching
    # this line means raw_code survived parsing.
    assert model.raw_code is not None
    assert "trip_id" in model.raw_code

    # The SELECT currently exposes seven columns; we don't pin the exact
    # count here (the source-generated columns dict mirrors sources.yml,
    # which may evolve), but at least one column entry must round-trip
    # from the manifest.
    assert len(model.columns) >= 1


def test_austin_manifest_iter_models_yields_staging_models() -> None:
    """The fixture exposes the staging-layer models — at minimum the original
    ``stg_bikeshare_trips`` plus the engineered ``stg_bikeshare_station_pairs``
    that #170 added for the ``unique_combination`` drafter-steering e2e.
    """
    manifest = load(_FIXTURE_DIR)
    models = list(manifest.iter_models())
    names = sorted(m.name for m in models)
    assert "stg_bikeshare_trips" in names
    assert "stg_bikeshare_station_pairs" in names


def test_austin_manifest_loads_station_pairs_model() -> None:
    """The engineered ``stg_bikeshare_station_pairs`` model (US-011 of #170)
    parses cleanly via :func:`signalforge.manifest.load`, carries the natural
    multi-column ``GROUP BY`` pattern in its ``raw_code``, and declares the
    composite-key columns the drafter will reason about when proposing
    ``unique_combination``.

    The hand-crafted manifest seed satisfies
    ``.claude/rules/testing-signal.md`` § "Hand-crafted manifest seed when
    workers can't run live tooling": Ralph workers in worktrees can't reach
    live BigQuery, so we commit the parsed manifest entry alongside the new
    model SQL and validate the seed survives Pydantic parsing here.
    """
    manifest = load(_FIXTURE_DIR)

    model = manifest.get_model("model.signalforge_test_austin.stg_bikeshare_station_pairs")
    assert model.name == "stg_bikeshare_station_pairs"
    assert model.unique_id == "model.signalforge_test_austin.stg_bikeshare_station_pairs"
    assert model.package_name == "signalforge_test_austin"
    assert model.original_file_path == "models/staging/stg_bikeshare_station_pairs.sql"
    # Source-as-model alias trick (DEC-005 of #170): the model's relation
    # name resolves to the source table directly so the engineered fixture
    # works without a live `dbt run`.
    assert model.alias == "bikeshare_trips"

    # raw_code survived parsing AND carries the multi-column GROUP BY shape
    # that signals natural composite-key uniqueness to the drafter.
    assert model.raw_code is not None
    assert "GROUP BY" in model.raw_code
    assert "start_station_id" in model.raw_code
    assert "end_station_id" in model.raw_code
    assert "subscriber_type" in model.raw_code

    # The three composite-key columns plus the two aggregates round-trip
    # from the manifest seed.
    column_names = set(model.columns.keys())
    assert {
        "start_station_id",
        "end_station_id",
        "subscriber_type",
        "trip_count",
        "total_duration_minutes",
    } <= column_names
