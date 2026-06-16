"""Tests for ``signalforge.airflow._resolve`` (issue #234 / US-003).

These tests import ONLY the airflow-free pure resolver — never the real
``apache-airflow`` package — so they run in the **default** pytest suite (NO
``airflow`` marker, NO ``importorskip``). They drive the resolver with a tiny
fake ``conn`` object (``.password`` + ``.extra_dejson``) and a dict-backed
``variable_lookup``, and cover EVERY branch of ``_resolve.py`` for the codecov
patch gate:

* provider + key from ``password``;
* Variable fallback when ``password`` is absent / blank;
* both absent → ``api_key is None`` (lenient — no raise, DEC-003);
* unknown provider → :class:`AirflowConfigError` (closed allowlist, DEC-005);
* ``extra`` typo key → :class:`AirflowConfigError` (``extra="forbid"``, DEC-010);
* ``profiles_dir`` accepted when ``project_dir`` is ``None`` (bounded gap);
* ``profiles_dir`` canonicalised inside ``project_dir`` (happy path);
* ``profiles_dir`` escaping ``project_dir`` → :class:`AirflowConfigError` (DEC-008);
* ``__repr__`` omits the key value AND any field-name label revealing it;
* :class:`HookResolution` is frozen.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from signalforge.airflow._resolve import (
    API_KEY_VARIABLE_KEY,
    HookResolution,
    _ConnectionExtra,
    resolve_connection,
)
from signalforge.airflow.errors import AirflowConfigError


class _FakeConn:
    """Duck-typed Airflow ``Connection`` slice the resolver reads."""

    def __init__(
        self, *, password: str | None = None, extra_dejson: dict[str, object] | None = None
    ) -> None:
        self._password = password
        self._extra_dejson = extra_dejson if extra_dejson is not None else {}

    @property
    def password(self) -> str | None:
        return self._password

    @property
    def extra_dejson(self) -> dict[str, object]:
        return self._extra_dejson


def _no_variable(_key: str) -> str | None:
    """A ``variable_lookup`` that always reports the Variable absent."""
    return None


def _dict_variable(mapping: dict[str, str | None]):
    """Build a dict-backed ``variable_lookup`` (mirrors ``Variable.get(default_var=None)``)."""

    def _lookup(key: str) -> str | None:
        return mapping.get(key)

    return _lookup


# ---------------------------------------------------------------------------
# api_key resolution (DEC-003)
# ---------------------------------------------------------------------------


def test_provider_and_key_from_password() -> None:
    """``password`` is the primary key source; ``provider`` from ``extra``."""
    conn = _FakeConn(password="sk-secret-123", extra_dejson={"provider": "anthropic"})
    res = resolve_connection(conn, variable_lookup=_no_variable)
    assert res.api_key == "sk-secret-123"
    assert res.provider == "anthropic"
    assert res.profiles_dir is None


def test_variable_fallback_when_no_password() -> None:
    """When ``password`` is absent, the Variable lookup supplies the key under
    the documented :data:`API_KEY_VARIABLE_KEY`."""
    conn = _FakeConn(password=None, extra_dejson={"provider": "openai"})
    lookup = _dict_variable({API_KEY_VARIABLE_KEY: "sk-from-variable"})
    res = resolve_connection(conn, variable_lookup=lookup)
    assert res.api_key == "sk-from-variable"
    assert res.provider == "openai"


def test_blank_password_falls_back_to_variable() -> None:
    """An empty-string ``password`` is treated as absent (falls back)."""
    conn = _FakeConn(password="", extra_dejson={})
    lookup = _dict_variable({API_KEY_VARIABLE_KEY: "sk-fallback"})
    res = resolve_connection(conn, variable_lookup=lookup)
    assert res.api_key == "sk-fallback"


def test_both_absent_yields_none_and_does_not_raise() -> None:
    """Lenient (DEC-003): neither source has a key → ``api_key is None``, no raise."""
    conn = _FakeConn(password=None, extra_dejson={})
    res = resolve_connection(conn, variable_lookup=_no_variable)
    assert res.api_key is None
    assert res.provider is None
    assert res.profiles_dir is None


def test_blank_variable_value_collapses_to_none() -> None:
    """A blank Variable value collapses to ``None`` (not an empty string)."""
    conn = _FakeConn(password=None, extra_dejson={})
    lookup = _dict_variable({API_KEY_VARIABLE_KEY: ""})
    res = resolve_connection(conn, variable_lookup=lookup)
    assert res.api_key is None


# ---------------------------------------------------------------------------
# provider allowlist (DEC-005)
# ---------------------------------------------------------------------------


def test_unknown_provider_raises() -> None:
    """A non-``None`` provider outside the closed allowlist fails loud."""
    conn = _FakeConn(password="k", extra_dejson={"provider": "definitely-not-a-provider"})
    with pytest.raises(AirflowConfigError) as excinfo:
        resolve_connection(conn, variable_lookup=_no_variable)
    rendered = str(excinfo.value)
    assert "provider" in rendered
    # Remediation lists the allowed providers.
    assert "anthropic" in rendered


def test_absent_provider_is_left_none() -> None:
    """When ``provider`` is omitted it stays ``None`` (prune-existing path)."""
    conn = _FakeConn(password="k", extra_dejson={"profiles_dir": None})
    res = resolve_connection(conn, variable_lookup=_no_variable)
    assert res.provider is None


# ---------------------------------------------------------------------------
# extra="forbid" (DEC-010)
# ---------------------------------------------------------------------------


def test_extra_typo_key_raises() -> None:
    """A typo key (``cache_scop``) fails loud at validation."""
    conn = _FakeConn(password="k", extra_dejson={"cache_scop": "project"})
    with pytest.raises(AirflowConfigError) as excinfo:
        resolve_connection(conn, variable_lookup=_no_variable)
    assert "extra" in str(excinfo.value).lower()


def test_extra_accepts_all_three_known_keys() -> None:
    """The full known-key set validates and round-trips."""
    extra = _ConnectionExtra.model_validate(
        {"profiles_dir": "/p", "provider": "gemini", "cache_scope": "project"}
    )
    assert extra.profiles_dir == "/p"
    assert extra.provider == "gemini"
    assert extra.cache_scope == "project"


def test_connection_extra_is_frozen() -> None:
    """``_ConnectionExtra`` is frozen (read-only once validated)."""
    extra = _ConnectionExtra()
    with pytest.raises(ValidationError):  # frozen mutation
        extra.provider = "anthropic"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# profiles_dir (DEC-008)
# ---------------------------------------------------------------------------


def test_profiles_dir_accepted_when_project_dir_none() -> None:
    """No anchor → path accepted as-is (the documented bounded-defence gap)."""
    conn = _FakeConn(password="k", extra_dejson={"profiles_dir": "/some/profiles"})
    res = resolve_connection(conn, variable_lookup=_no_variable, project_dir=None)
    assert res.profiles_dir == "/some/profiles"


def test_profiles_dir_canonicalised_inside_project_dir(tmp_path: Path) -> None:
    """A path inside the project tree canonicalises to its absolute resolved form."""
    project = tmp_path / "proj"
    profiles = project / "dbt"
    profiles.mkdir(parents=True)
    conn = _FakeConn(password="k", extra_dejson={"profiles_dir": "dbt"})
    res = resolve_connection(conn, variable_lookup=_no_variable, project_dir=project)
    assert res.profiles_dir == str(profiles.resolve())


def test_profiles_dir_escape_raises(tmp_path: Path) -> None:
    """A ``profiles_dir`` escaping the project tree fails loud (DEC-008)."""
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    conn = _FakeConn(password="k", extra_dejson={"profiles_dir": str(outside)})
    with pytest.raises(AirflowConfigError) as excinfo:
        resolve_connection(conn, variable_lookup=_no_variable, project_dir=project)
    assert "profiles_dir" in str(excinfo.value)


# ---------------------------------------------------------------------------
# HookResolution leak-surface discipline + immutability (DEC-007)
# ---------------------------------------------------------------------------


def test_repr_omits_api_key_value_and_label() -> None:
    """``__repr__`` shows ``profiles_dir`` + ``provider`` ONLY — never the key
    value, never a field-name label that would reveal a credential is present."""
    res = HookResolution(profiles_dir="/p", provider="anthropic", api_key="sk-super-secret-VALUE")
    rendered = repr(res)
    # Safe fields appear.
    assert "/p" in rendered
    assert "anthropic" in rendered
    # The key value must NOT leak.
    assert "sk-super-secret-VALUE" not in rendered
    # No field-name label revealing a credential.
    assert "api_key" not in rendered
    assert "key" not in rendered


def test_hook_resolution_is_frozen() -> None:
    """``HookResolution`` is a frozen dataclass."""
    res = HookResolution(profiles_dir=None, provider=None, api_key=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.api_key = "leak"  # type: ignore[misc]
