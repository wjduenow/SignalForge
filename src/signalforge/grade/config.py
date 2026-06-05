"""Grade-layer config loader.

Loads the ``grade:`` top-level block from ``signalforge.yml`` into a
typed :class:`GradeConfig`. Mirrors :mod:`signalforge.prune.config` and
:mod:`signalforge.draft.config` verbatim so the CLI (#9) and any future
orchestrator sees one calling convention across stages:
``load_<stage>_config(project_dir, path=None) -> <Stage>Config``.

The outer file wrapper :class:`_GradeConfigFile` uses ``extra="ignore"``
at top level so sibling stage namespaces (``safety:``, ``llm:``,
``prune:``) silently coexist; the inner :class:`GradeConfig` uses
``extra="forbid"`` so a typo like ``mdoel:`` instead of ``model:`` fails
loud rather than silently no-op'ing.

Design commitments operationalised here (``plans/super/7-quality-grader.md``):

* **DEC-001** — :class:`GradeConfig` is plumbed into
  :func:`signalforge.grade.grade_artifacts` as a keyword-only optional;
  this loader produces it from ``signalforge.yml``.
* **DEC-014** — Cost-control knobs (``cache_ttl``, ``max_output_tokens``,
  ``total_budget_seconds``) live here; the documented per-model cost
  numbers in ``docs/grade-ops.md`` (US-010) reference these defaults.
* **DEC-022** — ``project_dir`` defaults at the orchestrator entry, not
  here. The loader takes it as a required argument so the caller is
  explicit about the resolution base.
* **DEC-023..DEC-027** — Locked default values:
  ``model=None`` (resolves to the calling provider's default judge model
  at config-load — ``anthropic`` -> ``claude-sonnet-4-6`` per
  :data:`signalforge.llm.providers.PROVIDER_DEFAULT_MODELS`; #187 US-002 /
  DEC-004. The #187 calibration gate found ``claude-haiku-4-5`` grades
  the rubric stricter than Sonnet — below the 85% bar — so Haiku stays an
  explicit opt-in, not the default), ``cache_ttl="1h"``,
  ``max_output_tokens=1024`` (#187 DEC-004 — raised from 256 so a one-line
  ``gemini-2.5-flash`` grade JSON is substantially less likely to
  truncate), ``max_retries_429=3``, ``max_retries_5xx=1``,
  ``max_retries_conn=1``, ``total_budget_seconds=None`` (reinterpreted by
  #198 DEC-001 as an *optional* absolute hard ceiling; ``None`` → use the
  scaled-budget formula via ``budget_base_seconds=60`` /
  ``budget_per_pair_seconds=20.0``), the three opt-in soft ceilings
  ``max_grade_calls=None`` / ``max_grade_cost_usd=None`` /
  ``max_grade_tokens=None`` (off), ``min_pass_rate=0.7``,
  ``min_mean_score=0.5``, ``rubric=None``, ``fail_on_below_threshold=False``,
  ``require_complete=True`` (#202 US-006 — fail loud on a non-exempt
  ungraded pair after the bounded sweep).
* **#187 US-002 / DEC-006** — when ``model`` is set explicitly, a
  SKU-prefix/provider mismatch (e.g. ``provider="openai"`` with
  ``model="claude-sonnet-4-6"``) fails loud at config-load. The
  prefix table is :data:`signalforge.llm.providers.PROVIDER_SKU_PREFIXES`.

Resolution order (mirrors :func:`signalforge.draft.config.load_draft_config`):

* ``path is None``: candidate is ``<project_dir>/signalforge.yml``.
  Missing → :class:`GradeConfig` defaults silently.
* ``path is not None``: explicit path. Missing → raise
  :class:`signalforge.grade.errors.GradeConfigError` (the operator
  pointed at a file that does not exist; silent no-op would mask the
  typo).
* File present but ``grade:`` key absent or null → return defaults
  (other top-level keys reserved per DEC-020 / DEC-025 / DEC-027
  namespacing).
* ``grade:`` block well-formed → return the populated
  :class:`GradeConfig`.
* Unknown / typo'd inner field, non-mapping ``grade:`` block, YAML parse
  failure, or :class:`pydantic.ValidationError` from
  :class:`GradeConfig` → :class:`GradeConfigError` with the underlying
  exception preserved on ``__cause__``.

Path canonicalisation is intentionally NOT applied here: the existing
prune / draft / safety loaders also do not route their config paths
through ``_path_safety.canonicalise_path`` (they use plain
:meth:`Path.resolve`), and US-014 of the warehouse layer documented the
"duplicate, don't extract" decision for path-safety helpers. Adding
canonicalisation here would diverge from the established precedent
without addressing a concrete attack the typed errors don't already
surface.

``yaml.safe_load`` only — ``yaml.load`` accepts arbitrary Python object
construction tags and is unsafe for any input we don't fully control.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from signalforge.grade.errors import GradeConfigError, GradeRubricError
from signalforge.grade.rubric import Rubric, validate_rubric
from signalforge.llm.providers import PROVIDER_DEFAULT_MODELS, PROVIDER_SKU_PREFIXES

_DEFAULT_CONFIG_FILENAME = "signalforge.yml"


class GradeConfig(BaseModel):
    """User-facing knobs for the grade layer (DEC-023..DEC-027).

    Lives under the ``grade:`` top-level key in ``signalforge.yml``. The
    namespacing convention is established by ``safety-layer.md`` DEC-025
    / ``llm-drafter.md`` DEC-027 / ``prune-engine.md`` DEC-020 — each
    pipeline stage claims one top-level key. Sibling keys are silently
    ignored by this loader (they belong to other stages).

    Config-shaped per ``safety-layer.md`` DEC-015: ``extra="forbid"`` so
    typos like ``mdoel:`` instead of ``model:`` fail loud rather than
    silently no-op'ing. The :class:`_GradeConfigFile` outer wrapper uses
    ``extra="ignore"`` so other top-level keys (``safety:``, ``llm:``,
    ``prune:``) don't trip the strict validator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    model: str | None = None
    """LLM-judge model id (DEC-026; #187 US-002 / DEC-004).

    The sentinel default ``None`` means "use the calling provider's
    default judge model" — resolved at config-load by the
    :meth:`_resolve_model_default` before-validator to
    :data:`signalforge.llm.providers.PROVIDER_DEFAULT_MODELS` keyed on
    :attr:`provider` (``anthropic`` -> ``claude-sonnet-4-6``, ``openai``
    -> ``gpt-4o-mini``, ``gemini`` -> ``gemini-2.5-flash``). Anthropic
    defaults to Sonnet because the #187 calibration gate found
    ``claude-haiku-4-5`` grades stricter than Sonnet (below the 85% bar);
    Haiku is an explicit opt-in (``grade.model: claude-haiku-4-5``). An
    explicit ``model:`` is always honoured verbatim. After construction
    this field is always a concrete non-empty string — never ``None``.

    When set explicitly, a SKU-prefix/provider mismatch (e.g.
    ``provider="openai"`` with a ``claude-`` model) fails loud at
    config-load via :meth:`_validate_model_provider_compat` (#187
    DEC-006), reusing :data:`signalforge.llm.providers.PROVIDER_SKU_PREFIXES`."""

    cache_ttl: Literal["5m", "1h"] = "1h"
    """Anthropic prompt-cache TTL (DEC-024). Defaults to ``"1h"`` (vs.
    the drafter's ``"5m"``) because 60 sequential per-criterion calls
    can stretch beyond a 5-minute window under retry backoff; ``"1h"``
    gives margin at no extra cost (cache writes are one-shot regardless
    of TTL)."""

    max_output_tokens: int = 1024
    """Per-criterion judge response cap (DEC-025; #187 DEC-004).

    Raised from 256 to 1024 now that ``gemini-2.5-flash`` is a one-line
    default judge model — a verbose-but-valid one-line grade JSON from a
    cheaper/faster model can exceed 256 tokens, and a truncated response
    surfaces as the wrong typed degrade. This is a **cap**, not a target;
    the expected JSON is still ~150 tokens, so the larger ceiling costs
    nothing on the happy path while substantially reducing truncation
    risk. Note 1024 reduces but does not eliminate Gemini truncation at
    scale — ``docs/grade-ops.md`` § per-provider floors records that
    ``gemini-2.5-flash`` may still degrade on a minority of pairs at the
    full-fixture scale (#158) and recommends 4096 for Gemini-heavy runs.
    Independent of :attr:`signalforge.draft.DraftConfig.max_output_tokens`."""

    max_retries_429: int = 3
    """Mirrors :attr:`signalforge.draft.DraftConfig.max_retries_429`.
    The grader reuses the centralised :func:`signalforge.llm.call_llm`
    seam (#5 DEC-012) so the retry taxonomy is the full clauditor
    surface; this knob dials down the per-call attempt count for 429
    responses without changing the global default."""

    max_retries_5xx: int = 1
    """Mirrors :attr:`signalforge.draft.DraftConfig.max_retries_5xx`."""

    max_retries_conn: int = 1
    """Mirrors :attr:`signalforge.draft.DraftConfig.max_retries_conn`."""

    provider: str = "anthropic"
    """LLM provider strategy name, resolved against the
    :mod:`signalforge.llm.providers` registry (issue #135 DEC-007).

    Threaded into :func:`signalforge.llm.call_llm` from the grade engine's
    per-criterion judge call so a non-Anthropic provider (#136 OpenAI /
    #137 Gemini) is selected per stage, independently of the drafter's
    :attr:`signalforge.draft.DraftConfig.provider`. Deliberately a
    registry-validated ``str``, NOT a ``Literal`` (DEC-007): the provider
    registry is a plugin point designed to grow. The field validator fails
    loud on an unknown value — listing the registered provider names."""

    total_budget_seconds: int | None = None
    """Optional absolute hard ceiling on the whole-run wall-clock budget
    (#198 DEC-001; reinterpreted from the flat DEC-023 default).

    ``None`` (the new default) means the engine sizes the budget from the
    work via the scaled formula
    ``budget_base_seconds + budget_per_pair_seconds * ceil(num_pairs / max_concurrent_calls)``
    — a backstop that grows with model width and concurrency rather than a
    flat 300s that the pre-#186 sequential era was sized for. When set to an
    int, the effective budget is ``min(scaled, total_budget_seconds)`` — i.e.
    an explicit value still acts as a hard cap on top of the scaled estimate,
    preserving exact v0.1 absolute-cap semantics for pinned ``signalforge.yml``
    files (e.g. an operator who set ``total_budget_seconds: 600`` keeps that
    600s ceiling).

    Mirrors :attr:`signalforge.prune.PruneConfig.total_budget_seconds`
    degrade semantics: when the budget trips, un-evaluated
    ``(artifact, criterion)`` pairs land as a degraded
    :class:`signalforge.grade.models.GradingResult` rather than silently
    dropped (DEC-015)."""

    budget_base_seconds: int = 60
    """Fixed startup / overhead allowance in the scaled wall-clock formula
    (#198 DEC-001).

    The constant term in
    ``budget_base_seconds + budget_per_pair_seconds * ceil(num_pairs / max_concurrent_calls)``.
    Covers per-run setup (config resolution, cache priming, the first
    concurrency wave's ramp) that does not scale with the number of pairs.
    Must be positive."""

    budget_per_pair_seconds: float = 20.0
    """Per concurrency-wave wall allowance in the scaled formula (#198
    DEC-001).

    The scaled formula multiplies this by
    ``ceil(num_pairs / max_concurrent_calls)`` — i.e. the number of
    concurrency *waves*, not the raw pair count — so it is the wall-clock
    allowance per wave of ``max_concurrent_calls`` in-flight judge calls.

    Default ``20.0`` is grounded in the #179 baseline (Sonnet judge p50
    ~10s/call; 220 pairs at concurrency 10 → ``60 + 20.0 * ceil(220/10) =
    500s`` against a measured 222.9s — ~2.25× headroom). It is a runaway
    backstop sized to tolerate 429 retry storms, NOT a completion target;
    the ticket-literal ``2.0`` would compute 104s and degrade ~half the
    pairs, recreating the failure this scaling fixes. Must be positive."""

    max_grade_calls: int | None = None
    """Opt-in soft ceiling on the number of LLM judge calls (#198 DEC-002).

    ``None`` (default) → off. When set, dispatch stops once this many
    judge calls have been made and the remaining ``(artifact, criterion)``
    pairs DEGRADE (never raise) — mirroring the DEC-015 conservative-degrade
    contract. Cache-hit pairs (#189) make no LLM call and never count
    against this ceiling. Whichever of the three ``max_grade_*`` ceilings
    trips first stops dispatch. Must be positive when set."""

    max_grade_cost_usd: float | None = None
    """Opt-in soft ceiling on the total USD cost of the grade run (#198
    DEC-002).

    ``None`` (default) → off. When set, dispatch stops once the accumulated
    per-call cost (computed from per-call token usage via
    :mod:`signalforge.llm.pricing`, including cache-read/write economics)
    meets or exceeds this budget; remaining pairs DEGRADE (never raise).
    Whichever of the three ``max_grade_*`` ceilings trips first stops
    dispatch. Must be positive when set."""

    max_grade_tokens: int | None = None
    """Opt-in soft ceiling on the total token movement of the grade run
    (#198 DEC-002).

    ``None`` (default) → off. When set, dispatch stops once the accumulated
    token count (input + output + cache-creation + cache-read across the
    judge calls) meets or exceeds this budget; remaining pairs DEGRADE
    (never raise). Whichever of the three ``max_grade_*`` ceilings trips
    first stops dispatch. Must be positive when set."""

    sweep_max_rounds: int = 3
    """Maximum number of bounded transient-recovery sweep rounds (#202 US-005 / DEC-206).

    After the main concurrent grade pass completes, an ALWAYS-ON recovery
    sweep re-grades any pair that degraded with
    :attr:`signalforge.grade.models.GradingResult.degrade_reason_type` ==
    ``"transient"`` — SEQUENTIALLY (concurrency 1, so there is no second
    thundering herd) — until zero transient pairs remain OR this many sweep
    rounds have run. ``"budget"`` and ``"ceiling"`` degrades are NEVER swept
    (they are not retriable on a calmer pass). A recovered pair is written
    to the grade cache like any other success.

    Default ``3`` gives a transient LLM/network blip a few calmer retries to
    recover so a run reaches 100% scored. ``0`` runs no sweep rounds (the
    main pass stands alone). Must be non-negative — a negative value is an
    operator misconfiguration; fail loud at config-load rather than silently
    clamp."""

    sweep_cooldown_seconds: float = 2.0
    """Cool-down wait (seconds) between the main pass and the first sweep
    round AND between successive sweep rounds (#202 US-005 / DEC-206).

    Gives an overloaded provider a moment to recover before the sweep
    re-touches the transient failures. ``0.0`` disables the wait (the sweep
    itself stays always-on — only the pause is skipped). The sleep routes
    through the test-overridable :data:`_async_sleep` module alias so the
    test suite runs instantly. Must be non-negative."""

    max_concurrent_calls: int = 10
    """Asyncio dispatch concurrency cap for the per-``(artifact, criterion)``
    judge calls (issue #186 DEC-003).

    Default ``10`` — matches the ticket's "realistic concurrency target" for
    the typical ~270–290-call grade run on a ~70-artifact model; operators
    raise it via ``signalforge.yml`` for ~170-col wide models without code
    changes. Range-bounded ``[1, 100]`` inclusive by the field validator —
    ``< 1`` would dispatch nothing; ``> 100`` invites provider rate-limit
    storms with no operator-visible benefit (the per-vendor TPM ceiling is
    the load-bearing throttle below that).

    Setting ``1`` yields bit-for-bit equivalent behaviour to v0.1 sequential
    output (the semaphore serialises in dispatch order). Mirrors the
    config-file-only convention of :attr:`min_pass_rate` /
    :attr:`min_mean_score` / :attr:`cache_ttl` — no CLI flag (DEC-023 of
    #186).

    When the configured :attr:`provider` has
    :attr:`signalforge.llm.providers.LLMProvider.supports_async` ``= False``,
    :func:`grade_artifacts` raises
    :class:`signalforge.llm.errors.LLMProviderAsyncUnsupportedError` at
    orchestrator entry — **regardless of** ``max_concurrent_calls`` (the
    engine consumes ``call_llm_async`` exclusively post-#186, so cap=1
    is NOT an escape hatch; the operator must pick an async-capable
    provider). Tightened by QG Pass 1 Concern #2 (DEC-006 of #186)."""

    min_pass_rate: float = 0.7
    """Fraction of ``(artifact, criterion)`` pairs that must score
    ``passed=True`` for the rubric to count as passed overall (DEC-016).
    Bounded ``[0.0, 1.0]`` inclusive; mirrors
    :class:`signalforge.grade.GradeThresholds.min_pass_rate`."""

    min_mean_score: float = 0.5
    """Floor on the mean numeric score across non-null verdicts
    (DEC-016). Bounded ``[0.0, 1.0]`` inclusive; mirrors
    :class:`signalforge.grade.GradeThresholds.min_mean_score`."""

    rubric: Rubric | None = None
    """Optional rubric override. ``None`` (the default) means the
    orchestrator falls back to :data:`signalforge.grade.rubric.DEFAULT_RUBRIC`
    at :func:`grade_artifacts` entry. When provided, must be a non-empty
    tuple of :class:`Criterion` and must satisfy
    :func:`signalforge.grade.rubric.validate_rubric` (no duplicate
    ids). Pydantic recursively validates each YAML mapping into the
    typed :class:`Criterion`."""

    fail_on_below_threshold: bool = False
    """Hard-fail switch for the aggregate threshold check.

    Default ``False`` — v0.1 ships report-only posture by default; a
    below-threshold rubric does not fail the run, the operator's diff
    surfaces the verdict and the operator decides.

    When ``True``, :func:`signalforge.grade.grade_artifacts` raises
    :class:`signalforge.grade.GradeBelowThresholdError` once the
    aggregate :class:`signalforge.grade.GradingReport.passed` is
    ``False`` (i.e. ``pass_rate < min_pass_rate`` and/or
    ``mean_score < min_mean_score``). The raise lands AFTER the
    fail-closed sidecar JSON write so the operator has a complete
    ``grade.json`` on disk for diagnosis (DEC-021 ordering invariant —
    pinned by
    ``test_grade_below_threshold_writes_sidecar_before_raising``).

    Graduated from v0.2 reservation to v0.1 wiring in #9 (US-002).
    The CLI (#9) maps the raise to a non-zero exit code so a
    ``signalforge generate`` invocation in CI can gate on threshold
    compliance — see ``docs/cli-ops.md`` for the exit-code tier."""

    require_complete: bool = True
    """Fail-loud switch for the grade-completeness contract (#202 US-006 /
    DEC-204 + DEC-207).

    Default ``True`` — after the always-on bounded transient-recovery
    sweep (#202 US-005), :func:`signalforge.grade.grade_artifacts` raises
    :class:`signalforge.grade.GradeIncompleteError` if any *non-exempt*
    ``(artifact, criterion)`` pair is still ungraded (``score=None``). An
    incomplete grade corpus is a structural failure the operator must see,
    not a verdict to fold silently into ``aggregate_complete=False``.

    The DEC-204 trip/exempt matrix branches on the
    :attr:`signalforge.grade.GradingResult.degrade_reason_type`
    discriminator (#202 US-001):

    * ``"transient"`` → ALWAYS trips. A transient pair that survived the
      sweep is an unrecovered LLM/network failure.
    * ``"budget"`` AND :attr:`total_budget_seconds` is ``None`` (the
      DEFAULT-scaled-budget formula) → trips. A default-scaled-budget
      overrun is a Stage-1 sizing canary — the budget was sized for the
      work, so the engine is at fault, not an operator ceiling.
    * EXEMPT (never trip): ``"ceiling"`` degrades (an explicit
      ``max_grade_*`` opt-in the operator chose); and ``"budget"`` degrades
      when :attr:`total_budget_seconds` was set EXPLICITLY (a deliberate
      operator time-ceiling — a curtailed run is the contract, not a
      surprise).

    The raise lands AFTER the fail-closed sidecar JSON write so the
    operator has a complete ``grade.json`` on disk for diagnosis (mirrors
    the :attr:`fail_on_below_threshold` raise-after-sidecar ordering), and
    BEFORE the :attr:`fail_on_below_threshold` check — incomplete is
    structural, below-threshold is verdictual.

    When ``False``, the engine never raises on incompleteness; the
    ungraded pairs surface via ``aggregate_complete=False`` (the v0.1
    report-only posture). The CLI ``--require-complete`` flag (US-007,
    a separate ticket) wires this field per-run.

    ``extra="forbid"`` makes a typo such as ``require_complte:`` fail loud
    at config-load rather than silently leaving the contract armed."""

    cache_enabled: bool = True
    """Master switch for the per-``(artifact, criterion)`` grade cache
    (issue #189 DEC-016).

    Default ``True`` — content-addressed cache lookup + write run on
    every grade pair. The cache key is a content-hash of the inputs
    that genuinely determine the verdict (rubric criterion, artefact
    payload, model + provider + prompt version, ...) so any change
    that should invalidate a prior verdict invalidates the key by
    construction — no TTL knob is needed in v0.1 (deferred to a future
    ticket if demand emerges).

    When ``False``, :func:`signalforge.grade.grade_artifacts` skips
    BOTH the lookup AND the write — every pair routes through the
    live LLM judge call. Operators reach for this knob to bypass the
    cache for debugging, after a manual fixture edit, or during
    calibration work. The CLI's ``signalforge generate --no-cache``
    flag flips this field on a per-run copy via
    :meth:`pydantic.BaseModel.model_copy` so the on-disk
    ``signalforge.yml`` is unaffected (US-007 wires the flag).

    ``extra="forbid"`` makes a typo such as ``cache_enable:`` (missing
    the trailing ``d``) fail loud at config-load via
    :class:`pydantic.ValidationError`, rather than silently leaving
    the cache enabled."""

    @model_validator(mode="before")
    @classmethod
    def _resolve_model_default(cls, data: Any) -> Any:
        """Resolve the sentinel ``model=None`` to the provider's default judge model.

        Runs BEFORE field validation (and before the frozen instance
        exists) so the injected value flows through the normal
        construction path — :class:`GradeConfig` is ``frozen=True`` and a
        ``mode="after"`` mutation would raise. Only a dict input is
        rewritten; an already-constructed instance (e.g. from
        ``model_validate`` of a :class:`GradeConfig`) passes through
        untouched.

        When ``model`` is absent or ``None``, inject
        :data:`signalforge.llm.providers.PROVIDER_DEFAULT_MODELS` keyed on
        the requested ``provider`` (defaulting to ``"anthropic"`` to
        match the field default). A provider NOT in the default-model table
        is left alone — no injection — via ``.get()`` so this never masks
        an error with a ``KeyError`` (#187 US-002 / DEC-004). Two such
        cases follow downstream: an *unregistered* provider is rejected by
        the ``provider`` field-validator (:class:`UnknownProviderError`);
        a *registered* provider absent from the default-model table with no
        explicit model is rejected by
        :meth:`_validate_model_provider_compat` (which requires the
        operator to set ``grade.model`` explicitly).
        """
        if not isinstance(data, dict):
            return data
        if data.get("model") is None:
            provider = data.get("provider", "anthropic")
            resolved = PROVIDER_DEFAULT_MODELS.get(provider)
            if resolved is not None:
                # Copy-on-write so we don't mutate a caller-owned dict.
                data = {**data, "model": resolved}
        return data

    @field_validator("model")
    @classmethod
    def _model_non_empty(cls, v: str | None) -> str | None:
        # ``None`` only survives to here when the provider was unknown and
        # the before-validator deliberately declined to inject a default
        # (so the provider field-validator can raise the typed error).
        # Pass it through cleanly rather than tripping the non-empty guard.
        if v is None:
            return v
        if not v or not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("max_output_tokens", "budget_base_seconds", "budget_per_pair_seconds")
    @classmethod
    def _positive(cls, v: int | float) -> int | float:
        """Positive-only knobs (#198 DEC-001 split).

        Covers :attr:`max_output_tokens` (zero/negative would make the LLM
        refuse output) plus the two always-on scaled-budget terms
        :attr:`budget_base_seconds` / :attr:`budget_per_pair_seconds` (a
        non-positive term would size the wall-clock backstop to ``0`` and
        degrade every pair before any call). ``total_budget_seconds`` and the
        three ``max_grade_*`` ceilings are now optional and live on the
        separate :meth:`_optional_positive` validator below."""
        # Reject non-finite floats up front: ``yaml.safe_load`` parses
        # ``.nan`` / ``.inf``, and ``nan <= 0`` / ``inf <= 0`` are both
        # ``False`` so they would slip past the positivity check — a NaN
        # ``budget_per_pair_seconds`` then crashes ``int(nan)``/``math.ceil(nan)``
        # in ``_compute_effective_budget`` (Pydantic floats allow inf/nan by
        # default). Int fields can't carry inf/nan — coercion rejects them earlier.
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("must be a finite number")
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator(
        "total_budget_seconds",
        "max_grade_calls",
        "max_grade_cost_usd",
        "max_grade_tokens",
    )
    @classmethod
    def _optional_positive(cls, v: int | float | None) -> int | float | None:
        """Allow-``None``-or-positive knobs (#198 DEC-001 / DEC-002).

        :attr:`total_budget_seconds` (optional absolute cap) and the three
        opt-in soft ceilings :attr:`max_grade_calls` /
        :attr:`max_grade_cost_usd` / :attr:`max_grade_tokens` all default to
        ``None`` (off). ``None`` passes through untouched; a *present* value
        must be positive — a zero/negative cap would trip immediately and
        degrade the whole run, the silent-no-op failure mode the strict
        validator exists to prevent."""
        if v is None:
            return v
        # Reject non-finite floats (``max_grade_cost_usd: .inf`` would make the
        # cost ceiling never trip — ``cost_usd >= inf`` is always ``False`` —
        # i.e. a silent no-op; ``.nan`` is likewise never ``>=``). Same rationale
        # as :meth:`_positive`.
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("must be a finite number")
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("max_concurrent_calls")
    @classmethod
    def _max_concurrent_calls_bounded(cls, v: int) -> int:
        """Range ``[1, 100]`` inclusive (issue #186 DEC-003).

        ``< 1`` would dispatch nothing (semaphore acquire deadlock at
        construction); ``> 100`` invites provider rate-limit storms with
        no operator-visible benefit (the per-vendor TPM ceiling is the
        real throttle below that). Both ends are operator-actionable
        misconfigurations — fail loud at config-load rather than
        silently clamp.
        """
        if v < 1 or v > 100:
            raise ValueError("must be in the closed interval [1, 100]")
        return v

    @field_validator("max_retries_429", "max_retries_5xx", "max_retries_conn", "sweep_max_rounds")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be non-negative")
        return v

    @field_validator("sweep_cooldown_seconds")
    @classmethod
    def _non_negative_finite_float(cls, v: float) -> float:
        """Non-negative finite cool-down (#202 US-005 / DEC-206).

        ``0.0`` is allowed — it disables the inter-round pause while leaving
        the sweep itself always-on. A negative value is an operator
        misconfiguration; a non-finite float (``.nan`` / ``.inf`` parses out
        of ``yaml.safe_load``) would make the test-overridable
        :func:`signalforge.grade.engine._async_sleep` wait forever / crash,
        so reject it up-front (same rationale as :meth:`_positive`)."""
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        if v < 0.0:
            raise ValueError("must be non-negative")
        return v

    @field_validator("provider")
    @classmethod
    def _provider_registered(cls, v: str) -> str:
        """Reject an unknown provider name at config-load (issue #135 DEC-007).

        Membership is checked against the live
        :mod:`signalforge.llm.providers` registry via
        :func:`signalforge.llm.providers.provider_for`, which raises
        :class:`signalforge.llm.errors.UnknownProviderError` listing the
        available provider names. Import is local to the validator to keep
        the grade-config module free of any import-time coupling to the LLM
        provider registry.

        ``UnknownProviderError`` is an ``LLMError`` (an ``Exception`` that is
        NOT a ``ValueError`` / ``TypeError`` / ``AssertionError``), so Pydantic
        v2 does NOT wrap it into a ``ValidationError`` — it propagates raw and
        ``load_grade_config`` surfaces it directly with its available-keys
        remediation."""
        from signalforge.llm.providers import provider_for

        provider_for(v)
        return v

    @field_validator("min_pass_rate", "min_mean_score")
    @classmethod
    def _bounded_unit(cls, v: float) -> float:
        # NaN and infinities slip through the bare ``<`` / ``>`` comparisons
        # (NaN is unordered; both `nan < 0.0` and `nan > 1.0` are False),
        # so reject non-finite values up-front.
        if not math.isfinite(v):
            raise ValueError("must be a finite number in the closed interval [0.0, 1.0]")
        if v < 0.0 or v > 1.0:
            raise ValueError("must be in the closed interval [0.0, 1.0]")
        return v

    @model_validator(mode="after")
    def _validate_model_provider_compat(self) -> GradeConfig:
        """Reject a SKU-prefix/provider mismatch (#187 US-002 / DEC-006).

        After field validation ``model`` is always a concrete string (the
        before-validator resolved the sentinel, OR the operator set it
        explicitly, OR the provider was unknown and the provider
        field-validator already raised before reaching here). When
        :attr:`provider` is one of the *known-prefix* providers in
        :data:`signalforge.llm.providers.PROVIDER_SKU_PREFIXES` AND the
        model carries a *different* known provider's SKU prefix, fail
        loud: e.g. ``provider="openai"`` with ``model="claude-sonnet-4-6"``
        is an operator mistake that would otherwise send a ``claude-`` SKU
        through the OpenAI strategy.

        The prefix table is
        :data:`signalforge.llm.providers.PROVIDER_SKU_PREFIXES` (single
        source of truth — no hardcoded prefixes here). Two cases are
        deliberately left alone:

        * A model whose prefix matches no known provider (forward-compat:
          a future SKU the table doesn't yet enumerate must not be
          rejected as a mismatch).
        * A registry-valid provider that is NOT in the prefix table
          (a custom/plugin provider) *with an explicit model*. Such a
          provider may use any model name — the cross-vendor mismatch
          concept only applies among the three known-prefix vendors, so
          the check does not fire when :attr:`provider` is outside the
          table.

        A registry-valid provider absent from
        :data:`signalforge.llm.providers.PROVIDER_DEFAULT_MODELS` AND given
        no explicit ``model`` reaches here with ``model is None`` (the
        before-validator had no default model to inject; the ``provider``
        field-validator passed because the provider IS registered). We
        cannot guess a custom provider's model, so this fails loud rather
        than letting ``None`` flow into the engine — which keeps the
        post-construction "``model`` is never ``None``" invariant the
        consumers assert on genuinely true (#187 QG).

        This is a read-only check — no mutation — so it is safe on the
        frozen instance.
        """
        model = self.model
        if model is None:
            raise ValueError(
                f"provider {self.provider!r} has no built-in default model; "
                f"set 'grade.model' explicitly in signalforge.yml"
            )
        # Only the known-prefix providers participate in the mismatch check.
        if self.provider not in PROVIDER_SKU_PREFIXES:
            return self
        if model.startswith(PROVIDER_SKU_PREFIXES[self.provider]):
            return self
        # If the model carries ANOTHER known provider's prefix, that's a mismatch.
        for other_provider, prefix in PROVIDER_SKU_PREFIXES.items():
            if other_provider == self.provider:
                continue
            if model.startswith(prefix):
                raise ValueError(
                    f"model {model!r} has the {other_provider!r} SKU prefix "
                    f"{prefix!r} but provider is {self.provider!r}; set a "
                    f"{self.provider!r}-compatible model or change the provider"
                )
        return self

    @model_validator(mode="after")
    def _validate_rubric_structure(self) -> GradeConfig:
        # Per-criterion shape is enforced by ``Criterion`` (extra=forbid,
        # non-empty fields). The rubric-level invariants — non-empty
        # tuple, no duplicate ids — live on ``validate_rubric``. Re-raise
        # ``GradeRubricError`` as ``ValueError`` so Pydantic wraps it in
        # the standard ``ValidationError`` and the loader can convert
        # the whole thing to ``GradeConfigError`` on a single seam.
        if self.rubric is None:
            return self
        try:
            validate_rubric(self.rubric)
        except GradeRubricError as exc:
            raise ValueError(str(exc)) from exc
        return self


class _GradeConfigFile(BaseModel):
    """Outer wrapper for the ``signalforge.yml`` top-level mapping.

    ``extra="ignore"`` at this level — sibling top-level keys
    (``safety:``, ``llm:``, ``prune:``, future ``diff:`` ...) are
    reserved for other stages per the namespacing convention and must
    not trigger a grade-layer validation error. The strict
    ``extra="forbid"`` lives on :class:`GradeConfig` itself.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    grade: GradeConfig = Field(default_factory=GradeConfig)


def load_grade_config(project_dir: Path, path: Path | None = None) -> GradeConfig:
    """Load a :class:`GradeConfig` from ``signalforge.yml``.

    Mirrors :func:`signalforge.prune.config.load_prune_config` and
    :func:`signalforge.draft.config.load_draft_config` so the CLI (#9)
    and any future orchestrator sees one calling convention across
    stages: ``(project_dir, path=None)``.

    Resolution:

    * ``path is None``: look for ``<project_dir>/signalforge.yml``.
      Missing → :class:`GradeConfig` defaults silently.
    * ``path is not None``: use that exact path. Missing → raise
      :class:`signalforge.grade.errors.GradeConfigError` (mirrors the
      drafter's explicit-path-missing behaviour). Silent no-op would
      mask a typo in the operator's CLI flag.

    Args:
        project_dir: Project root used as the base for the default
            config-file lookup (``<project_dir>/signalforge.yml``).
        path: Optional explicit config path. ``None`` falls back to the
            project-relative default.

    Returns:
        A fully-validated :class:`GradeConfig`. When the file is
        absent, empty, or the ``grade:`` key is missing, the defaults
        from DEC-023..DEC-027 apply.

    Raises:
        GradeConfigError: The explicit ``path`` is missing, the file is
            not valid YAML, its top level is not a mapping, the
            ``grade:`` block is not a mapping, or the contents fail
            :class:`GradeConfig` validation (typo, out-of-range numeric
            knob, malformed rubric override, ...). The original
            :class:`pydantic.ValidationError` (if any) is preserved on
            ``__cause__``.
    """
    if path is not None:
        config_file = path
        if not config_file.exists():
            raise GradeConfigError(
                f"signalforge grade config file not found at {config_file!r}",
                remediation=(
                    "The explicit config path passed to load_grade_config "
                    "does not exist. Verify the path (typo, wrong working "
                    "directory) or omit the argument to fall back to "
                    "<project_dir>/signalforge.yml."
                ),
            )
    else:
        config_file = project_dir / _DEFAULT_CONFIG_FILENAME
        if not config_file.exists():
            return GradeConfig()

    raw_text = config_file.read_text(encoding="utf-8").strip()
    if not raw_text:
        return GradeConfig()

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise GradeConfigError(
            f"signalforge.yml is not valid YAML: {exc}",
        ) from exc

    if loaded is None:
        # File parses to None (e.g. only comments) — same as empty.
        return GradeConfig()

    if not isinstance(loaded, dict):
        raise GradeConfigError(
            f"signalforge.yml top level must be a mapping; got {type(loaded).__name__}",
        )

    if "grade" not in loaded or loaded["grade"] is None:
        # Missing `grade:` key (or `grade:` with null value) — sibling
        # top-level keys reserved per the namespacing convention.
        return GradeConfig()

    grade_block = loaded["grade"]
    if not isinstance(grade_block, dict):
        raise GradeConfigError(
            f"signalforge.yml: 'grade' must be a mapping; got {type(grade_block).__name__}",
        )

    try:
        wrapper = _GradeConfigFile.model_validate({"grade": grade_block})
    except ValidationError as exc:
        raise GradeConfigError(
            f"signalforge.yml: 'grade' block failed schema validation: {exc}",
        ) from exc

    return wrapper.grade


__all__ = ["GradeConfig", "load_grade_config"]
