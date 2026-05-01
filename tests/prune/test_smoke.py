"""Public-API smoke test for signalforge.prune (US-013).

Mirrors tests/test_smoke.py at the package root. Asserts every public
name lands at the expected re-export path with the expected type.
"""

from __future__ import annotations


def test_public_api_imports() -> None:
    """Every documented public name is importable from signalforge.prune."""
    from signalforge.prune import (
        DropReason,  # noqa: F401
        PruneAuditRecordTooLargeError,  # noqa: F401
        PruneAuditWriteError,  # noqa: F401
        PruneConfig,  # noqa: F401
        PruneConfigError,  # noqa: F401
        PruneDecision,  # noqa: F401
        PruneError,  # noqa: F401
        PruneEvent,  # noqa: F401
        PruneResult,  # noqa: F401
        PruneTimeoutError,  # noqa: F401
        PruneTrustedModelNotFoundError,  # noqa: F401
        Scope,  # noqa: F401
        load_prune_config,
        prune_tests,
    )

    # Spot-check that the imports are not None and at least one is callable
    assert prune_tests is not None
    assert callable(prune_tests)
    assert callable(load_prune_config)


def test_public_api_dunder_all_matches_imports() -> None:
    """``signalforge.prune.__all__`` enumerates every public name."""
    import signalforge.prune as prune

    expected = {
        "prune_tests",
        "PruneResult",
        "PruneDecision",
        "PruneConfig",
        "load_prune_config",
        "DropReason",
        "Scope",
        "PruneEvent",
        "PruneError",
        "PruneConfigError",
        "PruneTrustedModelNotFoundError",
        "PruneTimeoutError",
        "PruneAuditWriteError",
        "PruneAuditRecordTooLargeError",
    }
    assert set(prune.__all__) == expected


def test_internal_helpers_are_not_re_exported() -> None:
    """Underscore-prefixed helpers stay out of the public surface.

    They remain reachable via dotted import (``from
    signalforge.prune.engine import _sleep``) for tests, but are not
    part of the package's public API.
    """
    import signalforge.prune as prune

    # A handful of internals that should NOT be on the public surface
    assert not hasattr(prune, "_compile_test")
    assert not hasattr(prune, "_write_prune_event")
    assert not hasattr(prune, "_build_prune_event")
    assert not hasattr(prune, "_RequiresFutureData")
    assert not hasattr(prune, "_compute_config_hash")
    assert not hasattr(prune, "_compute_compiled_sql_hash")
    assert not hasattr(prune, "_sleep")
