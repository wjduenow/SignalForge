"""In-process smoke test for the multi-model fixture (issue #37, US-006).

Validates that the committed ``tests/fixtures/dbt_project_multi/target/manifest.json``
loads cleanly via :func:`signalforge.manifest.load` without requiring any
network access or environment variables. The US-007 CLI integration tests
exercise the fixture end-to-end against fakes; this test is the cheap,
always-on guard that the manifest stays valid for the loader and that the
engineered determinism (literal-source column on ``stg_a``) survives.

Traces to plans/super/37-multi-model-select.md DEC-013 (multi-model fixture
shape) and the US-006 acceptance criteria:

- ``test_dbt_project_multi_loads`` — three models with the expected unique_ids.
- ``test_dbt_project_multi_has_engineered_always_passes_column`` — ``stg_a``
  carries a ``source`` column whose SQL origin is a literal value, so any
  LLM-drafted ``not_null`` test on it is mathematically guaranteed to
  always-pass and the prune engine routes the decision to
  ``DropReason="always-passes"``.
- ``test_dbt_project_multi_tag_distribution`` — pins 2 staging + 1 marts so
  the US-007 selector integration tests have a stable target.
"""

from __future__ import annotations

from pathlib import Path

from signalforge.manifest import load
from signalforge.manifest.models import Manifest

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "dbt_project_multi"

_EXPECTED_UNIQUE_IDS = frozenset(
    {
        "model.dbt_project_multi.stg_a",
        "model.dbt_project_multi.stg_b",
        "model.dbt_project_multi.fct_x",
    }
)


def test_dbt_project_multi_loads() -> None:
    """The committed multi-model manifest loads with exactly three models."""
    manifest = load(_FIXTURE_DIR)
    assert isinstance(manifest, Manifest)

    models = list(manifest.iter_models())
    assert len(models) == 3
    assert {m.unique_id for m in models} == _EXPECTED_UNIQUE_IDS

    # Spot-check path + package_name on each so a refactor that breaks the
    # path field surfaces here, not in the US-007 selector tests.
    stg_a = manifest.get_model("model.dbt_project_multi.stg_a")
    assert stg_a.name == "stg_a"
    assert stg_a.package_name == "dbt_project_multi"
    assert stg_a.original_file_path == "models/staging/stg_a.sql"

    stg_b = manifest.get_model("model.dbt_project_multi.stg_b")
    assert stg_b.name == "stg_b"
    assert stg_b.original_file_path == "models/staging/stg_b.sql"

    fct_x = manifest.get_model("model.dbt_project_multi.fct_x")
    assert fct_x.name == "fct_x"
    assert fct_x.original_file_path == "models/marts/fct_x.sql"


def test_dbt_project_multi_has_engineered_always_passes_column() -> None:
    """``stg_a`` carries an engineered literal column for the always-passes path.

    The fixture's ``models/staging/stg_a.sql`` projects ``'austin' AS source``
    (a string literal). The manifest column entry must surface as ``source`` so
    the US-007 CLI integration tests (and any LLM drafter exercising the
    fixture) can rely on a ``not_null`` test on this column being
    mathematically guaranteed to always-pass on a representative sample.

    See ``testing-signal.md`` — "Engineered determinism for LLM-driven
    assertions" — for the rationale; the same trick is used by the Austin
    e2e fixture.
    """
    manifest = load(_FIXTURE_DIR)
    stg_a = manifest.get_model("model.dbt_project_multi.stg_a")

    # The column must be present in the manifest's column dict.
    assert "source" in stg_a.columns, (
        "stg_a.columns is missing the literal `source` column — the always-"
        "passes drop-path guarantee depends on this column existing in the "
        "manifest exactly as the SQL projects it."
    )

    # The raw_code must contain the literal projection so any downstream
    # SQL-aware consumer (or a maintainer reading the fixture) can verify
    # the always-pass derivation by inspection.
    assert stg_a.raw_code is not None
    assert "'austin' as source" in stg_a.raw_code


def test_dbt_project_multi_tag_distribution() -> None:
    """Pin the tag distribution: 2 staging + 1 marts.

    The US-007 CLI integration tests will key on this distribution to verify
    ``--select tag:staging`` returns two models and ``--select tag:marts``
    returns one. Drifting the distribution here without updating those tests
    is a coordination failure we want to catch loudly.
    """
    manifest = load(_FIXTURE_DIR)
    tag_to_unique_ids: dict[str, set[str]] = {}
    for model in manifest.iter_models():
        for tag in model.tags:
            tag_to_unique_ids.setdefault(tag, set()).add(model.unique_id)

    assert tag_to_unique_ids == {
        "staging": {
            "model.dbt_project_multi.stg_a",
            "model.dbt_project_multi.stg_b",
        },
        "marts": {"model.dbt_project_multi.fct_x"},
    }
