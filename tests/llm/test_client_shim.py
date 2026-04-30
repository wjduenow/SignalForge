"""Tests for the Anthropic SDK shim (US-005 / DEC-012).

Covers:

* The factory returns an object satisfying
  :class:`signalforge.llm._client._AnthropicClientProtocol`.
* The placeholder stub fake (``tests/llm/_fake.py::_StubAnthropicClient``)
  also satisfies the protocol — confirms the structural shape both real and
  test clients commit to.
* DEC-012 enforcement at the regex level: no ``# pyright: ignore`` /
  ``# type: ignore`` comments outside ``_client.py`` in
  :mod:`signalforge.llm`. (US-014 lands a stricter AST scan; this is the
  cheap floor.)
* No direct ``anthropic.Anthropic(`` construction outside ``_client.py``
  in :mod:`signalforge.llm`. Mirrors the safety AST scan precedent at the
  regex level; full AST scan is US-014's job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from signalforge.llm._client import (
    _AnthropicClientProtocol,
    _make_anthropic_client,
)

from ._fake import _StubAnthropicClient

pytestmark = pytest.mark.llm


_LLM_SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "signalforge" / "llm"


def _llm_py_files_excluding_client() -> list[Path]:
    """All ``.py`` under ``src/signalforge/llm/`` except ``_client.py``."""
    return [p for p in _LLM_SRC_DIR.rglob("*.py") if p.name != "_client.py"]


def test_make_anthropic_client_returns_protocol_satisfying_object() -> None:
    """The factory returns something with a ``messages`` attribute.

    We pass a fake api_key so the SDK doesn't try to read the env var; we
    do not actually invoke any network call.
    """
    client = _make_anthropic_client(api_key="test-key-not-real")
    assert hasattr(client, "messages")
    assert hasattr(client.messages, "create")
    assert hasattr(client.messages, "count_tokens")


def test_stub_satisfies_protocol() -> None:
    """The placeholder stub satisfies ``_AnthropicClientProtocol``.

    ``_AnthropicClientProtocol`` is ``@runtime_checkable``, so we can use
    ``isinstance`` directly. This is the contract the full
    ``FakeAnthropicClient`` (US-006) inherits.
    """
    stub = _StubAnthropicClient()
    assert isinstance(stub, _AnthropicClientProtocol)
    assert hasattr(stub.messages, "create")
    assert hasattr(stub.messages, "count_tokens")


def test_no_pyright_ignores_outside_client_shim() -> None:
    """DEC-012: every Anthropic-SDK ``# pyright: ignore`` lives in ``_client.py``.

    Walks every ``.py`` under ``src/signalforge/llm/`` (except
    ``_client.py``) and asserts that no line contains ``# pyright: ignore``
    or ``# type: ignore``. US-014's AST scan will be more thorough; this is
    the cheap floor.
    """
    offenders: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"#\s*(pyright|type):\s*ignore")
    for path in _llm_py_files_excluding_client():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append((path, lineno, line))
    assert offenders == [], (
        "Found `# pyright: ignore` / `# type: ignore` outside _client.py — "
        "DEC-012 requires Anthropic-SDK noise be confined to "
        "signalforge.llm._client. Offenders: " + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
    )


def test_anthropic_client_construction_only_in_shim() -> None:
    """No direct ``anthropic.Anthropic(`` construction outside the shim.

    Lightweight regex version; the full AST scan is US-014's job (and
    extends the safety AST scan to cover this same invariant).
    """
    offenders: list[tuple[Path, int, str]] = []
    needle = "anthropic.Anthropic("
    for path in _llm_py_files_excluding_client():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if needle in line:
                offenders.append((path, lineno, line))
    assert offenders == [], (
        "Found `anthropic.Anthropic(` construction outside _client.py — "
        "DEC-012 requires the SDK be instantiated only via "
        "_make_anthropic_client. Offenders: " + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
    )
