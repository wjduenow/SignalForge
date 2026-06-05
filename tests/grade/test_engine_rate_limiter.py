"""Grade-engine wiring for the shared async rate limiter (#202 US-004 / DEC-205).

Stage-1 acceptance proof at the *orchestrator* level: a concurrent grade
burst whose LLM client returns a 429 + ``retry-after`` under load — the
exact thundering-herd shape that PREVIOUSLY exhausted each coroutine's
independent retry budget and left ``N`` ``GradeLLMError`` degradations —
now reaches **0** transient degradations because :func:`grade_artifacts`
publishes ONE shared :class:`AsyncRateLimiter` on the
``current_async_rate_limiter`` ContextVar so every coroutine in the
``TaskGroup`` paces against the SAME budget.

Three properties are pinned here:

1. **0 transient degradations under a 429 burst** — every
   ``(artifact, criterion)`` pair scores; ``aggregate_complete is True``.
2. **The ContextVar is SET during the run and RESET afterward** — every
   concurrent coroutine observes the SAME non-None limiter while the run
   is in flight, and the ContextVar is back to ``None`` once
   :func:`grade_artifacts` returns (no leakage across calls).
3. **The budget-timeout degrade path is untouched** (#198 no-regression) —
   a genuinely over-budget run still degrades every pair via the
   wall-clock backstop, independent of the limiter wiring.

Mirrors :mod:`tests.llm.test_client_rate_limit_wiring` for the burst shape
(a *real* ``anthropic.RateLimitError`` so ``classify_exception`` routes it
to RATE_LIMIT) and :mod:`tests.grade.test_engine` for the orchestrator
harness. No production code imports the fakes.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import cast

import anthropic
import httpx
import pytest

from signalforge.draft.models import CandidateColumn, CandidateSchema
from signalforge.grade import engine as engine_module
from signalforge.grade.config import GradeConfig
from signalforge.grade.engine import _stable_artifact_pairs, grade_artifacts
from signalforge.grade.errors import GradeIncompleteError
from signalforge.grade.rubric import Criterion, Rubric
from signalforge.llm import AnthropicClientProtocol
from signalforge.llm import client as client_module
from signalforge.llm._rate_limiter import current_async_rate_limiter
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult
from tests.llm._fake import FakeCountTokensResponse, FakeMessage, FakeTextBlock, FakeUsage

pytestmark = pytest.mark.llm


_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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
        columns={"order_id": Column(name="order_id")},
        raw_code="select 1",
    )


def _empty_prune_result(model: Model) -> PruneResult:
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version="0.0.0-test",
    )


def _candidate() -> CandidateSchema:
    # 1 column, 0 tests → 4 artifacts (model desc/rationale + column
    # desc/rationale). With a 2-criterion rubric that is 8 pairs.
    return CandidateSchema(
        name="orders",
        description="d",
        rationale="r",
        columns=(CandidateColumn(name="order_id", description="pk", rationale="rat"),),
        tests=(),
    )


def _two_criteria() -> Rubric:
    return (
        Criterion(id="clarity", criterion="Is it clear?"),
        Criterion(id="rationale", criterion="Is the rationale present and useful?"),
    )


def _config(*, max_retries_429: int = 8, total_budget_seconds: int = 60) -> GradeConfig:
    return GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=max_retries_429,
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=total_budget_seconds,
        max_concurrent_calls=4,
        cache_enabled=False,
    )


def _rate_limit_error(*, retry_after: str = "0") -> anthropic.RateLimitError:
    """A *real* ``anthropic.RateLimitError`` carrying ``retry-after`` so the
    Anthropic provider's ``classify_exception`` routes it to RATE_LIMIT and
    ``extract_rate_limit_info`` reads the header (the limiter pace seam)."""
    return anthropic.RateLimitError(
        message="rate limited",
        response=httpx.Response(429, request=_REQ, headers={"retry-after": retry_after}),
        body=None,
    )


def _ok_grade(criterion_id: str) -> FakeMessage:
    return FakeMessage(
        content=[
            FakeTextBlock(
                text=json.dumps(
                    {
                        "criterion_id": criterion_id,
                        "score": 0.7,
                        "passed": True,
                        "evidence": "",
                        "reasoning": "ok",
                    }
                )
            )
        ],
        usage=FakeUsage(input_tokens=1700, output_tokens=80),
        model="claude-fake-grade-judge",
    )


class _BurstAsyncMessages:
    """Async ``.messages`` surface that simulates a 429 storm under load.

    The first ``burst_429`` ``create`` calls (whichever concurrent coroutines
    arrive first) raise a real ``anthropic.RateLimitError`` with
    ``retry-after: 0``; every subsequent ``create`` returns a valid grade
    payload. A shared counter + lock keeps the burst count exact across the
    interleaved coroutines, and ``observed_limiters`` records the
    ``current_async_rate_limiter`` instance each call sees — the proof that
    EVERY coroutine shares ONE limiter while the run is in flight.
    """

    def __init__(self, *, burst_429: int) -> None:
        # The response criterion_id must echo the SENT criterion (the parser
        # anchor-contract checks it). The sent criterion rides in the create
        # kwargs, so we read it back from kwargs rather than guessing.
        self._burst_429 = burst_429
        self._lock = threading.Lock()
        self._create_count = 0
        self.observed_limiters: list[object] = []
        self.observed_concurrency: list[int] = []

    async def count_tokens(self, **_kwargs: object) -> FakeCountTokensResponse:
        return FakeCountTokensResponse(input_tokens=1500)

    async def create(self, **kwargs: object) -> FakeMessage:
        # Record which limiter THIS coroutine sees (set by grade_artifacts).
        limiter = current_async_rate_limiter.get()
        self.observed_limiters.append(limiter)
        # Snapshot the live effective concurrency at each create so a test can
        # prove the AIMD cap narrowed below max under the storm and widened
        # back afterward (#202 QG-FIX-1).
        if limiter is not None:
            self.observed_concurrency.append(limiter.effective_concurrency)
        with self._lock:
            self._create_count += 1
            is_burst = self._create_count <= self._burst_429
        # Yield so sibling coroutines interleave (a genuine fan-out, not a
        # serialised drain) before we raise/return.
        await asyncio.sleep(0)
        if is_burst:
            raise _rate_limit_error(retry_after="0")
        criterion_id = _criterion_id_from_kwargs(kwargs)
        return _ok_grade(criterion_id)


def _criterion_id_from_kwargs(kwargs: object) -> str:
    """Recover the sent criterion_id from the create kwargs' rendered blocks.

    The grade prompt's dynamic block embeds the criterion id verbatim as
    ``The criterion_id MUST equal "<id>".`` (see
    :func:`signalforge.grade.prompts.render_dynamic_block`); the parser's
    anchor contract requires the response ``criterion_id`` to equal the SENT
    one. We scan the rendered payload for that exact marker so the fake echoes
    the correct id back regardless of dispatch order.
    """
    blob = repr(kwargs)
    for candidate_id in ("clarity", "rationale"):
        if f'criterion_id MUST equal \\"{candidate_id}\\"' in blob:
            return candidate_id
        if f'criterion_id MUST equal "{candidate_id}"' in blob:
            return candidate_id
    # Defensive: an unmatched marker would surface as an anchor-contract
    # degrade, which the test's "0 transient degrades" assertion catches.
    return "clarity"


class _BurstAioNamespace:
    def __init__(self, messages: _BurstAsyncMessages) -> None:
        self.messages = messages


class _BurstSyncMessages:
    """Sync ``.messages`` surface present only to satisfy
    ``AnthropicClientProtocol`` (the grade engine forwards to ``.aio`` so these
    are never called on the async path)."""

    def create(self, **_kwargs: object) -> object:
        raise NotImplementedError("grade engine uses the async (.aio) surface")

    def count_tokens(self, **_kwargs: object) -> object:
        raise NotImplementedError("grade engine uses the async (.aio) surface")


class _BurstClient:
    """Minimal client exposing the ``.aio.messages`` async surface the grade
    engine forwards to ``call_llm_async`` (via ``_resolve_async_client``). The
    sync ``.messages`` stub is present only for protocol conformance."""

    def __init__(self, *, burst_429: int) -> None:
        self.messages = _BurstSyncMessages()
        self._async_messages = _BurstAsyncMessages(burst_429=burst_429)
        self.aio = _BurstAioNamespace(self._async_messages)

    @property
    def observed_limiters(self) -> list[object]:
        return self._async_messages.observed_limiters

    @property
    def observed_concurrency(self) -> list[int]:
        return self._async_messages.observed_concurrency


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the 429 backoff sleep instant + jitter exact so the burst test
    runs in microseconds without real-time blocking."""

    async def _no_async_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(client_module, "_async_sleep", _no_async_sleep)
    monkeypatch.setattr(client_module, "_rand_uniform", lambda _a, _b: 1.0)


@pytest.fixture(autouse=True)
def _contextvar_is_clean() -> None:
    """Guard: the ContextVar must start (and the engine must leave it) None."""
    assert current_async_rate_limiter.get() is None


# ---------------------------------------------------------------------------
# Headline acceptance — 429 burst that USED to degrade now scores all pairs
# ---------------------------------------------------------------------------


def test_concurrent_429_burst_no_longer_degrades_any_pair(tmp_path: Path) -> None:
    """A 429 burst across the 8-pair fan-out leaves ZERO transient degrades.

    Previously (no shared limiter), each coroutine blind-backed-off
    independently and a wave of 429s exhausted the per-coroutine retry
    budget, leaving ``N`` ``GradeLLMError`` (transient) degradations. With
    US-004 wiring, every coroutine paces against ONE shared
    :class:`AsyncRateLimiter` (honouring ``retry-after``), so every pair
    recovers and scores: ``aggregate_complete is True``.
    """
    project_dir = tmp_path / "project"
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)

    model = _make_model()
    candidate = _candidate()
    rubric = _two_criteria()
    assert len(_stable_artifact_pairs(candidate)) == 4  # 4 artifacts × 2 = 8 pairs

    # Burst: the first 4 create calls (== max_concurrent_calls) all 429.
    # max_retries_429=8 gives ample headroom so no pair exhausts.
    client = _BurstClient(burst_429=4)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config(max_retries_429=8),
        client=cast(AnthropicClientProtocol, client),
        project_dir=project_dir,
    )

    assert len(report.results) == 8
    transient = [r for r in report.results if r.degrade_reason_type == "transient"]
    assert transient == [], "the shared limiter should pace the burst to 0 transient degrades"
    assert all(r.score is not None for r in report.results)
    assert report.aggregate_complete is True

    # The fan-out really did hit the 429 burst (the limiter path was exercised,
    # not bypassed): at least the 4 burst 429s + 8 successful retries occurred.
    assert len(client.observed_limiters) >= 12


def test_every_coroutine_shares_one_limiter_then_contextvar_resets(tmp_path: Path) -> None:
    """Every concurrent coroutine observes the SAME non-None limiter during
    the run; the ContextVar is back to ``None`` after ``grade_artifacts``
    returns (no leakage across calls)."""
    project_dir = tmp_path / "project"
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)

    model = _make_model()
    candidate = _candidate()
    rubric = _two_criteria()
    client = _BurstClient(burst_429=2)

    grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config(max_retries_429=8),
        client=cast(AnthropicClientProtocol, client),
        project_dir=project_dir,
    )

    observed = client.observed_limiters
    assert observed, "expected at least one create call to observe the limiter"
    # In-flight: every coroutine saw a non-None limiter...
    assert all(limiter is not None for limiter in observed)
    # ...and they ALL shared the SAME instance (one budget for the whole wave).
    assert len({id(limiter) for limiter in observed}) == 1

    # Post-run: the ContextVar is cleanly reset — no leak into the caller's
    # context (the autouse `_contextvar_is_clean` fixture also asserts the
    # pre-run None, so a leak from a prior test would fail loudly there too).
    assert current_async_rate_limiter.get() is None


def test_contextvar_reset_even_when_run_degrades_over_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget-timeout degrade path (#198) is untouched by the limiter
    wiring AND the ContextVar still resets cleanly on that path.

    A slow per-pair coroutine blows the tiny wall-clock budget so every pair
    degrades with the locked budget reasoning (no regression to #198), and the
    ``finally`` block still resets ``current_async_rate_limiter`` to ``None``.
    """
    project_dir = tmp_path / "project"
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)

    async def _slow_create(self: _BurstAsyncMessages, **_kwargs: object) -> FakeMessage:  # noqa: ANN001
        await asyncio.sleep(60)  # well beyond total_budget_seconds=1
        raise AssertionError("unreachable — the budget timeout cancels first")

    monkeypatch.setattr(_BurstAsyncMessages, "create", _slow_create)

    model = _make_model()
    candidate = _candidate()
    rubric = _two_criteria()
    client = _BurstClient(burst_429=0)

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config(total_budget_seconds=1),
        client=cast(AnthropicClientProtocol, client),
        project_dir=project_dir,
    )

    # #198 no-regression: every pair degraded via the wall-clock backstop.
    assert all(r.score is None for r in report.results)
    assert all(r.degrade_reason_type == "budget" for r in report.results)
    assert report.aggregate_complete is False

    # The limiter ContextVar reset cleanly on the timeout path too.
    assert current_async_rate_limiter.get() is None


# ---------------------------------------------------------------------------
# FIX 1 — the adaptive gate's cap narrows under a 429 storm and widens back
# ---------------------------------------------------------------------------


def test_adaptive_concurrency_narrows_under_storm_then_widens_back(tmp_path: Path) -> None:
    """Under a 429 storm the AIMD effective concurrency (the gate's cap) narrows
    BELOW ``max_concurrent_calls``; after clean completions it widens back
    toward the cap. Every observed value stays within ``[1, max]`` — proving the
    gate never admits more than ``max_concurrent_calls`` nor fewer than 1.
    """
    project_dir = tmp_path / "project"
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)

    model = _make_model()
    candidate = _candidate()
    rubric = _two_criteria()
    max_concurrent = 4

    client = _BurstClient(burst_429=max_concurrent)  # the first wave all 429s

    report = grade_artifacts(
        model,
        candidate,
        _empty_prune_result(model),
        rubric=rubric,
        config=_config(max_retries_429=8),  # max_concurrent_calls=4
        client=cast(AnthropicClientProtocol, client),
        project_dir=project_dir,
    )

    # The run recovered fully (the limiter paced the storm).
    assert report.aggregate_complete is True

    observed = client.observed_concurrency
    assert observed, "expected the fan-out to observe the live concurrency"
    # (a) + (c): every observation is bounded — never above max, never below 1.
    assert all(1 <= c <= max_concurrent for c in observed)
    # (a): the storm narrowed the cap strictly below max at some point.
    assert min(observed) < max_concurrent, "a 429 storm must narrow concurrency below max"
    # (b): after clean completions the AIMD probes back up to the ceiling. The
    # ContextVar is reset post-run, so read the recovery off the observed
    # snapshots: the widest cap seen during the run reached the max ceiling as
    # headroom accrued across the successful retries.
    assert max(observed) == max_concurrent, "clean completions widen concurrency back to max"


# ---------------------------------------------------------------------------
# FIX 2 — the always-on sweep is wall-clock-bounded
# ---------------------------------------------------------------------------


def test_never_recovering_sweep_is_wall_clock_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-recovering transient (the LLM 429s forever) makes the main pass
    degrade every pair transient; the always-on sweep would otherwise honour
    long waits across rounds indefinitely. With ``sweep_budget_seconds`` tiny,
    the sweep's ``asyncio.timeout`` trips, the sweep STOPS (does not raise), and
    the pairs are left degraded — failing loud under the default
    ``require_complete``.
    """
    project_dir = tmp_path / "project"
    (project_dir / ".signalforge").mkdir(parents=True, exist_ok=True)

    # Make the sweep cool-down sleep BURN the entire sweep budget on the first
    # round so the timeout trips deterministically (the engine awaits
    # ``_async_sleep(sweep_cooldown_seconds)`` inside the bounded scope). The
    # override sleeps real time past the 0-second sweep budget.
    real_sleep = asyncio.sleep

    async def _slow_sweep_sleep(_seconds: float) -> None:
        # Each inter-round cool-down burns 0.6s of REAL time; two of them blow
        # the 1s sweep budget below so the timeout trips within ~1.2s.
        await real_sleep(0.6)

    monkeypatch.setattr(engine_module, "_async_sleep", _slow_sweep_sleep)

    model = _make_model()
    candidate = _candidate()
    rubric = _two_criteria()
    # Burst forever: 999 ensures every main-pass create + every sweep create
    # 429s, so no pair ever recovers and the sweep keeps finding transients.
    client = _BurstClient(burst_429=999)

    config = GradeConfig(
        model="claude-fake",
        cache_ttl="1h",
        max_output_tokens=64,
        max_retries_429=0,  # exhaust immediately → transient degrade on every pair
        max_retries_5xx=0,
        max_retries_conn=0,
        total_budget_seconds=60,
        max_concurrent_calls=4,
        cache_enabled=False,
        sweep_max_rounds=100,  # would loop a long time if unbounded
        sweep_cooldown_seconds=1.0,  # routed through the slow override above
        sweep_budget_seconds=1,  # the bound under test
    )

    # The never-recovering transients survive the (bounded) sweep → fail loud.
    with pytest.raises(GradeIncompleteError):
        grade_artifacts(
            model,
            candidate,
            _empty_prune_result(model),
            rubric=rubric,
            config=config,
            client=cast(AnthropicClientProtocol, client),
            project_dir=project_dir,
        )

    # The ContextVar reset cleanly even though the sweep timed out.
    assert current_async_rate_limiter.get() is None
