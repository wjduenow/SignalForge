"""``signalforge lint`` subcommand — pre-flight pipeline validator.

Validates the five existing ``signalforge.yml`` config blocks
(``safety:``, ``llm:``, ``prune:``, ``grade:``, ``diff:``) against their
per-stage loaders, then loads the dbt manifest (issue #49 — surfaces
manifest-schema-version mismatches and missing ``target/manifest.json``
as a pre-LLM-call failure), and optionally resolves a model in the
loaded manifest when ``--model <name>`` is supplied. No warehouse, no
LLM, no network — sub-second target. Originally shipped as US-004 of
issue #9 (config-only validator); broadened in issue #49 to cover the
manifest seam.

Multi-error reporting per DEC-008:

* Zero loaders raise → exit 0 with silent stdout (git-style).
* Exactly one loader raises → exit
  ``map_exception_to_exit_code(exc)`` (typically tier 1 for
  ``*ConfigNotFoundError`` / ``*ConfigInvalidError`` /
  ``ManifestNotFoundError`` / ``UnsupportedManifestVersionError``;
  tier 2 for ``ModelNotFoundError`` / ``CliInputError``) with the
  canonical ``ERROR: <message>`` single-line stderr shape.
* Two or more loaders raise → exit
  ``max(map_exception_to_exit_code(exc) for ... in failures)`` (so the
  most severe per-failure tier wins) with a header + bullet list:

  .. code-block:: text

      ERROR: lint found N validation errors:
        - safety: <error msg>
        - prune:  <error msg>
        - manifest: <error msg>

  This mirrors the DEC-008 tier-2 multi-violation shape used by the
  drafter's whole-draft anchor-contract error so CI parsers see one
  visual contract for any "list of problems" output the CLI emits.

The manifest is loaded AFTER every config loader so the operator still
sees ``signalforge.yml`` typos when ``target/manifest.json`` is also
missing — both surface in the same run.

When ``--model <name>`` is supplied, lint also resolves the model in
the loaded manifest. Three forms are accepted:

* ``unique_id`` form: ``model.<pkg>.<name>`` — routed to
  :meth:`Manifest.get_model` directly.
* file-path form: ``models/path/to/<name>.sql`` — routed to
  :meth:`Manifest.get_model` directly.
* bare-name form: ``<name>`` — looked up via :meth:`Manifest.iter_models`
  matching on :attr:`Model.name`. The bare-name branch sidesteps the
  ``Manifest.get_model`` gotcha (see ``testing-signal.md`` §
  "Multi-surface drift") where bare names route to the file-path branch
  and surface a confusing ``ModelNotFoundError`` even when the model
  exists under a unique_id. A bare name matching two or more enabled
  models raises :class:`ModelNotFoundError` with a disambiguation hint.

Project-root resolution mirrors :mod:`signalforge.cli.generate`:

* ``--project-dir <PATH>`` is an absolute assertion (DEC-027) — the
  supplied path must directly contain ``dbt_project.yml``.
* No flag → walk up from cwd until ``dbt_project.yml`` is found
  (DEC-001).

The sub-second target is a smoke assertion in
``tests/cli/test_lint.py``. The five config loaders are pure YAML parse
+ Pydantic validation; the manifest load is a single JSON parse +
Pydantic validation. Nothing in this path opens a network connection or
touches the warehouse.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from signalforge.cli._helpers import (
    canonicalise_user_path,
    format_error_to_stderr,
    map_exception_to_exit_code,
)
from signalforge.cli.errors import CliPathError
from signalforge.diff import load_diff_config
from signalforge.draft import load_draft_config
from signalforge.grade import load_grade_config
from signalforge.manifest import (
    Manifest,
    ManifestError,
    Model,
    ModelNotFoundError,
)
from signalforge.manifest import load as load_manifest
from signalforge.prune import load_prune_config
from signalforge.safety import load_safety_config

__all__ = ["add_parser", "cmd_lint"]


_LOGGER = logging.getLogger("signalforge.cli")


# Five (block-label, loader) pairs in the documented signalforge.yml
# top-level order. The label drives the multi-error bullet output so the
# operator sees which block failed without having to grep the message.
# Keeping the loaders in a tuple (immutable, ordered) makes the iteration
# deterministic — every run of `signalforge lint` against the same input
# emits errors in the same order.
_BLOCK_LOADERS: tuple[tuple[str, object], ...] = (
    ("safety", load_safety_config),
    ("llm", load_draft_config),
    ("prune", load_prune_config),
    ("grade", load_grade_config),
    ("diff", load_diff_config),
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``lint`` subcommand on the top-level parser."""
    parser = subparsers.add_parser(
        "lint",
        help="Validate the signalforge.yml config blocks and the dbt manifest.",
        description=(
            "Validate the five signalforge.yml config blocks (safety, llm, "
            "prune, grade, diff) AND load the dbt manifest. Exits 0 when "
            "every block parses cleanly (or is absent — each loader returns "
            "defaults silently per its own DEC) AND the manifest loads "
            "without error. On failure, lists every failing block in one "
            "run rather than short-circuiting on the first failure. No "
            "warehouse, no LLM, no network."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "Override the default <project_dir>/signalforge.yml. Path is "
            "canonicalised against the resolved project_dir."
        ),
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help=(
            "Override the default <project_dir>/target/manifest.json. Path "
            "is canonicalised against the resolved project_dir and must "
            "stay inside it (no symlink escapes)."
        ),
    )
    parser.add_argument(
        "--model",
        metavar="NAME",
        default=None,
        help=(
            "Optional: also resolve a model in the loaded manifest and "
            "report whether it exists. Accepts three forms: a unique_id "
            "(model.<pkg>.<name>), a file path "
            "(models/path/to/<name>.sql), or a bare model name (<name>). "
            "Bare names match against Model.name across enabled nodes and "
            "fail loud if two or more models share the name."
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
    parser.set_defaults(func=cmd_lint)


def _resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve the dbt project root from the arguments.

    Mirrors :func:`signalforge.cli.generate._resolve_project_dir` (DEC-001
    walk-up; DEC-027 absolute-assertion under ``--project-dir``). The
    duplication is deliberate per the project's path-safety convention
    (each layer's containment gate stays homogeneous; promotion to a
    shared helper is a future v0.2 refinement).
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
    while current is not None:
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
            "Run `signalforge lint` from inside a dbt project, or pass "
            "--project-dir <PATH> pointing at the directory that contains "
            "dbt_project.yml."
        ),
    )


def _resolve_model_for_lint(manifest: Manifest, key: str) -> Model:
    """Resolve ``key`` to a :class:`Model` across all three input shapes.

    * ``key.startswith("model.")`` → unique_id branch via
      :meth:`Manifest.get_model`.
    * ``"/" in key`` or ``key.endswith(".sql")`` → file-path branch via
      :meth:`Manifest.get_model`.
    * Else → bare-name branch: scan :meth:`Manifest.iter_models` for
      ``Model.name == key``. One match returns the model; zero matches
      raises :class:`ModelNotFoundError` with a hint suggesting the
      unique_id / file-path form; multiple matches raises
      :class:`ModelNotFoundError` with a disambiguation list (capped at
      five unique_ids to keep stderr readable).

    The bare-name branch sidesteps the ``Manifest.get_model`` gotcha
    pinned by ``testing-signal.md`` § "Multi-surface drift on user-facing
    model arguments" where bare names route through the file-path branch
    and surface a confusing ``ModelNotFoundError`` even when the model
    exists under its unique_id.
    """
    if key.startswith("model.") or "/" in key or key.endswith(".sql"):
        return manifest.get_model(key)

    matches = [m for m in manifest.iter_models() if m.name == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sample = ", ".join(m.unique_id for m in matches[:5])
        more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
        raise ModelNotFoundError(
            f"Bare model name {key!r} matches {len(matches)} enabled models: {sample}{more}",
            remediation=(
                "Disambiguate by passing the full unique_id "
                "(model.<pkg>.<name>) or the file path "
                "(models/path/to/<name>.sql)."
            ),
        )
    raise ModelNotFoundError(
        f"No enabled model with name {key!r} in the manifest.",
        remediation=(
            "Check the model name spelling, or pass the unique_id form "
            "(model.<pkg>.<name>) / file path (models/path/to/<name>.sql). "
            "Disabled models do not match bare-name lookup."
        ),
    )


def cmd_lint(args: argparse.Namespace) -> int:
    """Validate every config block + the manifest and report all failures in one run.

    Returns the integer exit code per the four-tier CLI taxonomy
    (DEC-008). Every loader failure routes through
    :func:`map_exception_to_exit_code` so the tier matches what the same
    exception would produce from ``cmd_generate``:

    * ``0`` — every loader returned cleanly (or returned defaults silently
      because the block / file was absent) AND the manifest loaded
      cleanly AND, if ``--model <name>`` was supplied, the model
      resolved.
    * single failure → :func:`map_exception_to_exit_code` of the
      exception (typically tier 1 for ``*ConfigNotFoundError`` /
      ``*ConfigInvalidError`` / ``ManifestNotFoundError`` /
      ``UnsupportedManifestVersionError``; tier 2 for
      ``ModelNotFoundError`` / ``ModelDisabledError`` / ``CliInputError``).
    * multiple failures → ``max(...)`` of the per-failure tiers, rendered
      with the multi-error header + bullets shape (DEC-008).
    * project-root / path-canonicalisation failures from the CLI layer
      itself route through the same mapper (typically tier 1 for
      ``CliPathError``, tier 2 for ``CliInputError``).

    The manifest load is intentionally appended AFTER every config loader
    so the operator sees ``signalforge.yml`` typos AND a missing /
    schema-mismatched ``target/manifest.json`` in one run rather than
    fixing them one cascade at a time.
    """
    try:
        project_dir = _resolve_project_dir(args)
        config_path = canonicalise_user_path(args.config, project_dir)
        manifest_path = canonicalise_user_path(getattr(args, "manifest", None), project_dir)
    except Exception as exc:  # noqa: BLE001 — uniform CLI boundary catch (DEC-016)
        message = format_error_to_stderr(exc)
        print(message, file=sys.stderr)
        return map_exception_to_exit_code(exc)

    # Collect every loader failure rather than short-circuiting on the
    # first — the operator sees every problem in one run (DEC-008
    # multi-error reporting).
    failures: list[tuple[str, Exception]] = []
    for block_name, loader in _BLOCK_LOADERS:
        try:
            # The loader signature is uniform across stages:
            # ``(project_dir, path=None) -> <Config>``.
            loader(project_dir, config_path)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 — collect every typed error
            failures.append((block_name, exc))

    # Issue #49 — manifest load + optional model resolution. We attempt
    # both regardless of whether config loaders failed (multi-error
    # reporting prefers showing every problem in one run); a manifest
    # failure adds one more entry to ``failures``. The model-resolution
    # step only runs when the manifest loaded successfully, since a
    # missing/invalid manifest makes model lookup undefined.
    manifest_obj: Manifest | None = None
    try:
        manifest_obj = load_manifest(project_dir, manifest_path=manifest_path)
    except ManifestError as exc:
        failures.append(("manifest", exc))

    model_arg = getattr(args, "model", None)
    if model_arg is not None and manifest_obj is not None:
        try:
            _resolve_model_for_lint(manifest_obj, model_arg)
        except ManifestError as exc:
            failures.append(("model", exc))

    if not failures:
        return 0

    if len(failures) == 1:
        # Single-error shape: reuse the canonical CLI formatter so
        # remediation footers render uniformly with the rest of the CLI.
        # Route through ``map_exception_to_exit_code`` so the tier
        # matches what ``cmd_generate`` would produce for the same
        # exception (DEC-008 four-tier contract).
        _block_name, exc = failures[0]
        message = format_error_to_stderr(exc)
        print(message, file=sys.stderr)
        return map_exception_to_exit_code(exc)

    # Multi-error shape (DEC-008 header + bullets). Each bullet names
    # the failing block AND a one-line summary of the error message.
    # Pydantic-derived messages can carry embedded newlines (e.g.
    # ``"1 validation error for _PruneConfigFile\nprune.test_timeout_seconds\n
    # Value error, must be positive ..."``); we collapse those into a
    # single line so the bullet shape stays one ``  - <block>: <msg>``
    # row per failure — CI parsers key on the leading two-space dash.
    # The header generalises to "lint found N validation errors" — covers
    # both ``signalforge.yml`` blocks and the manifest / model entries
    # added by issue #49.
    header = f"ERROR: lint found {len(failures)} validation errors:"
    bullets: list[str] = []
    for block_name, exc in failures:
        # Use the typed error's ``message`` attribute if present (every
        # layer-base error class in the repo carries one); fall back to
        # ``str(exc)`` for forward-compat exceptions that don't.
        msg = getattr(exc, "message", None)
        if msg is None:
            msg = str(exc) or exc.__class__.__name__
        # Collapse internal whitespace runs (newlines, indentation) so
        # the bullet renders on one visual line.
        msg = " ".join(msg.split())
        bullets.append(f"  - {block_name}: {msg}")
    print(header + "\n" + "\n".join(bullets), file=sys.stderr)
    # Multi-error exit code = max of the per-failure tiers. Loader
    # failures are typically tier 1 (load), but a CliInputError that
    # bubbles up from a deeper validator surfaces as tier 2 — picking
    # the most-severe keeps the four-tier contract honest (DEC-008).
    return max(map_exception_to_exit_code(exc) for _, exc in failures)
