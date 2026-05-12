"""Tests for :func:`signalforge.safety.request.build_llm_request` (US-010).

The request builder is the single entry point that ties together every piece
of the safety layer. These tests cover:

* Per-mode dispatch (schema-only / aggregate-only / sample) and DEC-012(c)'s
  zero-adapter-calls invariant for schema-only.
* DEC-010 column-name hashing — both in the ``schema`` field handed to the
  LLM and in the keys of ``sampled_rows`` for sample mode.
* DEC-014 ``AuditEvent`` reproducibility — every audit row carries
  ``signalforge_version``, ``policy_hash``, ``audit_schema_version``, and
  ``policy_flags``.
* DEC-011 fail-closed semantics — any exception from ``audit.write``
  propagates and the partial :class:`LLMRequest` is dropped.
* DEC-022 transitive immutability — every sequence on the returned
  :class:`LLMRequest` is a :class:`tuple`, not a :class:`list`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import signalforge
from signalforge.manifest.loader import load
from signalforge.manifest.models import Model
from signalforge.safety import audit
from signalforge.safety.errors import AuditWriteError
from signalforge.safety.models import AuditEvent, LLMRequest, SamplingMode
from signalforge.safety.policy import SafetyPolicy, _compute_policy_hash
from signalforge.safety.redact import hash_column_name
from signalforge.safety.request import build_llm_request
from signalforge.warehouse.models import ColumnStats, TableRef
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.safety


_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "safety" / "manifest_with_pii_meta.json"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def customers_model() -> Model:
    """The ``model.sf_demo.customers`` model from the safety fixture manifest.

    Five columns: ``id`` (no signal), ``email`` (pattern), ``customer_ssn_optout``
    (column meta opt-out), ``taxpayer_id`` (PII tag), ``birth_date``
    (``meta.contains_pii``). Four redacted, one passes through.
    """
    manifest = load(_FIXTURE.parent, manifest_path=_FIXTURE)
    return manifest.get_model("model.sf_demo.customers")


def _stats(*, count: int = 100, distinct: int = 80, nulls: int = 5) -> ColumnStats:
    return ColumnStats(
        count=count,
        distinct=distinct,
        nulls=nulls,
        min=0,
        max=999,
        data_type="INT64",
    )


def _policy(tmp_path: Path, **overrides: Any) -> SafetyPolicy:
    """Construct a :class:`SafetyPolicy` with a tmp-path audit file.

    Direct construction skips ``load_safety_config``'s ``audit_path`` sanity
    gate, which is fine for tests that need an absolute path.
    """
    base: dict[str, Any] = {"audit_path": tmp_path / "audit.jsonl"}
    base.update(overrides)
    return SafetyPolicy(**base)


class _AuditRecorder:
    """Hand-rolled ``audit.write`` wrapper that records calls.

    No ``MagicMock`` here per task instructions — a plain class with an
    explicit ``__call__`` is what tests want, and it composes cleanly with
    ``monkeypatch.setattr``.
    """

    def __init__(self, *, raise_on_call: BaseException | None = None) -> None:
        self.calls: list[tuple[AuditEvent, Path]] = []
        self._raise = raise_on_call

    def __call__(self, event: AuditEvent, audit_path: Path) -> None:
        self.calls.append((event, audit_path))
        if self._raise is not None:
            raise self._raise


# ---------------------------------------------------------------------------
# Schema-only mode
# ---------------------------------------------------------------------------


def test_build_llm_request_schema_only_zero_warehouse_calls(
    customers_model: Model, tmp_path: Path
) -> None:
    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    request = build_llm_request(customers_model, fake, policy)

    fake.assert_all_expectations_met()
    assert fake.enter_count == 0
    assert request.mode is SamplingMode.SCHEMA_ONLY
    assert request.sampled_rows is None
    assert request.aggregates is None


def test_build_llm_request_schema_only_returns_columns_sent_with_hashed_for_redacted(
    customers_model: Model, tmp_path: Path
) -> None:
    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    request = build_llm_request(customers_model, fake, policy)

    # The PII columns must be hashed, the safe column must remain.
    assert "id" in request.columns_sent
    assert "customer_ssn_optout" not in request.columns_sent
    assert "email" not in request.columns_sent
    assert "taxpayer_id" not in request.columns_sent
    assert "birth_date" not in request.columns_sent
    assert hash_column_name("customer_ssn_optout") in request.columns_sent
    assert hash_column_name("email") in request.columns_sent


def test_build_llm_request_schema_only_schema_field_pairs_hashed_with_type(
    customers_model: Model, tmp_path: Path
) -> None:
    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    request = build_llm_request(customers_model, fake, policy)

    schema_dict = dict(request.schema)
    # Hashed names appear for redacted columns; types are pulled from the
    # manifest column.data_type (None becomes "" — the columns in the fixture
    # have no data_type set).
    assert hash_column_name("email") in schema_dict
    assert "id" in schema_dict


# ---------------------------------------------------------------------------
# Aggregate-only mode
# ---------------------------------------------------------------------------


def test_build_llm_request_aggregate_only_calls_column_stats_per_non_redacted_column(
    customers_model: Model, tmp_path: Path
) -> None:
    table = TableRef.from_model(customers_model)
    fake = FakeAdapter()
    fake.expect_column_stats(table=table, column="id", returns=_stats())
    policy = _policy(tmp_path, mode=SamplingMode.AGGREGATE_ONLY)

    request = build_llm_request(customers_model, fake, policy)

    fake.assert_all_expectations_met()
    assert request.sampled_rows is None
    assert request.aggregates is not None
    # aggregates is tuple[tuple[name, stats], ...] — convert to dict for the
    # membership checks. The tuple shape (vs. dict) is the DEC-022 immutability
    # guarantee: downstream consumers can't mutate values post-audit.
    aggregates_by_name = dict(request.aggregates)
    assert "id" in aggregates_by_name
    assert isinstance(aggregates_by_name["id"], ColumnStats)
    assert aggregates_by_name[hash_column_name("email")] is None


def test_build_llm_request_aggregate_only_no_sample_rows_calls(
    customers_model: Model, tmp_path: Path
) -> None:
    table = TableRef.from_model(customers_model)
    fake = FakeAdapter()
    fake.expect_column_stats(table=table, column="id", returns=_stats())
    policy = _policy(tmp_path, mode=SamplingMode.AGGREGATE_ONLY)

    # No sample_rows expectations queued — any call raises.
    build_llm_request(customers_model, fake, policy)
    fake.assert_all_expectations_met()


# ---------------------------------------------------------------------------
# Sample mode
# ---------------------------------------------------------------------------


def _sample_rows() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "email": "alice@example.com",
            "customer_ssn_optout": "111-11-1111",
            "taxpayer_id": "12345",
            "birth_date": "1990-01-01",
        },
        {
            "id": 2,
            "email": "bob@example.com",
            "customer_ssn_optout": "222-22-2222",
            "taxpayer_id": "67890",
            "birth_date": "1985-06-15",
        },
    ]


def test_build_llm_request_sample_calls_sample_rows(customers_model: Model, tmp_path: Path) -> None:
    table = TableRef.from_model(customers_model)
    fake = FakeAdapter()
    fake.expect_sample_rows(table=table, n=100, returns=_sample_rows())
    policy = _policy(tmp_path, mode=SamplingMode.SAMPLE)

    request = build_llm_request(customers_model, fake, policy)

    fake.assert_all_expectations_met()
    assert request.sampled_rows is not None
    assert len(request.sampled_rows) == 2


def test_build_llm_request_sample_redacts_values_to_redacted_constant(
    customers_model: Model, tmp_path: Path
) -> None:
    table = TableRef.from_model(customers_model)
    fake = FakeAdapter()
    fake.expect_sample_rows(table=table, n=100, returns=_sample_rows())
    policy = _policy(tmp_path, mode=SamplingMode.SAMPLE)

    request = build_llm_request(customers_model, fake, policy)

    assert request.sampled_rows is not None
    first = request.sampled_rows[0]
    # ``id`` not redacted; everything else is.
    assert first["id"] == 1
    assert first[hash_column_name("email")] == "<REDACTED>"
    assert first[hash_column_name("taxpayer_id")] == "<REDACTED>"


def test_build_llm_request_sample_redacts_names_in_rows_to_hashed(
    customers_model: Model, tmp_path: Path
) -> None:
    table = TableRef.from_model(customers_model)
    fake = FakeAdapter()
    fake.expect_sample_rows(table=table, n=100, returns=_sample_rows())
    policy = _policy(tmp_path, mode=SamplingMode.SAMPLE)

    request = build_llm_request(customers_model, fake, policy)

    assert request.sampled_rows is not None
    first = request.sampled_rows[0]
    # Redacted real names absent; hashed names present.
    assert "email" not in first
    assert "customer_ssn_optout" not in first
    assert "taxpayer_id" not in first
    assert "birth_date" not in first
    assert hash_column_name("email") in first
    assert hash_column_name("customer_ssn_optout") in first
    assert hash_column_name("taxpayer_id") in first
    assert hash_column_name("birth_date") in first


# ---------------------------------------------------------------------------
# Audit-event content + write semantics
# ---------------------------------------------------------------------------


def test_build_llm_request_audit_emitted_exactly_once(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)
    build_llm_request(customers_model, fake, policy)

    assert len(rec.calls) == 1


def test_build_llm_request_audit_carries_signalforge_version(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)
    build_llm_request(customers_model, fake, policy)

    event, _ = rec.calls[0]
    assert event.signalforge_version == signalforge.__version__


def test_build_llm_request_audit_carries_policy_hash(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)
    build_llm_request(customers_model, fake, policy)

    event, _ = rec.calls[0]
    assert event.policy_hash == _compute_policy_hash(policy)


def test_build_llm_request_audit_carries_schema_version_1(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)
    build_llm_request(customers_model, fake, policy)

    event, _ = rec.calls[0]
    assert event.audit_schema_version == 2


def test_build_llm_request_audit_policy_flags_sample_mode_enabled(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    table = TableRef.from_model(customers_model)
    fake = FakeAdapter()
    fake.expect_sample_rows(table=table, n=100, returns=_sample_rows())
    policy = _policy(tmp_path, mode=SamplingMode.SAMPLE)
    build_llm_request(customers_model, fake, policy)

    event, _ = rec.calls[0]
    assert "sample_mode_enabled" in event.policy_flags


def test_build_llm_request_audit_policy_flags_redaction_disabled(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    # ``redact: {replace: []}`` resolves to an empty pattern tuple.
    policy = SafetyPolicy(
        mode=SamplingMode.SCHEMA_ONLY,
        redact_patterns=(),
        audit_path=tmp_path / "audit.jsonl",
    )
    build_llm_request(customers_model, fake, policy)

    event, _ = rec.calls[0]
    assert "redaction_disabled" in event.policy_flags


def test_build_llm_request_audit_policy_flags_audit_path_overridden(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)
    build_llm_request(customers_model, fake, policy)

    event, _ = rec.calls[0]
    assert "audit_path_overridden" in event.policy_flags


def test_build_llm_request_audit_policy_flags_default_no_flags(
    customers_model: Model, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _AuditRecorder()
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    # Construct directly to keep the default ``audit_path``.
    policy = SafetyPolicy()
    build_llm_request(customers_model, fake, policy)

    event, _ = rec.calls[0]
    assert event.policy_flags == ()


# ---------------------------------------------------------------------------
# Fail-closed audit (DEC-011)
# ---------------------------------------------------------------------------


def test_build_llm_request_audit_write_failure_raises_audit_write_error_no_request_returned(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-011 — an ``audit.write`` failure aborts the call.

    The partial :class:`LLMRequest` must never escape; the function must
    raise rather than return.
    """
    boom = AuditWriteError(path=tmp_path / "audit.jsonl", cause=OSError("disk full"))
    rec = _AuditRecorder(raise_on_call=boom)
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    with pytest.raises(AuditWriteError):
        build_llm_request(customers_model, fake, policy)


def test_build_llm_request_wraps_raw_oserror_as_audit_write_error(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #38 — the writer propagates raw exceptions; the orchestrator
    owns the typed wrap.

    Mirrors ``draft_from_request``'s wrap shape: a raw ``OSError`` from the
    writer surfaces to the caller as ``AuditWriteError`` with ``cause`` set.
    """
    rec = _AuditRecorder(raise_on_call=OSError("simulated write failure"))
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    with pytest.raises(AuditWriteError) as excinfo:
        build_llm_request(customers_model, fake, policy)
    assert isinstance(excinfo.value.cause, OSError)
    assert "simulated write failure" in str(excinfo.value.cause)


def test_build_llm_request_wraps_serialisation_failure_as_audit_write_error(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``TypeError`` from ``json.dumps`` inside the writer wraps as
    ``AuditWriteError`` at the orchestrator boundary (issue #38).
    """
    rec = _AuditRecorder(raise_on_call=TypeError("simulated json failure"))
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    with pytest.raises(AuditWriteError) as excinfo:
        build_llm_request(customers_model, fake, policy)
    assert isinstance(excinfo.value.cause, TypeError)


def test_build_llm_request_audit_record_too_large_propagates_typed(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AuditRecordTooLargeError`` is already typed and must propagate
    as-is — the orchestrator's blanket-except clause must NOT rewrap it.
    """
    from signalforge.safety.errors import AuditRecordTooLargeError

    boom = AuditRecordTooLargeError(size=5000, limit=4000)
    rec = _AuditRecorder(raise_on_call=boom)
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    with pytest.raises(AuditRecordTooLargeError) as excinfo:
        build_llm_request(customers_model, fake, policy)
    # Same instance — not wrapped.
    assert excinfo.value is boom


def test_build_llm_request_keyboard_interrupt_propagates_unwrapped(
    customers_model: Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signal-shaped exits (``KeyboardInterrupt`` / ``SystemExit``) must
    propagate untouched — wrapping them as ``AuditWriteError`` would
    silently demote a Ctrl-C into an audit error and bury the user's
    intent. Mirrors the same clause in ``draft_from_request``.
    """
    rec = _AuditRecorder(raise_on_call=KeyboardInterrupt())
    monkeypatch.setattr("signalforge.safety.request.audit.write", rec)

    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    with pytest.raises(KeyboardInterrupt):
        build_llm_request(customers_model, fake, policy)


# ---------------------------------------------------------------------------
# Immutability (DEC-022) and shape invariants
# ---------------------------------------------------------------------------


def test_build_llm_request_returned_request_is_transitively_immutable(
    customers_model: Model, tmp_path: Path
) -> None:
    table = TableRef.from_model(customers_model)
    fake = FakeAdapter()
    fake.expect_sample_rows(table=table, n=100, returns=_sample_rows())
    policy = _policy(tmp_path, mode=SamplingMode.SAMPLE)

    request = build_llm_request(customers_model, fake, policy)

    assert request.columns_sent.__class__ is tuple
    assert request.redactions.__class__ is tuple
    assert request.schema.__class__ is tuple
    assert request.sampled_rows is not None
    assert request.sampled_rows.__class__ is tuple


def test_build_llm_request_redactions_match_classifications(
    customers_model: Model, tmp_path: Path
) -> None:
    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    request = build_llm_request(customers_model, fake, policy)

    redacted_real = {r.column_name for r in request.redactions}
    assert redacted_real == {
        "email",
        "customer_ssn_optout",
        "taxpayer_id",
        "birth_date",
    }
    reasons = {r.column_name: r.reason for r in request.redactions}
    assert reasons["customer_ssn_optout"] == "column_meta_optout"
    assert reasons["taxpayer_id"] == "tag_pii_column"
    assert reasons["birth_date"] == "meta_contains_pii_column"
    assert reasons["email"] == "pattern_match"


def test_build_llm_request_columns_sent_count_matches_columns_in_model(
    customers_model: Model, tmp_path: Path
) -> None:
    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    request = build_llm_request(customers_model, fake, policy)

    assert len(request.columns_sent) == len(customers_model.columns)
    # Sanity: schema field has the same arity.
    assert len(request.schema) == len(customers_model.columns)


def test_build_llm_request_returns_llmrequest_instance(
    customers_model: Model, tmp_path: Path
) -> None:
    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    request = build_llm_request(customers_model, fake, policy)

    assert isinstance(request, LLMRequest)
    assert request.model_unique_id == customers_model.unique_id


def test_build_llm_request_writes_audit_to_disk_under_default_path(
    customers_model: Model, tmp_path: Path
) -> None:
    """End-to-end: with no ``audit.write`` patch, the JSONL file is created."""
    fake = FakeAdapter()
    policy = _policy(tmp_path, mode=SamplingMode.SCHEMA_ONLY)

    build_llm_request(customers_model, fake, policy)

    audit_file = tmp_path / "audit.jsonl"
    assert audit_file.exists()
    assert audit_file.read_text(encoding="utf-8").count("\n") == 1


# Re-export so the import-only ``audit`` reference does not get culled by lint.
_ = audit
