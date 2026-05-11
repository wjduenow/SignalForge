"""Draft-config loader for ``signalforge.yml`` (US-009).

Defines :class:`DraftConfig` — the config-shaped Pydantic v2 model that
mirrors the ``llm:`` block of ``signalforge.yml`` — and
:func:`load_draft_config`, the resolution helper. Mirrors the shape of
:mod:`signalforge.safety.config`.

Design commitments operationalised here:

* **DEC-005** — Default ``cache_ttl="5m"``; opt-in to ``"1h"`` via the
  config. The ``extended-cache-ttl-2025-04-11`` beta header is a
  consequence of selecting ``"1h"`` and is set by the LLM seam, not here.
* **DEC-011** — ``DraftConfig`` uses ``extra="forbid"`` (config-shaped:
  typos must fail loud — see ``safety-layer.md`` DEC-015). Contrast with
  read-back / response-shaped models which use ``extra="ignore"`` for
  forward-compat.
* **DEC-017** — Defaults: ``model="claude-sonnet-4-6"``,
  ``cheap_model="claude-haiku-4-5-20251001"``, ``max_output_tokens=4096``,
  ``cache_ttl="5m"``, ``max_retries_429=3``, ``max_retries_5xx=1``,
  ``max_retries_conn=1``.
* **DEC-027** — ``signalforge.yml`` top-level namespace key for this
  layer is ``llm:``. Other top-level keys (``safety:``, ``prune:``,
  ``grade:``, …) are reserved for other stages and silently ignored by
  this loader.

Resolution order:

1. If ``path=`` is explicit, the file MUST exist; missing →
   :class:`signalforge.draft.errors.DraftConfigNotFoundError`.
2. Else ``<project_dir>/signalforge.yml``. Missing → defaults silently.
3. Empty file (zero bytes / whitespace-only / only YAML comments) →
   defaults.
4. Non-mapping top-level (YAML list, scalar, …) →
   :class:`signalforge.draft.errors.DraftConfigInvalidError`.
5. Missing ``llm:`` key → defaults (other top-level keys reserved per
   DEC-027 namespace).
6. Schema-invalid contents (typo, unknown ``cache_ttl``, non-positive
   ``max_output_tokens``, ...) →
   :class:`signalforge.draft.errors.DraftConfigInvalidError` wrapping the
   underlying :class:`pydantic.ValidationError` on ``cause``.

``yaml.safe_load`` only — ``yaml.load`` accepts arbitrary Python object
construction tags and is unsafe for any input we don't fully control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from signalforge.draft.errors import DraftConfigInvalidError, DraftConfigNotFoundError

_DEFAULT_CONFIG_FILENAME = "signalforge.yml"


class DraftConfig(BaseModel):
    """User-facing draft-config model (DEC-017).

    Config-shaped per ``safety-layer.md`` DEC-015: ``extra="forbid"`` so
    typos in ``signalforge.yml`` fail loud rather than silently no-op.
    The :class:`_DraftConfigFile` outer wrapper uses ``extra="ignore"``
    so other top-level keys (``safety:``, ``prune:``, ...) reserved by
    DEC-027 don't trip the strict validator.

    All retry knobs are exposed so #9's batch-CLI mode can dial them down
    when iterating over many models in one run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    model: str = "claude-sonnet-4-6"
    """Default Anthropic model. Any string the SDK accepts is allowed —
    the three blessed IDs are documented in the README; ``cheap_model``
    holds the v0.1 Haiku ID."""

    cheap_model: str = "claude-haiku-4-5-20251001"
    """Informational; not selected automatically. The CLI (#9) flips on
    ``--cheap`` to swap ``model`` for this value."""

    max_output_tokens: int = 4096
    """Anthropic ``max_tokens`` ceiling. Must be positive (validator)."""

    cache_ttl: Literal["5m", "1h"] = "5m"
    """Prompt-cache TTL. ``"1h"`` opts into the
    ``extended-cache-ttl-2025-04-11`` beta header at the LLM seam."""

    max_retries_429: int = 3
    """429 (rate limit) retry budget."""

    max_retries_5xx: int = 1
    """5xx (server error) retry budget."""

    max_retries_conn: int = 1
    """Connection / transport-error retry budget."""

    @field_validator("max_output_tokens")
    @classmethod
    def _max_output_tokens_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_output_tokens must be positive")
        return v


class _DraftConfigFile(BaseModel):
    """Outer wrapper for the ``signalforge.yml`` top-level mapping.

    ``extra="ignore"`` at this level — other top-level keys (``safety:``,
    ``prune:``, ``grade:``, ...) are reserved for other stages per
    DEC-027 and must not trigger a draft-layer validation error. The
    strict ``extra="forbid"`` lives on :class:`DraftConfig` itself.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    llm: DraftConfig | None = None


def load_draft_config(project_dir: Path, path: Path | None = None) -> DraftConfig:
    """Load a :class:`DraftConfig` from ``signalforge.yml``.

    See module docstring for the full resolution order and error contract.

    Args:
        project_dir: Project root used as the base for the default
            config-file lookup (``<project_dir>/signalforge.yml``).
        path: Optional explicit config path. When given the file must
            exist; missing raises
            :class:`signalforge.draft.errors.DraftConfigNotFoundError`.

    Returns:
        A fully-validated :class:`DraftConfig`. When the file is absent
        or the ``llm:`` key is missing, the defaults from DEC-017 apply.

    Raises:
        DraftConfigNotFoundError: Explicit ``path=`` was given and the
            file does not exist.
        DraftConfigInvalidError: The file is not valid YAML, its top
            level is not a mapping, the ``llm:`` block is not a mapping,
            or the contents fail :class:`DraftConfig` validation (typo,
            unknown ``cache_ttl``, non-positive ``max_output_tokens``,
            ...). The original exception (if any) is preserved on
            ``cause``.
    """
    # Resolve project_dir for parity with other readers (manifest,
    # safety, warehouse). `signalforge.yml` is read directly off
    # `project_dir`, so there's no traversal attack surface here — the
    # `_path_safety` helper from the safety layer is overkill for this
    # story (per the US-014 duplication rule from `warehouse-adapters.md`,
    # we'd copy not import; for plain `project_dir / signalforge.yml`,
    # plain `Path.resolve` is sufficient).
    project_dir = project_dir.resolve(strict=True)

    if path is not None:
        config_file = path.resolve(strict=False)
        if not config_file.exists():
            raise DraftConfigNotFoundError(path=path)
    else:
        config_file = project_dir / _DEFAULT_CONFIG_FILENAME
        if not config_file.exists():
            return DraftConfig()

    raw_text = config_file.read_text(encoding="utf-8").strip()
    if not raw_text:
        return DraftConfig()

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise DraftConfigInvalidError(
            f"signalforge.yml is not valid YAML: {exc}",
            cause=exc,
        ) from exc

    if loaded is None:
        # File parses to None (e.g. only comments) — same as empty.
        return DraftConfig()

    if not isinstance(loaded, dict):
        raise DraftConfigInvalidError(
            f"signalforge.yml top level must be a mapping; got {type(loaded).__name__}",
        )

    if "llm" not in loaded or loaded["llm"] is None:
        # Missing `llm:` key (or `llm:` with null value) — other
        # top-level keys reserved per DEC-027 namespace.
        return DraftConfig()

    llm_block = loaded["llm"]
    if not isinstance(llm_block, dict):
        raise DraftConfigInvalidError(
            f"signalforge.yml: 'llm' must be a mapping; got {type(llm_block).__name__}",
        )

    try:
        wrapper = _DraftConfigFile.model_validate({"llm": llm_block})
    except ValidationError as exc:
        raise DraftConfigInvalidError(
            f"signalforge.yml: 'llm' block failed schema validation: {exc}",
            cause=exc,
        ) from exc

    # `wrapper.llm` is non-None here because we already filtered the
    # missing-key branch above; assert for the type checker.
    assert wrapper.llm is not None
    return wrapper.llm


__all__ = ["DraftConfig", "load_draft_config"]
