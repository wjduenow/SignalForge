"""Unit tests for the public :mod:`signalforge.skill` module (US-002 of
issue #141 — ``plans/super/141-claude-skill-install.md``).

These tests pin the six load-bearing contracts from US-002's TDD list:

1. happy path — fresh dir, returns absolute SKILL.md path;
2. overwrite SKILL.md but preserve sibling user files (DEC-003);
3. refuse-with-typed-error when SKILL.md is a pre-existing symlink (DEC-005);
4. symlink-cycle dest raises ``SkillDestPathError`` (DEC-005, mirrors
   ``copy_demo``'s 3.12/3.13 cycle signals);
5. monkeypatched package-data lookup raises
   ``SkillPackageDataMissingError`` (DEC-007);
6. existing regular file at ``dest`` raises ``SkillDestUnsafeError`` (DEC-008).

See ``.claude/rules/testing-signal.md`` — every test has at least one
real assertion that can fail; no ``tests/skill/__init__.py`` (pytest
src-layout convention).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.skill import (
    SkillDestPathError,
    SkillDestUnsafeError,
    SkillError,
    SkillPackageDataMissingError,
    install_skill,
)

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_install_skill_to_fresh_dir_writes_skill_md(tmp_path: Path) -> None:
    """Happy path: ``install_skill`` writes SKILL.md under
    ``<dest>/.claude/skills/signalforge/`` and returns its absolute path."""

    result = install_skill(tmp_path)

    assert isinstance(result, Path)
    assert result.is_absolute()
    assert result == (tmp_path / ".claude" / "skills" / "signalforge" / "SKILL.md").resolve()
    assert result.is_file()
    assert result.stat().st_size > 0


# ---------------------------------------------------------------------------
# Overwrite policy (DEC-003) — SKILL.md replaced, siblings preserved
# ---------------------------------------------------------------------------


def test_install_skill_overwrites_existing_skill_md_unchanged_otherwise(
    tmp_path: Path,
) -> None:
    """DEC-003: ``install_skill`` always overwrites the files SignalForge
    ships (SKILL.md + bundled assets); preserves any sibling files the
    operator added under ``.claude/skills/signalforge/``."""

    skill_dir = tmp_path / ".claude" / "skills" / "signalforge"
    skill_dir.mkdir(parents=True)
    old_skill_md = skill_dir / "SKILL.md"
    old_skill_md.write_text("OLD", encoding="utf-8")
    sibling = skill_dir / "notes.txt"
    sibling.write_text("keepme", encoding="utf-8")

    install_skill(tmp_path)

    assert old_skill_md.read_text(encoding="utf-8") != "OLD"
    assert old_skill_md.stat().st_size > 0
    # User-authored sibling is untouched.
    assert sibling.read_text(encoding="utf-8") == "keepme"


# ---------------------------------------------------------------------------
# Symlinked SKILL.md → SkillDestUnsafeError (DEC-005)
# ---------------------------------------------------------------------------


def test_install_skill_refuses_when_skill_md_is_symlink(tmp_path: Path) -> None:
    """DEC-005: if ``<dest>/.claude/skills/signalforge/SKILL.md`` exists
    AND is a symlink, the install refuses — writing would follow the
    link and clobber an arbitrary destination."""

    skill_dir = tmp_path / ".claude" / "skills" / "signalforge"
    skill_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("not ours", encoding="utf-8")
    skill_md = skill_dir / "SKILL.md"
    skill_md.symlink_to(elsewhere)

    with pytest.raises(SkillDestUnsafeError):
        install_skill(tmp_path)

    # Defence: the link target is untouched.
    assert elsewhere.read_text(encoding="utf-8") == "not ours"


def test_install_skill_refuses_when_install_dir_ancestor_is_symlink(
    tmp_path: Path,
) -> None:
    """DEC-005 (QG-extended): if ANY ancestor under <dest> back to
    ``.claude/`` is a symlinked directory, the install refuses — writing
    through a symlinked ancestor would land in the resolved target
    (e.g. ``.claude/skills/signalforge/`` repointed to /tmp/attacker)
    without operator consent.

    Without this gate, the SKILL.md-only ``is_symlink()`` check would
    pass (SKILL.md inside the linked dir is not itself a symlink) and
    ``shutil.copytree`` would write straight through. Pinned per the
    QG review finding.
    """

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "preexisting.txt").write_text("attacker's data", encoding="utf-8")

    skills_parent = tmp_path / ".claude" / "skills"
    skills_parent.mkdir(parents=True)
    # ``.claude/skills/signalforge/`` -> /tmp/.../elsewhere/  (symlinked ancestor)
    (skills_parent / "signalforge").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(SkillDestUnsafeError):
        install_skill(tmp_path)

    # Defence: the link target's preexisting file is untouched.
    assert (elsewhere / "preexisting.txt").read_text(encoding="utf-8") == "attacker's data"
    # No SKILL.md materialised under the symlinked path.
    assert not (elsewhere / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# Symlink-cycle dest → SkillDestPathError (DEC-005, mirrors copy_demo)
# ---------------------------------------------------------------------------


def test_install_skill_with_cyclic_symlink_dest_raises_dest_path_error(
    tmp_path: Path,
) -> None:
    """DEC-005: a symlink cycle at ``dest`` surfaces as
    :class:`SkillDestPathError` on both Python <=3.12 (``RuntimeError``)
    and >=3.13 (``OSError(ELOOP)``, gh-108958)."""

    link_a = tmp_path / "loop_a"
    link_b = tmp_path / "loop_b"
    link_a.symlink_to(link_b)
    link_b.symlink_to(link_a)

    # Probe the same way ``install_skill`` does — skip cleanly on
    # filesystems where ``resolve(strict=True)`` does not enforce the
    # cycle guard (matches the precedent in tests/test_demo.py).
    try:
        link_a.resolve(strict=True)
    except (RuntimeError, OSError):
        pass
    else:
        pytest.skip(
            "filesystem does not raise on symlink cycles; "
            "SkillDestPathError path is verified on the CI Linux runner"
        )

    with pytest.raises(SkillDestPathError) as excinfo:
        install_skill(link_a)
    assert isinstance(excinfo.value.cause, RuntimeError | OSError)


# ---------------------------------------------------------------------------
# Package-data missing → SkillPackageDataMissingError (DEC-007)
# ---------------------------------------------------------------------------


def test_install_skill_missing_package_data_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-007: if ``importlib.resources`` cannot locate the bundled
    ``skills/signalforge/`` tree, raise
    :class:`SkillPackageDataMissingError`.

    Simulated by monkeypatching the ``files`` lookup to return a
    non-directory traversable (matches the precedent in
    ``tests/test_demo.py::test_copy_demo_fixture_missing_raises``).
    """

    import signalforge.skill as skill_mod

    class _NotADir:
        def joinpath(self, name: str) -> _NotADir:
            return self

        def is_dir(self) -> bool:
            return False

    monkeypatch.setattr(skill_mod, "files", lambda pkg: _NotADir())

    with pytest.raises(SkillPackageDataMissingError) as excinfo:
        install_skill(tmp_path)
    assert "bundled" in str(excinfo.value) or "missing" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Regular-file dest → SkillDestUnsafeError (DEC-008)
# ---------------------------------------------------------------------------


def test_install_skill_dest_is_file_raises_unsafe(tmp_path: Path) -> None:
    """DEC-008: passing an existing regular file as ``dest`` raises
    :class:`SkillDestUnsafeError` — we cannot create
    ``<file>/.claude/skills/...`` underneath it."""

    file_dest = tmp_path / "not_a_dir"
    file_dest.write_text("hello", encoding="utf-8")

    with pytest.raises(SkillDestUnsafeError):
        install_skill(file_dest)

    # File contents unchanged — defence-in-depth.
    assert file_dest.read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------------
# Error-class shape (DEC-008)
# ---------------------------------------------------------------------------


def test_skill_errors_share_base_class() -> None:
    """All three concretes subclass :class:`SkillError`."""
    assert issubclass(SkillDestPathError, SkillError)
    assert issubclass(SkillDestUnsafeError, SkillError)
    assert issubclass(SkillPackageDataMissingError, SkillError)


def test_skill_error_str_renders_remediation_footer() -> None:
    """``__str__`` renders ``message`` + ``↳ Remediation:`` line when
    a ``default_remediation`` is set (mirrors :class:`DemoError`)."""
    err = SkillDestUnsafeError("destination 'x' is not a directory")
    rendered = str(err)
    assert "destination 'x' is not a directory" in rendered
    assert "↳ Remediation:" in rendered
