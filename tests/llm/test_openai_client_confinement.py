"""#136 US-001 DEC-010 — OpenAI SDK type-ignore confinement.

Every ``# type: ignore`` / ``# pyright: ignore`` line in the
``signalforge.llm`` tree that ALSO mentions "openai" must live ONLY in
``_openai_client.py`` — the one-shim-per-vendor SDK seam. Mirrors the
spirit of the Anthropic-SDK confinement scan (Scan 3 in
``tests/test_audit_completeness.py``) and the Snowflake-shaped per-file
line scan in ``tests/warehouse/test_snowflake_client_confinement.py``; a
simple file/line scan suffices here.

The companion Scan 9 in ``tests/test_audit_completeness.py`` enforces the
AST-level construction-call confinement (``openai.OpenAI(...)`` only in
the shim). This line-based scan is the cheap floor; Scan 9 is the
load-bearing AST one.
"""

from __future__ import annotations

from pathlib import Path

_LLM_DIR = Path(__file__).resolve().parents[2] / "src" / "signalforge" / "llm"
_SHIM_FILENAME = "_openai_client.py"


def _openai_type_ignore_lines(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, text) for lines carrying an openai-mentioning
    type/pyright ignore directive.
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        lowered = line.lower()
        has_ignore = "type: ignore" in lowered or "pyright: ignore" in lowered
        if has_ignore and "openai" in lowered:
            hits.append((lineno, line.strip()))
    return hits


def test_openai_type_ignores_only_in_shim() -> None:
    """No ``.py`` under ``signalforge/llm/`` other than
    ``_openai_client.py`` may carry an openai-mentioning type-ignore.
    """
    offenders: list[str] = []
    # ``rglob`` (not ``glob``) so nested modules under signalforge/llm/
    # are also scanned — PR #152 CodeRabbit catch: top-level-only glob
    # let openai-mentioning ignore directives in subpackages bypass the
    # guard (the package is flat today but a future subpackage would
    # silently un-confine the scan).
    for py in sorted(_LLM_DIR.rglob("*.py")):
        if py.name == _SHIM_FILENAME:
            continue
        for lineno, text in _openai_type_ignore_lines(py):
            offenders.append(f"{py.relative_to(_LLM_DIR)}:{lineno}: {text}")

    assert not offenders, (
        "openai SDK type-ignore must live only in "
        f"{_SHIM_FILENAME}, but found:\n" + "\n".join(offenders)
    )


def test_shim_actually_carries_openai_type_ignore() -> None:
    """Sanity: the shim itself DOES carry at least one openai-mentioning
    type-ignore. Without this, the confinement scan above could pass
    vacuously after a refactor that dropped the seam.
    """
    shim = _LLM_DIR / _SHIM_FILENAME
    assert _openai_type_ignore_lines(shim), (
        f"{_SHIM_FILENAME} should confine the openai SDK type-ignore; "
        "the confinement scan is only meaningful if the seam exists"
    )


# ---------------------------------------------------------------------------
# Coverage-closing tests for PR #152 codecov gaps on the shim internals.
# Confinement-test file is the natural home — these tests pin the per-shim
# behaviours that the production import surface depends on (the adapter
# façade + the tiktoken fallback) but that no production caller currently
# exercises in the default test set (the orchestrator drives via the
# FakeOpenAIClient, never through _OpenAIClientAdapter; tiktoken's fallback
# fires only on an unknown model id).
# ---------------------------------------------------------------------------


def test_openai_client_adapter_messages_create_delegates_to_chat_completions() -> None:
    """``_OpenAIClientAdapter.messages.create(**kwargs)`` MUST delegate
    verbatim to ``self._raw.chat.completions.create(**kwargs)``.

    The orchestrator's ``call_llm`` hard-calls
    ``llm_client.messages.create(...)``; the adapter is the only thing
    that maps that into OpenAI's actual SDK call shape. A regression
    that breaks the delegation (e.g. a refactor that swaps the SDK call
    path) would surface here, not in the integration tests (those use
    ``FakeOpenAIClient`` which has its own ``.messages.create`` and
    never goes through the adapter).
    """
    from types import SimpleNamespace
    from typing import Any

    from signalforge.llm._openai_client import _OpenAIClientAdapter

    captured: dict[str, Any] = {}
    sentinel = object()

    def _create(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    adapter = _OpenAIClientAdapter(raw)

    result = adapter.messages.create(
        model="gpt-4o",
        max_tokens=128,
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )

    assert result is sentinel
    assert captured == {
        "model": "gpt-4o",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
    }


def test_count_openai_tokens_falls_back_to_cl100k_base_for_unknown_model() -> None:
    """``_count_openai_tokens`` MUST NOT raise on an unknown model id —
    DEC-012 of #136 documents the ``cl100k_base`` fallback so a
    newer-than-tiktoken OpenAI SKU still produces a usable estimate
    rather than crashing the ``--estimate`` flow. ``--estimate`` is a
    calibration signal, not a billing guarantee (mirrors the
    planner-estimate caveat in ``warehouse-adapters.md``).
    """
    from signalforge.llm._openai_client import _count_openai_tokens

    # A model id ``tiktoken.encoding_for_model`` doesn't recognise.
    # Must return a positive int via the cl100k_base fallback, NOT raise.
    count = _count_openai_tokens("not-a-real-openai-model-xyz", "hello world")
    assert isinstance(count, int)
    assert count > 0
