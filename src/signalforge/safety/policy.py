"""User-facing safety-policy model and helpers (US-005).

Defines :class:`SafetyPolicy` — the config-shaped Pydantic v2 model that
mirrors the ``safety:`` block of ``signalforge.yml`` — plus two
underscore-prefixed helpers exercised by other safety-layer modules:

* :func:`_resolve_redact_patterns` — applies ``redact: {extend|replace: [...]}``
  semantics on top of :data:`DEFAULT_REDACT_PATTERNS` (DEC-007).
* :func:`_compute_policy_hash` — deterministic 16-hex ``blake2b-8`` of the policy
  used as :class:`signalforge.safety.models.AuditEvent.policy_hash` (DEC-014).

Design commitments operationalised here:

* **DEC-015** — The policy uses ``extra="forbid"`` (config-shaped: typos
  must fail loud). Contrast with :mod:`signalforge.safety.models`, which
  uses ``extra="ignore"`` (data-shaped: forward-compat for audit replay).
* **DEC-017** — A ``@model_validator(mode="before")`` resolves the
  ``redact:`` block into ``redact_patterns`` *before* field validation,
  so the typo-rejection in the ``redact:`` sub-keys uses the typed
  :class:`signalforge.safety.errors.UnknownConfigKeyError` rather than
  Pydantic's own ``extra="forbid"`` (which would only fire at the top
  level).
* **DEC-018** — :meth:`SafetyPolicy.with_mode` is the canonical override
  path for the CLI's ``--mode`` flag. Frozen Pydantic models cannot be
  mutated; this helper hands back a fresh ``SafetyPolicy`` with the new
  mode and every other field preserved.
* **DEC-021** — Constructing a policy with ``mode=SAMPLE`` emits a
  WARNING via :data:`_LOGGER`. This fires once per construction; the
  CLI's quiet flag is the user's escape hatch.
* **DEC-023** — Patterns ``""``, ``"*"`` and ``"?"`` are rejected at
  construction time. ``"*"`` would silently disable the redactor; ``""``
  would never match anything; ``"?"`` would match every single-character
  column. All three are footgun-shaped, so we raise
  :class:`signalforge.safety.errors.InvalidPatternError` rather than
  accept them.
* **DEC-024** — A ``@field_validator(mode="before")`` on ``mode`` accepts
  any case + ``-`` / ``_`` mix (``"schema-only"`` /
  ``"schema_only"`` / ``"Schema-Only"`` / …). Unknown values raise
  :class:`signalforge.safety.errors.InvalidSamplingModeError`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from signalforge.safety.errors import (
    InvalidConfigError,
    InvalidPatternError,
    InvalidSamplingModeError,
    UnknownConfigKeyError,
)
from signalforge.safety.models import SamplingMode

_LOGGER: Final = logging.getLogger("signalforge.safety")


DEFAULT_REDACT_PATTERNS: Final[tuple[str, ...]] = (
    "*email",
    "email",
    "*phone",
    "phone",
    "*ssn",
    "ssn",
)
"""Six built-in redaction patterns. Case-insensitive ``fnmatch`` globs
matched lowercased in US-008's ``_matches_redaction_pattern``. Each PII
class is covered by both a prefixed form (``"*email"``) and a bare form
(``"email"``) so columns like ``user_email`` and ``email`` both match."""


DEFAULT_AUDIT_PATH: Final[Path] = Path(".signalforge/audit.jsonl")
"""Default audit-log path, relative to ``project_dir``."""


def _resolve_redact_patterns(block: Any) -> tuple[str, ...]:
    """Resolve a raw ``redact:`` config block into a pattern tuple.

    The contract (DEC-007):

    * ``None`` or ``{}`` → built-in defaults.
    * ``{"extend": [...]}`` → built-ins plus the user's additions.
    * ``{"replace": [...]}`` → user's list verbatim. Empty list emits a
      WARNING and disables redaction entirely.
    * Both ``extend`` and ``replace`` set → :class:`InvalidConfigError`.
    * Any other key → :class:`UnknownConfigKeyError` on the first
      offender (sorted) so the message is deterministic.

    Module-level helper (not a method) so the same logic is reachable
    from the ``model_validator(mode="before")`` and from ad-hoc tests.
    """
    if block is None or block == {}:
        return DEFAULT_REDACT_PATTERNS
    if not isinstance(block, dict):
        raise InvalidConfigError(
            message=f"redact must be a mapping; got {type(block).__name__}",
        )

    has_extend = "extend" in block
    has_replace = "replace" in block
    if has_extend and has_replace:
        raise InvalidConfigError(
            message="redact cannot specify both extend and replace; choose one",
            remediation=(
                "Use redact.extend to append patterns to the built-ins; "
                "use redact.replace to substitute them entirely."
            ),
        )

    unknown = set(block) - {"extend", "replace"}
    if unknown:
        first = sorted(unknown)[0]
        raise UnknownConfigKeyError(key=first, scope="safety.redact")

    if has_extend:
        extra = block["extend"]
        if not isinstance(extra, list):
            raise InvalidConfigError(
                message=f"redact.extend must be a list; got {type(extra).__name__}",
            )
        return DEFAULT_REDACT_PATTERNS + tuple(extra)

    # has_replace branch (mutual exclusion enforced above).
    replacements = block["replace"]
    if not isinstance(replacements, list):
        raise InvalidConfigError(
            message=f"redact.replace must be a list; got {type(replacements).__name__}",
        )
    if len(replacements) == 0:
        _LOGGER.warning(
            "redaction disabled: %s",
            '{"message":"signalforge.yml: redact.replace=[] disables all redaction patterns"}',
        )
    return tuple(replacements)


class SafetyPolicy(BaseModel):
    """User-facing safety-policy config (DEC-015 + DEC-017 + DEC-018).

    Frozen Pydantic v2 model with ``extra="forbid"`` so typos at the
    top level (``redacts:`` instead of ``redact:``) raise loudly. Field
    defaults match SignalForge's "secure by default" posture: schema-only
    mode, the six built-in redaction patterns, and a project-relative
    audit path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    mode: SamplingMode = SamplingMode.SCHEMA_ONLY
    redact_patterns: tuple[str, ...] = DEFAULT_REDACT_PATTERNS
    sample_size: int = 100
    audit_path: Path = DEFAULT_AUDIT_PATH

    @model_validator(mode="before")
    @classmethod
    def _resolve_redact_patterns_validator(cls, data: Any) -> Any:
        """Translate ``redact: {extend|replace: [...]}`` into
        ``redact_patterns`` before per-field validation runs.

        Only handles dict input. Already-built ``SafetyPolicy`` instances
        and other shapes pass through untouched.
        """
        if not isinstance(data, dict):
            return data
        if "redact" in data:
            redact_block = data.pop("redact")
            data["redact_patterns"] = _resolve_redact_patterns(redact_block)
        return data

    @field_validator("mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: Any) -> Any:
        """Accept any case + ``-`` / ``_`` mix; reject unknown values."""
        if isinstance(value, SamplingMode):
            return value
        if isinstance(value, str):
            normalised = value.lower().replace("_", "-")
            for member in SamplingMode:
                if member.value == normalised:
                    return member
            raise InvalidSamplingModeError(
                value=value,
                allowed=tuple(m.value for m in SamplingMode),
            )
        # Non-string, non-enum input (e.g. mode=42, mode=None, mode=[]) must
        # raise the typed error rather than fall through to Pydantic's generic
        # ValidationError — keeps the safety-layer error hierarchy homogeneous.
        raise InvalidSamplingModeError(
            value=value,
            allowed=tuple(m.value for m in SamplingMode),
        )

    @field_validator("redact_patterns")
    @classmethod
    def _validate_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for p in patterns:
            if p == "":
                raise InvalidPatternError(value=p, reason="empty pattern")
            if p == "*":
                raise InvalidPatternError(
                    value=p,
                    reason=(
                        "matches all column names; use redact: {replace: []} "
                        "to disable redaction explicitly"
                    ),
                )
            if p == "?":
                raise InvalidPatternError(
                    value=p,
                    reason="matches every single-character column name",
                )
        return patterns

    @model_validator(mode="after")
    def _warn_on_sample_mode(self) -> SafetyPolicy:
        if self.mode is SamplingMode.SAMPLE:
            _LOGGER.warning(
                "sample mode enabled: %s",
                (
                    '{"message":"Sample mode enabled — raw row data will be '
                    'sent to the LLM. Verify column tags/meta opt-outs."}'
                ),
            )
        return self

    def with_mode(self, mode: SamplingMode) -> SafetyPolicy:
        """Return a new :class:`SafetyPolicy` with ``mode`` overridden.

        Used by issue #9's CLI to apply ``--mode`` after loading from
        ``signalforge.yml``. Frozen Pydantic models cannot be mutated;
        this is the canonical override path (DEC-018).

        Re-runs all validators (including the sample-mode WARNING per
        DEC-021) by going through ``model_validate`` rather than
        ``model_copy``. ``model_copy(update=...)`` is a shallow shortcut
        that skips ``@model_validator(mode="after")``, which would
        silently enable sample mode without emitting the WARNING — a
        regression caught by Quality-Gate review.
        """
        data = self.model_dump()
        data["mode"] = mode
        return SafetyPolicy.model_validate(data)


def _compute_policy_hash(policy: SafetyPolicy) -> str:
    """Deterministic 16-hex-char ``blake2b`` digest of the policy (DEC-014).

    The hash is what :class:`signalforge.safety.models.AuditEvent.policy_hash`
    carries for every audit row, letting future readers tell whether two
    audits ran under the same policy without re-loading
    ``signalforge.yml``. The double-serialise (``model_dump_json`` →
    ``json.dumps(sort_keys=True)``) forces canonical key ordering since
    Pydantic does not guarantee it.

    Issue #55 migrated from ``SHA-256[:16]`` to ``blake2b(digest_size=8)``
    so every reproducibility hash in the audit / sidecar corpus reads one
    recipe. Consumers correlating ``policy_hash`` across audit JSONLs must
    gate on ``audit_schema_version >= 3`` to skip records produced by the
    pre-migration writer.

    ``audit_path`` is dumped as a string by Pydantic; the hash is stable
    across runs as long as the policy serialises identically.
    """
    payload = policy.model_dump_json(by_alias=True, exclude_none=False)
    canonical = json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


__all__ = [
    "DEFAULT_REDACT_PATTERNS",
    "DEFAULT_AUDIT_PATH",
    "SafetyPolicy",
]
