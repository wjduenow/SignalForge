"""Tests for ``signalforge lint`` (US-004 — config-only validator).

Covers the eight cases from the US-004 acceptance criteria:

* happy path with all 5 blocks valid → exit 0
* happy path with no ``signalforge.yml`` at all → exit 0
* invalid ``safety:`` block → exit 2 (input tier — bad mode value)
* invalid ``prune:`` block → exit 1 (load tier — config validation)
* multiple invalid blocks → exit max(per-failure tier), stderr in
  DEC-008 header+bullets shape
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

import json
import re
import time
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._factories import make_fake_dbt_project


def _manifest_with_models(unique_ids_and_names: list[tuple[str, str]]) -> dict[str, object]:
    """Build a minimal dbt manifest dict containing the supplied models.

    Each tuple is ``(unique_id, name)``; the helper expands to a node
    with the structural fields :class:`signalforge.manifest.models.Model`
    requires (resource_type, package_name, raw_code, schema, columns).
    Used by the model-resolution lint tests (issue #49). ``raw_code``
    is a no-op SELECT so :class:`ModelMissingSqlError` never fires.
    """
    nodes: dict[str, object] = {}
    for unique_id, name in unique_ids_and_names:
        # unique_id is canonically ``model.<pkg>.<name>``.
        parts = unique_id.split(".")
        pkg = parts[1] if len(parts) >= 3 else "pkg"
        nodes[unique_id] = {
            "unique_id": unique_id,
            "name": name,
            "resource_type": "model",
            "package_name": pkg,
            "original_file_path": f"models/{name}.sql",
            "path": f"{name}.sql",
            "database": "fake_db",
            "schema": "fake_schema",
            "raw_code": f"select 1 as id -- {name}",
            "columns": {},
        }
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
        },
        "nodes": nodes,
        "disabled": {},
    }


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
    """A single bad ``safety.mode`` → exit 2; stderr names the block / mode.

    ``InvalidSamplingModeError`` maps to tier 2 (input invariant) — the
    operator gave a bogus value, not a load-time problem with the file —
    and ``cmd_lint`` routes loader failures through
    ``map_exception_to_exit_code`` so the tier matches what the same
    exception would produce from ``cmd_generate`` (DEC-008 four-tier
    contract; PR #26 review feedback).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _INVALID_SAFETY_MODE)

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    assert code == 2, f"expected exit 2 (input tier); got {code}; stderr={err!r}"
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
    """Three bad blocks → exit max(per-failure tier); stderr matches the
    DEC-008 header+bullets shape.

    The three failures are: invalid ``safety.mode`` (tier 2 — input),
    negative ``prune.test_timeout_seconds`` (tier 1 — load), and zero
    ``grade.total_budget_seconds`` (tier 1 — load). ``cmd_lint`` returns
    ``max(...)`` of the per-failure tiers so the most severe wins —
    here the bad mode lifts the run to tier 2 (PR #26 review feedback).
    """
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _THREE_BAD_BLOCKS)

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    assert code == 2, f"expected exit 2 (max per-failure tier); got {code}; stderr={err!r}"
    # Header + 3 bullets shape per DEC-008 (multi-error). The regex pins
    # "ERROR: lint found 3 validation errors:" + at least three
    # ``  - <block>: <msg>`` bullets, each with a non-empty message body.
    # The header text generalised from "signalforge.yml has..." to
    # "lint found..." in issue #49 so the same shape covers manifest /
    # model entries alongside the five config blocks.
    pattern = (
        r"^ERROR: lint found 3 validation errors:\n"
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


def test_lint_project_dir_without_dbt_project_yml_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--project-dir <bogus>`` exits 1 via cmd_lint's load-time error catch.

    Issue #60 pins the patch line in ``cmd_lint``'s outer ``try / except``
    that routes :class:`CliPathError` through :func:`print_stderr`.
    The block-level config tests (above) exercise the per-block failure
    loop, not the outer catch — ``_resolve_project_dir`` raises BEFORE
    any block-level work, so a bogus ``--project-dir`` is the cleanest
    trigger for the outer path. Without this test the line stays
    uncovered (Codecov reported it as a patch-coverage gap on PR #88).
    """
    empty_dir = tmp_path / "no_dbt_here"
    empty_dir.mkdir()

    code = main(["lint", "--project-dir", str(empty_dir)])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1 (CliPathError); got {code}; stderr={err!r}"
    assert err.startswith("ERROR:"), err
    assert "dbt_project.yml" in err
    assert "Traceback" not in err


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
    small file; the manifest load is a single JSON parse + Pydantic
    validation against the minimal fixture manifest. The 1.0 s threshold
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


# ---------------------------------------------------------------------------
# Issue #49 — manifest load + --model resolution
# ---------------------------------------------------------------------------


def test_lint_reports_missing_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``target/manifest.json`` → exit 1; stderr names the manifest block.

    Builds a fake project with ``with_manifest=False`` so the loader
    surfaces :class:`ManifestNotFoundError` (tier 1). The config blocks
    parse cleanly so the manifest is the sole failure and the
    single-error stderr shape fires.
    """
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    # No signalforge.yml either — every config loader returns defaults.

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1 (load tier); got {code}; stderr={err!r}"
    assert err.startswith("ERROR:")
    # Single-error shape uses the typed error's own message directly.
    assert "manifest" in err.lower() or "target" in err.lower()
    assert "Traceback" not in err


def test_lint_reports_unsupported_manifest_version(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Manifest v20 (Fusion / unsupported) → exit 1 with a clear message.

    Writes a manifest with ``dbt_schema_version: v20.json`` — outside
    the v9–v12 supported range. The loader raises
    :class:`UnsupportedManifestVersionError` (tier 1).
    """
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    manifest_path = project_dir / "target" / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v20.json",
                },
                "nodes": {},
                "disabled": {},
            }
        ),
        encoding="utf-8",
    )

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1; got {code}; stderr={err!r}"
    assert err.startswith("ERROR:")
    assert "v9" in err or "schema" in err.lower() or "version" in err.lower()
    assert "Traceback" not in err


def test_lint_config_and_manifest_failures_both_listed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bad ``safety:`` block AND missing manifest → both in the bullet list.

    The header generalised from "signalforge.yml has N validation errors"
    to "lint found N validation errors" in issue #49 so the same shape
    covers manifest entries alongside the five config blocks.
    """
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    _write_signalforge_yml(project_dir, _INVALID_SAFETY_MODE)

    code = main(["lint", "--project-dir", str(project_dir)])
    _out, err = _capture(capsys)

    # safety is tier 2 (input), manifest is tier 1 (load); max = 2.
    assert code == 2, f"expected exit 2 (max of safety=2, manifest=1); got {code}; stderr={err!r}"
    assert err.startswith("ERROR: lint found 2 validation errors:")
    assert "safety" in err
    assert "manifest" in err
    assert "Traceback" not in err


def test_lint_resolves_model_by_unique_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--model model.shop.customers`` resolves cleanly → exit 0; silent stdout."""
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    (project_dir / "target" / "manifest.json").write_text(
        json.dumps(_manifest_with_models([("model.shop.customers", "customers")])),
        encoding="utf-8",
    )

    code = main(
        [
            "lint",
            "--project-dir",
            str(project_dir),
            "--model",
            "model.shop.customers",
        ]
    )
    out, err = _capture(capsys)

    assert code == 0, f"expected exit 0; got {code}; stderr={err!r}"
    assert out == ""
    assert "Traceback" not in err


def test_lint_resolves_model_by_bare_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--model customers`` (bare name) resolves via iter_models → exit 0.

    Pinned by the testing-signal.md "Multi-surface drift" gotcha: bare
    names route through ``Manifest.get_model``'s file-path branch and
    raise ``ModelNotFoundError`` even when a model with that name
    exists. The bare-name handler scans :meth:`Manifest.iter_models` and
    sidesteps the gotcha for the operator.
    """
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    (project_dir / "target" / "manifest.json").write_text(
        json.dumps(_manifest_with_models([("model.shop.customers", "customers")])),
        encoding="utf-8",
    )

    code = main(["lint", "--project-dir", str(project_dir), "--model", "customers"])
    out, err = _capture(capsys)

    assert code == 0, f"expected exit 0 for bare-name lookup; got {code}; stderr={err!r}"
    assert out == ""
    assert "Traceback" not in err


def test_lint_resolves_model_by_file_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--model models/customers.sql`` resolves via the file-path branch."""
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    (project_dir / "target" / "manifest.json").write_text(
        json.dumps(_manifest_with_models([("model.shop.customers", "customers")])),
        encoding="utf-8",
    )

    code = main(
        [
            "lint",
            "--project-dir",
            str(project_dir),
            "--model",
            "models/customers.sql",
        ]
    )
    out, err = _capture(capsys)

    assert code == 0, f"expected exit 0; got {code}; stderr={err!r}"
    assert out == ""


def test_lint_model_not_found_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--model nonexistent`` → exit 2; stderr names the missing model.

    ``ModelNotFoundError`` is tier 2 (input — operator-supplied data is
    wrong).
    """
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    (project_dir / "target" / "manifest.json").write_text(
        json.dumps(_manifest_with_models([("model.shop.customers", "customers")])),
        encoding="utf-8",
    )

    code = main(["lint", "--project-dir", str(project_dir), "--model", "ghosts"])
    _out, err = _capture(capsys)

    assert code == 2, f"expected exit 2 (input tier); got {code}; stderr={err!r}"
    assert err.startswith("ERROR:")
    assert "ghosts" in err
    assert "Traceback" not in err


def test_lint_bare_model_name_ambiguous_disambiguates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two enabled models share a name → exit 2; stderr lists the unique_ids.

    Bare-name lookup fails loud when multiple packages register a model
    with the same name; the remediation tells the operator to pass the
    unique_id or file path.
    """
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    (project_dir / "target" / "manifest.json").write_text(
        json.dumps(
            _manifest_with_models(
                [
                    ("model.shop.customers", "customers"),
                    ("model.finance.customers", "customers"),
                ]
            )
        ),
        encoding="utf-8",
    )

    code = main(["lint", "--project-dir", str(project_dir), "--model", "customers"])
    _out, err = _capture(capsys)

    assert code == 2, f"expected exit 2; got {code}; stderr={err!r}"
    assert err.startswith("ERROR:")
    assert "model.shop.customers" in err
    assert "model.finance.customers" in err
    assert "Traceback" not in err


def test_lint_model_flag_skipped_when_manifest_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Manifest fails to load → model resolution is silently skipped.

    Only the manifest failure surfaces; no spurious second bullet for a
    model lookup we couldn't perform (the manifest is the dependency).
    """
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    # No manifest.json on disk; --model would be undefined.

    code = main(["lint", "--project-dir", str(project_dir), "--model", "customers"])
    _out, err = _capture(capsys)

    assert code == 1, f"expected exit 1 (manifest tier); got {code}; stderr={err!r}"
    # Single-error shape — only the manifest entry surfaces.
    assert err.startswith("ERROR:")
    assert "model" not in err.lower().split("\n")[0] or "manifest" in err.lower()
    # Header line refers to the manifest, not a "2 validation errors"
    # multi-error header.
    assert "validation errors:" not in err.split("\n")[0]


def test_lint_manifest_path_override(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--manifest <path>`` reads the override location rather than the default."""
    project_dir = make_fake_dbt_project(tmp_path, with_manifest=False)
    custom = project_dir / "alt_manifest.json"
    custom.write_text(
        json.dumps(_manifest_with_models([("model.shop.customers", "customers")])),
        encoding="utf-8",
    )

    code = main(
        [
            "lint",
            "--project-dir",
            str(project_dir),
            "--manifest",
            str(custom),
            "--model",
            "customers",
        ]
    )
    _out, err = _capture(capsys)

    assert code == 0, f"expected exit 0; got {code}; stderr={err!r}"
    assert "Traceback" not in err


def test_lint_makes_no_llm_or_warehouse_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #49 AC: lint never calls the Anthropic API or any warehouse client.

    We patch the constructors / SDK seams so any accidental wiring would
    raise loudly. The lint run must complete (exit 0) because none of
    those paths are touched.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    _write_signalforge_yml(project_dir, _VALID_ALL_BLOCKS)

    # Any attempt to construct an Anthropic client would call this
    # factory; raising here proves no LLM seam was reached. The grep
    # for ``_make_anthropic_client`` mirrors the SDK confinement in
    # ``signalforge.llm._client``.
    def _explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("lint must not invoke the LLM seam")

    import signalforge.llm._client as _llm_client

    monkeypatch.setattr(_llm_client, "_make_anthropic_client", _explode)

    # The warehouse adapter's BigQuery shim would be touched only via
    # ``from_profile`` or direct instantiation; both should stay
    # untouched. Patch the factory to fail loud.
    import signalforge.warehouse.base as _warehouse_base

    monkeypatch.setattr(
        _warehouse_base.WarehouseAdapter,
        "from_profile",
        classmethod(lambda cls, profile: _explode()),
    )

    code = main(["lint", "--project-dir", str(project_dir), "--model", "anything"])
    _out, err = _capture(capsys)

    # Model lookup against an empty manifest fails (tier 2) — that's
    # fine; the assertion is that the lint flow never reached the LLM
    # or warehouse seams (their _explode patches would have raised
    # AssertionError instead, which propagates as a tier-1 panic-path
    # tier and stderr would carry a different message).
    assert code in {0, 2}, f"unexpected exit {code}; stderr={err!r}"
    assert "must not invoke" not in err, "lint reached an LLM/warehouse seam"
    assert "Traceback" not in err
