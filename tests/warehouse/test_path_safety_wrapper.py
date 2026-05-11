"""Tests for the warehouse-layer ``_path_safety`` wrapper (issue #43).

The wrapper translates the layer-neutral
:class:`signalforge._common.path_safety.PathContainmentError` into
:class:`signalforge.warehouse.errors.ProfileNotFoundError` so warehouse
callers (``warehouse/profiles.py``) keep one typed-error catch surface.
These tests pin the wrap's diagnostic shape — in particular
``searched_paths`` carries the project-scoped location actually
validated, not a cwd-relative bare name (Copilot regression catch on
PR #72).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.warehouse._path_safety import canonicalise_path
from signalforge.warehouse.errors import ProfileNotFoundError


@pytest.mark.unit
def test_wrapper_translates_to_profile_not_found_error(tmp_path: Path) -> None:
    """A containment failure inside the common helper surfaces as
    :class:`ProfileNotFoundError` at the wrapper boundary.
    """
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.yml"
    outside.write_text("x")

    with pytest.raises(ProfileNotFoundError):
        canonicalise_path(outside, project)


@pytest.mark.unit
def test_wrapper_searched_paths_is_project_scoped_for_relative_input(
    tmp_path: Path,
) -> None:
    """A relative ``input_path`` records the project-scoped joined path
    in ``ProfileNotFoundError.searched_paths``, not a cwd-relative bare
    name. Pinned per Copilot review on PR #72 (the wrapper's
    diagnostic surface for ``warehouse/profiles.py``-style callers).
    """
    # Trigger a failure on a relative input by pointing project_dir at a
    # missing directory — the failure mode doesn't matter; we only check
    # the searched_paths shape.
    missing_project = tmp_path / "does-not-exist"

    with pytest.raises(ProfileNotFoundError) as excinfo:
        canonicalise_path("profiles.yml", missing_project)

    searched = excinfo.value.searched_paths
    assert len(searched) == 1
    # Must include the project_dir context, not just the bare relative
    # filename.
    assert searched[0] == missing_project / "profiles.yml"


@pytest.mark.unit
def test_wrapper_searched_paths_preserves_absolute_input(tmp_path: Path) -> None:
    """An absolute ``input_path`` is recorded verbatim in
    ``searched_paths`` (no double-prefix with ``project_dir``).
    """
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside" / "profiles.yml"

    with pytest.raises(ProfileNotFoundError) as excinfo:
        canonicalise_path(outside, project)

    searched = excinfo.value.searched_paths
    assert len(searched) == 1
    assert searched[0] == outside
