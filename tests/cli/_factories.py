"""Test-side fixture builders for ``tests/cli/test_generate.py`` (US-005).

Two helpers:

* :func:`make_fake_dbt_project` — drops a minimal ``dbt_project.yml`` and
  an empty ``target/`` dir under ``tmp_path`` and returns the project
  root. The CLI's project-root resolver (`_resolve_project_dir`) is
  satisfied by the presence of ``dbt_project.yml``; the CLI's pipeline
  is patched out at the stage-entry boundary, so the manifest /
  warehouse / draft / prune / grade / diff stages never read from
  these paths.
* :func:`make_typed_stage_returns` — produces typed-but-trivial return
  values (a :class:`Manifest` containing one :class:`Model`, a
  :class:`CandidateSchema`, a :class:`PruneResult`, a
  :class:`GradingReport`, a :class:`DiffReport`) so individual stage
  patches can return real types. Used by the stage-order test and the
  happy-path test.

Lives under ``tests/cli/`` and is never imported from production code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from signalforge.diff.models import DiffReport
from signalforge.draft.models import CandidateColumn, CandidateSchema
from signalforge.draft.schema import DraftOutcome
from signalforge.grade.models import GradingReport
from signalforge.manifest.models import Column, Manifest, Model
from signalforge.prune.models import PruneResult

_DBT_PROJECT_YML = """name: signalforge_cli_test
version: 1.0.0
config-version: 2

profile: signalforge_cli_test

model-paths: ["models"]
target-path: target
"""


def make_fake_dbt_project(tmp_path: Path, name: str = "project") -> Path:
    """Create a minimal dbt project layout under ``tmp_path / name``.

    Drops ``dbt_project.yml`` (so ``_resolve_project_dir`` is satisfied)
    and an empty ``target/`` dir. Does NOT write a manifest.json — every
    test that exercises ``cmd_generate`` patches ``manifest.load`` so
    the on-disk file is never opened.
    """
    project_dir = tmp_path / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "dbt_project.yml").write_text(_DBT_PROJECT_YML, encoding="utf-8")
    (project_dir / "target").mkdir(exist_ok=True)
    (project_dir / ".signalforge").mkdir(exist_ok=True)
    return project_dir


def make_model() -> Model:
    """Construct a tiny :class:`Model` for tests."""
    return Model(
        unique_id="model.shop.customers",
        name="customers",
        resource_type="model",
        package_name="shop",
        original_file_path="models/customers.sql",
        path="customers.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={"id": Column(name="id")},
        raw_code="select 1 as id",
    )


def make_manifest(model: Model | None = None) -> Manifest:
    m = model if model is not None else make_model()
    return Manifest(
        metadata={"dbt_schema_version": "v12"},
        nodes={m.unique_id: m},
    )


def make_candidate(model_name: str = "customers") -> CandidateSchema:
    return CandidateSchema(
        name=model_name,
        description="Test candidate",
        columns=(CandidateColumn(name="id", description="primary key"),),
        tests=(),
    )


def make_draft_outcome(candidate: CandidateSchema) -> DraftOutcome:
    """Construct a :class:`DraftOutcome`-shaped object for stage patching.

    The test mocks ``draft_schema``'s return value entirely; it never
    has to satisfy the real :class:`DraftOutcome` protocol because the
    CLI only reads ``.candidate``. We use a tiny ``object()`` shim
    rather than building real :class:`LLMRequest` / :class:`LLMResult`
    objects.
    """

    class _FakeOutcome:
        def __init__(self, candidate: CandidateSchema) -> None:
            self.candidate = candidate

    return _FakeOutcome(candidate)  # type: ignore[return-value]


def make_prune_result(model: Model) -> PruneResult:
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )


def make_grading_report(model: Model) -> GradingReport:
    return GradingReport(
        signalforge_version="0.0.0-test",
        run_id="run-cli-test",
        timestamp=datetime(2026, 5, 3, tzinfo=UTC),
        duration_seconds=0.0,
        model_unique_id=model.unique_id,
        rubric_hash="0" * 16,
        thresholds=(0.0, 0.0),
        results=(),
    )


def make_diff_report(model: Model, candidate: CandidateSchema) -> DiffReport:
    """Build a minimal :class:`DiffReport` whose render output contains a
    distinctive marker for stdout assertions.
    """
    return DiffReport(
        signalforge_version="0.0.0-test",
        model_unique_id=model.unique_id,
        run_id="run-cli-test",
        duration_seconds=0.0,
        proposed_yaml="version: 2\nmodels: []\n",
        existing_yaml=None,
        unified_diff="--- existing\n+++ proposed\n",
        entries=(),
        kept_count=0,
        dropped_count=0,
        flagged_count=0,
        has_existing_schema=False,
        candidate_hash="a" * 16,
        prune_result_hash="b" * 16,
        grading_report_hash="c" * 16,
    )
