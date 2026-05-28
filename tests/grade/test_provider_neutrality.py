"""Provider-neutrality proof — US-005 of issue #135 (AC #2 + AC #3).

Drives :func:`signalforge.grade.grade_artifacts` end-to-end with a test-only,
no-cache provider (``supports_prompt_caching=False`` /
``supports_token_count=False``) selected via ``GradeConfig(provider=...)``, and
pins the capability-degrade invariants from DEC-008 / DEC-011:

* **AC #2** — registering the provider was the *only* wiring needed:
  ``provider_for(name)`` resolves it and ``GradeConfig(provider=name)``
  validates, with no edit to the orchestrator or any ``Literal``.
* **AC #3** — ``grade_artifacts`` writes a valid audit JSONL + sidecar JSON;
  every produced ``GradeEvent`` records ``cache_*_input_tokens == 0`` and
  round-trips through the strict ``extra="forbid"`` drift mirror with intact
  reproducibility blake2b hashes; NO dual-zero cache-anomaly WARNING fires; and
  the create kwargs the fake received carry no ``cache_control`` / beta header.

Registry isolation mirrors ``tests/llm/test_providers.py`` — snapshot + restore
``signalforge.llm.providers._REGISTRY`` so registering the fake provider in one
test never leaks into another.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from signalforge.draft.models import CandidateSchema
from signalforge.grade.config import GradeConfig
from signalforge.grade.engine import grade_artifacts
from signalforge.grade.models import GradeEvent, GradingReport
from signalforge.grade.rubric import Criterion, Rubric
from signalforge.llm.providers import provider_for, register_provider

if TYPE_CHECKING:
    from signalforge.llm import AnthropicClientProtocol
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult
from tests.grade.test_drift_detector import StrictGradeEvent
from tests.llm._fake_provider import (
    FAKE_NOCACHE_PROVIDER_NAME,
    FakeNoCacheProvider,
)

_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "grade"

# A 16-hex blake2b-8 fingerprint (the reproducibility-hash recipe across the
# audit corpus). Used to assert the produced events carry well-formed hashes.
_BLAKE2B8_HEX = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture
def _isolate_registry() -> Any:
    """Snapshot + restore the process-level provider registry."""
    from signalforge.llm import providers as providers_module

    saved = dict(providers_module._REGISTRY)
    try:
        yield
    finally:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)


def _make_model() -> Model:
    return Model(
        unique_id="model.shop.orders",
        name="orders",
        resource_type="model",
        package_name="shop",
        original_file_path="models/orders.sql",
        path="orders.sql",
        database="fake_project",
        schema="dataset",  # type: ignore[call-arg]
        columns={
            "order_id": Column(name="order_id"),
            "customer_id": Column(name="customer_id"),
        },
        raw_code="select 1",
    )


def _empty_prune_result(model: Model) -> PruneResult:
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )


def _load_sample_candidate() -> CandidateSchema:
    raw = (_FIXTURE_PATH / "sample_candidate.json").read_text(encoding="utf-8")
    return CandidateSchema.model_validate_json(raw)


def _single_criterion() -> Rubric:
    """A one-criterion rubric so every judge call shares one ``criterion_id``.

    The grade parser anchors on ``returned.criterion_id == sent.criterion_id``;
    a single canned response carrying this id satisfies every call.
    """
    return (Criterion(id="clarity", criterion="Is it clear?"),)


def _canned_judge_response() -> str:
    """A grade-judge JSON payload the parser accepts for the ``clarity`` call."""
    return json.dumps(
        {
            "criterion_id": "clarity",
            "score": 0.8,
            "passed": True,
            "evidence": "concise and unambiguous",
            "reasoning": "reads clearly",
        }
    )


def _project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)
    return project_dir


def _fast_config() -> GradeConfig:
    return GradeConfig(
        provider=FAKE_NOCACHE_PROVIDER_NAME,
        model="fake-nocache-judge",
        max_output_tokens=64,
        max_retries_429=0,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_registering_provider_is_the_only_wiring_needed(_isolate_registry: None) -> None:
    """AC #2: registering the fake provider is sufficient for the seam to accept
    it — ``provider_for`` resolves it and ``GradeConfig(provider=...)`` validates,
    with no other code change (DEC-011)."""
    provider = FakeNoCacheProvider()
    register_provider(provider)

    # Registry resolves the freshly-registered provider by name.
    assert provider_for(FAKE_NOCACHE_PROVIDER_NAME) is provider

    # The registry-validated config str accepts it (and rejects an unknown name).
    config = GradeConfig(provider=FAKE_NOCACHE_PROVIDER_NAME)
    assert config.provider == FAKE_NOCACHE_PROVIDER_NAME

    from signalforge.llm.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError):
        GradeConfig(provider="definitely-not-registered")


def test_grade_artifacts_drives_nocache_provider_end_to_end(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    _isolate_registry: None,
) -> None:
    """AC #3: a full ``grade_artifacts`` run on the no-cache provider produces a
    valid audit JSONL + sidecar, zero cache tokens, intact reproducibility
    hashes, no dual-zero cache-anomaly WARNING, and create-kwargs free of any
    cache marker / beta header."""
    register_provider(FakeNoCacheProvider(response_text=_canned_judge_response()))

    project_dir = _project(tmp_path)
    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _single_criterion()

    audit_path = project_dir / ".signalforge" / "grade.jsonl"
    sidecar_path = project_dir / ".signalforge" / "grade.json"

    with caplog.at_level("WARNING", logger="signalforge.llm.client"):
        report = grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=_fast_config(),
            project_dir=project_dir,
            audit_path=audit_path,
            sidecar_path=sidecar_path,
        )

    # The sample fixture has 2 columns + 1 column test + 0 model tests = 7
    # artifacts × 1 criterion = 7 judge calls, all scored (none degraded).
    assert isinstance(report, GradingReport)
    assert len(report.results) == 7
    assert report.aggregate_complete is True
    assert all(r.score == 0.8 and r.passed for r in report.results)

    # --- Audit JSONL: one durable record per call, all cache tokens 0. ---
    rows = _read_jsonl(audit_path)
    assert len(rows) == len(report.results) == 7
    events = [GradeEvent.model_validate(r) for r in rows]
    for event in events:
        assert event.cache_creation_input_tokens == 0
        assert event.cache_read_input_tokens == 0
        assert event.run_id == report.run_id
        # Reproducibility blake2b-8 fingerprints are present + well-formed.
        assert _BLAKE2B8_HEX.match(event.rubric_hash)
        assert _BLAKE2B8_HEX.match(event.prompt_version_template)
        assert _BLAKE2B8_HEX.match(event.criterion_prompt_hash)
        assert _BLAKE2B8_HEX.match(event.response_text_hash)

    # --- Drift detector: each JSONL line round-trips through the strict
    #     extra="forbid" mirror (zero-cache events validate cleanly). ---
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            StrictGradeEvent.model_validate_json(line)

    # --- Sidecar JSON: present + round-trips through GradingReport. ---
    assert sidecar_path.exists()
    round_tripped = GradingReport.model_validate_json(sidecar_path.read_text(encoding="utf-8"))
    assert round_tripped.run_id == report.run_id
    assert len(round_tripped.results) == len(report.results)

    # --- No dual-zero cache-anomaly WARNING: a non-caching provider must
    #     have suppressed it (it would otherwise false-alarm on every call). ---
    assert not any("cache marker no-op" in rec.getMessage() for rec in caplog.records), (
        "no-cache provider must suppress the dual-zero cache-anomaly WARNING"
    )


def test_nocache_provider_builds_no_cache_marker_or_beta_header(
    tmp_path: Path,
    _isolate_registry: None,
) -> None:
    """AC #3: the create kwargs the no-cache client receives carry no
    ``cache_control`` marker on any content block and no
    ``anthropic-beta`` / ``extra_headers`` cache header — neither the
    orchestrator nor the provider emits one (DEC-008)."""
    from tests.llm._fake_provider import FakeNoCacheClient

    register_provider(FakeNoCacheProvider(response_text=_canned_judge_response()))

    project_dir = _project(tmp_path)
    # Inject an inspectable client so we can read the exact create kwargs.
    client = FakeNoCacheClient(response_text=_canned_judge_response())

    model = _make_model()
    candidate = _load_sample_candidate()
    rubric = _single_criterion()

    # ``grade_artifacts`` types its ``client`` kwarg against the Anthropic
    # injection surface (DEC-012: the default provider's client protocol). A
    # non-Anthropic provider builds its own client and the orchestrator hands
    # it straight to the strategy, so the cast is the documented seam.
    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_fast_config(),
        client=cast("AnthropicClientProtocol", client),
        project_dir=project_dir,
        audit_path=project_dir / ".signalforge" / "grade.jsonl",
        sidecar_path=project_dir / ".signalforge" / "grade.json",
    )

    calls = client.create_calls
    assert calls, "expected at least one messages.create call"
    for call in calls:
        # No extended-cache beta header anywhere.
        assert "extra_headers" not in call or not call["extra_headers"]
        # No cache_control marker on any content block.
        for message in call["messages"]:
            for block in message["content"]:
                assert "cache_control" not in block
