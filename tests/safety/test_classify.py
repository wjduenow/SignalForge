"""Classification matrix for ``_classify_column`` (US-008).

Operationalises DEC-003 (the four opt-out signals + pattern), DEC-020
(case-insensitivity + suspicious-column heuristic), and DEC-024
(``RedactionReason`` ``Literal``).

Hand-constructed ``Column`` / ``Model`` instances cover the matrix because
parametrisation is easier than building a manifest fixture for every cell.
The end-of-file ``test_classify_with_fixture_manifest`` then exercises the
real loader against ``tests/fixtures/safety/manifest_with_pii_meta.json``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from signalforge.manifest.loader import load
from signalforge.manifest.models import Column, Config, Model
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.redact import _classify_column, hash_column_name

pytestmark = pytest.mark.safety


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_column(
    name: str,
    *,
    tags: tuple[str, ...] = (),
    meta: dict | None = None,
) -> Column:
    return Column(name=name, tags=list(tags), meta=meta or {})


def _make_model(
    *,
    unique_id: str = "model.test.x",
    tags: tuple[str, ...] = (),
    meta: dict | None = None,
    columns: dict[str, Column] | None = None,
) -> Model:
    # ``Model`` has no top-level ``meta`` field — meta lives in ``config.meta``.
    # ``tags`` is top-level, so we set it both at the top and on the
    # config (mirroring what dbt actually serialises).
    return Model(
        unique_id=unique_id,
        name="x",
        resource_type="model",
        package_name="test",
        original_file_path="models/x.sql",
        path="x.sql",
        tags=list(tags),
        config=Config(materialized="table", tags=list(tags), meta=meta or {}),
        columns=columns or {},
        raw_code="select 1",
    )


def _default_policy() -> SafetyPolicy:
    return SafetyPolicy()


# ---------------------------------------------------------------------------
# No-signal baseline
# ---------------------------------------------------------------------------


def test_classify_no_signal_returns_none() -> None:
    column = _make_column("created_at")
    model = _make_model(columns={"created_at": column})
    assert _classify_column(column, model, _default_policy()) is None


# ---------------------------------------------------------------------------
# Column-level signals
# ---------------------------------------------------------------------------


def test_classify_column_meta_signalforge_sample_false_redacts() -> None:
    column = _make_column("anything", meta={"signalforge": {"sample": False}})
    model = _make_model(columns={"anything": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.redacted is True
    assert record.reason == "column_meta_optout"
    assert record.column_name == "anything"
    assert record.hashed_name == hash_column_name("anything")


def test_classify_column_tag_pii_redacts() -> None:
    column = _make_column("anything", tags=("pii",))
    model = _make_model(columns={"anything": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "tag_pii_column"
    assert record.redacted is True


def test_classify_column_tag_pii_case_insensitive() -> None:
    column = _make_column("anything", tags=("PII",))
    model = _make_model(columns={"anything": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "tag_pii_column"


def test_classify_column_meta_contains_pii_true_redacts() -> None:
    column = _make_column("anything", meta={"contains_pii": True})
    model = _make_model(columns={"anything": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "meta_contains_pii_column"


def test_classify_column_meta_contains_pii_truthy_string_redacts() -> None:
    column = _make_column("anything", meta={"contains_pii": "yes"})
    model = _make_model(columns={"anything": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "meta_contains_pii_column"


def test_classify_column_meta_contains_pii_truthy_int_redacts() -> None:
    column = _make_column("anything", meta={"contains_pii": 1})
    model = _make_model(columns={"anything": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "meta_contains_pii_column"


def test_classify_column_meta_contains_pii_falsy_zero_does_not_redact() -> None:
    # 0 is falsy; should not trigger meta_contains_pii_column. Name is
    # non-suspicious + does not match a default pattern, so the call
    # returns None.
    column = _make_column("created_at", meta={"contains_pii": 0})
    model = _make_model(columns={"created_at": column})
    assert _classify_column(column, model, _default_policy()) is None


def test_classify_column_meta_contains_pii_empty_string_does_not_redact() -> None:
    column = _make_column("created_at", meta={"contains_pii": ""})
    model = _make_model(columns={"created_at": column})
    assert _classify_column(column, model, _default_policy()) is None


# ---------------------------------------------------------------------------
# Model-level signals
# ---------------------------------------------------------------------------


def test_classify_model_meta_optout_redacts_when_column_has_no_signal() -> None:
    column = _make_column("created_at")
    model = _make_model(
        columns={"created_at": column},
        meta={"signalforge": {"sample": False}},
    )
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "model_meta_optout"


def test_classify_model_tag_pii_redacts() -> None:
    column = _make_column("created_at")
    model = _make_model(columns={"created_at": column}, tags=("pii",))
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "tag_pii_model"


def test_classify_model_tag_pii_case_insensitive() -> None:
    column = _make_column("created_at")
    model = _make_model(columns={"created_at": column}, tags=("Pii",))
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "tag_pii_model"


def test_classify_model_meta_contains_pii_true_redacts() -> None:
    column = _make_column("created_at")
    model = _make_model(columns={"created_at": column}, meta={"contains_pii": True})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "meta_contains_pii_model"


# ---------------------------------------------------------------------------
# Pattern match
# ---------------------------------------------------------------------------


def test_classify_pattern_match_redacts() -> None:
    column = _make_column("customer_email")
    model = _make_model(columns={"customer_email": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "pattern_match"


def test_classify_pattern_match_case_insensitive() -> None:
    column = _make_column("CUSTOMER_EMAIL")
    model = _make_model(columns={"CUSTOMER_EMAIL": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "pattern_match"


def test_classify_pattern_match_uppercase_pattern_against_lowercase_name() -> None:
    # Even when the policy carries an uppercase pattern, the matcher
    # lowercases both sides before comparing.
    policy = SafetyPolicy(redact_patterns=("*EMAIL",))
    column = _make_column("customer_email")
    model = _make_model(columns={"customer_email": column})
    record = _classify_column(column, model, policy)
    assert record is not None
    assert record.reason == "pattern_match"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_classify_precedence_column_over_model_meta_optout() -> None:
    # Both column and model carry meta sample=False; column wins.
    column = _make_column("anything", meta={"signalforge": {"sample": False}})
    model = _make_model(
        columns={"anything": column},
        meta={"signalforge": {"sample": False}},
    )
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "column_meta_optout"


def test_classify_precedence_column_tag_over_model_tag() -> None:
    column = _make_column("anything", tags=("pii",))
    model = _make_model(columns={"anything": column}, tags=("pii",))
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "tag_pii_column"


def test_classify_precedence_signals_over_pattern_match() -> None:
    # Column matches *email AND has a meta opt-out; meta wins.
    column = _make_column("customer_email", meta={"signalforge": {"sample": False}})
    model = _make_model(columns={"customer_email": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "column_meta_optout"


def test_classify_precedence_column_meta_over_column_tag() -> None:
    # First-match-wins: column meta sample=False fires before tag pii.
    column = _make_column(
        "anything",
        tags=("pii",),
        meta={"signalforge": {"sample": False}},
    )
    model = _make_model(columns={"anything": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "column_meta_optout"


# ---------------------------------------------------------------------------
# RedactionRecord shape
# ---------------------------------------------------------------------------


def test_classify_returns_redaction_record_with_correct_hashed_name() -> None:
    column = _make_column("customer_email")
    model = _make_model(columns={"customer_email": column})
    record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.hashed_name == hash_column_name(record.column_name)


# ---------------------------------------------------------------------------
# Suspicious-substring WARNING heuristic (DEC-020)
# ---------------------------------------------------------------------------


def test_classify_suspicious_unmatched_column_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # "customer_token" matches the suspicious substring "token" but is NOT
    # matched by any default redact pattern — so we expect a WARNING and
    # _classify_column returns None.
    column = _make_column("customer_token")
    model = _make_model(columns={"customer_token": column})
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        result = _classify_column(column, model, _default_policy())
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "customer_token" in warnings[0].getMessage()


def test_classify_redacted_suspicious_column_no_extra_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # "contact_email_addr" has "email" substring AND matches *email — but it
    # is redacted by the pattern, so no warning should fire.
    column = _make_column("user_email")
    model = _make_model(columns={"user_email": column})
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        record = _classify_column(column, model, _default_policy())
    assert record is not None
    assert record.reason == "pattern_match"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_classify_non_suspicious_unmatched_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    column = _make_column("created_at")
    model = _make_model(columns={"created_at": column})
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        result = _classify_column(column, model, _default_policy())
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


# ---------------------------------------------------------------------------
# Real-fixture round trip
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "safety" / "manifest_with_pii_meta.json"


def test_classify_with_fixture_manifest() -> None:
    """End-to-end: load the safety fixture, classify each documented column."""
    project_dir = _FIXTURE_PATH.parent
    manifest = load(project_dir, manifest_path=_FIXTURE_PATH)
    policy = _default_policy()

    customers = manifest.get_model("model.sf_demo.customers")

    # id: no signal, no pattern, not suspicious → None.
    assert _classify_column(customers.columns["id"], customers, policy) is None

    # email: matches *email pattern.
    rec = _classify_column(customers.columns["email"], customers, policy)
    assert rec is not None
    assert rec.reason == "pattern_match"

    # customer_ssn_optout: column meta signalforge.sample=False (also
    # matches *ssn — meta should win).
    rec = _classify_column(customers.columns["customer_ssn_optout"], customers, policy)
    assert rec is not None
    assert rec.reason == "column_meta_optout"

    # taxpayer_id: column tag pii.
    rec = _classify_column(customers.columns["taxpayer_id"], customers, policy)
    assert rec is not None
    assert rec.reason == "tag_pii_column"

    # birth_date: column meta contains_pii=True.
    rec = _classify_column(customers.columns["birth_date"], customers, policy)
    assert rec is not None
    assert rec.reason == "meta_contains_pii_column"

    # orders_pii_at_model.order_id: model carries tag pii.
    orders = manifest.get_model("model.sf_demo.orders_pii_at_model")
    rec = _classify_column(orders.columns["order_id"], orders, policy)
    assert rec is not None
    assert rec.reason == "tag_pii_model"
