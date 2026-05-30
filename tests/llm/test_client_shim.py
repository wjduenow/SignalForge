"""Tests for the Anthropic SDK shim (US-005 / DEC-012).

Covers:

* The factory returns an object satisfying
  :class:`signalforge.llm.AnthropicClientProtocol`.
* The placeholder stub fake (``tests/llm/_fake.py::_StubAnthropicClient``)
  also satisfies the protocol — confirms the structural shape both real and
  test clients commit to.
* DEC-012 enforcement at the regex level: no ``# pyright: ignore`` /
  ``# type: ignore`` comments outside ``_anthropic_client.py`` in
  :mod:`signalforge.llm`. (US-014 lands a stricter AST scan; this is the
  cheap floor.)
* No direct ``anthropic.Anthropic(`` construction outside
  ``_anthropic_client.py`` in :mod:`signalforge.llm`. Mirrors the safety AST
  scan precedent at the regex level; full AST scan is US-014's job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from signalforge.llm import AnthropicClientProtocol
from signalforge.llm._anthropic_client import _make_anthropic_client

from ._fake import _StubAnthropicClient

pytestmark = pytest.mark.llm


_LLM_SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "signalforge" / "llm"


def _llm_py_files_excluding_client() -> list[Path]:
    """All ``.py`` under ``src/signalforge/llm/`` except the per-vendor
    SDK shims (``_anthropic_client.py`` and ``_openai_client.py``).

    Each vendor shim is the sole home for its own SDK's ``# pyright:
    ignore`` / ``# type: ignore`` comments per the one-shim-per-vendor
    convention (``.claude/rules/llm-drafter.md`` § "One SDK seam").
    The per-vendor *confinement* of each shim's ignores is asserted by
    the per-vendor cheap-floor tests:

    * Anthropic: this module's :func:`test_no_pyright_ignores_outside_client_shim`
      (but excluding the OpenAI shim — its docstring mentions the phrase
      and its lazy ``import openai`` carries a legitimate ignore).
    * OpenAI: ``tests/llm/test_openai_client_confinement.py``.

    Excluding the OpenAI shim from the Anthropic regex floor mirrors how
    Scan 9 in ``tests/test_audit_completeness.py`` excludes
    ``_anthropic_client.py`` (and vice-versa for Scan 3) — each
    per-vendor seam is invisible to the others' scans by construction.
    """
    excluded_names = {"_anthropic_client.py", "_openai_client.py"}
    return [p for p in _LLM_SRC_DIR.rglob("*.py") if p.name not in excluded_names]


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
    """The placeholder stub satisfies ``AnthropicClientProtocol``.

    ``AnthropicClientProtocol`` is ``@runtime_checkable``, so we can use
    ``isinstance`` directly. This is the contract the full
    ``FakeAnthropicClient`` (US-006) inherits.
    """
    stub = _StubAnthropicClient()
    assert isinstance(stub, AnthropicClientProtocol)
    assert hasattr(stub.messages, "create")
    assert hasattr(stub.messages, "count_tokens")


def test_no_pyright_ignores_outside_client_shim() -> None:
    """DEC-012: every Anthropic-SDK ``# pyright: ignore`` lives in ``_anthropic_client.py``.

    Walks every ``.py`` under ``src/signalforge/llm/`` (except
    ``_anthropic_client.py``) and asserts no line carries an
    **Anthropic-mentioning** ``# pyright: ignore`` / ``# type: ignore``.
    The line was originally an unconditional "no ignore anywhere else"
    check (US-005 only had one vendor); #137 US-001 added the Gemini
    shim ``_gemini_client.py`` which carries its own SDK ignores, so
    the scan now narrows to Anthropic-specific lines. The Gemini-side
    confinement gate lives in
    ``tests/llm/test_gemini_client_confinement.py``; both gates apply
    independently (mirrors the per-vendor split in
    ``tests/warehouse/test_snowflake_client_confinement.py``).
    """
    offenders: list[tuple[Path, int, str]] = []
    ignore_re = re.compile(r"#\s*(pyright|type):\s*ignore")
    for path in _llm_py_files_excluding_client():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not ignore_re.search(line):
                continue
            # Anthropic-specific lines are the ones DEC-012 confines. A
            # Gemini-shim line carrying `# type: ignore[import-not-found]`
            # for `google.genai` would otherwise trip this scan vacuously
            # — its confinement is the Gemini gate's job.
            if "anthropic" in line.lower():
                offenders.append((path, lineno, line))
    assert offenders == [], (
        "Found Anthropic-mentioning `# pyright: ignore` / `# type: ignore` "
        "outside _anthropic_client.py — DEC-012 requires Anthropic-SDK "
        "noise be confined to signalforge.llm._anthropic_client. "
        "Offenders: " + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
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
        "Found `anthropic.Anthropic(` construction outside _anthropic_client.py — "
        "DEC-012 requires the SDK be instantiated only via "
        "_make_anthropic_client. Offenders: " + ", ".join(f"{p}:{n}" for p, n, _ in offenders)
    )
