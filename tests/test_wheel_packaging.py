"""Maintainer-only wheel packaging smoke for the demo tree.

US-002 (issue #47) — gated by ``@pytest.mark.wheel_smoke`` so the default
``pytest`` run skips it. Maintainers run ``pytest -m wheel_smoke --no-cov``
before declaring an init-demo PR ready.

The test builds the wheel via ``python -m build --wheel --outdir <tmp>``
(falling back to ``uvx --from build pyproject-build`` when ``build`` is
not installed in the active Python — matches the project's ``uvx``
convention for ephemeral build tooling, see
``tests/fixtures/regenerate.sh``), opens the artifact via :mod:`zipfile`,
and asserts that the canonical ``src/signalforge/_demo/`` file set ships
under ``signalforge/_demo/`` inside the wheel. Mirrors the
``cli_subprocess`` precedent (``tests/cli/test_subprocess_smoke.py``)
for marker-gated subprocess smokes; the ``--no-cov`` flag is required
because the coverage gate in ``addopts`` would fail a marker-specific
run that exercises only this file (see ``testing-signal.md`` §
"Coverage measurement" / "Known gap").

Closes the P-1 BLOCKER from ``plans/super/47-init-demo.md``: Hatchling's
default ``packages`` glob behaviour on non-``.py`` data files is not
contractually guaranteed, so we add an explicit ``include`` directive in
``pyproject.toml`` AND gate the result with this wheel-build inspection.

DEC-002 — ``[tool.hatch.build.targets.wheel] include = ["src/signalforge/_demo"]``.
DEC-003 — ``wheel_smoke`` marker + this test.
DEC-006 — Ship ``.gitignore`` as-is; the dedicated dotfile test below
pins inclusion explicitly.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

# Repo root resolved from this file's location so the test is invariant to
# pytest's cwd. ``tests/`` lives directly under the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Canonical demo file set under ``signalforge/_demo/`` inside the built
# wheel. Sourced from the on-disk tree at ``src/signalforge/_demo/`` (7
# files; the Austin test fixture has 8 because it carries ``regenerate.sh``
# which is maintainer-only and deliberately excluded from the demo per
# ``plans/super/47-init-demo.md`` line 66). Drift between this list and the
# on-disk tree fails the test loudly — that is the point.
_EXPECTED_DEMO_FILES: tuple[str, ...] = (
    "signalforge/_demo/.gitignore",
    "signalforge/_demo/dbt_project.yml",
    "signalforge/_demo/profiles.yml",
    "signalforge/_demo/signalforge.yml",
    "signalforge/_demo/models/staging/sources.yml",
    "signalforge/_demo/models/staging/stg_bikeshare_trips.sql",
    "signalforge/_demo/target/manifest.json",
)

# Canonical bundled-skill file set under ``signalforge/skills/`` inside the
# built wheel. Established by US-001 of ``plans/super/141-claude-skill-install.md``
# (DEC-001 — the shipped SignalForge skill lives in ``src/signalforge/skills/``
# so Ralph workers can update it from worktrees; DEC-010 — wheel packaging via
# ``[tool.hatch.build.targets.wheel].include``; DEC-011 — wheel_smoke gates the
# file set so a drop fails loud at packaging time; DEC-022 — maintainer-only
# skills under repo-root ``.claude/skills/`` MUST stay excluded). The placeholder
# eval-sidecar lives under ``assets/`` to mirror the SKILL Spec convention.
_EXPECTED_SKILL_FILES: tuple[str, ...] = (
    "signalforge/skills/signalforge/SKILL.md",
    "signalforge/skills/signalforge/assets/SKILL.eval.json",
)


def _build_command(outdir: Path) -> list[str]:
    """Pick the wheel-build invocation available in the current environment.

    Prefers ``python -m build`` when the active interpreter has ``build``
    importable (this is the ticket's canonical invocation, US-002 of
    ``plans/super/47-init-demo.md``). Falls back to ``uvx --from build
    pyproject-build`` when ``build`` is not installed — mirrors the
    project's ephemeral-tooling convention (``tests/fixtures/regenerate.sh``
    uses ``uvx`` for ephemeral ``dbt`` installs at pinned versions).

    Raises ``RuntimeError`` if neither path is available — the maintainer
    needs a loud, actionable failure rather than a confusing
    ``CalledProcessError`` for "No module named build".
    """
    if importlib.util.find_spec("build") is not None:
        return [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)]
    uvx = shutil.which("uvx")
    if uvx is not None:
        return [uvx, "--from", "build", "pyproject-build", "--wheel", "--outdir", str(outdir)]
    raise RuntimeError(
        "wheel_smoke needs `python -m build` available. Install via "
        "`pip install build` in the active venv, or install `uvx` "
        "(https://docs.astral.sh/uv/) on PATH for the ephemeral fallback."
    )


def _build_wheel(outdir: Path) -> Path:
    """Build the wheel into ``outdir`` and return the resolved artifact path.

    Returns the resolved path to the freshly built ``.whl`` artifact.
    Raises ``subprocess.CalledProcessError`` on build failure (the
    maintainer needs to see the error to fix it) or ``AssertionError``
    if no wheel landed in ``outdir``. A 60-second timeout mirrors the
    ``cli_subprocess`` precedent — Hatchling on this repo builds in
    well under 5 seconds; a 60s timeout means a real regression, not a
    slow build.
    """
    subprocess.run(
        _build_command(outdir),
        cwd=str(_REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    wheels = sorted(outdir.glob("*.whl"))
    assert wheels, f"no wheel artifact landed in {outdir}"
    # Exactly one wheel is expected per build invocation.
    assert len(wheels) == 1, f"expected one wheel in {outdir}, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def _built_wheel_members(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """Build the wheel ONCE per test-module run and return its member list.

    Module-scoped because ``python -m build --wheel`` is the expensive
    operation here (~5-15s including PEP 517 isolation). Running it once
    and asserting separate invariants over the resulting member set keeps
    ``pytest -m wheel_smoke`` fast without weakening either assertion.
    """
    outdir = tmp_path_factory.mktemp("wheel-build")
    wheel_path = _build_wheel(outdir)
    with zipfile.ZipFile(wheel_path) as zf:
        return set(zf.namelist())


@pytest.mark.wheel_smoke
def test_wheel_includes_all_demo_files(_built_wheel_members: set[str]) -> None:
    """Every file in ``src/signalforge/_demo/`` ships in the built wheel.

    Gates DEC-002 (``include = ["src/signalforge/_demo"]``) at packaging
    time. Without the directive the wheel ships zero demo data files —
    Hatchling's default ``packages`` glob is not guaranteed to pick up
    non-``.py`` files.
    """
    missing = [name for name in _EXPECTED_DEMO_FILES if name not in _built_wheel_members]
    assert not missing, (
        f"wheel is missing demo files: {missing}. "
        f"Check `[tool.hatch.build.targets.wheel] include` in pyproject.toml."
    )


@pytest.mark.wheel_smoke
def test_wheel_excludes_scripts_directory(_built_wheel_members: set[str]) -> None:
    """The repo-root ``scripts/`` dir MUST NOT ship in the built wheel.

    Established by US-003 of ``plans/super/157-e2e-cost-and-parallel.md``:
    ``scripts/measure_e2e_cost.py`` is a maintainer-only audit helper that
    runs from the repo checkout and is never invoked from an installed
    wheel. The ``[tool.hatch.build.targets.wheel]`` table in
    ``pyproject.toml`` deliberately omits ``scripts/`` from both
    ``packages`` and ``include`` — this test gates that omission so a
    future contributor adding ``scripts/`` to either list (or Hatchling
    silently picking it up) fails loud at packaging time rather than
    silently bloating the wheel.
    """
    scripts_members = [name for name in _built_wheel_members if name.startswith("scripts/")]
    assert not scripts_members, (
        "wheel unexpectedly ships entries under `scripts/`: "
        f"{scripts_members}. Check `[tool.hatch.build.targets.wheel]` in "
        "pyproject.toml — `scripts/` is maintainer-only and must stay out "
        "of the wheel (US-003 of plans/super/157-e2e-cost-and-parallel.md)."
    )


@pytest.mark.wheel_smoke
def test_wheel_includes_all_bundled_skill_files(_built_wheel_members: set[str]) -> None:
    """Every file in ``src/signalforge/skills/`` ships in the built wheel.

    Gates DEC-010 (``include = [..., "src/signalforge/skills"]``) at
    packaging time. The bundled SignalForge skill (US-007) plus its
    placeholder eval sidecar (US-001) must reach an installed wheel so
    the ``install-skill`` CLI (US-002) can copy them into
    ``~/.claude/skills/signalforge/``. Without the directive Hatchling's
    default ``packages`` glob is not guaranteed to pick up non-``.py``
    skill data (mirrors the demo-tree precedent, ``DEC-002`` of
    ``plans/super/47-init-demo.md``).
    """
    missing = [name for name in _EXPECTED_SKILL_FILES if name not in _built_wheel_members]
    assert not missing, (
        f"wheel is missing bundled skill files: {missing}. "
        f"Check `[tool.hatch.build.targets.wheel] include` in pyproject.toml."
    )


@pytest.mark.wheel_smoke
def test_wheel_excludes_maintainer_only_claude_skills(_built_wheel_members: set[str]) -> None:
    """No ``.claude/skills/*`` entry may ship in the built wheel.

    DEC-022 of ``plans/super/141-claude-skill-install.md`` — maintainer-only
    skills (``release-manager``, ``review-agentskills-spec``) live at
    repo-root ``.claude/skills/`` and MUST stay out of the distributed
    wheel. They are orchestrator-only conventions and a Ralph worker
    cannot edit them from a worktree (see the ``ralph-worker-claude-dir-perms``
    memory), so accidentally bundling them would both bloat the wheel and
    surface internal tooling to end-users. The shipped, user-facing skill
    lives under ``src/signalforge/skills/`` (see the positive assertion
    above); this negative gate catches a future contributor who mirrors
    the repo-root ``.claude/skills/`` tree into the wheel by mistake.
    """
    leaked = sorted(name for name in _built_wheel_members if ".claude/skills/" in name)
    assert not leaked, (
        "wheel unexpectedly ships entries under `.claude/skills/`: "
        f"{leaked}. Maintainer-only skills (release-manager, "
        "review-agentskills-spec) must stay at repo-root `.claude/skills/` "
        "and out of the wheel (DEC-022 of plans/super/141-claude-skill-install.md). "
        "The shipped user-facing skill lives under `src/signalforge/skills/`."
    )


@pytest.mark.wheel_smoke
def test_wheel_includes_demo_gitignore_dotfile(_built_wheel_members: set[str]) -> None:
    """``signalforge/_demo/.gitignore`` ships in the wheel (DEC-006).

    Hatchling's ``include`` glob behaviour on dotfiles is not contractually
    guaranteed (see ``plans/super/47-init-demo.md`` P-6). If this test
    fails, the fallback per DEC-006 is to rename the source-tree file to
    ``gitignore.demo`` and have ``copy_demo`` rewrite the on-disk name at
    copy time. The wheel_smoke surface is the load-bearing gate for
    discovering the regression — manual ``unzip -l`` at release time is
    too late.
    """
    assert "signalforge/_demo/.gitignore" in _built_wheel_members, (
        "wheel does not ship `signalforge/_demo/.gitignore`. "
        "Hatchling may have dropped the dotfile under the directory glob; "
        "see DEC-006 of plans/super/47-init-demo.md for the fallback."
    )
