"""Public-API enforcement for ``signalforge.diff`` (US-012 of #8).

DEC-004 of ``plans/super/8-diff-renderer.md``: the public surface in
``signalforge.diff.__init__`` exposes the orchestrator
(:func:`signalforge.diff.render_diff`), the config
(:class:`signalforge.diff.DiffConfig` + :func:`signalforge.diff.load_diff_config`),
the typed result models (:class:`signalforge.diff.DiffReport`,
:class:`signalforge.diff.DiffEntry`, :data:`signalforge.diff.Tier`),
and the seven-class :class:`signalforge.diff.DiffError` hierarchy.
The three concrete renderers (``AnsiRenderer``, ``MarkdownRenderer``,
``JsonRenderer``) and every internal helper module are private —
reachable via dotted import only, but absent from
``signalforge.diff.__all__`` and from ``dir(signalforge.diff)``'s
documented contract.

Mirrors :mod:`tests.safety.test_public_api`,
:mod:`tests.warehouse.test_public_api`, and
:mod:`tests.manifest.test_public_api` so ``__all__`` and the
documented surface cannot drift.
"""

from __future__ import annotations

import pytest

import signalforge.diff as diff_pkg

# The exact public surface documented in docs/diff-ops.md and pinned by
# DEC-004 of plans/super/8-diff-renderer.md. Adding a name here without
# adding it to ``signalforge.diff.__all__`` (or vice versa) breaks this
# test loudly.
_DOCUMENTED_PUBLIC = (
    # Orchestrator
    "render_diff",
    "render_to_text",
    # Config
    "DiffConfig",
    "load_diff_config",
    # Result models + literal
    "DiffEntry",
    "DiffReport",
    "Tier",
    # Errors (9)
    "DiffError",
    "DiffCandidateModelMismatchError",
    "DiffPruneResultModelMismatchError",
    "DiffGradingReportModelMismatchError",
    "DiffInputTooLargeError",
    "DiffSidecarRecordTooLargeError",
    "DiffSidecarWriteError",
    "DiffTestFileRecordTooLargeError",
    "DiffTestFileWriteError",
)


def test_documented_surface_importable_from_package_root() -> None:
    """Every documented name resolves on ``signalforge.diff``."""
    for name in _DOCUMENTED_PUBLIC:
        assert hasattr(diff_pkg, name), f"signalforge.diff is missing {name!r}"


def test_all_lists_documented_surface() -> None:
    """``__all__`` matches the documented surface exactly.

    A new public-API name landing in ``__all__`` without a docs update
    (or vice versa) breaks this test; the failure message points at
    the divergence rather than letting it slip through review.
    """
    assert sorted(diff_pkg.__all__) == sorted(_DOCUMENTED_PUBLIC), (
        "signalforge.diff.__all__ does not match the documented surface. "
        f"Missing from __all__: {sorted(set(_DOCUMENTED_PUBLIC) - set(diff_pkg.__all__))}; "
        f"unexpected in __all__: {sorted(set(diff_pkg.__all__) - set(_DOCUMENTED_PUBLIC))}."
    )


def test_each_public_name_is_importable_via_from_signalforge_diff() -> None:
    """Every documented public name is importable directly.

    A regression that left a name listed in ``__all__`` but not actually
    exposed at module scope (e.g., a typo'd re-export) would slip past
    the ``hasattr`` check above; this test exercises the actual
    ``from signalforge.diff import <name>`` path.
    """
    from signalforge.diff import (  # noqa: F401
        DiffCandidateModelMismatchError,
        DiffConfig,
        DiffEntry,
        DiffError,
        DiffGradingReportModelMismatchError,
        DiffInputTooLargeError,
        DiffPruneResultModelMismatchError,
        DiffReport,
        DiffSidecarRecordTooLargeError,
        DiffSidecarWriteError,
        DiffTestFileRecordTooLargeError,
        DiffTestFileWriteError,
        Tier,
        load_diff_config,
        render_diff,
        render_to_text,
    )


@pytest.mark.parametrize(
    "private_name",
    [
        "AnsiRenderer",
        "MarkdownRenderer",
        "JsonRenderer",
        "Renderer",
        "write_sidecar",
        "emit_proposed_yaml",
        "artifact_id_for",
        "compute_args_hashes",
        "strip_ansi_escapes",
        "escape_markdown_scalar",
    ],
)
def test_private_concretes_not_in_all(private_name: str) -> None:
    """Concrete renderers and internal helpers stay out of ``__all__``.

    DEC-004: only the typed-result + orchestrator + config + errors
    form the public contract. The three renderer classes live at
    ``signalforge.diff._renderers``; helper modules
    (``_emitter``, ``_sidecar``, ``_artifact_id``, ``_ansi_safety``,
    ``_markdown_safety``) are reachable via dotted import for internal
    use but must not appear in ``__all__`` / the package's documented
    surface.

    Note: Python attaches imported submodules to the parent package's
    namespace regardless of ``__init__.py``, so we assert against
    ``__all__`` (the ``from package import *`` surface) rather than
    against ``hasattr(diff_pkg, ...)``. The ``_``-prefixed submodules
    (``_renderers``, ``_emitter``, ``_sidecar``, ...) are similarly
    accessible via dotted import as a documented escape hatch.
    """
    assert private_name not in diff_pkg.__all__, (
        f"private name {private_name!r} leaked into signalforge.diff.__all__"
    )


def test_concrete_renderers_reachable_via_dotted_import() -> None:
    """Concrete renderers stay reachable for internal use.

    DEC-004 keeps the concretes private from the package surface but
    documents them as importable via ``signalforge.diff._renderers``
    so future internal callers (orchestrator, CLI) don't need to
    re-implement rendering. A test fixture or v0.2 CLI reaching for
    ``from signalforge.diff._renderers import AnsiRenderer`` must keep
    working — this test pins the dotted-import escape hatch.
    """
    from signalforge.diff._renderers import (  # noqa: F401
        AnsiRenderer,
        JsonRenderer,
        MarkdownRenderer,
        Renderer,
    )


def test_private_sidecar_writer_reachable_via_dotted_import() -> None:
    """Sidecar writer reachable via dotted import (defence in depth)."""
    from signalforge.diff._sidecar import write_sidecar  # noqa: F401


def test_error_hierarchy_is_complete() -> None:
    """All nine error classes inherit from ``DiffError``.

    A regression that landed a typed error without subclassing the
    base would surface as a missing-base-class error here. Mirrors
    the equivalent assertion in ``tests/grade/test_errors.py``.
    """
    from signalforge.diff import (
        DiffCandidateModelMismatchError,
        DiffError,
        DiffGradingReportModelMismatchError,
        DiffInputTooLargeError,
        DiffPruneResultModelMismatchError,
        DiffSidecarRecordTooLargeError,
        DiffSidecarWriteError,
        DiffTestFileRecordTooLargeError,
        DiffTestFileWriteError,
    )

    for cls in (
        DiffCandidateModelMismatchError,
        DiffPruneResultModelMismatchError,
        DiffGradingReportModelMismatchError,
        DiffInputTooLargeError,
        DiffSidecarRecordTooLargeError,
        DiffSidecarWriteError,
        DiffTestFileRecordTooLargeError,
        DiffTestFileWriteError,
    ):
        assert issubclass(cls, DiffError), f"{cls.__name__} is not a DiffError subclass"
