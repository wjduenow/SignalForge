"""Typed error hierarchy for ``signalforge.airflow``.

Implements US-003 of issue #230 (DEC-003). Mirrors the layer-base
rendering convention established by every other ``signalforge.*.errors``
module (manifest, warehouse, safety, llm, draft, prune, grade, diff,
cli, demo, ingest, skill): the :class:`AirflowIntegrationError` base
carries a ``remediation`` field; ``__str__`` renders ``message`` plus a
``↳ Remediation: <text>`` line when remediation is set. Subclasses define
a ``default_remediation`` class attribute used when no explicit
``remediation`` is provided. Every user-supplied value flowing into a
message routes through :func:`_format_value` (``repr()``-based, ANSI-safe
— same log-injection defence as the ingest / diff / prune / warehouse
error modules).

**This module is pure-Python and Airflow-free** (no ``from airflow ...``
import). That is load-bearing: it lets ``signalforge.airflow.__init__``
re-export the error names **eagerly** (only the operator/hook names stay
lazy — DEC-006), and it lets ``signalforge.cli._helpers`` import
:class:`AirflowConfigError` for the exit-code table without dragging
Apache Airflow into the base install (the no-eager-import gate, DEC-009).

The 7th AST scan in ``tests/test_audit_completeness.py`` walks every
``errors.py`` under ``src/signalforge/*/`` (this is the 14th such module)
and gates that every concrete leaf appears in
``signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE``. Per DEC-003 the
registration is **defensive / scan-7 compliance**: these errors surface
through Airflow's own task runner (``AirflowFailException``), not the
``signalforge`` CLI panic path, so the tier is notional — but every
``signalforge.*`` typed error must resolve to exactly one tier, so the
concrete is mapped and the abstract base is excluded.

Exit-code tier (cli-layer.md four-tier taxonomy):

* :class:`AirflowConfigError` — tier 2 (input: operator misconfiguration,
  e.g. a missing/invalid ``project_dir`` or ``model`` passed to an
  operator — mirrors ``ModelNotFoundError``'s "the operator named
  something the project rejects" tier).

See ``plans/super/230-airflow-skeleton.md`` for the full design.
"""

from __future__ import annotations

from typing import ClassVar


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages.

    Embedding raw user input in error strings is a log-injection seam: a
    crafted ``project_dir`` / ``model`` containing ``"\\x1b[31m"`` or
    ``"foo'\\nINFO: spoofed log line"`` could pollute log viewers or
    stack traces. Routing every user-controlled value through ``repr()``
    quotes the string, escapes control characters, and makes whitespace
    visible. Mirrors ``signalforge.ingest.errors._format_value`` /
    ``signalforge.diff.errors._format_value`` (DEC-022 of #6).
    """
    return repr(v)


class AirflowIntegrationError(Exception):
    """Abstract base for all ``signalforge.airflow`` errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log
    output and CLI output both read cleanly.

    Listed in ``_EXCEPTION_MAPPING_EXCLUDED_BASES`` (the 7th AST scan's
    excluded-bases set) — every concrete leaf below must appear in the
    exit-code mapping, but the base is excluded (the MRO walk in
    ``map_exception_to_exit_code`` resolves forward-compat subclasses to
    their parent's tier). v0.7 ships exactly one concrete
    (:class:`AirflowConfigError`, tier 2); the base gets no fallback-tier
    entry in ``_EXCEPTION_TO_EXIT_CODE`` — it lives only in the excluded
    set, and a forgotten future concrete falls through to tier 1 where
    the AST scan catches the missing per-class entry at test time (see
    ``.claude/rules/cli-layer.md`` § dual registration).
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


class AirflowConfigError(AirflowIntegrationError):
    """An operator was misconfigured.

    Raised when a SignalForge Airflow operator (or hook) reads its
    configuration — ``project_dir`` / ``model`` from operator kwargs, an
    Airflow Variable, or the task environment — and finds it missing,
    empty, or invalid. The first thing the implementing epic-#228
    children raise. CLI tier 2 (input — operator-supplied configuration
    that conflicts with what the task needs to run).
    """

    default_remediation: ClassVar[str] = (
        "Check the operator's configuration — the required `project_dir` and "
        "`model` must be set (via operator kwargs, an Airflow Variable, or the "
        "task environment) and must point at a valid dbt project and model. "
        "See the signalforge.airflow operator documentation for the expected "
        "configuration shape."
    )


# Sorted alphabetically (mirrors safety / draft / prune / grade / diff /
# warehouse / demo / ingest / skill error modules).
__all__ = [
    "AirflowConfigError",
    "AirflowIntegrationError",
]
