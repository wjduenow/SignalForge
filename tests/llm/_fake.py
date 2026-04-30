"""Placeholder fake for the Anthropic SDK client.

The full ``FakeAnthropicClient`` with an ``expect_*`` expectation-tracking
API (mirroring ``tests/warehouse/_fake.py::FakeBigQueryClient``) lands in
US-006. This stub exists so US-005's protocol-conformance tests have an
importable target that satisfies the structural shape of
:class:`signalforge.llm._client._AnthropicClientProtocol`.

Lives under ``tests/llm/`` and is never imported from production code.
"""

from __future__ import annotations

from typing import Any


class _StubMessages:
    """Stand-in for ``anthropic.Anthropic().messages`` (US-005 placeholder).

    Both methods raise ``NotImplementedError`` — US-006 replaces this stub
    with the real expectation-tracking fake. The shape (``create`` and
    ``count_tokens`` directly on ``messages``) matches the installed
    ``anthropic`` SDK as of US-005.
    """

    def create(self, **kwargs: Any) -> Any:
        raise NotImplementedError("FakeAnthropicClient.messages.create lands in US-006")

    def count_tokens(self, **kwargs: Any) -> Any:
        raise NotImplementedError("FakeAnthropicClient.messages.count_tokens lands in US-006")


class _StubAnthropicClient:
    """Minimal client that satisfies ``_AnthropicClientProtocol``.

    Constructed by US-005's protocol-conformance tests; replaced by the
    full ``FakeAnthropicClient`` in US-006.
    """

    def __init__(self) -> None:
        self.messages = _StubMessages()


__all__ = ["_StubAnthropicClient", "_StubMessages"]
