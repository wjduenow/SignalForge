"""Public API contract tests for :mod:`signalforge.warehouse` (DEC-017).

Mirrors :mod:`tests.manifest.test_public_api`: guards the warehouse
package's public surface against accidental drift — both shrinkage (a
documented name disappears) and growth (an internal helper silently
becomes part of the public namespace).
"""

from __future__ import annotations

import pytest

import signalforge.warehouse as sf_warehouse
import signalforge.warehouse.errors as sf_warehouse_errors

REQUIRED_NAMES = {
    "WarehouseAdapter",
    "BigQueryAdapter",
    "Dialect",
    "TableRef",
    "PartitionFilter",
    "ColumnStats",
    "TestResult",
    "DbtProfileTarget",
    "load_profile",
    "BIGQUERY_DIALECT",
    "WarehouseError",
}


@pytest.mark.unit
def test_all_names_are_bound() -> None:
    """Every name listed in ``__all__`` must be a real attribute on the package."""
    for name in sf_warehouse.__all__:
        assert hasattr(sf_warehouse, name), (
            f"signalforge.warehouse.__all__ lists {name!r} but it is not bound on the package"
        )


@pytest.mark.unit
def test_no_underscore_prefixed_in_all() -> None:
    """``__all__`` must not advertise underscore-prefixed (private) names."""
    for name in sf_warehouse.__all__:
        assert not name.startswith("_"), (
            f"signalforge.warehouse.__all__ contains private-looking name {name!r}"
        )


@pytest.mark.unit
def test_all_is_sorted() -> None:
    """``__all__`` must remain sorted alphabetically (capitals before lowercase).

    Hard-coded literal in :mod:`signalforge.warehouse`'s ``__init__`` —
    pyright's ``reportUnsupportedDunderAll`` rejects a computed
    ``sorted([...])`` form. This test catches drift instead.
    """
    assert sf_warehouse.__all__ == sorted(sf_warehouse.__all__)


@pytest.mark.unit
def test_internal_helpers_not_in_namespace() -> None:
    """Internal helpers are reachable via dotted-module imports but must NOT
    pollute the package's top-level namespace.

    ``from signalforge.warehouse import *`` consumes ``__all__``; the
    helper modules (``_sql_safety``, ``_path_safety``,
    ``_test_result_repr``) intentionally stay off it.
    """
    # Reachable as dotted modules — these imports must succeed.
    from signalforge.warehouse import _path_safety, _sql_safety, _test_result_repr

    assert _sql_safety is not None
    assert _path_safety is not None
    assert _test_result_repr is not None

    # But not advertised as public attributes alongside the documented surface.
    public_attrs = {n for n in dir(sf_warehouse) if not n.startswith("__")}
    for helper in ("_sql_safety", "_path_safety", "_test_result_repr"):
        # The submodules are reachable as attributes after import (Python
        # caches them on the parent package), but they must never appear
        # in ``__all__`` — that is what ``from package import *`` consumes.
        assert helper not in sf_warehouse.__all__, (
            f"Internal helper {helper!r} leaked into signalforge.warehouse.__all__"
        )
        # Sanity: the helper IS the underscore-prefixed module form, and
        # the bare-name (without underscore) must not exist as a public
        # attribute either.
        bare = helper.lstrip("_")
        assert bare not in public_attrs, (
            f"Helper {helper!r} appears to be re-exported as {bare!r} on the package"
        )


@pytest.mark.unit
def test_all_includes_required_classes() -> None:
    """Documented public surface must be present in ``__all__``."""
    missing = REQUIRED_NAMES - set(sf_warehouse.__all__)
    assert not missing, f"signalforge.warehouse.__all__ is missing required names: {missing}"


@pytest.mark.unit
def test_all_includes_full_error_hierarchy() -> None:
    """Every name in :mod:`signalforge.warehouse.errors`'s ``__all__`` must be
    re-exported by the package's top-level ``__all__``."""
    errors_all = set(sf_warehouse_errors.__all__)
    package_all = set(sf_warehouse.__all__)
    missing = errors_all - package_all
    assert not missing, f"signalforge.warehouse.__all__ does not re-export error classes: {missing}"
