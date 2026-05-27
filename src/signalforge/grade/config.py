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
  ``model="claude-sonnet-4-6"``, ``cache_ttl="1h"``,
  ``max_output_tokens=256``, ``max_retries_429=3``, ``max_retries_5xx=1``,
  ``max_retries_conn=1``, ``total_budget_seconds=300``,
  ``min_pass_rate=0.7``, ``min_mean_score=0.5``, ``rubric=None``,
  ``fail_on_below_threshold=False``.

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
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from signalforge.grade.errors import GradeConfigError, GradeRubricError
from signalforge.grade.rubric import Rubric, validate_rubric

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

    model: str = "claude-sonnet-4-6"
    """LLM-judge model id (DEC-026). Mirrors the drafter's default.
    Haiku 4.5 is documented as a v0.2 ``cheap_model`` option for
    cost-conscious mode but is not exposed in v0.1."""

    cache_ttl: Literal["5m", "1h"] = "1h"
    """Anthropic prompt-cache TTL (DEC-024). Defaults to ``"1h"`` (vs.
    the drafter's ``"5m"``) because 60 sequential per-criterion calls
    can stretch beyond a 5-minute window under retry backoff; ``"1h"``
    gives margin at no extra cost (cache writes are one-shot regardless
    of TTL)."""

    max_output_tokens: int = 256
    """Per-criterion judge response cap (DEC-025). The expected JSON
    response is ~150 tokens; 256 gives 2× safety. Independent of
    :attr:`signalforge.draft.DraftConfig.max_output_tokens`."""

    max_retries_429: int = 3
    """Mirrors :attr:`signalforge.draft.DraftConfig.max_retries_429`.
    The grader reuses the centralised :func:`signalforge.llm.call_anthropic`
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

    total_budget_seconds: int = 300
    """Whole-run wall-clock budget (DEC-023). 5 minutes default — ~3×
    safety on 60 calls × 1s p50. Mirrors :attr:`signalforge.prune.PruneConfig.total_budget_seconds`
    semantics: when the budget trips, un-evaluated ``(artifact, criterion)``
    pairs land as a degraded :class:`signalforge.grade.models.GradingResult`
    rather than silently dropped."""

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

    @field_validator("model")
    @classmethod
    def _model_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("max_output_tokens", "total_budget_seconds")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("max_retries_429", "max_retries_5xx", "max_retries_conn")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
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
