"""TDD-first behaviour tests for ``signalforge.llm.cost.rollup_audit_dir``
(issue #157 / US-002 of plans/super/157-e2e-cost-and-parallel.md).

The 15 locked test names below pin every AC from the bead. Each
happy-path test hand-constructs a tiny audit JSONL with known token
counts under ``tmp_path``, multiplies against the live
:data:`signalforge.llm.pricing.PRICES` table values **manually in the
test**, and asserts on the exact USD figure — engineered determinism per
``testing-signal.md``. The expected value is NOT recomputed at test time
via the same code path under test (would be a tautology); the
multiplications are spelled out using the public ``ModelPricing`` fields
read from :func:`lookup`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalforge.llm.cost import (
    CostReport,
    CostRollupAuditMissingError,
    CostRollupMalformedRecordError,
    CostRollupUnknownModelError,
    ModelRollup,
    ProviderRollup,
    rollup_audit_dir,
)
from signalforge.llm.pricing import PRICE_TABLE_VERSION, lookup

# ---------------------------------------------------------------------------
# Engineered-fixture helpers — hand-author audit JSONL lines with known
# token counts so per-record USD is hand-computable from the live
# pricing table without round-tripping through the code under test.
# ---------------------------------------------------------------------------


def _draft_record(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    model_unique_id: str = "model.cost.fixture",
    timestamp: str = "2026-05-29T00:00:00.000000Z",
) -> dict[str, object]:
    """Build one ``LLMResponseEvent``-shaped dict with every required field."""
    return {
        "timestamp": timestamp,
        "model_unique_id": model_unique_id,
        "prompt_version": "0000000000000000",
        "response_text_hash": "1111111111111111",
        "parsed_schema_hash": "2222222222222222",
        "sent_sql_hash": "3333333333333333",
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": model,
        "signalforge_version": "0.0.0.test",
        "audit_schema_version": 1,
    }


def _grade_record(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    artifact_id: str = "column.email.description",
    criterion_id: str = "clarity",
    timestamp: str = "2026-05-29T00:00:00.000000Z",
) -> dict[str, object]:
    """Build one ``GradeEvent``-shaped dict with every required field."""
    return {
        "audit_schema_version": 1,
        "signalforge_version": "0.0.0.test",
        "run_id": "00112233445566778899aabbccddeeff",
        "timestamp": timestamp,
        "model_unique_id": "model.cost.fixture",
        "artifact_id": artifact_id,
        "criterion_id": criterion_id,
        "score": 0.9,
        "passed": True,
        "evidence": "test evidence",
        "reasoning": "test reasoning",
        "rubric_hash": "4444444444444444",
        "prompt_version_template": "5555555555555555",
        "criterion_prompt_hash": "6666666666666666",
        "response_text_hash": "7777777777777777",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    """Write ``records`` as JSONL (one JSON dict per line) to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec))
            fh.write("\n")


def _make_project(tmp_path: Path) -> Path:
    """Create an empty SignalForge project dir under ``tmp_path``."""
    project = tmp_path / "project"
    project.mkdir()
    return project


def _audit_dir(project: Path) -> Path:
    """The canonical ``<project>/.signalforge/`` directory."""
    d = project / ".signalforge"
    d.mkdir(exist_ok=True)
    return d


def _expected_usd(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> float:
    """Compute the per-record expected USD using the live PRICES table.

    Used in tests to spell out the arithmetic; the rollup engine is the
    code under test, so we compose the formula from the public pricing
    fields rather than calling into the engine's internal calculator.
    """
    p = lookup(model)
    return (
        input_tokens * p.input_per_mtok
        + output_tokens * p.output_per_mtok
        + cache_creation * p.cache_write_5m_per_mtok
        + cache_read * p.cache_read_per_mtok
    ) / 1_000_000


# ---------------------------------------------------------------------------
# Acceptance criteria (15 locked test names from the bead).
# ---------------------------------------------------------------------------


def test_rollup_empty_project_raises_missing_audit_error(tmp_path: Path) -> None:
    """Neither JSONL present → ``CostRollupAuditMissingError`` (AC4)."""
    project = _make_project(tmp_path)
    # No .signalforge/ at all.
    with pytest.raises(CostRollupAuditMissingError):
        rollup_audit_dir(project)


def test_rollup_only_llm_responses_returns_degraded_report(tmp_path: Path) -> None:
    """Only drafter JSONL present → degraded report (AC5).

    ``audit_files_consumed`` reports only the file that existed. USD
    is still computed from the present file (Pass-2 F5 — defends the
    only-drafter code path against a regression that routed records
    to a zero-cost branch).
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="claude-sonnet-4-6",
                input_tokens=1000,
                output_tokens=500,
            )
        ],
    )

    report = rollup_audit_dir(project)

    assert isinstance(report, CostReport)
    assert report.audit_files_consumed == ("llm_responses.jsonl",)
    assert "anthropic" in report.per_provider
    # Hand-computed: (1000 × $3.00 + 500 × $15.00) / 1e6 = $0.0105.
    model = report.per_provider["anthropic"].per_model["claude-sonnet-4-6"]
    assert model.total_usd == pytest.approx(0.0105)
    assert report.total_usd == pytest.approx(0.0105)


def test_rollup_only_grade_returns_degraded_report(tmp_path: Path) -> None:
    """Only grader JSONL present → degraded report (AC5).

    Pass-2 F5: also pin USD so the only-grader code path can't
    regress to a zero-cost branch.
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [
            _grade_record(
                model="claude-sonnet-4-6",
                input_tokens=1000,
                output_tokens=500,
            )
        ],
    )

    report = rollup_audit_dir(project)

    assert isinstance(report, CostReport)
    assert report.audit_files_consumed == ("grade.jsonl",)
    assert "anthropic" in report.per_provider
    # Hand-computed: (1000 × $3.00 + 500 × $15.00) / 1e6 = $0.0105.
    model = report.per_provider["anthropic"].per_model["claude-sonnet-4-6"]
    assert model.total_usd == pytest.approx(0.0105)
    assert report.total_usd == pytest.approx(0.0105)


def test_rollup_both_jsonls_returns_full_report(tmp_path: Path) -> None:
    """Both JSONLs present → full report (AC3).

    ``audit_files_consumed`` carries both file names; per-model token
    totals sum across both files.
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="claude-sonnet-4-6",
                input_tokens=1000,
                output_tokens=2000,
            )
        ],
    )
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [
            _grade_record(
                model="claude-sonnet-4-6",
                input_tokens=500,
                output_tokens=100,
            )
        ],
    )

    report = rollup_audit_dir(project)

    # Tuple, not set — drafter-then-grader ordering is part of the contract
    # (Pass-2 F3). Downstream consumers can rely on this ordering for stable
    # rendering / serialization across runs.
    assert report.audit_files_consumed == ("llm_responses.jsonl", "grade.jsonl")
    anth = report.per_provider["anthropic"]
    model = anth.per_model["claude-sonnet-4-6"]
    assert model.input_tokens == 1500  # 1000 + 500
    assert model.output_tokens == 2100  # 2000 + 100
    assert model.call_count == 2


def test_rollup_anthropic_uses_cache_pricing(tmp_path: Path) -> None:
    """Anthropic cache fields multiply against the cache rates (AC1, AC2).

    The hand-computed expected USD blends non-cached input (3.00/Mtok),
    output (15.00/Mtok), cache_write_5m (3.75/Mtok), and cache_read
    (0.30/Mtok) on ``claude-sonnet-4-6``.
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=500_000,
                cache_creation=200_000,
                cache_read=800_000,
            )
        ],
    )

    report = rollup_audit_dir(project)

    expected = _expected_usd(
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_creation=200_000,
        cache_read=800_000,
    )
    rolled = report.per_provider["anthropic"].per_model["claude-sonnet-4-6"]
    assert rolled.total_usd == pytest.approx(expected)
    # Sanity check the manual math: 1.0*3 + 0.5*15 + 0.2*3.75 + 0.8*0.30
    # = 3 + 7.5 + 0.75 + 0.24 = 11.49
    assert rolled.total_usd == pytest.approx(11.49)


def test_rollup_openai_zero_cache_pricing(tmp_path: Path) -> None:
    """OpenAI cache rates are 0.0 (AC2); cache tokens contribute nothing."""
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [
            _grade_record(
                model="gpt-4o",
                input_tokens=1_000_000,
                output_tokens=500_000,
                # OpenAI normally records cache_* = 0, but even if non-zero
                # the cache rate is 0.0 so the product is 0.
                cache_creation=200_000,
                cache_read=800_000,
            )
        ],
    )

    report = rollup_audit_dir(project)

    rolled = report.per_provider["openai"].per_model["gpt-4o"]
    # gpt-4o: input 2.50, output 10.00, cache 0/0 → 1*2.5 + 0.5*10 = 7.5
    assert rolled.total_usd == pytest.approx(7.5)


def test_rollup_gemini_zero_cache_pricing(tmp_path: Path) -> None:
    """Gemini cache rates are 0.0 (AC2)."""
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [
            _grade_record(
                model="gemini-2.5-flash",
                input_tokens=1_000_000,
                output_tokens=500_000,
            )
        ],
    )

    report = rollup_audit_dir(project)

    rolled = report.per_provider["gemini"].per_model["gemini-2.5-flash"]
    # gemini-2.5-flash: input 0.30, output 2.50 → 0.30 + 1.25 = 1.55
    assert rolled.total_usd == pytest.approx(1.55)


def test_rollup_mixed_provider_aggregates_correctly(tmp_path: Path) -> None:
    """Anthropic drafter + Gemini grader in one project (AC2)."""
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=200_000,
            )
        ],
    )
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [
            _grade_record(
                model="gemini-2.5-flash",
                input_tokens=2_000_000,
                output_tokens=100_000,
            )
        ],
    )

    report = rollup_audit_dir(project)

    assert set(report.per_provider) == {"anthropic", "gemini"}
    anth_usd = report.per_provider["anthropic"].subtotal_usd
    gem_usd = report.per_provider["gemini"].subtotal_usd
    # Anthropic Sonnet: 1*3 + 0.2*15 = 3 + 3 = 6
    assert anth_usd == pytest.approx(6.0)
    # Gemini flash: 2*0.30 + 0.1*2.50 = 0.6 + 0.25 = 0.85
    assert gem_usd == pytest.approx(0.85)
    assert report.total_usd == pytest.approx(6.85)


def test_rollup_malformed_jsonl_line_raises_typed_error(tmp_path: Path) -> None:
    """Bad JSONL → ``CostRollupMalformedRecordError`` w/ ``line_num`` (AC6)."""
    project = _make_project(tmp_path)
    audit = _audit_dir(project)
    # Hand-author: one valid line, one corrupt JSON, one valid line.
    text = "\n".join(
        [
            json.dumps(_draft_record(model="claude-sonnet-4-6", input_tokens=10, output_tokens=10)),
            "{this is not valid json",
            json.dumps(_draft_record(model="claude-sonnet-4-6", input_tokens=10, output_tokens=10)),
        ]
    )
    (audit / "llm_responses.jsonl").write_text(text + "\n", encoding="utf-8")

    with pytest.raises(CostRollupMalformedRecordError) as exc_info:
        rollup_audit_dir(project)

    err = exc_info.value
    assert err.line_num == 2
    assert err.reason  # non-empty


def test_rollup_unknown_model_raises_typed_error(tmp_path: Path) -> None:
    """Audit record references absent SKU → ``CostRollupUnknownModelError`` (AC7).

    Variant: the model id matches a KNOWN provider prefix (``claude-``)
    but isn't in ``PRICES`` — exercises the ``_compute_record_usd`` →
    ``lookup`` → ``EstimateUnknownModelError`` → wrap branch.
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="claude-future-9-9",
                input_tokens=100,
                output_tokens=100,
            )
        ],
    )

    with pytest.raises(CostRollupUnknownModelError) as exc_info:
        rollup_audit_dir(project)

    assert exc_info.value.model_id == "claude-future-9-9"


def test_rollup_unknown_provider_prefix_raises_typed_error(tmp_path: Path) -> None:
    """Model id matches NO known provider prefix → typed error (Pass-3 F1).

    Future vendor (e.g. ``databricks-llama-3-70b``, ``mistral-large``)
    whose model id doesn't start with one of ``_PROVIDER_PREFIXES``
    must route through the no-prefix branch of ``_ingest_jsonl`` and
    raise ``CostRollupUnknownModelError`` with the cost-rollup-specific
    remediation. Without this test the no-prefix branch is dead code;
    deleting it would silently fall through to the lookup branch and
    surface the wrong error class.
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="databricks-llama-3-70b",
                input_tokens=100,
                output_tokens=100,
            )
        ],
    )

    with pytest.raises(CostRollupUnknownModelError) as exc_info:
        rollup_audit_dir(project)

    assert exc_info.value.model_id == "databricks-llama-3-70b"
    # Remediation must direct the operator at the right fix (PRICES table).
    assert "PRICES" in str(exc_info.value) or "pricing" in str(exc_info.value).lower()


def test_rollup_anthropic_opus_uses_opus_pricing_not_sonnet(tmp_path: Path) -> None:
    """Per-SKU pricing wired correctly across the Anthropic tier (Pass-3 F4).

    Existing happy-path tests exercise only ``claude-sonnet-4-6``. A
    bug that hard-coded ``lookup("claude-sonnet-4-6")`` regardless of
    the actual model id would produce a 5× cost under-report when the
    real model is opus. Pin opus pricing at the wire so the wrong-tier
    bug fails loud.

    Hand-computed against PRICES (2026-05-28):
      claude-opus-4-7: input $15.00/Mtok, output $75.00/Mtok
      (1e6 × $15.00 + 0.5e6 × $75.00) / 1e6 = $15.00 + $37.50 = $52.50
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=500_000,
            )
        ],
    )

    report = rollup_audit_dir(project)

    opus = report.per_provider["anthropic"].per_model["claude-opus-4-7"]
    assert opus.total_usd == pytest.approx(52.50)
    # Distinct from sonnet's $10.50 for the same token shape — a wrong-tier
    # lookup would land at sonnet pricing and fail this assertion.
    assert opus.total_usd != pytest.approx(10.50)


def test_rollup_line_num_reflects_literal_file_position_with_blank_lines(
    tmp_path: Path,
) -> None:
    """``line_num`` tracks the LITERAL file line (Pass-3 F3).

    ``_ingest_jsonl`` skips blank lines but ``enumerate(fh, start=1)``
    increments unconditionally — so a malformed line on file line 4
    surfaces as ``line_num=4`` even when intervening blanks were
    skipped. This contract makes the error's diagnostic compatible
    with ``sed -n '4p' <file>``. Pin it so a future "optimisation"
    that filters blank lines BEFORE enumeration breaks loud.
    """
    project = _make_project(tmp_path)
    audit_file = _audit_dir(project) / "llm_responses.jsonl"
    # Layout: valid record on line 1, blank lines 2 & 3, malformed on line 4.
    valid = json.dumps(_draft_record(model="claude-sonnet-4-6", input_tokens=100, output_tokens=50))
    audit_file.write_text(f"{valid}\n\n\n{{bad json on line 4\n", encoding="utf-8")

    with pytest.raises(CostRollupMalformedRecordError) as exc_info:
        rollup_audit_dir(project)

    assert exc_info.value.line_num == 4, (
        f"expected literal-line-num invariant (sed -n '4p' compatibility); "
        f"got line_num={exc_info.value.line_num}"
    )


def test_rollup_pins_pricing_table_version(tmp_path: Path) -> None:
    """``CostReport.pricing_table_version == PRICE_TABLE_VERSION`` (AC9)."""
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [_grade_record(model="gpt-4o", input_tokens=10, output_tokens=10)],
    )

    report = rollup_audit_dir(project)

    assert report.pricing_table_version == PRICE_TABLE_VERSION


def test_rollup_call_count_matches_jsonl_line_count(tmp_path: Path) -> None:
    """``ModelRollup.call_count`` reflects the number of audit records."""
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(model="claude-sonnet-4-6", input_tokens=10, output_tokens=10),
            _draft_record(model="claude-sonnet-4-6", input_tokens=20, output_tokens=20),
            _draft_record(model="claude-sonnet-4-6", input_tokens=30, output_tokens=30),
        ],
    )

    report = rollup_audit_dir(project)

    rolled = report.per_provider["anthropic"].per_model["claude-sonnet-4-6"]
    assert rolled.call_count == 3


def test_rollup_rejects_audit_path_outside_project_dir(tmp_path: Path) -> None:
    """A ``.signalforge`` symlink pointing outside ``project_dir`` is
    rejected via path canonicalisation → ``CostRollupAuditMissingError``
    (AC8).
    """
    project = _make_project(tmp_path)
    outside = tmp_path / "outside_audit"
    outside.mkdir()
    # Symlink the audit dir to somewhere outside the project root.
    (project / ".signalforge").symlink_to(outside, target_is_directory=True)
    # Place a real audit file inside the outside dir so the symlink
    # actually has content — without the symlink-hardening defence we
    # would mistakenly read it.
    _write_jsonl(
        outside / "llm_responses.jsonl",
        [_draft_record(model="claude-sonnet-4-6", input_tokens=10, output_tokens=10)],
    )

    with pytest.raises(CostRollupAuditMissingError):
        rollup_audit_dir(project)


def test_rollup_rejects_symlink_loop_in_project_dir(tmp_path: Path) -> None:
    """A symlink loop on ``project_dir`` is rejected via path
    canonicalisation → ``CostRollupAuditMissingError`` (AC8).
    """
    loop_a = tmp_path / "loop_a"
    loop_b = tmp_path / "loop_b"
    loop_a.symlink_to(loop_b, target_is_directory=True)
    loop_b.symlink_to(loop_a, target_is_directory=True)

    with pytest.raises(CostRollupAuditMissingError):
        rollup_audit_dir(loop_a)


def test_rollup_grand_total_equals_sum_of_provider_subtotals(tmp_path: Path) -> None:
    """``CostReport.total_usd`` matches a hand-computed mixed-provider sum.

    Pass-2 F1: previously this test re-derived the sum the same way the
    engine does (``sum(p.subtotal_usd ...)``), which made the assertion
    tautological — any sign-flip in the engine would still produce a
    self-consistent report. Hand-compute the expected grand total
    against the locked ``PRICES`` table (PRICE_TABLE_VERSION
    "2026-05-28") so the wire formula is pinned independently.
    """
    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "llm_responses.jsonl",
        [
            _draft_record(
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=500_000,
            )
        ],
    )
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [
            _grade_record(
                model="gpt-4o",
                input_tokens=2_000_000,
                output_tokens=1_000_000,
            ),
            _grade_record(
                model="gemini-2.5-flash",
                input_tokens=500_000,
                output_tokens=300_000,
            ),
        ],
    )

    report = rollup_audit_dir(project)

    # Hand-computed against PRICES (2026-05-28):
    #   anthropic claude-sonnet-4-6: (1e6 × $3.00 + 0.5e6 × $15.00) / 1e6 = $10.50
    #   openai    gpt-4o:            (2e6 × $2.50 + 1e6   × $10.00) / 1e6 = $15.00
    #   gemini    gemini-2.5-flash:  (0.5e6 × $0.30 + 0.3e6 × $2.50) / 1e6 = $0.90
    #   grand total                                                       = $26.40
    assert report.per_provider["anthropic"].subtotal_usd == pytest.approx(10.50)
    assert report.per_provider["openai"].subtotal_usd == pytest.approx(15.00)
    assert report.per_provider["gemini"].subtotal_usd == pytest.approx(0.90)
    assert report.total_usd == pytest.approx(26.40)
    # Belt-and-braces structural invariants (kept for the regression they
    # close — but no longer the only signal in this test).
    summed = sum(p.subtotal_usd for p in report.per_provider.values())
    assert report.total_usd == pytest.approx(summed)
    for provider in report.per_provider.values():
        model_sum = sum(m.total_usd for m in provider.per_model.values())
        assert provider.subtotal_usd == pytest.approx(model_sum)


# ---------------------------------------------------------------------------
# Result-shape sanity (frozen-dataclass invariants pinned at US-001 stand).
# ---------------------------------------------------------------------------


def test_rollup_result_shapes_are_frozen(tmp_path: Path) -> None:
    """Result objects are frozen dataclasses (DEC-004): attribute
    assignment raises ``FrozenInstanceError``. Reaffirms the
    reproducibility contract on the implementation path.
    """
    from dataclasses import FrozenInstanceError

    project = _make_project(tmp_path)
    _write_jsonl(
        _audit_dir(project) / "grade.jsonl",
        [_grade_record(model="gpt-4o", input_tokens=10, output_tokens=10)],
    )
    report = rollup_audit_dir(project)

    with pytest.raises(FrozenInstanceError):
        report.total_usd = 0.0  # type: ignore[misc]
    provider = next(iter(report.per_provider.values()))
    with pytest.raises(FrozenInstanceError):
        provider.subtotal_usd = 0.0  # type: ignore[misc]
    model = next(iter(provider.per_model.values()))
    with pytest.raises(FrozenInstanceError):
        model.total_usd = 0.0  # type: ignore[misc]
    # Used names so the test imports flag the unused-name warning.
    assert isinstance(report, CostReport)
    assert isinstance(provider, ProviderRollup)
    assert isinstance(model, ModelRollup)
