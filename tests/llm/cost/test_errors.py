"""Typed-error contract tests for the ``signalforge.llm.cost`` subpackage
(issue #157 / US-001 of plans/super/157-e2e-cost-and-parallel.md).

Pins five load-bearing properties:

* Every concrete error inherits :class:`CostError`.
* :class:`CostError` itself inherits :class:`LLMError` — preserves the
  per-stage hierarchy ``LLMError → CostError → concrete``, so a caller's
  ``except LLMError`` clause still catches rollup failures.
* Each concrete carries a non-empty ``default_remediation`` (the
  ``manifest-readers.md`` "errors carry remediation" contract).
* Each concrete's ``__str__`` renders the ``message`` and
  ``↳ Remediation:`` line (the base-class rendering contract from
  :class:`signalforge.llm.errors.LLMError`).
* Each concrete is mapped to exit-code tier 2 in
  :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`;
  :class:`CostError` base is dual-registered at tier 2 (safety net) AND
  appears in :data:`_EXCEPTION_MAPPING_EXCLUDED_BASES` so scan-7 does
  not require it to be mapped.
* The seven public names (``rollup_audit_dir``, the three result-shape
  dataclasses, the four error classes) are importable from
  ``signalforge.llm.cost``.
* The stub :func:`rollup_audit_dir` raises ``NotImplementedError``
  (US-002 fills in the body).
"""

from __future__ import annotations

import pytest

from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE
from signalforge.llm.cost import (
    CostError,
    CostReport,
    CostRollupAuditMissingError,
    CostRollupMalformedRecordError,
    CostRollupUnknownModelError,
    ModelRollup,
    ProviderRollup,
    rollup_audit_dir,
)
from signalforge.llm.errors import LLMError
from tests.test_audit_completeness import _EXCEPTION_MAPPING_EXCLUDED_BASES

_CONCRETE_ERRORS: tuple[type[CostError], ...] = (
    CostRollupAuditMissingError,
    CostRollupMalformedRecordError,
    CostRollupUnknownModelError,
)


def test_public_surface_importable() -> None:
    """All seven public names are re-exported from ``signalforge.llm.cost``.

    Pins the AC from the bead: ``from signalforge.llm.cost import …``
    succeeds for ``rollup_audit_dir``, ``CostReport``, ``ProviderRollup``,
    and the four error classes.
    """
    # Identity check: each import resolved to a non-``None`` object.
    # The actual import statement at module top would already have
    # failed if any name were missing — this assertion turns silent
    # success into a load-bearing one.
    names = (
        rollup_audit_dir,
        CostReport,
        ProviderRollup,
        ModelRollup,
        CostError,
        CostRollupAuditMissingError,
        CostRollupMalformedRecordError,
        CostRollupUnknownModelError,
    )
    for obj in names:
        assert obj is not None


def test_cost_error_inherits_llm_error() -> None:
    """``CostError`` lives under the ``LLMError`` umbrella so a caller's
    ``except LLMError:`` clause still catches every rollup failure.

    This is the contract the cli-layer.md mapping table relies on: the
    MRO walk in :func:`signalforge.cli._helpers.map_exception_to_exit_code`
    falls back to the parent's tier only because every concrete is in
    the same family tree.
    """
    assert issubclass(CostError, LLMError)


def test_concretes_inherit_cost_error() -> None:
    """Every concrete rollup error subclasses :class:`CostError`."""
    for cls in _CONCRETE_ERRORS:
        assert issubclass(cls, CostError), (
            f"{cls.__name__} must inherit CostError so callers can catch "
            f"every rollup failure via `except CostError`."
        )


def test_each_concrete_has_non_empty_default_remediation() -> None:
    """Operationalises the ``manifest-readers.md`` "errors carry
    remediation" contract: every concrete carries a class-level
    ``default_remediation`` that is a non-empty string.
    """
    for cls in _CONCRETE_ERRORS:
        remediation = cls.default_remediation
        assert isinstance(remediation, str) and remediation.strip(), (
            f"{cls.__name__}.default_remediation must be a non-empty string; got: {remediation!r}"
        )


def test_each_concrete_renders_remediation_line() -> None:
    """The base ``__str__`` (inherited from :class:`LLMError`) renders
    ``<message>\\n  ↳ Remediation: <text>`` for every concrete instance.

    Constructs each concrete with realistic kwargs (the exact shapes
    documented by US-001) and asserts both halves of the rendered string
    are present.
    """
    instances: tuple[CostError, ...] = (
        CostRollupAuditMissingError(project_dir="/tmp/x", audit_dir=".signalforge"),
        CostRollupMalformedRecordError(
            path="/tmp/x/.signalforge/llm_responses.jsonl",
            line_num=3,
            reason="JSONDecodeError at column 1",
        ),
        CostRollupUnknownModelError(model_id="claude-future-9-9"),
    )
    for exc in instances:
        rendered = str(exc)
        assert exc.message in rendered, (
            f"{type(exc).__name__}.__str__ missing message body; got: {rendered!r}"
        )
        assert "↳ Remediation:" in rendered, (
            f"{type(exc).__name__}.__str__ missing remediation footer; got: {rendered!r}"
        )
        assert exc.remediation in rendered, (
            f"{type(exc).__name__}.__str__ missing remediation text; got: {rendered!r}"
        )


def test_each_concrete_maps_to_exit_code_tier_2() -> None:
    """Per DEC-002 of plans/super/157-e2e-cost-and-parallel.md, every
    concrete rollup error maps to CLI tier 2 (input-validation).

    The rollup is part of the LLM call-economics layer, not a fail-closed
    audit-write seam — a tier-3 mapping would imply "external dep
    failed," which is not what these errors mean. See
    :class:`signalforge.llm.errors.EstimateUnknownModelError` for the
    same tier-2 reasoning ("looked-up identifier not in a static
    table").
    """
    for cls in _CONCRETE_ERRORS:
        assert cls in _EXCEPTION_TO_EXIT_CODE, (
            f"{cls.__name__} missing from _EXCEPTION_TO_EXIT_CODE; "
            f"register at tier 2 per DEC-002 of #157 US-001."
        )
        assert _EXCEPTION_TO_EXIT_CODE[cls] == 2, (
            f"{cls.__name__} mapped to tier "
            f"{_EXCEPTION_TO_EXIT_CODE[cls]}; expected tier 2 "
            f"(input-validation) per DEC-002 of #157 US-001."
        )


def test_cost_error_base_dual_registered_at_tier_2() -> None:
    """``CostError`` is dual-registered at tier 2 as a single-tier
    safety net per ``cli-layer.md`` § "7th AST scan" — mirrors the nine
    other single-tier base entries (``ManifestError`` → 1,
    ``LLMError`` → 3, etc.).

    The dual-registration is a forward-compat safety net: a new concrete
    subclass that forgets a per-class mapping still gets tier 2 via the
    MRO walk in :func:`map_exception_to_exit_code`. Scan-7 still fails
    loud on the missing per-class entry.
    """
    assert CostError in _EXCEPTION_TO_EXIT_CODE, (
        "CostError missing from _EXCEPTION_TO_EXIT_CODE; dual-register "
        "at tier 2 per cli-layer.md § '7th AST scan' (the single-tier "
        "safety-net pattern)."
    )
    assert _EXCEPTION_TO_EXIT_CODE[CostError] == 2


def test_cost_error_excluded_from_scan_7_required_mapping() -> None:
    """``CostError`` is in :data:`_EXCEPTION_MAPPING_EXCLUDED_BASES`
    so scan-7 does not require it to be mapped (the dual-registration
    above is the safety net, not the contract).

    Mirrors every other per-stage abstract base in the excluded set.
    """
    assert "CostError" in _EXCEPTION_MAPPING_EXCLUDED_BASES, (
        "CostError must appear in _EXCEPTION_MAPPING_EXCLUDED_BASES so "
        "scan-7 treats it as an abstract base — its three concretes "
        "carry the contract; the base entry is a safety net."
    )


def test_rollup_audit_dir_stub_raises_not_implemented() -> None:
    """US-001 ships the signature only; the body raises
    ``NotImplementedError`` until US-002 lands the implementation.
    """
    with pytest.raises(NotImplementedError):
        rollup_audit_dir(".")
