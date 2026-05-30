"""Typed exception hierarchy for the LLM cost-rollup layer.

Implements DEC-002 of plans/super/157-e2e-cost-and-parallel.md (US-001):
the rollup helper walks per-run audit JSONLs and turns token counts into
USD via :mod:`signalforge.llm.pricing`. Three concrete failure modes —
audit file(s) absent, malformed JSONL line, unknown model id — each get a
typed error so the wrapper script / library consumers can pattern-match
without sniffing message text.

The hierarchy mirrors :mod:`signalforge.llm.errors` and the other per-stage
``errors.py`` modules: every error carries a class-level
``default_remediation`` string that the base ``__str__`` renders on a
separate ``↳ Remediation:`` line, and every user-supplied string passes
through :func:`signalforge.llm.errors._format_value` (i.e. ``repr()``) so
adversarial input — embedded quotes, control chars, ANSI escapes — cannot
smuggle special characters into log viewers or error messages.

:class:`CostError` is a direct :class:`LLMError` subclass: rollup is part
of the LLM call-economics layer (alongside :mod:`signalforge.llm.pricing`),
not a fail-closed audit-write seam. The base is registered in
:data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` at tier 2 as a
dual-registration safety net per ``.claude/rules/cli-layer.md`` § "7th AST
scan" — every concrete is individually registered too; the base entry only
fires for forward-compat subclasses a contributor might add without
updating the table.
"""

from __future__ import annotations

from typing import ClassVar

from signalforge.llm.errors import LLMError, _format_value


class CostError(LLMError):
    """Base class for all cost-rollup errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    is inherited from :class:`LLMError` and renders the message and the
    remediation on separate lines so log output and CLI output both read
    cleanly.
    """

    default_remediation: ClassVar[str] = "(no remediation set — this is the base class)"


class CostRollupAuditMissingError(CostError):
    """Neither ``llm_responses.jsonl`` nor ``grade.jsonl`` was found under
    the supplied ``<project_dir>/<audit_dir>``.

    Both JSONLs are produced by the SignalForge pipeline as it issues
    LLM calls. Absence means the operator pointed the rollup at a project
    where the pipeline has not run yet, or at a directory that is not a
    SignalForge project at all.
    """

    default_remediation: ClassVar[str] = (
        "Run `signalforge generate` (or another LLM-issuing subcommand) "
        "against the project first so the audit JSONLs are written under "
        "`<project_dir>/.signalforge/`, then re-run the cost rollup."
    )

    def __init__(
        self,
        project_dir: str,
        audit_dir: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.audit_dir = audit_dir
        # Format the combined audit-root path as ONE repr-safe string so the
        # operator sees a single quoted, copy-pasteable path rather than two
        # separately-quoted reprs joined by a literal slash (PR #162 review).
        audit_root = f"{project_dir.rstrip('/')}/{audit_dir.lstrip('/')}"
        message = (
            f"no audit JSONLs found under {_format_value(audit_root)}; "
            f"expected at least one of llm_responses.jsonl or grade.jsonl"
        )
        super().__init__(message, remediation=remediation)


class CostRollupMalformedRecordError(CostError):
    """A line in an audit JSONL could not be deserialised into the
    expected event shape (``LLMResponseEvent`` / ``GradeEvent``).

    The audit writers ship one JSON object per line via a fail-closed
    durability seam, so a malformed record almost always means the file
    has been hand-edited or partially overwritten by an external tool —
    the pipeline itself never emits an unparseable record. The
    ``line_num`` field is one-indexed so the operator can jump straight
    to the offending row with ``sed -n '<N>p'``.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the JSONL file at the cited line number; the audit "
        "writers never emit malformed records, so the most likely cause "
        "is a hand-edit or partial-overwrite by an external tool. "
        "Restore from version control or re-run the pipeline."
    )

    def __init__(
        self,
        path: str,
        line_num: int,
        reason: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.path = path
        self.line_num = line_num
        self.reason = reason
        message = (
            f"malformed audit record in {_format_value(path)} at line "
            f"{line_num}: {_format_value(reason)}"
        )
        super().__init__(message, remediation=remediation)


class CostRollupUnknownModelError(CostError):
    """An audit record references a model id that is not present in
    :data:`signalforge.llm.pricing.PRICES`.

    Distinct from :class:`signalforge.llm.errors.EstimateUnknownModelError`
    (which fires at the ``--estimate`` flag's input-validation boundary)
    because this one fires at rollup time after a real call has been
    issued — the LLM seam already produced an audit record, so the
    pricing table is the lagging artefact. Same tier-2 mapping reasoning
    applies: "looked-up identifier not in a static table" is an
    input-shape failure, not an external-dep one.
    """

    default_remediation: ClassVar[str] = (
        "Add the model id to signalforge.llm.pricing.PRICES (per-million "
        "input / output / cache rates) and re-run the rollup. The "
        "pricing table is the lagging artefact when a new SKU lands; "
        "see signalforge/llm/pricing.py for the existing entries."
    )

    def __init__(
        self,
        model_id: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.model_id = model_id
        message = (
            f"unknown model id in audit record: {_format_value(model_id)}; "
            f"not present in signalforge.llm.pricing.PRICES"
        )
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (matches the convention in signalforge.llm.errors).
__all__ = [
    "CostError",
    "CostRollupAuditMissingError",
    "CostRollupMalformedRecordError",
    "CostRollupUnknownModelError",
]
