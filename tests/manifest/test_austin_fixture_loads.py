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


def test_austin_manifest_iter_models_yields_only_staging() -> None:
    """The fixture has exactly one enabled model — the staging view."""
    manifest = load(_FIXTURE_DIR)
    models = list(manifest.iter_models())
    assert len(models) == 1
    assert models[0].name == "stg_bikeshare_trips"
