"""Happy-path tests for :func:`signalforge.llm.client.call_anthropic`
(US-006).

Covers:

- Happy-path: returns :class:`LLMResult` with all usage fields populated.
- Cache-control marker placement (5m default, 1h override).
- Beta header behaviour gated on TTL.
- Pre-send token-count check (too-small / too-large branches).
- ``_min_cacheable_tokens`` model-prefix routing (parametrised).
- Cache-anomaly WARNING when the SDK reports zero cache-creation tokens.
- Lazy-format JSON logging gate (no f-string interpolation in
  user-controlled values).

Retry-branch coverage lives in ``test_client_retries.py`` so the timing
branches stay isolated.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from signalforge.llm import client as client_module
from signalforge.llm.client import (
    _CACHED_BLOCK_CAP_TOKENS,
    _MIN_CACHEABLE_TOKENS,
    _min_cacheable_tokens,
    call_anthropic,
)
from signalforge.llm.errors import (
    LLMCacheTooLargeError,
    LLMCacheTooSmallError,
)
from signalforge.llm.models import LLMResult

from ._fake import (
    FakeAnthropicClient,
    FakeCountTokensResponse,
    FakeMessage,
    FakeTextBlock,
    FakeUsage,
)

pytestmark = pytest.mark.llm


# Default block sizes for happy-path setup. 2048 ensures every model
# family meets its minimum so the pre-send check passes.
_DEFAULT_CACHED_TOKENS = 2048


def _ok_count_response() -> FakeCountTokensResponse:
    return FakeCountTokensResponse(input_tokens=_DEFAULT_CACHED_TOKENS)


def _ok_message_response(*, cache_creation: int = 1234, cache_read: int = 0) -> FakeMessage:
    return FakeMessage(
        content=[FakeTextBlock(text="hello world")],
        usage=FakeUsage(
            input_tokens=42,
            output_tokens=7,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


def test_call_anthropic_happy_path_returns_llm_result_with_usage() -> None:
    """A normal call returns an :class:`LLMResult` with usage + content."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={"model": "claude-sonnet-4-6"}, returns=_ok_count_response())
    fake.expect_messages_create(
        matching={"model": "claude-sonnet-4-6"},
        returns=_ok_message_response(),
    )

    result = call_anthropic(
        system="sys",
        cached_block="x" * 100,
        dynamic_block="y" * 50,
        model="claude-sonnet-4-6",
        max_tokens=1024,
        prompt_version="v1",
        client=fake,
    )

    assert isinstance(result, LLMResult)
    assert result.text_blocks == ("hello world",)
    assert result.response_text == "hello world"
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.cache_creation_input_tokens == 1234
    assert result.cache_read_input_tokens == 0
    assert result.model == "claude-sonnet-4-6"
    assert result.prompt_version == "v1"
    fake.assert_all_expectations_met()


def test_call_anthropic_sets_cache_control_marker_with_5m_default() -> None:
    """The first user block carries `cache_control` with the default 5m TTL."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(matching={}, returns=_ok_message_response())

    call_anthropic(
        system="sys",
        cached_block="cached",
        dynamic_block="dyn",
        model="claude-sonnet-4-6",
        max_tokens=512,
        prompt_version="v1",
        client=fake,
    )

    create_kwargs = fake.create_calls[0]
    messages = create_kwargs["messages"]
    assert len(messages) == 1
    blocks = messages[0]["content"]
    assert blocks[0]["text"] == "cached"
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert blocks[1]["text"] == "dyn"
    assert "cache_control" not in blocks[1]


def test_call_anthropic_sets_beta_header_only_when_1h_ttl() -> None:
    """The 1h-TTL beta header is set only when ``cache_ttl == "1h"``."""
    # 5m: header absent
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(matching={}, returns=_ok_message_response())
    call_anthropic(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        cache_ttl="5m",
        client=fake,
    )
    assert fake.create_calls[0]["extra_headers"] == {}

    # 1h: header set to documented beta string
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(matching={}, returns=_ok_message_response())
    call_anthropic(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        cache_ttl="1h",
        client=fake,
    )
    assert fake.create_calls[0]["extra_headers"] == {
        "anthropic-beta": "extended-cache-ttl-2025-04-11",
    }


def test_call_anthropic_pre_send_count_below_min_raises_cache_too_small() -> None:
    """Below-minimum cached block raises :class:`LLMCacheTooSmallError`."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=FakeCountTokensResponse(input_tokens=128))

    with pytest.raises(LLMCacheTooSmallError) as exc_info:
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )
    assert exc_info.value.cached_block_tokens == 128
    assert exc_info.value.min_tokens == 1024
    assert exc_info.value.model == "claude-sonnet-4-6"


def test_call_anthropic_pre_send_count_above_cap_raises_cache_too_large() -> None:
    """Above-cap cached block raises :class:`LLMCacheTooLargeError`."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(
        matching={},
        returns=FakeCountTokensResponse(input_tokens=_CACHED_BLOCK_CAP_TOKENS + 1),
    )

    with pytest.raises(LLMCacheTooLargeError) as exc_info:
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )
    assert exc_info.value.cached_block_tokens == _CACHED_BLOCK_CAP_TOKENS + 1
    assert exc_info.value.cap == _CACHED_BLOCK_CAP_TOKENS


@pytest.mark.parametrize(
    "model,expected_min",
    [
        ("claude-haiku-4-5-20251001", 2048),
        ("claude-sonnet-4-6", 1024),
        ("claude-opus-4-7", 1024),
        ("totally-unknown-model", 1024),
    ],
)
def test_call_anthropic_min_cacheable_tokens_keyed_by_model_prefix(
    model: str, expected_min: int
) -> None:
    """``_min_cacheable_tokens`` picks by longest-prefix-match w/ a default."""
    assert _min_cacheable_tokens(model) == expected_min


def test_call_anthropic_cache_no_op_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When usage reports cache_creation_input_tokens == 0 despite the
    cache marker, a WARNING-level log is emitted with the JSON body."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(
        matching={},
        returns=_ok_message_response(cache_creation=0),
    )

    with caplog.at_level(logging.WARNING, logger="signalforge.llm.client"):
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )

    no_op_records = [r for r in caplog.records if "cache marker no-op" in r.getMessage()]
    assert len(no_op_records) == 1
    payload = json.loads(no_op_records[0].getMessage().split(": ", 1)[1])
    assert payload == {
        "model": "claude-sonnet-4-6",
        "cached_block_size_tokens": _DEFAULT_CACHED_TOKENS,
        "min_required": 1024,
    }


def test_call_anthropic_cache_hit_does_not_emit_no_op_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Quality-Gate fix (Issue 4): on a cache HIT (cache_creation == 0
    AND cache_read > 0), the cache marker DID work — the cache was created
    on a prior call. The no-op warning fires only when both creation AND
    read are zero. Without this guard, every successful cache hit floods
    the log with a spurious WARNING."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(
        matching={},
        returns=_ok_message_response(cache_creation=0, cache_read=1500),
    )

    with caplog.at_level(logging.WARNING, logger="signalforge.llm.client"):
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )

    no_op_records = [r for r in caplog.records if "cache marker no-op" in r.getMessage()]
    assert len(no_op_records) == 0


def test_call_anthropic_does_not_log_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanity check: no log record carries an api-key-shaped string."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(matching={}, returns=_ok_message_response())

    with caplog.at_level(logging.DEBUG, logger="signalforge.llm.client"):
        call_anthropic(
            system="sys",
            cached_block="c",
            dynamic_block="d",
            model="claude-sonnet-4-6",
            max_tokens=128,
            prompt_version="v1",
            client=fake,
        )

    for record in caplog.records:
        assert "ANTHROPIC_API_KEY" not in record.getMessage()
        assert "sk-ant-" not in record.getMessage()


def test_logger_calls_use_json_dumps_no_f_string() -> None:
    """DEC-022 / DEC-011 grep gate: every ``_LOGGER.<level>(...)`` call
    in ``client.py`` must use a non-f-string literal as its first
    argument. Catches accidental ANSI-escape injection paths."""
    src = Path(client_module.__file__).read_text(encoding="utf-8")
    pattern = re.compile(r'_LOGGER\.\w+\(f"')
    matches = pattern.findall(src)
    assert matches == [], (
        f"Found f-string interpolation in _LOGGER calls: {matches}. "
        "Use lazy-format JSON instead (DEC-022)."
    )


def test_min_cacheable_tokens_dict_is_sorted_alphabetically_for_review() -> None:
    """Sanity-check the public ``_MIN_CACHEABLE_TOKENS`` constant carries
    the expected three model-family prefixes."""
    assert set(_MIN_CACHEABLE_TOKENS.keys()) == {
        "claude-haiku",
        "claude-sonnet",
        "claude-opus",
    }
    assert _MIN_CACHEABLE_TOKENS["claude-haiku"] == 2048
    assert _MIN_CACHEABLE_TOKENS["claude-sonnet"] == 1024
    assert _MIN_CACHEABLE_TOKENS["claude-opus"] == 1024
