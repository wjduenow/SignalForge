"""Gated tests for :class:`SignalForgeHook.get_conn` (#234 US-004).

Belt-and-suspenders gating per ``testing-signal.md`` and the
``tests/airflow/test_operators.py`` precedent:

1. ``pytestmark = pytest.mark.airflow`` — every test is deselected by the default
   ``addopts`` ``-m '... and not airflow'`` so a plain ``uv run pytest`` never
   imports Apache Airflow.
2. A runtime ``pytest.importorskip("airflow")`` as the FIRST line of each test
   (NOT a module-scope import — that would run at collection time even when
   deselected, and this module IS collected before deselection). The skip carries
   a clear reason when a maintainer runs ``-m airflow`` without Airflow installed.

Run inside the constraints-pinned Airflow venv (see
docs/research/airflow-test-environment.md)::

    SF_RUN_AIRFLOW=1 PYTHONPATH="$PWD/src" \
        /path/to/.venv-airflow/bin/python -m pytest tests/airflow -m airflow --no-cov

These pin the hook's ``get_conn`` contract: it delegates to the airflow-free
:func:`signalforge.airflow._resolve.resolve_connection` against a real
``airflow.models.Connection`` (the hook's ``get_connection`` is monkeypatched to
return it, so no Airflow metastore / DB is needed), the Airflow Variable
fallback, the ``AirflowConfigError`` misconfig paths, the secrets-hygiene
guarantees (no raw key logged; ``register_secret`` redacts to ``***``), and the
redacting ``__repr__``.
"""

from __future__ import annotations

import importlib
import json
import logging

import pytest

from signalforge.airflow import _airflow_compat
from signalforge.airflow._resolve import API_KEY_VARIABLE_KEY, HookResolution
from signalforge.airflow.errors import AirflowConfigError

pytestmark = pytest.mark.airflow

_AIRFLOW_SKIP = "Apache Airflow not installed (run inside the constraints-pinned airflow venv)"

_SECRET = "sk-super-secret-value-42"


def _hook_class() -> type:
    """Resolve the real hook class (airflow present — built by the factory)."""
    hooks = importlib.import_module("signalforge.airflow.hooks")
    return hooks.SignalForgeHook


def _make_connection(
    *, password: str | None = None, extra: dict[str, object] | None = None
) -> object:
    """Build a real ``airflow.models.Connection`` with the given password/extra."""
    from airflow.models import Connection

    return Connection(
        conn_id="signalforge_default",
        conn_type="generic",
        password=password,
        extra=json.dumps(extra) if extra is not None else None,
    )


def _hook_with_conn(monkeypatch: pytest.MonkeyPatch, conn: object) -> object:
    """Build a hook whose ``get_connection`` returns ``conn`` (no metastore)."""
    hook = _hook_class()(signalforge_conn_id="signalforge_default")
    # Instance attribute shadows the inherited ``BaseHook.get_connection``; the
    # hook calls ``self.get_connection(conn_id)``, so a plain ``lambda conn_id``
    # is the right shape (no bound ``self``).
    monkeypatch.setattr(hook, "get_connection", lambda conn_id: conn)
    return hook


# --------------------------------------------------------------------------- #
# get_conn resolution
# --------------------------------------------------------------------------- #


def test_get_conn_happy_provider_and_key_from_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """``password`` → ``api_key``; ``extra.provider`` → ``provider``."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    conn = _make_connection(password=_SECRET, extra={"provider": "anthropic"})
    hook = _hook_with_conn(monkeypatch, conn)

    res = hook.get_conn()

    assert isinstance(res, HookResolution)
    assert res.provider == "anthropic"
    assert res.api_key == _SECRET


def test_get_conn_variable_fallback_when_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``password`` → the API key comes from the Airflow Variable lookup.

    The hook wires ``variable_lookup`` to ``_airflow_compat.airflow_variable_get``
    via dotted access at call time, so monkeypatching that shim attribute swaps
    the Variable source without an Airflow metastore.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    def _fake_variable_get(key: str) -> str | None:
        return _SECRET if key == API_KEY_VARIABLE_KEY else None

    monkeypatch.setattr(_airflow_compat, "airflow_variable_get", _fake_variable_get)

    conn = _make_connection(password=None, extra={"provider": "openai"})
    hook = _hook_with_conn(monkeypatch, conn)

    res = hook.get_conn()

    assert res.api_key == _SECRET
    assert res.provider == "openai"


def test_get_conn_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``extra.provider`` outside the closed allowlist → ``AirflowConfigError``."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    conn = _make_connection(password=_SECRET, extra={"provider": "bogus-llm"})
    hook = _hook_with_conn(monkeypatch, conn)

    with pytest.raises(AirflowConfigError):
        hook.get_conn()


def test_get_conn_invalid_extra_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo / unknown ``extra`` key → ``AirflowConfigError`` (``extra="forbid"``).

    Covers the "misconfigured Connection surfaces a tier-2 ``AirflowConfigError``
    at the hook seam" path — the resolver raises and ``get_conn`` propagates it
    unchanged.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    conn = _make_connection(password=_SECRET, extra={"cache_scop": "project"})
    hook = _hook_with_conn(monkeypatch, conn)

    with pytest.raises(AirflowConfigError):
        hook.get_conn()


def test_get_conn_lenient_when_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither ``password`` nor a Variable → ``api_key is None`` (lenient, DEC-003).

    The hook does NOT enforce key-requiredness — the consuming operator does. So
    a prune-existing-only Connection (no LLM key) resolves cleanly.
    """
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    def _no_variable(_key: str) -> str | None:
        return None

    monkeypatch.setattr(_airflow_compat, "airflow_variable_get", _no_variable)

    conn = _make_connection(password=None, extra={})
    hook = _hook_with_conn(monkeypatch, conn)

    res = hook.get_conn()

    assert res.api_key is None
    assert res.provider is None


# --------------------------------------------------------------------------- #
# Secrets hygiene (DEC-006 / DEC-007)
# --------------------------------------------------------------------------- #


def test_get_conn_never_logs_raw_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Resolving a Connection must not emit the raw API key into any log record."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    conn = _make_connection(password=_SECRET, extra={"provider": "anthropic"})
    hook = _hook_with_conn(monkeypatch, conn)

    with caplog.at_level(logging.DEBUG):
        res = hook.get_conn()

    assert res.api_key == _SECRET  # resolution succeeded …
    assert _SECRET not in caplog.text  # … but the key never hit the logs.


def test_register_secret_masks_value_in_logs() -> None:
    """``_airflow_compat.register_secret`` registers the value with Airflow's
    secrets masker so it redacts to ``***`` (belt-and-braces, DEC-006)."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    from airflow.utils.log.secrets_masker import _secrets_masker

    _airflow_compat.register_secret(_SECRET)

    redacted = _secrets_masker().redact(f"the resolved key is {_SECRET} ok")

    assert _SECRET not in redacted
    assert "***" in redacted


def test_repr_redacts_to_conn_id_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """``__repr__`` shows only ``signalforge_conn_id`` — never the resolved key
    (which is not even stored on the hook) or a credential-revealing label."""
    pytest.importorskip("airflow", reason=_AIRFLOW_SKIP)

    hook = _hook_class()(signalforge_conn_id="my_conn")

    rendered = repr(hook)

    assert rendered == "SignalForgeHook(signalforge_conn_id='my_conn')"
    assert _SECRET not in rendered
    assert "api_key" not in rendered
