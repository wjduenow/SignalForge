"""Diff-renderer config loader.

Implements US-003 of issue #8 (DEC-010): introduces the ``diff:`` top-level
namespace in ``signalforge.yml`` and the typed :class:`DiffConfig` knob
block. Mirrors :mod:`signalforge.grade.config`,
:mod:`signalforge.prune.config`, and :mod:`signalforge.draft.config`
verbatim so the CLI (#9) and any future orchestrator sees one calling
convention across stages: ``load_<stage>_config(project_dir, path=None) ->
<Stage>Config``.

The outer file wrapper :class:`_DiffConfigFile` uses ``extra="ignore"`` at
top level so sibling stage namespaces (``safety:``, ``llm:``, ``prune:``,
``grade:``) silently coexist; the inner :class:`DiffConfig` uses
``extra="forbid"`` so a typo like ``contxt_lines:`` instead of
``context_lines:`` fails loud rather than silently no-op'ing
(``safety-layer.md`` DEC-015 / ``prune-engine.md`` DEC-020 /
``grade-layer.md`` DEC-029).

Design commitments operationalised here (``plans/super/8-diff-renderer.md``):

* **DEC-010** — Field set and ``diff:`` namespace. The nine knobs are the
  locked v0.1 surface; every cap is user-overridable downward only; the
  renderer doesn't read above its own defaults.

Resolution order (mirrors :func:`signalforge.grade.config.load_grade_config`):

* ``path is None``: candidate is ``<project_dir>/signalforge.yml``.
  Missing → :class:`DiffConfig` defaults silently.
* ``path is not None``: explicit path. Missing → raise :class:`DiffError`
  with a remediation (the operator pointed at a file that does not exist;
  silent no-op would mask the typo).
* File present but ``diff:`` key absent or null → return defaults (other
  top-level keys reserved per DEC-010 namespacing).
* ``diff:`` block well-formed → return the populated :class:`DiffConfig`.
* Unknown / typo'd inner field, non-mapping ``diff:`` block, YAML parse
  failure, or :class:`pydantic.ValidationError` from :class:`DiffConfig`
  → :class:`DiffError` with the underlying exception preserved on
  ``__cause__``.

Implementation note on the typed-error choice. The error hierarchy from
US-001 (``signalforge.diff.errors``) ships seven classes — none of them a
"config" error — and the US-003 task instructions explicitly forbid adding
new subclasses in this ticket. The loader therefore raises the base
:class:`DiffError` with a descriptive ``remediation``; this keeps the
public surface stable for US-013 (the eventual CLI wiring) where a
``DiffConfigError`` could be added cleanly without breaking imports.
Mirrors the pattern used by :func:`signalforge.grade.config.load_grade_config`,
which raises a typed :class:`GradeConfigError`; the loader-level
behaviour is identical even though the exception class differs.

``yaml.safe_load`` only — ``yaml.load`` accepts arbitrary Python object
construction tags and is unsafe for any input we don't fully control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.diff.errors import DiffError

_DEFAULT_CONFIG_FILENAME = "signalforge.yml"

_RenderKind = Literal["ansi", "markdown", "json"]


class DiffConfig(BaseModel):
    """User-facing knobs for the diff renderer (DEC-010).

    Lives under the ``diff:`` top-level key in ``signalforge.yml``. The
    namespacing convention is established by ``safety-layer.md`` DEC-025
    / ``llm-drafter.md`` DEC-027 / ``prune-engine.md`` DEC-020 /
    ``grade-layer.md`` DEC-029 — each pipeline stage claims one top-level
    key. Sibling keys are silently ignored by this loader (they belong to
    other stages).

    Config-shaped per ``safety-layer.md`` DEC-015: ``extra="forbid"`` so
    typos like ``contxt_lines:`` instead of ``context_lines:`` fail loud
    rather than silently no-op'ing. The :class:`_DiffConfigFile` outer
    wrapper uses ``extra="ignore"`` so other top-level keys (``safety:``,
    ``llm:``, ``prune:``, ``grade:``) don't trip the strict validator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    context_lines: int = 3
    """Unified-diff context lines (passes through to
    :func:`difflib.unified_diff`'s ``n=``). Three lines is the
    ``diff -u`` default."""

    max_why_chars: int = 80
    """Hard truncation cap on the per-row ``why`` column in the
    kept/dropped table. Beyond this, the renderer truncates with an
    ellipsis. Keeps the table readable at any terminal width."""

    narrow_terminal_threshold: int = 60
    """Below this column count, the AnsiRenderer drops the ``why``
    column from the kept/dropped table and emits each ``why`` as a
    wrapped follow-up line below its row (DEC-013 of the plan).
    Defaults to 60 columns; tested against a 40-col snapshot fixture."""

    markdown_max_diff_chars: int = 60_000
    """Markdown body truncation cap (DEC-005). When the rendered
    Markdown diff body exceeds this cap, the MarkdownRenderer truncates
    the diff section (preserving complete hunks where possible) and
    appends a truncation marker."""

    existing_schema_size_limit_bytes: int = 10_485_760  # 10 MB
    """Hard cap on ``existing_schema`` YAML byte length, enforced
    BEFORE any ``yaml.safe_load`` call (DEC-006). Defends against
    billion-laughs / deep-nesting attacks. Above this cap raises
    :class:`signalforge.diff.errors.DiffInputTooLargeError`."""

    existing_schema_warn_at_bytes: int = 1_000_000  # 1 MB
    """Soft warning threshold for ``existing_schema`` YAML byte
    length (DEC-014). The renderer emits one ``WARNING`` log line
    when the payload exceeds this threshold but stays below the hard cap
    (:attr:`existing_schema_size_limit_bytes`). Must be strictly less
    than the hard cap; the model validator enforces this invariant
    so a typo at config-load time fails loud rather than silently
    disabling DEC-014."""

    sidecar_size_limit_bytes: int = 10_000_000
    """Hard cap on the diff sidecar JSON byte length, enforced BEFORE
    any ``os.open`` (DEC-009). Defends against pathological 1000-column
    models. Above this cap raises
    :class:`signalforge.diff.errors.DiffSidecarRecordTooLargeError`."""

    render_kind: _RenderKind = "ansi"
    """Selects which renderer concrete drives stdout output (DEC-004).
    The sidecar always uses the JSON renderer regardless of this
    setting; ``render_kind`` only governs the human-facing output."""

    respect_no_color_env: bool = True
    """When ``True`` (the default), the AnsiRenderer honours
    ``NO_COLOR`` and ``FORCE_COLOR`` environment variables along with
    ``sys.stdout.isatty()`` (DEC-021). When ``False``, colour is
    forced regardless — useful for tests and non-tty pipelines that
    want ANSI output."""

    @field_validator(
        "context_lines",
        "max_why_chars",
        "narrow_terminal_threshold",
        "markdown_max_diff_chars",
        "existing_schema_size_limit_bytes",
        "existing_schema_warn_at_bytes",
        "sidecar_size_limit_bytes",
    )
    @classmethod
    def _positive(cls, v: int) -> int:
        # All numeric knobs are byte-counts or character-counts that
        # must be strictly positive. A zero or negative cap would
        # silently disable the protection (DEC-006/DEC-009) or render
        # an empty table; refuse at config-load time.
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @model_validator(mode="after")
    def _warn_below_limit(self) -> DiffConfig:
        # The DEC-014 soft-warn fires when a payload exceeds
        # ``existing_schema_warn_at_bytes`` but stays below
        # ``existing_schema_size_limit_bytes``. If warn-at is >= the hard
        # cap, the warning is dead code — fail loud at config-load time
        # rather than silently disabling the contract.
        if self.existing_schema_warn_at_bytes >= self.existing_schema_size_limit_bytes:
            raise ValueError(
                "existing_schema_warn_at_bytes "
                f"({self.existing_schema_warn_at_bytes}) must be strictly less "
                f"than existing_schema_size_limit_bytes "
                f"({self.existing_schema_size_limit_bytes})"
            )
        return self


class _DiffConfigFile(BaseModel):
    """Outer wrapper for the ``signalforge.yml`` top-level mapping.

    ``extra="ignore"`` at this level — sibling top-level keys
    (``safety:``, ``llm:``, ``prune:``, ``grade:``, future ``cli:`` ...)
    are reserved for other stages per the namespacing convention and
    must not trigger a diff-layer validation error. The strict
    ``extra="forbid"`` lives on :class:`DiffConfig` itself.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    diff: DiffConfig = Field(default_factory=DiffConfig)


def load_diff_config(project_dir: Path, path: Path | None = None) -> DiffConfig:
    """Load a :class:`DiffConfig` from ``signalforge.yml``.

    Mirrors :func:`signalforge.grade.config.load_grade_config`,
    :func:`signalforge.prune.config.load_prune_config`, and
    :func:`signalforge.draft.config.load_draft_config` so the CLI (#9)
    and any future orchestrator sees one calling convention across
    stages: ``(project_dir, path=None)``.

    Resolution:

    * ``path is None``: look for ``<project_dir>/signalforge.yml``.
      Missing → :class:`DiffConfig` defaults silently.
    * ``path is not None``: use that exact path. Missing → raise
      :class:`signalforge.diff.errors.DiffError` (mirrors the grader's
      explicit-path-missing behaviour). Silent no-op would mask a typo
      in the operator's CLI flag.

    Args:
        project_dir: Project root used as the base for the default
            config-file lookup (``<project_dir>/signalforge.yml``).
        path: Optional explicit config path. ``None`` falls back to the
            project-relative default.

    Returns:
        A fully-validated :class:`DiffConfig`. When the file is absent,
        empty, or the ``diff:`` key is missing, the defaults from
        DEC-010 apply.

    Raises:
        DiffError: The explicit ``path`` is missing, the file is not
            valid YAML, its top level is not a mapping, the ``diff:``
            block is not a mapping, or the contents fail
            :class:`DiffConfig` validation (typo, out-of-range numeric
            knob, ...). The original :class:`pydantic.ValidationError`
            (if any) is preserved on ``__cause__``. US-003 reuses the
            base :class:`DiffError` rather than introducing a new
            subclass; a future ``DiffConfigError`` is a possible v0.2
            refinement.
    """
    if path is not None:
        config_file = path
        if not config_file.exists():
            raise DiffError(
                f"signalforge diff config file not found at {config_file!r}",
                remediation=(
                    "The explicit config path passed to load_diff_config "
                    "does not exist. Verify the path (typo, wrong working "
                    "directory) or omit the argument to fall back to "
                    "<project_dir>/signalforge.yml."
                ),
            )
    else:
        config_file = project_dir / _DEFAULT_CONFIG_FILENAME
        if not config_file.exists():
            return DiffConfig()

    # Symlink-harden the resolved config path BEFORE reading it.
    # Mirrors the orchestrator-level canonicalisation applied to
    # ``output_path`` / ``sidecar_path`` (post-QG fix; see
    # ``.claude/rules/diff-renderer.md``). A symlinked
    # ``signalforge.yml`` pointing outside ``project_dir`` (or a
    # cycle) is rejected here, well before
    # :meth:`pathlib.Path.read_text`.
    try:
        canonical = canonicalise_path(config_file, project_dir)
    except PathContainmentError as exc:
        raise DiffError(
            f"signalforge diff config path failed canonicalisation: {config_file!r}",
            remediation=(
                "The config path resolved outside <project_dir> or "
                "contains a symlink loop. Verify <project_dir> and the "
                "config file's symlink target."
            ),
        ) from exc

    try:
        raw_text = canonical.read_text(encoding="utf-8").strip()
    except (OSError, IsADirectoryError, PermissionError) as exc:
        raise DiffError(
            f"signalforge.yml could not be read: {exc}",
            remediation=(
                "Verify the config file is a regular file, readable by "
                "the current user, and not locked by another process."
            ),
        ) from exc
    if not raw_text:
        return DiffConfig()

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise DiffError(
            f"signalforge.yml is not valid YAML: {exc}",
            remediation=(
                "Fix the YAML syntax error reported above. Common causes: "
                "unbalanced quotes, tabs in indentation, or unescaped "
                "special characters."
            ),
        ) from exc

    if loaded is None:
        # File parses to None (e.g. only comments) — same as empty.
        return DiffConfig()

    if not isinstance(loaded, dict):
        raise DiffError(
            f"signalforge.yml top level must be a mapping; got {type(loaded).__name__}",
            remediation=(
                "The top level of signalforge.yml must be a YAML mapping "
                "with stage keys (e.g. `diff:`, `grade:`, `prune:`)."
            ),
        )

    if "diff" not in loaded or loaded["diff"] is None:
        # Missing `diff:` key (or `diff:` with null value) — sibling
        # top-level keys reserved per the namespacing convention.
        return DiffConfig()

    diff_block = loaded["diff"]
    if not isinstance(diff_block, dict):
        raise DiffError(
            f"signalforge.yml: 'diff' must be a mapping; got {type(diff_block).__name__}",
            remediation=(
                "The 'diff:' block must be a YAML mapping of knob names to "
                "values. See the DiffConfig field set for the supported keys."
            ),
        )

    try:
        wrapper = _DiffConfigFile.model_validate({"diff": diff_block})
    except ValidationError as exc:
        raise DiffError(
            f"signalforge.yml: 'diff' block failed schema validation: {exc}",
            remediation=(
                "Check the 'diff:' block for typos, unknown fields, or "
                "out-of-range numeric values. The strict `extra=forbid` "
                "policy on DiffConfig is intentional — silent no-op on a "
                "typo would mask a real config error."
            ),
        ) from exc

    return wrapper.diff


__all__ = ["DiffConfig", "load_diff_config"]
