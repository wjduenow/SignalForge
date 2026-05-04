"""Tests for ``signalforge lint`` (US-004 — config-only validator).

Covers the eight cases from the US-004 acceptance criteria:

* happy path with all 5 blocks valid → exit 0
* happy path with no ``signalforge.yml`` at all → exit 0
* invalid ``safety:`` block → exit 1, stderr names the block
* invalid ``prune:`` block → exit 1
* multiple invalid blocks → exit 1, stderr in DEC-008 header+bullets shape
* ``--config /nonexistent.yml`` → exit 1 with path-not-found error
* ``--help`` → exit 0
* sub-second performance smoke

Every test uses in-process :func:`signalforge.cli.main` + ``capsys`` per
the testing-signal convention. The on-disk ``signalforge.yml`` files are
tiny and live under ``tmp_path``; the CLI reads them through the real
loaders but the loaders themselves do no network / warehouse / LLM work,
so the suite stays under the sub-second target on every fixture.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._factories import make_fake_dbt_project


def _capture(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


def _write_signalforge_yml(project_dir: Path, body: str) -> Path:
    """Drop a ``signalforge.yml`` containing ``body`` under ``project_dir``."""
    config_file = project_dir / "signalforge.yml"
    config_file.write_text(body, encoding="utf-8")
    return config_file


_VALID_ALL_BLOCKS = """\
safety:
  mode: schema-only

llm:
  model: claude-sonnet-4-5
  max_output_tokens: 4096

prune:
  scope: sample
  test_timeout_seconds: 60
  total_budget_seconds: 600
  sample_size: 10000

grade:
  model: claude-sonnet-4-5
  total_budget_seconds: 600
  min_pass_rate: 0.5
  min_mean_score: 0.5

diff:
  context_lines: 3
  render_kind: ansi
"""


_INVALID_SAFETY_MODE = """\
safety:
  mode: not-a-real-mode
"""


_INVALID_PRUNE_TIMEOUT = """\
prune:
  test_timeout_seconds: -5
"""


_THREE_BAD_BLOCKS = """\
safety:
  mode: not-a-real-mode

prune:
  test_timeout_seconds: -1

grade:
  total_budget_seconds: 0
"""


def test_lint_returns_zero_on_valid_config_blocks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All 5 blocks present and valid → exit 0; stderr empty (git-style)."""
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _VALID_ALL_BLOCKS)

    code = main(["lint", "--project-dir", str(project_dir)])
    out, err = _capture(capsys)

    assert code == 0, f"expected exit 0; got {code}; stderr={err!r}"
    # Stdout silent on success (git-style); stderr clean (no traceback).
    assert "Traceback" not in err
    assert "ERROR" not in err


def test_lint_returns_zero_on_no_signalforge_yml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``signalforge.yml`` at all → exit 0 (each loader returns defaults silently)."""
    project_dir = make_fake_dbt_project(tmp_path)
    # Sanity: no config file exists in the fixture project.
    assert not (project_dir / "signalforge.yml").exists()

    code = main(["lint", "--project-dir", str(project_dir)])
    out, err = _capture(capsys)

    assert code == 0, f"expected exit 0; got {code}; stderr={err!r}"
    assert "Traceback" not in err
    assert "ERROR" not in err


def test_lint_invalid_safety_block_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A single bad ``safety.mode`` → exit 1; stderr names the block / mode."""
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _INVALID_SAFETY_MODE)

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1; got {code}; stderr={err!r}"
    assert err.startswith("ERROR:"), f"stderr did not start with ERROR:; got {err!r}"
    # Single-error shape is the canonical ``ERROR: <message>`` line; the
    # safety loader's typed error mentions the bad mode value.
    assert "not-a-real-mode" in err or "mode" in err.lower()
    assert "Traceback" not in err


def test_lint_invalid_prune_block_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A single bad ``prune.test_timeout_seconds`` → exit 1; stderr explains."""
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _INVALID_PRUNE_TIMEOUT)

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1; got {code}; stderr={err!r}"
    assert err.startswith("ERROR:")
    assert "Traceback" not in err
    # The PruneConfig validator raises with "must be positive"; the
    # loader wraps as PruneConfigError. Either substring is acceptable
    # signal that the right block failed.
    assert "prune" in err.lower() or "positive" in err.lower()


def test_lint_multiple_invalid_blocks_lists_all(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three bad blocks → exit 1; stderr matches the DEC-008 header+bullets shape."""
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _THREE_BAD_BLOCKS)

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1; got {code}; stderr={err!r}"
    # Header + 3 bullets shape per DEC-008 (multi-error). The regex pins
    # "ERROR: signalforge.yml has 3 validation errors:" + at least three
    # ``  - <block>: <msg>`` bullets, each with a non-empty message body.
    pattern = (
        r"^ERROR: signalforge\.yml has 3 validation errors:\n"
        r"  - \S+: .+\n  - \S+: .+\n  - \S+: .+"
    )
    assert re.search(pattern, err), (
        f"stderr did not match DEC-008 header+bullets shape; got:\n{err!r}"
    )
    # All three failing blocks named in the bullets.
    assert "safety" in err
    assert "prune" in err
    assert "grade" in err
    assert "Traceback" not in err


def test_lint_config_path_nonexistent_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--config <project_dir>/nonexistent.yml`` → exit 1 with not-found error.

    The path is inside ``project_dir`` so :func:`canonicalise_user_path`
    accepts it; the loader (the first one to run, ``load_safety_config``)
    raises :class:`signalforge.safety.errors.ConfigNotFoundError` which
    lints catches and routes to the single-error stderr shape.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    bogus = project_dir / "nonexistent.yml"
    assert not bogus.exists()

    code = main(["lint", "--project-dir", str(project_dir), "--config", str(bogus)])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1; got {code}; stderr={err!r}"
    assert err.startswith("ERROR:")
    assert "Traceback" not in err
    # The error names either the missing path or the not-found contract.
    assert "nonexistent.yml" in err or "not found" in err.lower()


def test_lint_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``signalforge lint --help`` → exit 0; stdout non-empty."""
    code = main(["lint", "--help"])
    out, err = _capture(capsys)

    assert code == 0
    # argparse routes ``--help`` to stdout.
    assert "lint" in out
    assert "Traceback" not in err


def test_lint_completes_subsecond(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Performance smoke: happy-path ``lint`` runs in well under a second.

    The five loaders are pure YAML parse + Pydantic validation against a
    small file, so the wall-clock budget is generous. The 1.0 s threshold
    catches a regression where a future refactor accidentally wires a
    network / warehouse call into the lint path.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _VALID_ALL_BLOCKS)

    t0 = time.monotonic()
    code = main(["lint", "--project-dir", str(project_dir)])
    elapsed = time.monotonic() - t0
    _out, _err = _capture(capsys)

    assert code == 0
    assert elapsed < 1.0, f"lint took {elapsed:.3f}s; expected sub-second"
