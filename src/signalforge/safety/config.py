"""Safety-config loader for ``signalforge.yml`` (US-006).

Implements DEC-016 (the full error contract for resolution / parsing) and
DEC-013 (symlink-hardened ``audit_path`` containment check) on top of the
:class:`signalforge.safety.policy.SafetyPolicy` model.

Resolution order (DEC-016):

1. If ``path=`` is explicit, the file MUST exist; missing →
   :class:`signalforge.safety.errors.ConfigNotFoundError`.
2. Else ``<project_dir>/signalforge.yml``. Missing → defaults silently.
3. Empty file (zero bytes / whitespace-only / only YAML comments) →
   defaults (DEBUG log).
4. Non-mapping top-level (YAML list, scalar, …) →
   :class:`signalforge.safety.errors.InvalidConfigError`.
5. Missing ``safety:`` key → defaults (other top-level keys reserved per
   DEC-025 namespace).
6. Schema-invalid contents → typed errors
   (:class:`signalforge.safety.errors.InvalidSamplingModeError`,
   :class:`signalforge.safety.errors.InvalidPatternError`,
   :class:`signalforge.safety.errors.UnknownConfigKeyError`,
   :class:`signalforge.safety.errors.PolicyValidationError`).

``audit_path`` is pre-validated (reject ``..`` segments) and routed through
:func:`signalforge.safety._path_safety.canonicalise_path` before being
handed to :class:`SafetyPolicy`. Both user-supplied and default paths flow
through the same gate.

``yaml.safe_load`` only — ``yaml.load`` accepts arbitrary Python object
construction tags and is unsafe for any input we don't fully control.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from signalforge.safety._path_safety import canonicalise_path
from signalforge.safety.errors import (
    ConfigNotFoundError,
    InvalidConfigError,
    InvalidPatternError,
    InvalidSamplingModeError,
    PolicyValidationError,
    UnknownConfigKeyError,
)
from signalforge.safety.policy import DEFAULT_AUDIT_PATH, SafetyPolicy

_LOGGER: Final = logging.getLogger("signalforge.safety")
_DEFAULT_CONFIG_FILENAME: Final = "signalforge.yml"


def load_safety_config(project_dir: Path, path: Path | None = None) -> SafetyPolicy:
    """Load a :class:`SafetyPolicy` from ``signalforge.yml``.

    See module docstring for the full resolution order and error contract.

    Args:
        project_dir: Project root used both as the base for the default
            config-file lookup (``<project_dir>/signalforge.yml``) and as
            the containment boundary for ``audit_path``.
        path: Optional explicit config path. When given the file must
            exist; missing raises :class:`ConfigNotFoundError`.

    Returns:
        A fully-validated :class:`SafetyPolicy`.

    Raises:
        ConfigNotFoundError: Explicit ``path=`` was given and the file does
            not exist.
        InvalidConfigError: The file is not valid YAML, its top level is
            not a mapping, the ``safety:`` block is not a mapping, or
            ``audit_path`` contains ``..`` segments / escapes
            ``project_dir`` / traverses a symlink loop.
        InvalidSamplingModeError: ``safety.mode`` is not a known sampling
            mode.
        InvalidPatternError: A redact pattern is empty or one of the bare
            wildcards ``"*"`` / ``"?"``.
        UnknownConfigKeyError: An unknown key was found under a known
            scope (e.g. ``safety.redacts:`` instead of
            ``safety.redact:``, or top-level ``safety.foo:``).
        PolicyValidationError: Generic Pydantic validation failure not
            covered by the more specific exceptions above.
    """
    # Default audit_path is project-relative (`.signalforge/audit.jsonl`).
    # Canonicalise against project_dir so every default-fallback branch ends
    # up with the same absolute, symlink-hardened path the user-override
    # branch produces. Without this, a user who omits `signalforge.yml`
    # gets an audit log at CWD-relative `.signalforge/audit.jsonl` rather
    # than `<project_dir>/.signalforge/audit.jsonl`. Reported by Copilot
    # PR review.
    default_audit_path = canonicalise_path(DEFAULT_AUDIT_PATH, project_dir)

    if path is not None:
        config_file = path
        if not config_file.exists():
            raise ConfigNotFoundError(path=config_file)
    else:
        config_file = project_dir / _DEFAULT_CONFIG_FILENAME
        if not config_file.exists():
            return SafetyPolicy(audit_path=default_audit_path)

    raw_text = config_file.read_text(encoding="utf-8").strip()
    if not raw_text:
        _LOGGER.debug("safety config file %r is empty; using defaults", str(config_file))
        return SafetyPolicy(audit_path=default_audit_path)

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise InvalidConfigError(
            message=f"signalforge.yml is not valid YAML: {exc}",
        ) from exc

    if loaded is None:
        # File parses to None (e.g. only comments) — same as empty.
        return SafetyPolicy(audit_path=default_audit_path)

    if not isinstance(loaded, dict):
        raise InvalidConfigError(
            message=(f"signalforge.yml top level must be a mapping; got {type(loaded).__name__}"),
        )

    safety_block = loaded.get("safety")
    if safety_block is None:
        # Missing safety: key — other top-level keys reserved per DEC-025.
        return SafetyPolicy(audit_path=default_audit_path)

    if not isinstance(safety_block, dict):
        raise InvalidConfigError(
            message=(
                f"signalforge.yml: 'safety' must be a mapping; got {type(safety_block).__name__}"
            ),
        )

    # Pre-validate audit_path (DEC-013): reject `..` segments outright,
    # then canonicalise + containment-check via the symlink-hardened helper.
    audit_path_raw = safety_block.get("audit_path")
    if audit_path_raw is not None:
        # Validate the type up front. YAML can parse `audit_path: 123` to
        # an int (or `audit_path:` to None — already filtered above) which
        # would crash inside `Path(...)` with TypeError, leaking a
        # non-SafetyError exception. Reported by Copilot PR review.
        if not isinstance(audit_path_raw, (str, os.PathLike)):
            raise InvalidConfigError(
                message=(
                    f"audit_path must be a string or path-like; "
                    f"got {type(audit_path_raw).__name__} ({audit_path_raw!r})"
                ),
                remediation=(
                    "Quote the value in signalforge.yml so YAML parses it as a string "
                    "(default: .signalforge/audit.jsonl)."
                ),
            )
        candidate = Path(audit_path_raw)
        if any(part == ".." for part in candidate.parts):
            raise InvalidConfigError(
                message=(f"audit_path may not contain '..' segments; got {audit_path_raw!r}"),
                remediation=(
                    "Use a path inside the project directory (default: .signalforge/audit.jsonl)."
                ),
            )
        # `canonicalise_path` accepts `Path | str`; narrow `os.PathLike`
        # input through `Path(...)` (already done as `candidate` above).
        resolved = canonicalise_path(candidate, project_dir)
        # Replace the raw value with the resolved Path so SafetyPolicy
        # stores the canonical form.
        safety_block = {**safety_block, "audit_path": resolved}
    else:
        # User did not override audit_path — apply the canonicalised default
        # so the policy's audit_path is always absolute (symmetric with the
        # branches above).
        safety_block = {**safety_block, "audit_path": default_audit_path}

    try:
        return SafetyPolicy.model_validate(safety_block)
    except (
        InvalidSamplingModeError,
        InvalidPatternError,
        InvalidConfigError,
        UnknownConfigKeyError,
    ):
        # Validators in SafetyPolicy raise our typed exceptions directly;
        # let those propagate without being wrapped.
        raise
    except ValidationError as exc:
        # Walk the error list and surface the most specific safety-layer
        # exception attached to a Pydantic error context, if any.
        for err in exc.errors():
            ctx = err.get("ctx", {}) or {}
            inner = ctx.get("error") if isinstance(ctx, dict) else None
            if isinstance(
                inner,
                (
                    InvalidSamplingModeError,
                    InvalidPatternError,
                    InvalidConfigError,
                    UnknownConfigKeyError,
                ),
            ):
                raise inner from exc
        # Translate Pydantic's `extra_forbidden` into our typed
        # UnknownConfigKeyError so DEC-026's contract holds: typos like
        # `redacts:` (instead of `redact:`) at the top of the safety block
        # surface as UnknownConfigKeyError, not the generic policy-validation
        # error. Reported by Copilot PR review.
        for err in exc.errors():
            if err.get("type") == "extra_forbidden":
                loc = err.get("loc", ())
                if loc:
                    bad_key = str(loc[-1])
                    scope = (
                        "safety." + ".".join(str(p) for p in loc[:-1]) if len(loc) > 1 else "safety"
                    )
                    raise UnknownConfigKeyError(key=bad_key, scope=scope) from exc
        # Last-resort wrap: surface the Pydantic failure as a typed
        # safety-layer error so callers can pattern-match.
        raise PolicyValidationError(
            field="<schema>",
            value=safety_block,
            reason=str(exc),
        ) from exc


__all__ = ["load_safety_config"]
