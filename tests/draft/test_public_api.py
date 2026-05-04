"""Public-API enforcement for ``signalforge.draft``.

DEC-013 of ``plans/super/9-cli-entrypoint.md``: the CLI tier-maps the
draft layer's typed exceptions to deterministic exit codes, so the
CLI cannot reach into private modules — every name the CLI imports
must be re-exported on :mod:`signalforge.draft`'s public surface.

Mirrors :mod:`tests.diff.test_public_api`,
:mod:`tests.safety.test_public_api`,
:mod:`tests.warehouse.test_public_api`, and
:mod:`tests.manifest.test_public_api` so ``__all__`` and the
documented surface cannot drift.
"""

from __future__ import annotations

import signalforge.draft as draft_pkg

# The exact public surface the CLI (and library callers) consume.
# Adding a name to ``signalforge.draft.__all__`` without adding it
# here (or vice versa) breaks this test loudly.
_DOCUMENTED_PUBLIC = (
    # Models
    "CandidateColumn",
    "CandidateSchema",
    "CandidateTest",
    # Config
    "DraftConfig",
    "load_draft_config",
    # Result + functions
    "DraftOutcome",
    "draft_from_request",
    "draft_schema",
    # Audit event (read-back model)
    "LLMResponseEvent",
    # Errors — base
    "DraftError",
    # Errors — config
    "DraftConfigInvalidError",
    "DraftConfigNotFoundError",
    # Errors — LLM-output base + concretes
    "LLMOutputError",
    "LLMOutputAnchorContractError",
    "LLMOutputJSONError",
    "LLMOutputValidationError",
    # Errors — prompt envelope + response audit
    "LLMResponseAuditRecordTooLargeError",
    "LLMResponseAuditWriteError",
    "PromptEnvelopeBreachError",
)


def test_documented_surface_importable_from_package_root() -> None:
    """Every documented name resolves on ``signalforge.draft``."""
    for name in _DOCUMENTED_PUBLIC:
        assert hasattr(draft_pkg, name), f"signalforge.draft is missing {name!r}"


def test_all_lists_documented_surface() -> None:
    """``__all__`` matches the documented surface exactly."""
    assert sorted(draft_pkg.__all__) == sorted(_DOCUMENTED_PUBLIC), (
        "signalforge.draft.__all__ does not match the documented surface. "
        f"Missing from __all__: {sorted(set(_DOCUMENTED_PUBLIC) - set(draft_pkg.__all__))}; "
        f"unexpected in __all__: {sorted(set(draft_pkg.__all__) - set(_DOCUMENTED_PUBLIC))}."
    )


def test_each_public_name_is_importable_via_from_signalforge_draft() -> None:
    """Every documented public name is importable directly.

    Pins the ``from signalforge.draft import <name>`` path the CLI
    (#9) and library callers rely on.
    """
    from signalforge.draft import (  # noqa: F401
        CandidateColumn,
        CandidateSchema,
        CandidateTest,
        DraftConfig,
        DraftConfigInvalidError,
        DraftConfigNotFoundError,
        DraftError,
        DraftOutcome,
        LLMOutputAnchorContractError,
        LLMOutputError,
        LLMOutputJSONError,
        LLMOutputValidationError,
        LLMResponseAuditRecordTooLargeError,
        LLMResponseAuditWriteError,
        LLMResponseEvent,
        PromptEnvelopeBreachError,
        draft_from_request,
        draft_schema,
        load_draft_config,
    )


def test_typed_errors_subclass_draft_error() -> None:
    """All draft-layer typed errors descend from ``DraftError``.

    A regression that landed a typed error without subclassing the
    base would surface as a missing-base-class error here. Mirrors
    the equivalent assertion in :mod:`tests.diff.test_public_api`.
    """
    from signalforge.draft import (
        DraftConfigInvalidError,
        DraftConfigNotFoundError,
        DraftError,
        LLMOutputAnchorContractError,
        LLMOutputError,
        LLMOutputJSONError,
        LLMOutputValidationError,
        LLMResponseAuditRecordTooLargeError,
        LLMResponseAuditWriteError,
        PromptEnvelopeBreachError,
    )

    for cls in (
        DraftConfigInvalidError,
        DraftConfigNotFoundError,
        LLMOutputAnchorContractError,
        LLMOutputError,
        LLMOutputJSONError,
        LLMOutputValidationError,
        LLMResponseAuditRecordTooLargeError,
        LLMResponseAuditWriteError,
        PromptEnvelopeBreachError,
    ):
        assert issubclass(cls, DraftError), f"{cls.__name__} is not a DraftError subclass"
