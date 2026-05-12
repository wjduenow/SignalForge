"""Skip-draft column signal tests (issue #54).

Exercises the two new RedactionReason literals (``draft_skip_column_meta``,
``draft_skip_model_meta``) added to :mod:`signalforge.safety.redact` and
the LLM-payload filtering they drive in
:func:`signalforge.safety.request.build_llm_request`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.manifest.models import Column, Config, Model
from signalforge.safety.models import DRAFT_SKIP_REASONS, SamplingMode
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.redact import _classify_column
from signalforge.safety.request import build_llm_request
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.safety


def _make_column(
    name: str,
    *,
    tags: tuple[str, ...] = (),
    meta: dict | None = None,
    data_type: str = "STRING",
) -> Column:
    return Column(name=name, tags=list(tags), meta=meta or {}, data_type=data_type)


def _make_model(
    *,
    columns: dict[str, Column],
    tags: tuple[str, ...] = (),
    meta: dict | None = None,
) -> Model:
    return Model(
        unique_id="model.sf_demo.orders",
        name="orders",
        resource_type="model",
        package_name="sf_demo",
        original_file_path="models/orders.sql",
        path="orders.sql",
        tags=list(tags),
        config=Config(materialized="table", tags=list(tags), meta=meta or {}),
        columns=columns,
        raw_code="select 1 as id",
    )


def _policy(tmp_path: Path) -> SafetyPolicy:
    return SafetyPolicy(
        mode=SamplingMode.SCHEMA_ONLY,
        audit_path=tmp_path / ".signalforge" / "audit.jsonl",
    )


# ---------------------------------------------------------------------------
# Column-level skip_draft
# ---------------------------------------------------------------------------


def test_classify_column_level_skip_draft_returns_draft_skip_column_meta(
    tmp_path: Path,
) -> None:
    column = _make_column("internal_token", meta={"signalforge": {"skip_draft": True}})
    model = _make_model(columns={"internal_token": column})
    rec = _classify_column(column, model, _policy(tmp_path))
    assert rec is not None
    assert rec.reason == "draft_skip_column_meta"
    assert rec.column_name == "internal_token"
    assert rec.redacted is True


def test_classify_model_level_skip_draft_returns_draft_skip_model_meta(
    tmp_path: Path,
) -> None:
    column = _make_column("id")
    model = _make_model(
        columns={"id": column},
        meta={"signalforge": {"skip_draft": True}},
    )
    rec = _classify_column(column, model, _policy(tmp_path))
    assert rec is not None
    assert rec.reason == "draft_skip_model_meta"


def test_classify_column_skip_wins_over_model_skip(tmp_path: Path) -> None:
    """Most-specific source wins for the audit reason."""
    column = _make_column("x", meta={"signalforge": {"skip_draft": True}})
    model = _make_model(
        columns={"x": column},
        meta={"signalforge": {"skip_draft": True}},
    )
    rec = _classify_column(column, model, _policy(tmp_path))
    assert rec is not None
    assert rec.reason == "draft_skip_column_meta"


def test_classify_skip_draft_wins_over_pii_signals(tmp_path: Path) -> None:
    """A column tagged 'pii' AND skip_draft routes to skip, not PII."""
    column = _make_column(
        "customer_email",
        tags=("pii",),
        meta={"signalforge": {"skip_draft": True}, "contains_pii": True},
    )
    model = _make_model(columns={"customer_email": column})
    rec = _classify_column(column, model, _policy(tmp_path))
    assert rec is not None
    assert rec.reason == "draft_skip_column_meta"


@pytest.mark.parametrize("value", [False, None, 0, "", "true", "yes", 1])
def test_classify_skip_draft_only_honours_explicit_true(
    tmp_path: Path, value: object
) -> None:
    """Non-True values do NOT enable skip_draft (strict ``is True`` check).

    Mirrors the existing ``sample is False`` shape — config noise must
    not silently engage a security-adjacent behaviour.
    """
    column = _make_column("benign_col", meta={"signalforge": {"skip_draft": value}})
    model = _make_model(columns={"benign_col": column})
    rec = _classify_column(column, model, _policy(tmp_path))
    assert rec is None


# ---------------------------------------------------------------------------
# Integration: build_llm_request filters skipped columns
# ---------------------------------------------------------------------------


def test_build_llm_request_omits_skipped_columns_from_payload(tmp_path: Path) -> None:
    columns = {
        "id": _make_column("id"),
        "internal_token": _make_column(
            "internal_token", meta={"signalforge": {"skip_draft": True}}
        ),
        "amount": _make_column("amount", data_type="NUMERIC"),
    }
    model = _make_model(columns=columns)
    fake = FakeAdapter()
    policy = _policy(tmp_path)

    request = build_llm_request(model, fake, policy)

    # The skipped column does NOT appear in any LLM-facing payload.
    assert "internal_token" not in request.columns_sent
    schema_names = [name for name, _ in request.schema]
    assert "internal_token" not in schema_names
    # The non-skipped columns appear with their real names.
    assert set(request.columns_sent) == {"id", "amount"}
    # But the audit RedactionRecord IS present for traceability.
    skipped_records = [r for r in request.redactions if r.reason in DRAFT_SKIP_REASONS]
    assert len(skipped_records) == 1
    assert skipped_records[0].column_name == "internal_token"
    assert skipped_records[0].reason == "draft_skip_column_meta"


def test_build_llm_request_model_level_skip_omits_every_column(tmp_path: Path) -> None:
    columns = {
        "a": _make_column("a"),
        "b": _make_column("b"),
    }
    model = _make_model(
        columns=columns,
        meta={"signalforge": {"skip_draft": True}},
    )
    fake = FakeAdapter()
    policy = _policy(tmp_path)

    request = build_llm_request(model, fake, policy)

    assert request.columns_sent == ()
    assert request.schema == ()
    skipped_records = [r for r in request.redactions if r.reason in DRAFT_SKIP_REASONS]
    assert len(skipped_records) == 2
    assert all(r.reason == "draft_skip_model_meta" for r in skipped_records)
    assert {r.column_name for r in skipped_records} == {"a", "b"}


def test_draft_skip_reasons_constant_matches_literal_values() -> None:
    """``DRAFT_SKIP_REASONS`` must enumerate exactly the new reasons —
    expanding RedactionReason without updating the constant would silently
    leak draft-skip columns into the LLM payload."""
    assert DRAFT_SKIP_REASONS == frozenset(
        {"draft_skip_column_meta", "draft_skip_model_meta"}
    )
