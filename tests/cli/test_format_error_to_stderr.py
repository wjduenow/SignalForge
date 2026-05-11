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
