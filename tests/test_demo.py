"""Tests for the public :mod:`signalforge.demo` module (US-003)."""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.demo import (
    DemoDestExistsError,
    DemoDestUnsafeError,
    DemoError,
    DemoFixtureMissingError,
    DemoPathError,
    copy_demo,
)

# ---------------------------------------------------------------------------
# Helper: expected top-level entries in the shipped demo tree.
# ---------------------------------------------------------------------------

_EXPECTED_TOP_LEVEL = frozenset(
    {
        ".gitignore",
        "dbt_project.yml",
        "models",
        "profiles.yml",
        "signalforge.yml",
        "target",
    }
)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_copy_demo_to_empty_dir(tmp_path: Path) -> None:
    dest = tmp_path / "demo"
    # Pre-create an empty dir — DEC documents that empty dirs proceed.
    dest.mkdir()
    result = copy_demo(dest)
    assert result.is_dir()
    entries = {p.name for p in result.iterdir()}
    assert _EXPECTED_TOP_LEVEL.issubset(entries)


def test_copy_demo_to_nonexistent_dir_creates_and_copies(tmp_path: Path) -> None:
    dest = tmp_path / "fresh"
    assert not dest.exists()
    result = copy_demo(dest)
    assert result.is_dir()
    entries = {p.name for p in result.iterdir()}
    assert _EXPECTED_TOP_LEVEL.issubset(entries)


def test_copy_demo_returns_resolved_dest_path(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    result = copy_demo(dest)
    assert isinstance(result, Path)
    assert result == dest.expanduser().resolve()
    # Resolved path is absolute.
    assert result.is_absolute()


def test_copy_demo_copies_target_manifest_json(tmp_path: Path) -> None:
    """DEC-011: the locked ``target/manifest.json`` ships with the tree."""
    dest = tmp_path / "demo"
    result = copy_demo(dest)
    manifest = result / "target" / "manifest.json"
    assert manifest.is_file()
    # Non-empty — pin against an empty-file regression.
    assert manifest.stat().st_size > 0


def test_copy_demo_copies_dotfile_gitignore(tmp_path: Path) -> None:
    """DEC-006: ``.gitignore`` is a dotfile and must be copied."""
    dest = tmp_path / "demo"
    result = copy_demo(dest)
    gitignore = result / ".gitignore"
    assert gitignore.is_file()
    assert gitignore.stat().st_size > 0


def test_copy_demo_with_relative_dest_resolves_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = copy_demo("demo-rel")
    assert result == (tmp_path / "demo-rel").resolve()
    assert result.is_dir()


def test_copy_demo_with_symlink_dest_resolves_target(tmp_path: Path) -> None:
    """A symlink ``dest`` resolves to its target; the symlink itself is
    not preserved — the resolved-target directory receives the copy."""

    real_target = tmp_path / "real"
    real_target.mkdir()
    symlink = tmp_path / "link"
    symlink.symlink_to(real_target)

    result = copy_demo(symlink)
    # resolved dest should be the real target, not the symlink itself.
    assert result == real_target.resolve()
    assert (real_target / "dbt_project.yml").is_file()


# ---------------------------------------------------------------------------
# Existence-gate (force=False)
# ---------------------------------------------------------------------------


def test_copy_demo_to_nonempty_dir_without_force_raises_dest_exists(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "preexisting.txt").write_text("hi")
    with pytest.raises(DemoDestExistsError) as excinfo:
        copy_demo(dest)
    # The default remediation footer surfaces in __str__.
    rendered = str(excinfo.value)
    assert "exists" in rendered
    assert "Remediation" in rendered
    # The preexisting file is untouched.
    assert (dest / "preexisting.txt").read_text() == "hi"


# ---------------------------------------------------------------------------
# Force semantics
# ---------------------------------------------------------------------------


def test_copy_demo_to_nonempty_dir_with_force_replaces_atomically(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "stale.txt").write_text("stale content")
    (dest / "old_subdir").mkdir()
    (dest / "old_subdir" / "inside.txt").write_text("also stale")

    result = copy_demo(dest, force=True)
    assert result.is_dir()
    # Stale content is gone.
    assert not (dest / "stale.txt").exists()
    assert not (dest / "old_subdir").exists()
    # Demo content is present.
    assert (dest / "dbt_project.yml").is_file()
    assert (dest / "target" / "manifest.json").is_file()


def test_copy_demo_force_against_home_raises_dest_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    with pytest.raises(DemoDestUnsafeError) as excinfo:
        copy_demo(fake_home, force=True)
    assert "force" in str(excinfo.value).lower() or "system or user" in str(excinfo.value)
    # The "home" dir was not nuked.
    assert fake_home.is_dir()


def test_copy_demo_force_against_root_raises_dest_unsafe() -> None:
    with pytest.raises(DemoDestUnsafeError):
        copy_demo("/", force=True)


def test_copy_demo_force_against_cwd_raises_dest_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DemoDestUnsafeError):
        copy_demo(tmp_path, force=True)
    # cwd directory wasn't nuked.
    assert tmp_path.is_dir()


# ---------------------------------------------------------------------------
# Symlink cycle → DemoPathError
# ---------------------------------------------------------------------------


def test_copy_demo_with_cyclic_symlink_raises_demo_path_error(tmp_path: Path) -> None:
    """A symlink cycle at the destination raises :class:`DemoPathError`."""

    # Create A -> B and B -> A (mutually pointing symlinks form a resolve cycle).
    link_a = tmp_path / "loop_a"
    link_b = tmp_path / "loop_b"
    link_a.symlink_to(link_b)
    link_b.symlink_to(link_a)

    # On some filesystems (notably WSL2), resolve() does NOT raise on this
    # pattern — it returns a path with the symlink unresolved. Skip when
    # the platform doesn't enforce the cycle guard the way the contract
    # expects; the GitHub Actions Linux runner does enforce it.
    try:
        link_a.resolve(strict=False)
    except RuntimeError:
        pass
    else:
        pytest.skip(
            "filesystem does not raise RuntimeError on symlink cycles; "
            "DemoPathError path is verified on the CI Linux runner"
        )

    with pytest.raises(DemoPathError) as excinfo:
        copy_demo(link_a)
    # The triggering RuntimeError rides on the cause.
    assert isinstance(excinfo.value.cause, RuntimeError)


# ---------------------------------------------------------------------------
# DemoFixtureMissingError — broken-install path
# ---------------------------------------------------------------------------


def test_copy_demo_fixture_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``importlib.resources`` cannot locate ``_demo``, raise.

    Simulated by monkeypatching the ``files`` lookup to return a
    non-directory traversable.
    """

    import signalforge.demo as demo_mod

    class _NotADir:
        def joinpath(self, name: str) -> _NotADir:
            return self

        def is_dir(self) -> bool:
            return False

    monkeypatch.setattr(demo_mod, "files", lambda pkg: _NotADir())
    with pytest.raises(DemoFixtureMissingError) as excinfo:
        copy_demo(tmp_path / "demo")
    assert "bundled" in str(excinfo.value) or "missing" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Error-class shape
# ---------------------------------------------------------------------------


def test_demo_errors_share_base_class() -> None:
    assert issubclass(DemoDestExistsError, DemoError)
    assert issubclass(DemoDestUnsafeError, DemoError)
    assert issubclass(DemoPathError, DemoError)
    assert issubclass(DemoFixtureMissingError, DemoError)


def test_demo_error_str_renders_remediation_footer() -> None:
    err = DemoDestExistsError("destination 'x' exists and is not empty")
    rendered = str(err)
    assert "destination 'x' exists and is not empty" in rendered
    assert "↳ Remediation:" in rendered


def test_demo_error_str_without_remediation_is_message_only() -> None:
    err = DemoError("plain message", remediation=None)
    # Base class default_remediation is None; subclasses set defaults.
    assert str(err) == "plain message"


def test_demo_error_remediation_override_wins() -> None:
    err = DemoDestExistsError("dest 'y' exists", remediation="custom hint")
    assert "custom hint" in str(err)
    assert "Remove the destination" not in str(err)


# ---------------------------------------------------------------------------
# Public API import surface
# ---------------------------------------------------------------------------


def test_public_import_surface() -> None:
    # The README + DEC-012 promises this exact import.
    # The signature accepts (dest, *, force).
    import inspect

    from signalforge.demo import copy_demo as _copy_demo  # noqa: F401

    sig = inspect.signature(_copy_demo)
    params = list(sig.parameters.values())
    assert params[0].name == "dest"
    assert sig.parameters["force"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["force"].default is False


def test_module_all_lists_public_surface() -> None:
    import signalforge.demo as demo_mod

    expected = {
        "DemoDestExistsError",
        "DemoDestUnsafeError",
        "DemoError",
        "DemoFixtureMissingError",
        "DemoPathError",
        "copy_demo",
    }
    assert set(demo_mod.__all__) == expected


# ---------------------------------------------------------------------------
# Sanity: the copy preserves a nested file structure
# ---------------------------------------------------------------------------


def test_copy_demo_preserves_nested_model_sql(tmp_path: Path) -> None:
    dest = tmp_path / "demo"
    copy_demo(dest)
    nested = dest / "models" / "staging" / "stg_bikeshare_trips.sql"
    assert nested.is_file()
    assert nested.stat().st_size > 0


def test_copy_demo_does_not_follow_into_unrelated_symlinks_on_destination(
    tmp_path: Path,
) -> None:
    """A symlink at ``dest`` that points to a target outside ``tmp_path``
    is followed via ``.resolve()`` — the test exists to pin behaviour,
    not to catch a regression (since ``init-demo`` deliberately does
    not enforce a containment boundary per DEC-004)."""

    real_target = tmp_path / "other"
    real_target.mkdir()
    symlink = tmp_path / "link"
    symlink.symlink_to(real_target)

    result = copy_demo(symlink)
    assert result == real_target.resolve()
    # Confirms the resolved dest is real_target, not the symlink path.
    assert not result.is_symlink()
    assert (result / "dbt_project.yml").is_file()
