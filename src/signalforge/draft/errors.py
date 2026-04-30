"""Typed exception hierarchy for the schema-drafting layer.

Implements US-007 (DEC-003 typed CandidateSchema + anchor-contract validator,
DEC-006 response audit). Mirrors the style established by
:mod:`signalforge.llm.errors` and :mod:`signalforge.safety.errors`: every
error carries a class-level ``default_remediation`` that the base
``__str__`` renders on a separate ``↳ Remediation:`` line, and every
user-supplied string is rendered through :func:`_format_value` (i.e.
``repr()``) so adversarial input — embedded quotes, control chars, ANSI
escapes — cannot smuggle special characters into log viewers or error
messages.

The hierarchy is two-tiered:

- :class:`DraftError` — base for everything in this layer.
- :class:`LLMOutputError` — base for parse-time failures of the LLM's
  textual response. Carries the bad-JSON envelope (``raw_text``,
  ``parse_position``, ``prompt_version``, ``model``, ``cache_hit``,
  ``input_tokens``, ``output_tokens``, ``excerpt``) so the CLI / response
  audit can render a forensically-useful incident report without sniffing
  message text. The full ``raw_text`` is preserved on the attribute even
  though ``__str__`` truncates the rendering.
- :class:`LLMResponseAuditWriteError` — fail-closed response-audit write
  failure. Direct :class:`DraftError` subclass (parallel to safety's
  :class:`signalforge.safety.errors.AuditWriteError`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

#: Maximum number of characters of ``raw_text`` rendered in
#: :meth:`LLMOutputError.__str__`. The full ``raw_text`` is always preserved
#: on the attribute — the cap only bounds error-message output to keep log
#: lines reasonable. 4 KB matches the safety-layer audit-record cap.
_RAW_TEXT_RENDER_LIMIT: int = 4000

#: Window radius (chars) around ``parse_position`` captured into ``excerpt``.
#: ``2 * _EXCERPT_RADIUS`` is the maximum excerpt length when the position is
#: in the middle of the text.
_EXCERPT_RADIUS: int = 80


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages (DEC-022).

    Embedding raw user input in error strings is a log-injection seam: a
    crafted value like ``"foo'\\nINFO: spoofed log line"`` (or an ANSI
    escape such as ``"\\x1b[31m"``) could pollute log viewers or stack
    traces. Routing every user-controlled value through ``repr()`` quotes
    the string, escapes control characters, and makes whitespace visible.
    """
    return repr(v)


def _compute_excerpt(raw_text: str, parse_position: tuple[int, int] | None) -> str:
    """Return a ``±_EXCERPT_RADIUS``-char window around ``parse_position``.

    Falls back to the first ``2 * _EXCERPT_RADIUS`` chars of ``raw_text`` when
    ``parse_position`` is ``None``. The position is converted from
    1-indexed ``(line, column)`` into a 0-indexed character offset by
    walking ``raw_text`` line-by-line — robust against multi-line responses.

    A sentinel ``⟨HERE⟩`` marker is injected at the offending offset so a
    reviewer can see exactly where parsing failed even when the surrounding
    text is entirely valid-looking JSON.
    """
    if parse_position is None:
        return raw_text[: 2 * _EXCERPT_RADIUS]

    # Resolve (line, column) -> 0-indexed offset. JSONDecodeError uses
    # 1-indexed lineno / colno; Pydantic ValidationError loc paths don't
    # carry a position at all (we accept None for those). If line is past
    # the end of raw_text, clamp to len(raw_text).
    line, column = parse_position
    offset = 0
    current_line = 1
    while current_line < line and offset < len(raw_text):
        nl = raw_text.find("\n", offset)
        if nl == -1:
            offset = len(raw_text)
            break
        offset = nl + 1
        current_line += 1
    offset += max(column - 1, 0)
    offset = max(0, min(offset, len(raw_text)))

    start = max(0, offset - _EXCERPT_RADIUS)
    end = min(len(raw_text), offset + _EXCERPT_RADIUS)

    # Inject a sentinel at the offending offset so the reviewer can see
    # exactly where parsing failed. The ⟨HERE⟩ marker is intentionally a
    # rare unicode bracket pair that won't collide with anything an LLM
    # is likely to emit verbatim.
    rel = offset - start
    window = raw_text[start:end]
    return window[:rel] + " ⟨HERE⟩ " + window[rel:]


class DraftError(Exception):
    """Base class for all draft-layer errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log output
    and CLI output both read cleanly.
    """

    default_remediation: ClassVar[str] = "(no remediation set — this is the base class)"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = (
            remediation if remediation is not None else type(self).default_remediation
        )

    def __str__(self) -> str:
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class LLMOutputError(DraftError):
    """Base for failures parsing the LLM's textual response.

    Carries the bad-JSON envelope so the CLI / response audit can render a
    forensically-useful incident report without sniffing message text:

    - ``raw_text`` — the full LLM response. Preserved on the attribute even
      when ``__str__`` truncates the rendering, because the response audit
      (US-012) needs it intact.
    - ``parse_position`` — 1-indexed ``(line, column)`` if known. ``None``
      for failures (e.g. Pydantic validation, anchor-contract) where no
      position-in-text applies.
    - ``prompt_version``, ``model``, ``cache_hit``, ``input_tokens``,
      ``output_tokens`` — provenance for "what produced this bad output."
    - ``excerpt`` — a ``±80``-char window around ``parse_position``,
      computed at construction time and rendered by ``__str__``. Inserts a
      ``⟨HERE⟩`` sentinel at the offending offset.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the LLM's raw response (preserved on `raw_text`) and the "
        "excerpt around the failure point. Re-run with a clearer manifest "
        "summary or retry the call; the prompt may be too ambiguous."
    )

    def __init__(
        self,
        message: str,
        *,
        raw_text: str,
        prompt_version: str,
        model: str,
        cache_hit: bool,
        input_tokens: int,
        output_tokens: int,
        parse_position: tuple[int, int] | None = None,
        remediation: str | None = None,
    ) -> None:
        self.raw_text = raw_text
        self.parse_position = parse_position
        self.prompt_version = prompt_version
        self.model = model
        self.cache_hit = cache_hit
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.excerpt = _compute_excerpt(raw_text, parse_position)
        super().__init__(message, remediation=remediation)

    def __str__(self) -> str:
        # We deliberately do NOT render the full raw_text — log lines
        # explode. The excerpt + position tail is the forensic surface;
        # the full raw_text lives on the attribute for the audit writer
        # (US-012) to capture. The 4 KB cap is belt-and-braces in case a
        # subclass ever decides to render raw_text directly.
        head = f"{self.message}\n  ↳ Remediation: {self.remediation}"
        if self.parse_position is not None:
            line, col = self.parse_position
            position_repr = f"line={line} col={col}"
        else:
            position_repr = "unknown"
        tail = (
            f"\n  ↳ Position: {position_repr} "
            f"(model={_format_value(self.model)}, "
            f"prompt_version={_format_value(self.prompt_version)})"
        )
        excerpt_render = self.excerpt
        if len(excerpt_render) > _RAW_TEXT_RENDER_LIMIT:
            excerpt_render = excerpt_render[:_RAW_TEXT_RENDER_LIMIT] + " …[truncated]"
        return f"{head}\n  ↳ Excerpt: {_format_value(excerpt_render)}{tail}"


class LLMOutputJSONError(LLMOutputError):
    """The LLM's response was not valid JSON.

    Auto-derives ``parse_position`` from the underlying
    :class:`json.JSONDecodeError` so the excerpt window is centred on the
    exact byte offset reported by the JSON parser.
    """

    default_remediation: ClassVar[str] = (
        "Check the LLM's raw response for truncation or malformed JSON "
        "(unbalanced braces, trailing comma, embedded prose). The model may "
        "have hit `max_output_tokens`; raise the cap or simplify the prompt."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: json.JSONDecodeError,
        raw_text: str,
        prompt_version: str,
        model: str,
        cache_hit: bool,
        input_tokens: int,
        output_tokens: int,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(
            message,
            raw_text=raw_text,
            parse_position=(cause.lineno, cause.colno),
            prompt_version=prompt_version,
            model=model,
            cache_hit=cache_hit,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            remediation=remediation,
        )


class LLMOutputValidationError(LLMOutputError):
    """The LLM's response parsed as JSON but didn't match the
    ``CandidateSchema`` shape.

    The Pydantic :class:`ValidationError` is preserved on ``cause``;
    ``parse_position`` is ``None`` because the failure is structural rather
    than positional in the text.
    """

    default_remediation: ClassVar[str] = (
        "The LLM produced JSON that did not match the CandidateSchema shape "
        "— likely a field with the wrong type or a missing required field. "
        "Inspect `cause.errors()` for the specific path; consider tightening "
        "the prompt's format section."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: ValidationError,
        raw_text: str,
        prompt_version: str,
        model: str,
        cache_hit: bool,
        input_tokens: int,
        output_tokens: int,
        parse_position: tuple[int, int] | None = None,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(
            message,
            raw_text=raw_text,
            parse_position=parse_position,
            prompt_version=prompt_version,
            model=model,
            cache_hit=cache_hit,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            remediation=remediation,
        )


class LLMOutputAnchorContractError(LLMOutputError):
    """The LLM's response violated the anchor contract.

    "Anchor contract" (DEC-003 / DEC-022): every column referenced by a
    candidate test must exist on the input model. Violations include tests
    citing nonexistent columns, model-level tests citing nonexistent
    columns, and duplicate test names within a column.

    ``violations`` carries one human-readable string per violation so a
    reviewer can see the full picture in a single error rather than
    iteratively re-running until each violation surfaces.
    """

    default_remediation: ClassVar[str] = (
        "The LLM referenced columns that don't exist in the input model — "
        "likely a hallucinated column name. Consider re-running with a "
        "clearer manifest summary, or trim ambiguous neighbour models that "
        "may have leaked column names into the LLM's context."
    )

    def __init__(
        self,
        message: str,
        *,
        violations: tuple[str, ...],
        raw_text: str,
        prompt_version: str,
        model: str,
        cache_hit: bool,
        input_tokens: int,
        output_tokens: int,
        parse_position: tuple[int, int] | None = None,
        remediation: str | None = None,
    ) -> None:
        self.violations = violations
        super().__init__(
            message,
            raw_text=raw_text,
            parse_position=parse_position,
            prompt_version=prompt_version,
            model=model,
            cache_hit=cache_hit,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            remediation=remediation,
        )


class DraftConfigNotFoundError(DraftError):
    """An explicit ``path=`` argument pointed at a missing draft config file.

    Raised only when the caller passed a path explicitly to
    :func:`signalforge.draft.config.load_draft_config`; the implicit
    default-discovery path (``<project_dir>/signalforge.yml``) is allowed to
    be absent and falls back to built-in :class:`DraftConfig` defaults.

    Mirrors :class:`signalforge.safety.errors.ConfigNotFoundError` (US-006).
    """

    default_remediation: ClassVar[str] = (
        "Verify the path is correct, or pass path=None to fall back to "
        "<project_dir>/signalforge.yml or built-in DraftConfig defaults."
    )

    def __init__(self, path: Path, *, remediation: str | None = None) -> None:
        self.path = path
        message = f"Draft config not found at {_format_value(str(path))}."
        super().__init__(message, remediation=remediation)


class DraftConfigInvalidError(DraftError):
    """The ``signalforge.yml`` ``llm:`` block failed schema validation.

    Wraps either a YAML parse failure, a wrong-shape top level, or a
    Pydantic :class:`pydantic.ValidationError` from
    :class:`signalforge.draft.config.DraftConfig` (the config-shaped model
    uses ``extra="forbid"`` per ``safety-layer.md`` DEC-015 so typos like
    ``mdoel:`` instead of ``model:`` fail loud rather than silently no-op).

    The original exception (if any) is preserved on ``cause`` so the CLI /
    response audit can render a forensically-useful incident report without
    sniffing message text.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the `llm:` block of signalforge.yml — likely a typo in a "
        "key (config-shaped models use extra='forbid'), an unknown "
        "`cache_ttl` value (must be '5m' or '1h'), or a non-positive "
        "`max_output_tokens`. See docs/draft-config-ops.md for the field "
        "reference."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(message, remediation=remediation)


class PromptEnvelopeBreachError(DraftError):
    """A model's ``raw_code`` contains the closing ``</MODEL_SQL>`` literal,
    breaking the prompt-injection envelope (DEC-007) before it can be sent.

    The envelope is the documented defence against adversarial dbt content:
    every byte between ``<MODEL_SQL>`` and ``</MODEL_SQL>`` is data, not
    instructions. A ``raw_code`` containing the closing tag — whether placed
    maliciously or by accident in a SQL comment — would terminate the fence
    early and let everything after be read by the LLM as instructions.

    Raised BEFORE any LLM call so a poisoned model never reaches Anthropic.
    """

    default_remediation: ClassVar[str] = (
        "The model's raw SQL contains the literal '</MODEL_SQL>' which would "
        "break the prompt-injection envelope. Inspect the model file (likely "
        "a SQL comment); remove the literal or escape it. If this is "
        "legitimate content (rare), open an issue — the envelope tag will "
        "need to rotate to an unguessable nonce."
    )

    def __init__(self, model_unique_id: str, *, remediation: str | None = None) -> None:
        self.model_unique_id = model_unique_id
        message = (
            f"Model {_format_value(model_unique_id)} contains the literal "
            f"'</MODEL_SQL>' in raw_code — refusing to render the prompt."
        )
        super().__init__(message, remediation=remediation)


class LLMResponseAuditRecordTooLargeError(DraftError):
    """A response-audit JSONL record would exceed the POSIX atomic-append size cap.

    POSIX guarantees ``write(2)`` is atomic only for payloads up to
    ``PIPE_BUF`` bytes (typically 4 KiB on Linux). The response-audit
    writer (US-012) enforces a size cap to keep concurrent appends from
    interleaving partial records. Mirrors safety's
    :class:`signalforge.safety.errors.AuditRecordTooLargeError`.

    Raised BEFORE any file is opened, so an oversize record leaves no
    on-disk artefact — the caller (``draft_from_request`` in US-013)
    sees the typed error and drops the partial response.
    """

    default_remediation: ClassVar[str] = (
        "Response-audit records must stay under the configured byte limit "
        "for atomic concurrent appends; the LLM emitted an unusually large "
        "response. Consider tightening the prompt's format section, or "
        "raising the limit in signalforge.draft.audit if 4 KB is genuinely "
        "insufficient (note: 4 KB is the POSIX-atomic-append guarantee on "
        "Linux — exceeding it makes concurrent writers unsafe)."
    )

    def __init__(self, size: int, limit: int, *, remediation: str | None = None) -> None:
        self.size = size
        self.limit = limit
        message = f"Response audit record size {size} exceeds atomic-append limit {limit}."
        if remediation is None:
            remediation = (
                f"Response-audit records must stay under {limit} bytes for "
                "atomic concurrent appends; reduce response payload or "
                "raise the limit if 4 KB is genuinely insufficient."
            )
        super().__init__(message, remediation=remediation)


class LLMResponseAuditWriteError(DraftError):
    """The fail-closed response-audit writer (US-012) could not durably
    persist the response receipt.

    Mirrors safety's :class:`signalforge.safety.errors.AuditWriteError`:
    the writer catches **no** exceptions internally; any ``OSError`` /
    ``PermissionError`` / encoding failure / size-cap violation propagates
    out via this class. The drafter must NOT return a
    :class:`signalforge.draft.models.CandidateSchema` whose response audit
    didn't durably hit disk — an unaudited LLM response is, by definition,
    output leaving without a receipt, exactly the failure mode the response
    audit exists to prevent.
    """

    default_remediation: ClassVar[str] = (
        "Verify the response-audit path is writable (permissions / disk "
        "space / SELinux contexts) and re-run. The draft is intentionally "
        "discarded when the audit write fails — re-running after fixing the "
        "underlying I/O issue is the supported recovery path."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (verified by tests/draft/test_errors.py).
__all__ = [
    "DraftConfigInvalidError",
    "DraftConfigNotFoundError",
    "DraftError",
    "LLMOutputAnchorContractError",
    "LLMOutputError",
    "LLMOutputJSONError",
    "LLMOutputValidationError",
    "LLMResponseAuditRecordTooLargeError",
    "LLMResponseAuditWriteError",
    "PromptEnvelopeBreachError",
]
