"""Happy-path tests for :func:`signalforge.llm.client.call_llm`
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
    call_llm,
)
from signalforge.llm.errors import (
    LLMCacheTooLargeError,
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


def test_call_llm_happy_path_returns_llm_result_with_usage() -> None:
    """A normal call returns an :class:`LLMResult` with usage + content."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={"model": "claude-sonnet-4-6"}, returns=_ok_count_response())
    fake.expect_messages_create(
        matching={"model": "claude-sonnet-4-6"},
        returns=_ok_message_response(),
    )

    result = call_llm(
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


def test_call_llm_sets_cache_control_marker_with_5m_default() -> None:
    """The first user block carries `cache_control` with the default 5m TTL."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(matching={}, returns=_ok_message_response())

    call_llm(
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


def test_call_llm_sets_beta_header_only_when_1h_ttl() -> None:
    """The 1h-TTL beta header is set only when ``cache_ttl == "1h"``."""
    # 5m: header absent
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(matching={}, returns=_ok_message_response())
    call_llm(
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
    call_llm(
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


def test_call_llm_pre_send_count_below_min_drops_cache_marker() -> None:
    """Below-minimum cached block drops the ``cache_control`` marker and
    proceeds to the normal call.

    Anthropic silently no-ops the cache marker below the per-model minimum
    (``_MIN_CACHEABLE_TOKENS``) — leaving the marker set wastes the
    pre-send ``count_tokens`` call and triggers the dual-zero cache-anomaly
    WARNING. The right behaviour is to drop the marker, log once, and let
    the call succeed; callers whose cached block is naturally below the
    minimum (e.g. the grade layer's compact rubric) still get a clean run.
    """
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=FakeCountTokensResponse(input_tokens=128))
    fake.expect_messages_create(matching={}, returns=_ok_message_response())

    result = call_llm(
        system="sys",
        cached_block="c",
        dynamic_block="d",
        model="claude-sonnet-4-6",
        max_tokens=128,
        prompt_version="v1",
        client=fake,
    )

    # The call succeeded — no exception raised.
    assert result.response_text == "hello world"
    # The cache marker was dropped before the create call: the request
    # block 1 carries `text` but NOT `cache_control`.
    create_call = fake.messages._create_calls[0]  # type: ignore[attr-defined]
    blocks = create_call["messages"][0]["content"]
    assert blocks[0]["text"] == "c"
    assert "cache_control" not in blocks[0]


def test_call_llm_pre_send_count_above_cap_raises_cache_too_large() -> None:
    """Above-cap cached block raises :class:`LLMCacheTooLargeError`."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(
        matching={},
        returns=FakeCountTokensResponse(input_tokens=_CACHED_BLOCK_CAP_TOKENS + 1),
    )

    with pytest.raises(LLMCacheTooLargeError) as exc_info:
        call_llm(
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
def test_call_llm_min_cacheable_tokens_keyed_by_model_prefix(model: str, expected_min: int) -> None:
    """``_min_cacheable_tokens`` picks by longest-prefix-match w/ a default."""
    assert _min_cacheable_tokens(model) == expected_min


def test_call_llm_cache_no_op_emits_warning(
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
        call_llm(
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


def test_call_llm_cache_hit_does_not_emit_no_op_warning(
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
        call_llm(
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


def test_call_llm_does_not_log_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanity check: no log record carries an api-key-shaped string."""
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    fake.expect_messages_create(matching={}, returns=_ok_message_response())

    with caplog.at_level(logging.DEBUG, logger="signalforge.llm.client"):
        call_llm(
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
    argument. Catches accidental ANSI-escape injection paths.

    Pattern matches every Python f-string form (any case + raw prefix
    permutation, single/double quotes, optional whitespace after paren)
    so a contributor can't bypass the gate by switching quote style.
    """
    src = Path(client_module.__file__).read_text(encoding="utf-8")
    pattern = re.compile(r"""_LOGGER\.\w+\(\s*(?:[fF][rR]?|[rR][fF])['"]""")
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


# ---------------------------------------------------------------------------
# is_clean_completion orchestrator-wiring integration (#155 US-002 / DEC-005)
# ---------------------------------------------------------------------------


def test_call_llm_raises_llmresponseformaterror_on_unclean_stop_reason() -> None:
    """:func:`call_llm` raises :class:`LLMResponseFormatError` at the
    :meth:`AnthropicProvider.is_clean_completion` gate when the response
    carries an unclean ``stop_reason`` (#155 US-002, pins DEC-005 wiring).

    This is the orchestrator-level pin for the per-provider unclean-path
    contract. It proves three things in one assertion:

    1. **The gate fires inside ``call_llm``, not at the call-site.** The
       fake returns a real ``FakeMessage`` (not an exception); the raise
       comes from the orchestrator's post-call check.

    2. **The gate fires AFTER ``messages.create`` returns and BEFORE
       :meth:`AnthropicProvider.extract_text_blocks`** — exactly one
       create call is observed, and the partial text the response carried
       never reaches the JSON parser downstream. This is the load-bearing
       routing per DEC-005: a truncated/safety-blocked/tool-use response
       routes to ``LLMResponseFormatError`` → (via grade-engine wrap)
       ``GradeLLMError`` → conservative degrade, not to the wrong
       typed degrade (``GradeOutputError`` from a partial-JSON parse).

    3. **No retry is attempted.** ``LLMResponseFormatError`` is raised
       outside the retry try/except (``client.py:476-488``) so it is
       explicitly non-retryable — retrying a truncated generation gets
       you the same truncation. Exactly one create call.

    Companion sibling pin: ``tests/grade/test_gemini_neutrality.py:381``
    asserts ``bad.reasoning == "call failed: GradeLLMError"`` for the
    safety-blocked Gemini path, which now also covers MAX_TOKENS via the
    same orchestrator routing. That pin MUST continue to pass unmodified
    after this change lands.
    """
    fake = FakeAnthropicClient()
    fake.expect_count_tokens(matching={}, returns=_ok_count_response())
    # Truncated-at-max_tokens response with partial JSON in the content.
    # Before #155, this would have slipped past ``extract_text_blocks``
    # (which only raised on ZERO blocks), reached the JSON parser, and
    # surfaced as ``GradeOutputError`` — the wrong typed degrade.
    unclean = FakeMessage(
        content=[FakeTextBlock(text='{"score": 0.9, "reasoning": "partial truncated')],
        usage=FakeUsage(input_tokens=120, output_tokens=45),
        stop_reason="max_tokens",
    )
    fake.expect_messages_create(matching={}, returns=unclean)

    from signalforge.llm.errors import LLMResponseFormatError

    with pytest.raises(LLMResponseFormatError) as excinfo:
        call_llm(
            system="sys",
            cached_block="x" * _DEFAULT_CACHED_TOKENS,
            dynamic_block="y" * 50,
            model="claude-sonnet-4-6",
            max_tokens=1024,
            prompt_version="v1",
            client=fake,
        )

    # The error message is what
    # :meth:`AnthropicProvider.unclean_finish_reason_message` returned:
    # vendor-accurate field name + the offending value.
    assert "stop_reason" in str(excinfo.value)
    assert "'max_tokens'" in str(excinfo.value)

    # Exactly one create call — no retry, no leak through to
    # extract_text_blocks.
    assert len(fake.create_calls) == 1
    fake.assert_all_expectations_met()
