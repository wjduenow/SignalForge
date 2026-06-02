"""Shape contract for :func:`signalforge.cli._helpers.format_error_to_stderr` (US-002).

Pins the canonical stderr shape (DEC-008) for the CLI-layer selector-failure
exception classes introduced by issue #37 US-002. The single-line ``ERROR:
<message>`` + ``  ↳ Remediation: <text>`` shape is what CI parsers across
clauditor and SignalForge-adjacent tooling key on; a regression here is a
public-surface break.

The two new classes:

* :class:`signalforge.cli.errors.CliSelectorParseError` — raised by
  ``cmd_generate`` (US-004) when :func:`signalforge.manifest.select.parse_selector`
  raises :class:`signalforge.manifest.errors.SelectorParseError`. Accepts an
  optional ``cause`` so the underlying parser detail surfaces in the stderr
  message body.
* :class:`signalforge.cli.errors.CliSelectorNoMatchError` — raised by
  ``cmd_generate`` (US-004) when a well-formed selector resolves to zero
  models in the project's manifest.

Both registered at tier 2 in
:data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` per DEC-007 of #37
and DEC-024 of #9 (the 7th AST scan). The exit-code mapping is exercised in
:mod:`tests.cli.test_exit_codes`; this file pins the *stderr-shape* half of
the contract.
"""

from __future__ import annotations

from signalforge.cli._helpers import format_error_to_stderr
from signalforge.cli.errors import (
    CliSelectorNoMatchError,
    CliSelectorParseError,
)

# ---------------------------------------------------------------------------
# CliSelectorParseError — single-line ERROR + Remediation
# ---------------------------------------------------------------------------


def test_cli_selector_parse_error_stderr_shape() -> None:
    """Without a ``cause``, the message is the locked
    ``f"selector {expr!r} failed to parse"`` form; the remediation footer
    is the locked default-remediation string from #37 DEC-007.
    """
    exc = CliSelectorParseError(expr="tag:")
    rendered = format_error_to_stderr(exc)

    # Two-line shape: ERROR header + Remediation footer.
    lines = rendered.split("\n")
    assert len(lines) == 2, f"expected two lines; got {rendered!r}"

    # Header: ERROR: <message>.
    assert lines[0] == "ERROR: selector 'tag:' failed to parse", f"header drift: {lines[0]!r}"

    # Footer: two-space indent + ↳ + Remediation: + text.
    assert lines[1].startswith("  ↳ Remediation: "), (
        f"remediation footer prefix drift: {lines[1]!r}"
    )
    # Default remediation references the help text + grammar.
    assert "signalforge generate --help" in lines[1]
    assert "tag:<name>" in lines[1]
    assert "path:<glob>" in lines[1]


def test_cli_selector_parse_error_with_cause_includes_cause_text() -> None:
    """When ``cause`` is supplied, the message body includes the cause's
    rendered form. ``cmd_generate`` (US-004) will pass the wrapped
    :class:`SelectorParseError` from the manifest layer; this test
    asserts the wrapping preserves the underlying detail.
    """
    cause = ValueError("empty tag")
    exc = CliSelectorParseError(expr="tag:", cause=cause)
    rendered = format_error_to_stderr(exc)

    # Cause text must appear so the operator sees *what* failed about the
    # parse, not just that it failed.
    assert "empty tag" in rendered, f"cause text missing from rendered message: {rendered!r}"
    # Header shape preserved.
    assert rendered.startswith("ERROR: "), f"header drift: {rendered!r}"
    # Expr still surfaces.
    assert "'tag:'" in rendered, f"expr missing: {rendered!r}"


def test_cli_selector_parse_error_str_includes_remediation() -> None:
    """The layer-base ``__str__`` renders the remediation footer
    independently of :func:`format_error_to_stderr` — pinning ``str(exc)``
    directly guards against a future regression that strips remediation
    from the base class.
    """
    exc = CliSelectorParseError(expr="tag:")
    rendered = str(exc)
    assert "selector 'tag:' failed to parse" in rendered
    assert "↳ Remediation:" in rendered


# ---------------------------------------------------------------------------
# CliSelectorNoMatchError — single-line ERROR + Remediation
# ---------------------------------------------------------------------------


def test_cli_selector_no_match_error_stderr_shape() -> None:
    """The zero-match shape: well-formed selector, but the manifest has no
    models matching it. Per #37 DEC-006 the message is the locked
    ``f"--select {expr!r} matched zero models in this project"`` form;
    remediation is the locked default-remediation string.
    """
    exc = CliSelectorNoMatchError(expr="tag:nonexistent")
    rendered = format_error_to_stderr(exc)

    lines = rendered.split("\n")
    assert len(lines) == 2, f"expected two lines; got {rendered!r}"

    assert lines[0] == ("ERROR: --select 'tag:nonexistent' matched zero models in this project"), (
        f"header drift: {lines[0]!r}"
    )

    assert lines[1].startswith("  ↳ Remediation: "), (
        f"remediation footer prefix drift: {lines[1]!r}"
    )
    assert "signalforge generate --help" in lines[1]
    assert "matching the criteria" in lines[1]


def test_cli_selector_no_match_error_str_includes_remediation() -> None:
    """As above for :class:`CliSelectorNoMatchError` — pin the layer-base
    ``__str__`` shape independently of :func:`format_error_to_stderr`.
    """
    exc = CliSelectorNoMatchError(expr="tag:nonexistent")
    rendered = str(exc)
    assert "matched zero models" in rendered
    assert "↳ Remediation:" in rendered


# ---------------------------------------------------------------------------
# Exit-code mapping table coverage
# ---------------------------------------------------------------------------


def test_cli_selector_parse_error_in_exit_code_table() -> None:
    """The 7th AST scan
    (:func:`tests.test_audit_completeness.test_every_typed_error_is_in_exit_code_mapping_table`)
    walks every ``src/signalforge/*/errors.py`` and asserts every
    concrete ``class <Name>Error`` declaration appears in
    :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`. US-002 of
    issue #37 lands :class:`CliSelectorParseError`; this test pins the
    scan to **pass** specifically for the new class so a future
    regression that drops it (or moves it to a different errors module
    without updating the mapping) breaks loud at the call-out, not just
    inside the broader 7th-scan failure list.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE

    assert CliSelectorParseError in _EXCEPTION_TO_EXIT_CODE, (
        "CliSelectorParseError missing from _EXCEPTION_TO_EXIT_CODE; "
        "the 7th AST scan would catch this, but the per-class assertion "
        "names the offending class up front."
    )
    assert _EXCEPTION_TO_EXIT_CODE[CliSelectorParseError] == 2, (
        "CliSelectorParseError must map to tier 2 (input-validation) per "
        "DEC-007 of plans/super/37-multi-model-select.md."
    )


def test_cli_selector_no_match_error_in_exit_code_table() -> None:
    """Per-class call-out for :class:`CliSelectorNoMatchError`; same
    rationale as the parse-error test above.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE

    assert CliSelectorNoMatchError in _EXCEPTION_TO_EXIT_CODE, (
        "CliSelectorNoMatchError missing from _EXCEPTION_TO_EXIT_CODE; "
        "the 7th AST scan would catch this, but the per-class assertion "
        "names the offending class up front."
    )
    assert _EXCEPTION_TO_EXIT_CODE[CliSelectorNoMatchError] == 2, (
        "CliSelectorNoMatchError must map to tier 2 (input-validation) "
        "per DEC-006 of plans/super/37-multi-model-select.md."
    )


# ---------------------------------------------------------------------------
# _safe_excepthook — belt-and-braces traceback strip (DEC-016)
# ---------------------------------------------------------------------------


def test_safe_excepthook_strips_traceback_and_prints_typed_message(
    capsys: object,
) -> None:
    """Issue #60 pins the patch line in ``_safe_excepthook`` that calls
    :func:`print_stderr`: the panic-path tail writes ``ERROR: <message>``
    to stderr for any non-``KeyboardInterrupt``/``SystemExit`` exception
    that escapes the main ``try / except`` (DEC-016 belt-and-braces).

    The hook is normally invoked by Python on an unhandled exception
    at top-level, which the in-process ``main(argv)`` test pattern
    cannot trigger directly. Call the hook explicitly with a test
    exception, then assert the stderr shape — the ANSI-laden message
    body proves :func:`print_stderr` ran the strip (issue #60).
    """
    import pytest as _pytest  # local import keeps the file header stable

    from signalforge.cli._helpers import _safe_excepthook

    assert isinstance(capsys, _pytest.CaptureFixture)
    capture: _pytest.CaptureFixture[str] = capsys  # type: ignore[assignment]

    # Carry an ANSI escape in the exception message so the assertion
    # proves the strip in print_stderr actually ran.
    exc = RuntimeError("\x1b[31mboom\x1b[0m")
    _safe_excepthook(type(exc), exc, None)

    captured = capture.readouterr()
    assert captured.out == ""
    # The strip path replaces the SGR bytes with empty strings, leaving
    # the bare token. No leading/trailing escape bytes survive.
    assert "\x1b[" not in captured.err, (
        "print_stderr did not strip the ANSI CSI bytes from the panic-path "
        f"stderr write: {captured.err!r}"
    )
    assert captured.err.rstrip("\n") == "ERROR: boom"


def test_safe_excepthook_passes_keyboard_interrupt_through(capsys: object) -> None:
    """Companion to the above: ``KeyboardInterrupt`` must NOT hit
    :func:`print_stderr` — Python's default hook is delegated to
    instead (DEC-016, the Ctrl-C semantics carve-out).

    Pins the early-return branch at line 689 so a future refactor that
    accidentally collapsed the two arms (e.g., always calling
    ``print_stderr``) would fail loud here.
    """
    import pytest as _pytest

    from signalforge.cli import _helpers

    assert isinstance(capsys, _pytest.CaptureFixture)
    capture: _pytest.CaptureFixture[str] = capsys  # type: ignore[assignment]

    calls: list[tuple[type[BaseException], BaseException, object]] = []

    def fake_default(
        exc_type: type[BaseException],
        exc_value: BaseException,
        tb: object,
    ) -> None:
        calls.append((exc_type, exc_value, tb))

    monkeypatched = _helpers.sys.__excepthook__
    _helpers.sys.__excepthook__ = fake_default  # type: ignore[assignment]
    try:
        kbd = KeyboardInterrupt()
        _helpers._safe_excepthook(type(kbd), kbd, None)
    finally:
        _helpers.sys.__excepthook__ = monkeypatched  # type: ignore[assignment]

    assert calls == [(KeyboardInterrupt, kbd, None)], (
        "_safe_excepthook must delegate KeyboardInterrupt to Python's "
        f"default hook; got calls={calls!r}"
    )
    captured = capture.readouterr()
    assert captured.err == "", (
        "KeyboardInterrupt must NOT write to stderr via print_stderr — the "
        f"default hook owns it. Got stderr={captured.err!r}"
    )


# ---------------------------------------------------------------------------
# ExceptionGroup renderer — US-010 of #186 / DEC-007 (defence-in-depth)
# ---------------------------------------------------------------------------


def test_exception_group_renders_as_multi_bullet() -> None:
    """Multi-exception ``ExceptionGroup`` renders as the existing
    multi-bullet stderr shape: header naming the concurrent-failure
    count + ``  - <Class>: <repr(msg)>`` bullets, one per inner
    exception. No ``Traceback`` ever leaks.

    Belt-and-braces defence (US-010 / DEC-007 of #186) for the grade
    asyncio orchestrator: the engine's inner ``BaseExceptionGroup``
    unwrap (``engine.py``) re-raises single-exception groups as the
    inner typed exception so callers can pattern-match; multi-exception
    groups bubble unchanged to ``cmd_<name>``'s single ``try / except
    Exception`` boundary, which routes through this renderer. A hostile
    non-grade-typed exception (``KeyError`` from buggy worker code)
    would otherwise leak a default ``[ExceptionGroup: …]`` repr plus a
    traceback — that's the failure mode this branch closes.
    """
    inner_a = KeyError("missing artifact id")
    inner_b = ValueError("budget config malformed")
    inner_c = RuntimeError("worker crashed")
    group = ExceptionGroup("concurrent failures", [inner_a, inner_b, inner_c])

    rendered = format_error_to_stderr(group)

    lines = rendered.split("\n")
    # Header + 3 bullets = 4 lines exactly.
    assert len(lines) == 4, f"expected header + 3 bullets; got {rendered!r}"

    # Header names the concurrent-failure count.
    assert lines[0] == "ERROR: Grade orchestrator encountered 3 concurrent failures:", (
        f"header drift: {lines[0]!r}"
    )

    # Each bullet identifies the exception class + a repr-quoted message.
    # KeyError's ``str()`` quotes its argument, so the inner repr quotes
    # the already-quoted form — that's the defence-in-depth: the bullet
    # text never contains raw control bytes.
    assert lines[1].startswith("  - KeyError: "), f"bullet 0 drift: {lines[1]!r}"
    assert "missing artifact id" in lines[1]
    assert lines[2] == "  - ValueError: 'budget config malformed'", f"bullet 1 drift: {lines[2]!r}"
    assert lines[3] == "  - RuntimeError: 'worker crashed'", f"bullet 2 drift: {lines[3]!r}"

    # The defence-in-depth invariant: never leak a traceback.
    assert "Traceback" not in rendered, f"traceback leaked: {rendered!r}"


def test_exception_group_caps_at_10_bullets_with_overflow() -> None:
    """A group with more than 10 inner exceptions caps at 10 bullets
    and appends a ``  ... and K more`` overflow line. Mirrors the
    failure-list cap pattern from :func:`format_batch_summary`
    (``_FAILURE_LIST_CAP`` precedent) — the operator gets the first N
    failures named instead of a runaway list.
    """
    inners: list[Exception] = [ValueError(f"failure number {i}") for i in range(12)]
    group = ExceptionGroup("concurrent failures", inners)

    rendered = format_error_to_stderr(group)
    lines = rendered.split("\n")

    # Header + 10 capped bullets + 1 overflow line = 12 lines.
    assert len(lines) == 12, f"expected header + 10 + overflow; got {rendered!r}"

    # Header names the *true* count (12), not the capped count (10).
    assert lines[0] == "ERROR: Grade orchestrator encountered 12 concurrent failures:", (
        f"header drift: {lines[0]!r}"
    )

    # First 10 bullets present in order.
    for i in range(10):
        assert lines[1 + i].startswith("  - ValueError: "), f"bullet {i} drift: {lines[1 + i]!r}"
        assert f"failure number {i}" in lines[1 + i], f"bullet {i} content drift: {lines[1 + i]!r}"

    # Overflow line — locked text, ``K = 12 - 10 = 2``.
    assert lines[-1] == "  ... and 2 more", f"overflow drift: {lines[-1]!r}"


def test_exception_group_repr_quotes_ansi_and_control_bytes_in_messages() -> None:
    """Inner-exception messages route through :func:`repr` so ANSI CSI
    bytes and other control characters cannot inject terminal-control
    sequences when the renderer's output reaches the
    :func:`print_stderr` sink. Defence-in-depth — ``print_stderr``
    also strips ANSI at the sink (#60), but the repr-quote here is the
    layer that survives a hypothetical sink-bypass.

    The repr-quote escapes ``\\x1b`` to the literal four-character
    sequence ``\\x1b`` (backslash + ``x`` + ``1`` + ``b``), so the
    bullet body cannot carry a raw escape byte.
    """
    inner = RuntimeError("\x1b[31mEVIL\x1b[0m")
    group = ExceptionGroup("concurrent failures", [inner])

    rendered = format_error_to_stderr(group)

    # No raw CSI byte survives the renderer.
    assert "\x1b[" not in rendered, f"raw ANSI CSI byte leaked through the renderer: {rendered!r}"
    # The repr-quote replaces the escape byte with the literal escape
    # sequence ``\\x1b`` — so the four-char form IS present in the
    # rendered text. That's the defence: it's now an inert quoted
    # string, not a live escape.
    assert "\\x1b" in rendered, (
        f"expected repr-escaped form of the ANSI byte in the bullet: {rendered!r}"
    )
    # No traceback leak.
    assert "Traceback" not in rendered, f"traceback leaked: {rendered!r}"


def test_exception_group_single_inner_renders_singular_header() -> None:
    """A 1-exception group (rare — the engine's inner unwrap re-raises
    these as the inner typed exception, so this path is only reached
    if a future refactor changes the unwrap shape) renders the header
    with singular grammar (``1 concurrent failure:``, no trailing
    ``s``). Pins the singular/plural grammar against drift.
    """
    inner = KeyError("solo")
    group = ExceptionGroup("solo group", [inner])

    rendered = format_error_to_stderr(group)
    lines = rendered.split("\n")

    assert len(lines) == 2, f"expected header + 1 bullet; got {rendered!r}"
    assert lines[0] == "ERROR: Grade orchestrator encountered 1 concurrent failure:", (
        f"singular-grammar drift: {lines[0]!r}"
    )
    assert lines[1].startswith("  - KeyError: "), f"bullet drift: {lines[1]!r}"


def test_non_group_exceptions_render_unchanged() -> None:
    """Non-``ExceptionGroup`` exceptions MUST render through the
    existing single-line ``ERROR: <message>`` shape. Pins that the
    new ``ExceptionGroup`` branch does NOT shadow the default arm.
    """
    exc = RuntimeError("boom")
    rendered = format_error_to_stderr(exc)
    assert rendered == "ERROR: boom", f"single-line shape drifted: {rendered!r}"
