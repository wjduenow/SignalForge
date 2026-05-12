"""Typed exception hierarchy for the CLI layer (US-003 / DEC-008).

Mirrors the layer-base pattern established by :mod:`signalforge.safety.errors`
and the other v0.1 stage error modules: every class carries an optional
``remediation`` field; ``__str__`` renders ``message`` plus a
``↳ Remediation: <text>`` line when remediation is set.

The CLI only ever raises these for failures the CLI itself produces (path
canonicalisation, input validation). Stage exceptions from upstream modules
flow up through :func:`signalforge.cli._helpers.format_error_to_stderr`
unchanged — the CLI is the sink for formatting, not a re-wrapper.
"""

from __future__ import annotations


class CliError(Exception):
    """Base class for CLI-layer errors.

    Subclasses do not redefine the rendering — the base ``__str__`` handles
    the optional remediation footer uniformly. Instances may pass
    ``remediation=`` to override the (otherwise absent) footer.
    """

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def __str__(self) -> str:
        if self.remediation is None:
            return self.message
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class CliPathError(CliError):
    """Raised by :func:`canonicalise_user_path` when a user-supplied path
    fails the symlink-hardened containment gate.
    """


class CliInputError(CliError):
    """Raised when the CLI itself rejects an input value (e.g., an
    out-of-range numeric flag) before dispatching to a subcommand handler.

    Argparse-native rejections (unknown flag, missing required arg) raise
    :class:`SystemExit` directly via the parser; this class is for the
    cases the CLI validates after argparse has parsed.
    """


# ---------------------------------------------------------------------------
# Selector-failure wrappers (issue #37 — US-002, DEC-007)
# ---------------------------------------------------------------------------
#
# The CLI layer wraps two manifest-layer surfaces in selector handling:
#
# * Parse failure — :class:`signalforge.manifest.errors.SelectorParseError`
#   raised by :func:`signalforge.manifest.select.parse_selector` on a
#   syntactically invalid ``--select`` expression. The CLI catches it and
#   re-raises as :class:`CliSelectorParseError` so the CLI's exception
#   ladder catches a single ``CliInputError`` subclass (DEC-007 of the
#   plan) — the upstream typed error rides on the ``cause`` kwarg so the
#   parser detail surfaces in the rendered message body.
# * Zero-match — :func:`signalforge.manifest.select.select_models` returns
#   an empty tuple for a well-formed selector that matches no models. The
#   manifest layer does NOT treat this as an error (the operator may have
#   passed a structurally valid but operationally empty filter); the CLI
#   converts the empty result to :class:`CliSelectorNoMatchError` so the
#   batch dispatcher in US-004 never runs the pipeline on zero models.
#
# Both registered at tier 2 (input-validation) in
# :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`; the 7th AST
# scan in ``tests/test_audit_completeness.py`` gates the registration.


_CLI_SELECTOR_PARSE_DEFAULT_REMEDIATION: str = (
    "Check the selector grammar with 'signalforge generate --help'. Valid "
    "forms: tag:<name>, path:<glob>, or a bare unique_id / file path. "
    "Multi-expression unions separate atoms with a comma."
)

_CLI_SELECTOR_NO_MATCH_DEFAULT_REMEDIATION: str = (
    "Check the selector syntax with 'signalforge generate --help' or "
    "verify your dbt project has models matching the criteria."
)


class CliSelectorParseError(CliInputError):
    """Raised by ``cmd_generate`` when the manifest layer's selector
    parser rejects the ``--select`` expression.

    Wraps :class:`signalforge.manifest.errors.SelectorParseError` (or any
    other parse-time failure surfaced via ``cause``) so the CLI's
    exception ladder catches a single ``CliInputError`` subclass — the
    upstream typed error rides on the ``cause`` kwarg and its rendered
    message body is folded into the wrapper's own message. Tier 2 per
    DEC-007 of ``plans/super/37-multi-model-select.md``.

    Message shape:

    * ``cause is None`` → ``f"selector {expr!r} failed to parse"``.
    * ``cause is not None`` → ``f"selector {expr!r} failed to parse: {cause}"``.

    The default remediation points at ``signalforge generate --help`` and
    summarises the grammar; it is overridable via ``remediation=`` for
    forward-compat with future helpers that want to tailor the hint.
    """

    def __init__(
        self,
        *,
        expr: str,
        cause: Exception | None = None,
        remediation: str | None = None,
    ) -> None:
        if cause is None:
            message = f"selector {expr!r} failed to parse"
        else:
            message = f"selector {expr!r} failed to parse: {cause}"
        super().__init__(
            message,
            remediation=(
                remediation if remediation is not None else _CLI_SELECTOR_PARSE_DEFAULT_REMEDIATION
            ),
        )
        self.expr = expr
        self.cause = cause


class CliSelectorNoMatchError(CliInputError):
    """Raised by ``cmd_generate`` when a well-formed selector resolves to
    zero models in the project's manifest.

    Mirrors the tier of :class:`signalforge.manifest.errors.ModelNotFoundError`
    (the bare positional ``<model>`` equivalent — tier 2, input-validation)
    so the operator's experience is consistent across the
    ``<model>``-positional and ``--select`` surfaces. Per DEC-006 of
    ``plans/super/37-multi-model-select.md``.

    Message shape:
    ``f"--select {expr!r} matched zero models in this project"``.

    Default remediation: ``"Check the selector syntax with 'signalforge
    generate --help' or verify your dbt project has models matching the
    criteria."`` — same wording the help text reaches for when the user
    asks ``--help`` directly.
    """

    def __init__(self, *, expr: str, remediation: str | None = None) -> None:
        message = f"--select {expr!r} matched zero models in this project"
        super().__init__(
            message,
            remediation=(
                remediation
                if remediation is not None
                else _CLI_SELECTOR_NO_MATCH_DEFAULT_REMEDIATION
            ),
        )
        self.expr = expr


# ---------------------------------------------------------------------------
# init-demo wrappers (issue #47 — US-004, DEC-012 / DEC-013)
# ---------------------------------------------------------------------------
#
# The CLI subcommand ``signalforge init-demo`` calls into the public
# :func:`signalforge.demo.copy_demo` helper. The helper raises four typed
# :class:`signalforge.demo.DemoError` subclasses; the CLI handler wraps each
# at the boundary into one of the four ``CliInitDemo*Error`` classes below so
# the four-tier exit-code taxonomy stays homogeneous (DEC-012). DEC-013 locks
# the dest-exists / dest-unsafe cases at tier 2 (input-validation — the
# operator chose a destination that already has content or that would clobber
# a system / user directory); the fixture-missing / generic copy-failure cases
# land at tier 1 (load — broken install or filesystem state we couldn't get
# into a usable shape before doing work).
#
# Each class carries a ``default_remediation`` so the layer-base ``__str__``
# renders the canonical ``ERROR: <message>\n  ↳ Remediation: <text>`` shape
# without subclasses having to redefine rendering.
#
# The :class:`signalforge.demo.DemoPathError` case (symlink-cycle resolve
# failure) re-uses the existing :class:`CliPathError` rather than getting its
# own wrapper — every CLI-originated path-safety failure produces one error
# type, regardless of which underlying helper detected the cycle.


_CLI_INIT_DEMO_DEST_EXISTS_DEFAULT_REMEDIATION: str = (
    "Remove the existing directory or run 'signalforge init-demo --force' to replace it."
)

_CLI_INIT_DEMO_DEST_UNSAFE_DEFAULT_REMEDIATION: str = (
    "Refusing to clobber a system or home directory. Choose a different <dest> "
    "(a fresh subdirectory, not '/', $HOME, or the current working directory)."
)

_CLI_INIT_DEMO_FIXTURE_MISSING_DEFAULT_REMEDIATION: str = (
    "Reinstall signalforge-dbt — the bundled demo fixture is missing from your install."
)

_CLI_INIT_DEMO_COPY_DEFAULT_REMEDIATION: str = (
    "Check disk space, permissions, and that the parent directory of <dest> is writable."
)


class CliInitDemoDestExistsError(CliInputError):
    """Raised by ``cmd_init_demo`` when the destination directory exists
    and is non-empty and ``--force`` was not supplied.

    Wraps :class:`signalforge.demo.DemoDestExistsError`. Tier 2 (input
    validation — the operator chose a non-empty destination without
    opting in to clobbering it). Mirrors the precedent set by
    :class:`signalforge.manifest.errors.ModelNotFoundError` (tier 2 for
    "the operator named something that conflicts with project state").

    Message shape:
    ``f"destination {dest!r} exists and is not empty: {cause}"`` when a
    ``cause`` is provided; otherwise just the dest path. The trailing
    ``cause`` rendering reflects the underlying ``DemoDestExistsError``'s
    own message body for diagnostic continuity.
    """

    def __init__(
        self,
        *,
        dest: str,
        cause: Exception | None = None,
        remediation: str | None = None,
    ) -> None:
        if cause is None:
            message = f"destination {dest!r} exists and is not empty"
        else:
            message = f"destination {dest!r} exists and is not empty: {cause}"
        super().__init__(
            message,
            remediation=(
                remediation
                if remediation is not None
                else _CLI_INIT_DEMO_DEST_EXISTS_DEFAULT_REMEDIATION
            ),
        )
        self.dest = dest
        self.cause = cause


class CliInitDemoDestUnsafeError(CliInputError):
    """Raised by ``cmd_init_demo`` when ``--force`` would target a
    catastrophic path (``/``, ``Path.home()``, or the current working
    directory).

    Wraps :class:`signalforge.demo.DemoDestUnsafeError`. Tier 2 (input
    validation — the operator named a target the blast-radius guard
    refuses to clobber even under ``--force``). DEC-001 of
    ``plans/super/47-init-demo.md``.
    """

    def __init__(
        self,
        *,
        dest: str,
        cause: Exception | None = None,
        remediation: str | None = None,
    ) -> None:
        if cause is None:
            message = (
                f"refusing to --force-replace {dest!r}: would clobber a "
                "top-level system or user directory"
            )
        else:
            message = (
                f"refusing to --force-replace {dest!r}: would clobber a "
                f"top-level system or user directory: {cause}"
            )
        super().__init__(
            message,
            remediation=(
                remediation
                if remediation is not None
                else _CLI_INIT_DEMO_DEST_UNSAFE_DEFAULT_REMEDIATION
            ),
        )
        self.dest = dest
        self.cause = cause


class CliInitDemoFixtureMissingError(CliError):
    """Raised by ``cmd_init_demo`` when the bundled
    ``signalforge._demo/`` tree is missing from the installed package.

    Wraps :class:`signalforge.demo.DemoFixtureMissingError`. Tier 1
    (load — the wheel install is broken and there is no work that can
    proceed). The wheel-packaging convention in
    ``.claude/rules/python-build.md`` makes this practically unreachable
    on a clean ``pip install signalforge-dbt`` run, but a corrupted
    install (partial wheel extract, hand-edited site-packages) would
    surface here. DEC-011 of ``plans/super/47-init-demo.md``.
    """

    def __init__(
        self,
        *,
        cause: Exception | None = None,
        remediation: str | None = None,
    ) -> None:
        if cause is None:
            message = "bundled demo fixture is missing from the signalforge-dbt install"
        else:
            message = f"bundled demo fixture is missing from the signalforge-dbt install: {cause}"
        super().__init__(
            message,
            remediation=(
                remediation
                if remediation is not None
                else _CLI_INIT_DEMO_FIXTURE_MISSING_DEFAULT_REMEDIATION
            ),
        )
        self.cause = cause


class CliInitDemoCopyError(CliError):
    """Raised by ``cmd_init_demo`` when the ``shutil.copytree`` /
    ``shutil.rmtree`` operation fails with a generic ``OSError``.

    Catch-all for filesystem failures the more-specific
    ``CliInitDemo*`` wrappers above do not cover: ENOSPC, EACCES on the
    parent directory, EROFS, etc. Tier 1 (load — the filesystem isn't
    in a state where work can proceed) per DEC-012 of
    ``plans/super/47-init-demo.md``.
    """

    def __init__(
        self,
        *,
        dest: str,
        cause: Exception | None = None,
        remediation: str | None = None,
    ) -> None:
        if cause is None:
            message = f"failed to copy demo tree to {dest!r}"
        else:
            message = f"failed to copy demo tree to {dest!r}: {cause}"
        super().__init__(
            message,
            remediation=(
                remediation if remediation is not None else _CLI_INIT_DEMO_COPY_DEFAULT_REMEDIATION
            ),
        )
        self.dest = dest
        self.cause = cause
