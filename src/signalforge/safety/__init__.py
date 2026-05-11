"""SignalForge PII safety layer.

Library-only safety primitives sitting between the warehouse adapter and the
LLM-drafting layer. Three sampling modes (`schema-only` (default),
`aggregate-only`, `sample`) plus column redaction (per-column dbt meta/tags +
configurable name-pattern matching) plus a fail-closed audit log.

Public API (v0.1):
    - SamplingMode, RedactionRecord, AuditEvent, LLMRequest — typed shapes
    - SafetyPolicy — user-facing config
    - load_safety_config — config-file loader
    - build_llm_request — single entry that produces an LLMRequest and writes
      the audit record (DEC-009)
    - aggregate_columns, redact_rows — composable helpers
    - SafetyError + 9 subclasses — typed exception hierarchy

Construct LLMRequest only via build_llm_request — direct construction bypasses
the audit log and is asserted against by tests/safety/test_public_api.py.
"""

from __future__ import annotations

from signalforge.safety.aggregate import aggregate_columns
from signalforge.safety.config import load_safety_config
from signalforge.safety.errors import (
    AuditRecordTooLargeError,
    AuditWriteError,
    ColumnNotInModelError,
    ConfigNotFoundError,
    InvalidConfigError,
    InvalidPatternError,
    InvalidSamplingModeError,
    PolicyValidationError,
    SafetyError,
    UnknownConfigKeyError,
)
from signalforge.safety.models import (
    AuditEvent,
    LLMRequest,
    RedactionReason,
    RedactionRecord,
    SamplingMode,
)
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.redact import redact_rows
from signalforge.safety.request import build_llm_request

__all__ = [
    # Models
    "SamplingMode",
    "RedactionReason",
    "RedactionRecord",
    "AuditEvent",
    "LLMRequest",
    "SafetyPolicy",
    # Functions
    "load_safety_config",
    "build_llm_request",
    "aggregate_columns",
    "redact_rows",
    # Errors
    "SafetyError",
    "ConfigNotFoundError",
    "InvalidConfigError",
    "InvalidSamplingModeError",
    "InvalidPatternError",
    "ColumnNotInModelError",
    "AuditWriteError",
    "AuditRecordTooLargeError",
    "PolicyValidationError",
    "UnknownConfigKeyError",
]
