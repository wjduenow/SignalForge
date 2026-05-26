"""Typed error hierarchy for ``signalforge.ingest``.

Implements US-001 of issue #104 (DEC-001). Mirrors the layer-base
rendering convention established by every other ``signalforge.*.errors``
module (manifest, warehouse, safety, llm, draft, prune, grade, diff,
cli, demo): the :class:`IngestError` base carries a ``remediation`` field;
``__str__`` renders ``message`` plus a ``↳ Remediation: <text>`` line when
remediation is set. Subclasses define a ``default_remediation`` class
attribute used when no explicit ``remediation`` is provided. Every
user-supplied value flowing into a message routes through
:func:`_format_value` (``repr()``-based, ANSI-safe — same log-injection
defence as the diff / prune / warehouse error modules).

The ingest layer is the reader/adapter that parses standard dbt
``schema.yml`` test syntax and emits a ``CandidateSchema`` so any
generator's tests (hand-written, dbt-codegen, dbt Copilot, …) can be run
through the prune step — extending Architectural Commitment #1 ("prune
any generator's tests, not just our own LLM drafts").

The 7th AST scan in ``tests/test_audit_completeness.py`` walks every
``errors.py`` under ``src/signalforge/*/`` (this is the 11th such module)
and gates that every concrete leaf appears in
``signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE``. The fast-follow
``signalforge prune-existing`` CLI subcommand (#105, DEC-004) wraps each
concrete into a ``CliPruneExisting*Error`` at the handler boundary; the
mapping registered now means #105 inherits the exit-code taxonomy with no
rework.

Exit-code tiers (cli-layer.md four-tier taxonomy):

* :class:`IngestSchemaNotFoundError` — tier 1 (load: the schema.yml path
  does not exist).
* :class:`IngestSchemaParseError` — tier 1 (load: malformed YAML /
  unreadable / path-containment failure).
* :class:`IngestSchemaTooLargeError` — tier 1 (load: schema bytes exceed
  the size cap — checked before any ``yaml.safe_load``, DEC-005).
* :class:`IngestModelNotFoundError` — tier 2 (input: the requested model
  name is absent from the schema.yml — the operator named something the
  file doesn't contain).
* :class:`IngestAnchorContractError` — tier 2 (input: one or more tests
  reference a column missing from the ``Model`` — DEC-002, whole-file
  collect-all).

See ``plans/super/104-ingest-external-tests.md`` for the full design.
"""

from __future__ import annotations

from typing import ClassVar


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages.

    Embedding raw user input in error strings is a log-injection seam: a
    crafted model name or schema path containing ``"\\x1b[31m"`` or
    ``"foo'\\nINFO: spoofed log line"`` could pollute log viewers or
    stack traces. Routing every user-controlled value through ``repr()``
    quotes the string, escapes control characters, and makes whitespace
    visible. Mirrors ``signalforge.diff.errors._format_value`` /
    ``signalforge.warehouse.errors`` (DEC-022 of #6).
    """
    return repr(v)


class IngestError(Exception):
    """Abstract base for all ``signalforge.ingest`` errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log
    output and CLI output both read cleanly.

    Listed in ``_EXCEPTION_MAPPING_EXCLUDED_BASES`` (the 7th AST scan's
    excluded-bases set) — every concrete leaf below must appear in the
    exit-code mapping, but the base is excluded (the MRO walk in
    ``map_exception_to_exit_code`` resolves forward-compat subclasses to
    their parent's tier). Like ``DemoError``, the concrete leaves span
    tiers 1 and 2, so the base gets **no** single fallback-tier entry in
    ``_EXCEPTION_TO_EXIT_CODE`` — a forgotten concrete falls through to
    tier 1 and the AST scan catches the missing per-class entry at test
    time (see ``docs/rules/cli-layer.md`` § dual registration).
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


class IngestSchemaNotFoundError(IngestError):
    """The supplied ``schema.yml`` path does not exist.

    Raised at reader entry before any read. CLI tier 1 (load — the
    surrounding state is not ready to start work).
    """

    default_remediation: ClassVar[str] = (
        "Verify the schema.yml path is correct and the file exists. Pass the "
        "path to the dbt YAML file that declares the model's tests "
        "(commonly models/<dir>/schema.yml or a per-model _<name>.yml)."
    )

    def __init__(self, path: object, *, remediation: str | None = None) -> None:
        self.path = path
        message = f"schema.yml not found at {_format_value(path)}."
        super().__init__(message, remediation=remediation)


class IngestSchemaParseError(IngestError):
    """The ``schema.yml`` could not be parsed.

    Covers malformed YAML, unreadable files (encoding / OS errors), and
    path-containment failures (symlink loop / escape from the project
    directory — the neutral ``PathContainmentError`` from
    ``signalforge._common.path_safety`` is wrapped here at the reader
    boundary). The triggering exception rides on the ``cause`` kwarg and
    is chained via ``__cause__``. CLI tier 1 (load).
    """

    default_remediation: ClassVar[str] = (
        "Verify the file is valid YAML (yaml.safe_load must parse it), is "
        "readable, and that no symlink in its path escapes the project "
        "directory. Run `dbt parse` to confirm dbt itself accepts the schema."
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
        if cause is not None:
            # Chain the underlying cause so callers branching on the OS /
            # YAML detail can read ``exc.__cause__`` (mirrors ``raise X
            # from cause``).
            self.__cause__ = cause


class IngestSchemaTooLargeError(IngestError):
    """The ``schema.yml`` byte length exceeds the configured size cap.

    DEC-005 (mirrors diff-renderer DEC-006): ``yaml.safe_load`` is safe
    against arbitrary-code execution but NOT against billion-laughs
    (nested anchor expansion) or arbitrary deep-nesting. The cap is
    checked on the raw byte length BEFORE any ``yaml.safe_load`` so the
    parser never sees a hostile payload. CLI tier 1 (load).
    """

    default_remediation: ClassVar[str] = (
        "The schema.yml exceeded the configured byte safety cap applied "
        "before yaml.safe_load. Inspect the file for accidental bloat or an "
        "attempted billion-laughs / deeply-nested-anchor payload. Trim the "
        "schema or split it across multiple files."
    )

    def __init__(self, size: int, limit: int, *, remediation: str | None = None) -> None:
        self.size = size
        self.limit = limit
        message = f"schema.yml size {size} exceeds parse-safety limit {limit}."
        super().__init__(message, remediation=remediation)


class IngestModelNotFoundError(IngestError):
    """The requested model name is absent from the ``schema.yml``.

    A ``schema.yml`` can declare multiple models; the reader selects the
    target by ``name``. When no ``models:`` entry matches, the operator
    named something the file does not contain. CLI tier 2 (input —
    mirrors ``ModelNotFoundError``'s tier in the manifest layer).
    """

    default_remediation: ClassVar[str] = (
        "Verify the model name matches a `name:` under `models:` in the "
        "schema.yml. Model names are matched exactly (case-sensitive)."
    )

    def __init__(self, model_name: object, *, remediation: str | None = None) -> None:
        self.model_name = model_name
        message = f"model {_format_value(model_name)} not found in schema.yml."
        super().__init__(message, remediation=remediation)


class IngestAnchorContractError(IngestError):
    """One or more tests reference a column missing from the ``Model``.

    DEC-002: a test referencing a column absent from the manifest ``Model``
    means the YAML is stale or wrong. Mirrors the drafter's anchor-contract
    spirit — collect every violation across the whole file and raise one
    typed error listing all of them, so the operator can fix in one pass.
    Distinct from *unsupported test types*, which are skip+recorded
    (DEC-003), not failed loud. CLI tier 2 (input).
    """

    default_remediation: ClassVar[str] = (
        "Each listed test references a column that is absent from the model. "
        "Either correct the column name in the schema.yml to match the "
        "model, or regenerate the manifest (`dbt parse`) so the model's "
        "column set is current."
    )

    def __init__(
        self,
        violations: tuple[str, ...],
        *,
        remediation: str | None = None,
    ) -> None:
        self.violations = tuple(violations)
        count = len(self.violations)
        joined = "; ".join(self.violations)
        message = f"{count} ingest anchor-contract violation{'' if count == 1 else 's'}: {joined}"
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (mirrors safety / draft / prune / grade / diff /
# warehouse / demo error modules).
__all__ = [
    "IngestAnchorContractError",
    "IngestError",
    "IngestModelNotFoundError",
    "IngestSchemaNotFoundError",
    "IngestSchemaParseError",
    "IngestSchemaTooLargeError",
]
