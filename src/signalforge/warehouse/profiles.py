"""dbt ``profiles.yml`` reader for the warehouse adapter layer.

Implements DEC-009 (file resolution order, typed return shape) and DEC-017
(``extra="forbid"`` + ``method`` validator + symlink-hardened
``<project_dir>/profiles.yml`` resolution) and DEC-023 (soft 1 MB warning
on profile size).

Public surface
--------------

* :class:`DbtProfileTarget` — a Pydantic v2 frozen model that captures the
  subset of dbt-bigquery's profile fields SignalForge consumes today. Every
  other field documented for dbt-bigquery 1.9 is exercised by the
  drift-detector test (``tests/warehouse/test_profiles.py``); when a future
  dbt-bigquery release adds a new field, the strict model in that test
  fails first, and the maintainer decides whether the field needs to flow
  through to :class:`DbtProfileTarget` or stay quietly absent.
* :func:`load_profile` — read a ``profiles.yml`` from one of three search
  paths and return the active target as a :class:`DbtProfileTarget`.

Design notes
------------

* **`extra="forbid"` is a deliberate divergence (DEC-017).** Manifest
  parsers use ``extra="ignore"`` for forward-compat against new dbt fields;
  profile parsers do *not*, because silently ignoring an unknown auth-config
  field could mean SignalForge falls back to ADC when the user thought they
  had configured a service account. Loud failure is the correct UX. The
  drift-detector test in the test module compensates for the strictness.
* **`method` field validator.** Only ``"oauth"`` (and ``None``, meaning
  "let dbt-bigquery default to ADC") are accepted. Every other method
  (``service-account``, ``service-account-json``, ``oauth-secrets``,
  ``impersonate-service-account``, …) raises
  :class:`UnsupportedAuthMethodError` directly from the validator so the
  Pydantic ``ValidationError`` carries the typed error in its ``__cause__``.
* **Three-path resolution.** ``DBT_PROFILES_DIR`` env → ``<project_dir>``
  → ``~/.dbt``. Only ``<project_dir>/profiles.yml`` is symlink-hardened —
  the env-var and home-dir paths are user-trusted.
* **`yaml.safe_load` only.** ``yaml.load`` accepts arbitrary Python object
  construction tags and is unsafe for any input we don't fully control.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from signalforge.warehouse._path_safety import canonicalise_path
from signalforge.warehouse.errors import (
    ProfileEnvVarUnsetError,
    ProfileNotFoundError,
    ProfileTargetNotFoundError,
    UnsupportedAuthMethodError,
)

_LOGGER = logging.getLogger("signalforge.warehouse")

_PROFILES_YAML_WARN_AT = 1 * 1024 * 1024
"""Soft warning threshold (DEC-023). Module-level so tests can monkeypatch
it down to a few bytes without committing a 1 MB fixture."""

_SUPPORTED_METHODS: frozenset[str] = frozenset({"oauth"})
"""Auth methods SignalForge v0.1 accepts. ``None`` is also accepted at the
field level (means "let dbt-bigquery default to ADC")."""


class DbtProfileTarget(BaseModel):
    """The subset of a dbt ``profiles.yml`` target SignalForge consumes.

    Strict by design (DEC-017): ``extra="forbid"`` means an unknown key
    raises a Pydantic ``ValidationError``. Forward-compat against new
    dbt-bigquery fields is the drift-detector test's responsibility.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    type: str
    method: str | None = None
    project: str | None = None
    dataset: str | None = Field(default=None, alias="schema")
    location: str | None = None
    priority: str | None = None
    maximum_bytes_billed: int | None = None

    @field_validator("method")
    @classmethod
    def _validate_method(cls, v: str | None) -> str | None:
        # DEC-017: validator's job is to RAISE — never return None to mean
        # "rejected". Returning None is the legitimate "use ADC" signal.
        if v is None:
            return None
        if v not in _SUPPORTED_METHODS:
            raise UnsupportedAuthMethodError(method=v)
        return v


# ---------------------------------------------------------------------------
# load_profile()
# ---------------------------------------------------------------------------


def _read_dbt_project_profile_name(project_dir: Path) -> str:
    """Return the ``profile:`` field from ``<project_dir>/dbt_project.yml``.

    Raises :class:`ProfileNotFoundError` when ``dbt_project.yml`` is missing
    or has no ``profile:`` field — both are pre-conditions for resolving the
    right target out of any candidate ``profiles.yml``.
    """
    dbt_project_path = project_dir / "dbt_project.yml"
    if not dbt_project_path.exists() or not dbt_project_path.is_file():
        raise ProfileNotFoundError(
            searched_paths=[dbt_project_path],
            remediation=(
                f"Create a dbt_project.yml at {dbt_project_path} with a "
                "`profile:` field naming the profile to load."
            ),
        )
    try:
        with dbt_project_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ProfileNotFoundError(
            searched_paths=[dbt_project_path],
            remediation=(f"dbt_project.yml at {dbt_project_path} is not valid YAML: {exc}"),
        ) from exc
    if not isinstance(raw, dict):
        raise ProfileNotFoundError(
            searched_paths=[dbt_project_path],
            remediation=(
                f"dbt_project.yml at {dbt_project_path} must be a YAML mapping at the top level."
            ),
        )
    profile_name = raw.get("profile")
    if not isinstance(profile_name, str) or not profile_name:
        raise ProfileNotFoundError(
            searched_paths=[dbt_project_path],
            remediation=(
                f"Add a `profile:` field to {dbt_project_path} naming the "
                "profile to load from profiles.yml."
            ),
        )
    return profile_name


def _maybe_warn_large_profile(path: Path) -> None:
    """Emit a WARNING log if ``path`` is larger than the soft threshold."""
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return
    if size_bytes > _PROFILES_YAML_WARN_AT:
        size_mb = size_bytes / (1024 * 1024)
        _LOGGER.warning(
            "Unusually large profiles.yml (%.2f MB at %s); parse may be slow",
            size_mb,
            path,
        )


_ENV_VAR_RE = re.compile(
    # dbt-compatible: ``{{ env_var('NAME') }}`` or ``{{ env_var("NAME") }}``
    # with an optional second positional arg used as the default.
    # Whitespace between args is tolerated. The outer ``{{ ... }}`` jinja
    # brackets are REQUIRED — this matches dbt's own jinja-rendering
    # semantics (a bare ``env_var('NAME')`` is just a string in dbt's
    # YAML, not an env-var reference). If the bundled init-demo profile
    # ever needs the bare form, extend the regex (and document the
    # divergence from dbt) — for v0.1 we follow dbt's contract.
    r"""\{\{\s*env_var\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
    r"""(?:\s*,\s*['"]([^'"]*)['"])?\s*\)\s*\}\}""",
    re.VERBOSE,
)


def _render_env_vars(text: str, *, path: Path) -> str:
    """Render dbt-style ``{{ env_var('NAME') }}`` macros in ``text``.

    Supports two forms — ``env_var('NAME')`` (raises
    :class:`ProfileEnvVarUnsetError` if NAME is unset) and
    ``env_var('NAME', 'default')`` (falls back to the literal default).

    This is a deliberately minimal jinja-compat shim — full jinja
    rendering (loops, conditionals, other macros) is out of scope. Just
    enough to make the bundled ``signalforge init-demo`` profile's
    ``project: "{{ env_var('GOOGLE_CLOUD_PROJECT') }}"`` line work
    without an extra rendering step. Issue #47.
    """

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)
        value = os.environ.get(var_name)
        if value is not None:
            return value
        if default is not None:
            return default
        raise ProfileEnvVarUnsetError(var_name=var_name, profiles_path=path)

    return _ENV_VAR_RE.sub(_replace, text)


def _load_profiles_yaml(path: Path) -> dict[str, Any]:
    """Read and parse ``path`` as YAML, returning the top-level mapping.

    Applies a dbt-compatible ``env_var('NAME')`` substitution pass over
    the raw text before YAML parsing — see :func:`_render_env_vars`. The
    substitution runs against the YAML text (not the parsed structure)
    so quoted ``"{{ env_var('NAME') }}"`` strings cleanly become quoted
    ``"<value>"`` strings; YAML quoting rules are preserved.
    """
    _maybe_warn_large_profile(path)
    raw_text = path.read_text(encoding="utf-8")
    rendered_text = _render_env_vars(raw_text, path=path)
    raw = yaml.safe_load(rendered_text)
    if not isinstance(raw, dict):
        raise ProfileNotFoundError(
            searched_paths=[path],
            remediation=(f"profiles.yml at {path} must be a YAML mapping at the top level."),
        )
    # Pyright narrows yaml.safe_load to Any; the isinstance check above
    # gates this cast at runtime.
    return raw


def _find_and_load_profiles_yaml(project_dir: Path) -> tuple[dict[str, Any], Path]:
    """Walk the three resolution paths and return the first hit.

    Resolution order (DEC-009):

    1. ``$DBT_PROFILES_DIR/profiles.yml`` — user-trusted.
    2. ``<project_dir>/profiles.yml`` — symlink-hardened (DEC-017).
    3. ``~/.dbt/profiles.yml`` — user-trusted.

    Raises :class:`ProfileNotFoundError` listing every path searched if no
    file is found.
    """
    searched: list[Path] = []

    env_dir = os.environ.get("DBT_PROFILES_DIR")
    if env_dir:
        env_path = Path(env_dir) / "profiles.yml"
        searched.append(env_path)
        if env_path.exists() and env_path.is_file():
            return _load_profiles_yaml(env_path), env_path

    # Project-dir path goes through the symlink-hardened gate (DEC-017).
    # canonicalise_path raises ProfileNotFoundError on escapes/cycles; we
    # let that propagate so the caller sees the symlink failure rather
    # than silently falling through to ~/.dbt.
    project_path = canonicalise_path("profiles.yml", project_dir)
    searched.append(project_path)
    if project_path.exists() and project_path.is_file():
        return _load_profiles_yaml(project_path), project_path

    home_path = Path.home() / ".dbt" / "profiles.yml"
    searched.append(home_path)
    if home_path.exists() and home_path.is_file():
        return _load_profiles_yaml(home_path), home_path

    raise ProfileNotFoundError(searched_paths=searched)


def load_profile(project_dir: Path, target: str | None = None) -> DbtProfileTarget:
    """Resolve and load the active dbt profile target for a project.

    Reads ``<project_dir>/dbt_project.yml`` for the profile name, walks the
    three-path resolution order to find the matching ``profiles.yml``, then
    selects the active output by ``target`` (argument → profile's ``target:``
    field → :class:`ProfileTargetNotFoundError`).

    Raises:
        ProfileNotFoundError: ``dbt_project.yml`` is missing or has no
            ``profile:`` field; or no ``profiles.yml`` was found in any of
            the three search paths; or ``<project_dir>/profiles.yml``'s
            symlink resolution escapes the project tree.
        ProfileTargetNotFoundError: the resolved profile does not contain
            an ``outputs.<target>`` entry for the requested ``target``.
        UnsupportedAuthMethodError: the active output's ``method`` field is
            not ``"oauth"`` (or unset). Raised from the field validator on
            :class:`DbtProfileTarget`.
    """
    profile_name = _read_dbt_project_profile_name(project_dir)
    raw_profiles, profiles_path = _find_and_load_profiles_yaml(project_dir)

    profile_block = raw_profiles.get(profile_name)
    if not isinstance(profile_block, dict):
        raise ProfileNotFoundError(
            searched_paths=[profiles_path],
            remediation=(
                f"profiles.yml at {profiles_path} has no top-level "
                f"`{profile_name}:` block. Add a profile entry matching the "
                "name in dbt_project.yml."
            ),
        )

    outputs = profile_block.get("outputs")
    if not isinstance(outputs, dict):
        raise ProfileNotFoundError(
            searched_paths=[profiles_path],
            remediation=(f"Profile `{profile_name}` at {profiles_path} has no `outputs:` mapping."),
        )

    selected_target: str | None
    if target is not None:
        selected_target = target
    else:
        default_target = profile_block.get("target")
        selected_target = default_target if isinstance(default_target, str) else None

    available = sorted(outputs)
    if not selected_target or selected_target not in outputs:
        raise ProfileTargetNotFoundError(
            profile_name=profile_name,
            target=selected_target if selected_target else "<unset>",
            available=available,
            profiles_path=profiles_path,
        )

    target_block = outputs[selected_target]
    if not isinstance(target_block, dict):
        raise ProfileTargetNotFoundError(
            profile_name=profile_name,
            target=selected_target,
            available=available,
            profiles_path=profiles_path,
        )

    return DbtProfileTarget.model_validate(target_block)


__all__ = ["DbtProfileTarget", "load_profile"]
