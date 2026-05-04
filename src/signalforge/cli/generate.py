"""``signalforge generate <model>`` subcommand (US-005).

Wires the v0.1 pipeline end-to-end: manifest → safety → draft → prune →
grade → diff. This is the CLI's reason to exist; everything else
(version, lint) is scaffolding around this entry point.

US-005 ships the core orchestration only. The runtime knob flags
(``--mode``, ``--min-score``, ``--write``, ``--dry-run``, ``--format``)
land in US-006; the observability flags (``--quiet``, ``--verbose``,
``--no-color``, progress) land in US-007. The exit-code AST scan that
verifies every typed exception in the layer maps to exactly one tier
lands in US-008.

Project-root resolution (DEC-001 + DEC-027):

* No flag → walk up from :func:`Path.cwd` to the nearest dir containing
  ``dbt_project.yml``. If we hit the filesystem root without finding one,
  exit 1 with remediation.
* ``--project-dir <PATH>`` → treat as an *absolute assertion*: the path
  must exist AND must contain ``dbt_project.yml``. The CLI does NOT walk
  up from the override (DEC-027) — passing the flag means "use this
  project, not whatever's above me".

Test-injection seam (DEC-013): two private factory functions
:func:`_make_anthropic_client` and :func:`_make_warehouse_adapter` are
patched by tests in ``tests/cli/test_generate.py`` to return
:class:`tests.llm._fake.FakeAnthropicClient` /
:class:`tests.warehouse._fake.FakeBigQueryClient`-backed adapters. Both
are ``_``-prefixed (DEC of safety-layer.md / llm-drafter.md / etc.) —
not part of the public CLI contract.

Stage-order test (DEC-025): ``test_generate_calls_stages_in_documented_order``
patches every stage entry point and asserts the documented
``safety → draft → prune → grade → diff`` ordering against
``parent.mock_calls``. Pins CLAUDE.md "Pipeline shape" as a contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from signalforge import diff as diff_module
from signalforge import draft as draft_module
from signalforge import grade as grade_module
from signalforge import manifest as manifest_module
from signalforge import prune as prune_module
from signalforge import safety as safety_module
from signalforge import warehouse as warehouse_module
from signalforge.cli._helpers import (
    canonicalise_user_path,
    format_error_to_stderr,
    map_exception_to_exit_code,
)
from signalforge.cli.errors import CliPathError
from signalforge.llm._client import _AnthropicClientProtocol
from signalforge.warehouse.base import WarehouseAdapter

__all__ = ["add_parser", "cmd_generate"]


_LOGGER = logging.getLogger("signalforge.cli")


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``generate`` subcommand on the top-level parser.

    US-005 ships only the positional + project-discovery flags. Knob
    flags (US-006) and observability flags (US-007) extend this parser
    in subsequent stories.
    """
    parser = subparsers.add_parser(
        "generate",
        help="Draft, prune, grade, and diff a dbt model's schema.yml.",
        description=(
            "Runs the full v0.1 pipeline against <model>: load manifest, "
            "build the safety policy, draft candidate artifacts via LLM, "
            "prune always-pass / known-clean-fail tests against warehouse "
            "samples, grade the survivors, and render a diff against any "
            "existing schema.yml."
        ),
    )
    parser.add_argument(
        "model",
        metavar="<model>",
        help=(
            "Model under draft. Accepts a dbt unique_id "
            "(e.g. 'model.proj.customers') or a file path "
            "(e.g. 'models/marts/customers.sql')."
        ),
    )
    parser.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        help=(
            "Absolute assertion: <PATH> must contain dbt_project.yml. "
            "When supplied, the CLI does NOT walk up from this path. "
            "Default: walk up from the current working directory."
        ),
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help=(
            "Override the default <project_dir>/target/manifest.json. "
            "Path is canonicalised against the resolved project_dir."
        ),
    )
    parser.add_argument(
        "--profiles-dir",
        metavar="PATH",
        default=None,
        help=(
            "Override the default profiles.yml search location. Mirrors "
            "dbt-core's --profiles-dir flag. Sets DBT_PROFILES_DIR for "
            "the duration of the run."
        ),
    )
    parser.set_defaults(func=cmd_generate)


# ---------------------------------------------------------------------------
# Test-injection seams (DEC-013)
# ---------------------------------------------------------------------------


def _make_anthropic_client() -> _AnthropicClientProtocol | None:
    """Return the Anthropic client to inject into the draft / grade stages.

    Default implementation returns ``None`` so the underlying stage's
    own ``client = anthropic.Anthropic(...)`` lazy construction (gated
    by the single SDK seam at :mod:`signalforge.llm._client`) runs.
    Tests patch this to return a :class:`tests.llm._fake.FakeAnthropicClient`.
    """
    return None


def _make_warehouse_adapter(profile: warehouse_module.DbtProfileTarget) -> WarehouseAdapter:
    """Construct the :class:`WarehouseAdapter` for the resolved profile.

    Tests patch this to return a fake-backed adapter (e.g. a
    :class:`signalforge.warehouse.adapters.bigquery.BigQueryAdapter`
    constructed with a :class:`tests.warehouse._fake.FakeBigQueryClient`).
    """
    return WarehouseAdapter.from_profile(profile)


# ---------------------------------------------------------------------------
# Project-root resolution (DEC-001 + DEC-027)
# ---------------------------------------------------------------------------


def _resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve the dbt project root from the arguments.

    DEC-001 walk-up semantics: from cwd, ascend until ``dbt_project.yml``
    is found. DEC-027 absolute-assertion semantics: ``--project-dir`` is
    NOT a walk-up starting point; the supplied path must directly contain
    ``dbt_project.yml`` or the CLI exits 1.
    """
    override = getattr(args, "project_dir", None)
    if override is not None:
        candidate = Path(override).resolve()
        if not candidate.is_dir() or not (candidate / "dbt_project.yml").is_file():
            raise CliPathError(
                f"--project-dir {override!r} does not contain dbt_project.yml",
                remediation=(
                    "Pass a path that points directly at a dbt project root "
                    "(the directory containing dbt_project.yml). The flag is "
                    "an absolute assertion; the CLI does not walk up from it."
                ),
            )
        _LOGGER.debug(
            "resolved project_dir: %s",
            json.dumps({"project_dir": str(candidate), "source": "flag"}),
        )
        return candidate

    cwd = Path.cwd().resolve()
    current: Path | None = cwd
    # ``Path.parents`` does not include the path itself — walk explicitly.
    visited: list[Path] = []
    while current is not None:
        visited.append(current)
        if (current / "dbt_project.yml").is_file():
            _LOGGER.debug(
                "resolved project_dir: %s",
                json.dumps({"project_dir": str(current), "source": "walk-up"}),
            )
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent

    raise CliPathError(
        f"could not find dbt_project.yml walking up from {cwd}",
        remediation=(
            "Run `signalforge generate` from inside a dbt project, or pass "
            "--project-dir <PATH> pointing at the directory that contains "
            "dbt_project.yml."
        ),
    )


# ---------------------------------------------------------------------------
# Subcommand entry point
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    """Run the full pipeline for ``args.model``.

    Returns the integer exit code per the four-tier taxonomy in
    :data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE`. No
    traceback ever leaks (DEC-016): every typed exception is caught at
    this boundary, formatted via :func:`format_error_to_stderr`, and
    routed to the right tier via :func:`map_exception_to_exit_code`. An
    untyped :class:`Exception` lands at the panic-path tier (1) with the
    same shape — no traceback, just the typed-error one-liner.

    Pipeline order is the documented ``safety → draft → prune → grade →
    diff`` (DEC-005). The stage-order test in
    ``tests/cli/test_generate.py`` pins this contract.
    """
    try:
        project_dir = _resolve_project_dir(args)

        manifest_override = canonicalise_user_path(args.manifest, project_dir)
        # ``--profiles-dir`` becomes an env var for the duration of the
        # call; the warehouse loader's three-path resolution honours
        # ``DBT_PROFILES_DIR`` first.
        if args.profiles_dir is not None:
            profiles_dir_resolved = canonicalise_user_path(args.profiles_dir, project_dir)
            if profiles_dir_resolved is not None:
                import os

                os.environ["DBT_PROFILES_DIR"] = str(profiles_dir_resolved)

        # 1. Manifest load + model selection.
        manifest = manifest_module.load(project_dir, manifest_path=manifest_override)
        model = manifest.get_model(args.model)

        # 2. Warehouse profile + adapter.
        profile = warehouse_module.load_profile(project_dir)
        adapter = _make_warehouse_adapter(profile)

        # 3. Safety policy (the first stage in the documented pipeline
        #    order — DEC-025 / CLAUDE.md "Pipeline shape").
        policy = safety_module.load_safety_config(project_dir)

        # 4. Draft.
        draft_config = draft_module.load_draft_config(project_dir)
        client = _make_anthropic_client()
        draft_outcome = draft_module.draft_schema(
            model,
            adapter,
            policy,
            manifest,
            config=draft_config,
            _client=client,
        )

        # 5. Prune.
        prune_config = prune_module.load_prune_config(project_dir)
        prune_result = prune_module.prune_tests(
            model,
            adapter,
            draft_outcome.candidate,
            manifest,
            config=prune_config,
            project_dir=project_dir,
        )

        # 6. Grade.
        grade_config = grade_module.load_grade_config(project_dir)
        grade_report = grade_module.grade_artifacts(
            model,
            draft_outcome.candidate,
            prune_result,
            config=grade_config,
            client=client,
            project_dir=project_dir,
        )

        # 7. Diff.
        diff_config = diff_module.load_diff_config(project_dir)
        diff_report = diff_module.render_diff(
            model,
            draft_outcome.candidate,
            prune_result,
            grading_report=grade_report,
            config=diff_config,
            project_dir=project_dir,
        )

        # 8. Render to stdout via the in-process helper (DEC-015). The
        #    JSON sidecar is already on disk via
        #    ``render_diff(write_sidecar=True)`` (default).
        rendered = diff_module.render_to_text(
            diff_report, config=diff_config, project_dir=project_dir
        )
        print(rendered)
        return 0

    except Exception as exc:  # noqa: BLE001 — the boundary catch (DEC-016)
        message = format_error_to_stderr(exc)
        print(message, file=sys.stderr)
        return map_exception_to_exit_code(exc)
