"""Behaviour tests for ``scripts/measure_e2e_cost.py``.

US-003 of plans/super/157-e2e-cost-and-parallel.md — the script is a
thin argparse wrapper around :func:`signalforge.llm.cost.rollup_audit_dir`.
Tests run mostly in-process via ``main([...])``; the gated
``@pytest.mark.cli_subprocess`` test shells out to the real interpreter
so the ``#!/usr/bin/env python3`` shebang + ``__main__`` block are also
exercised.

The micro-JSONL fixtures mirror the helpers in
``tests/llm/cost/test_rollup.py`` so a JSONL-shape drift in the audit
writers fails both layers simultaneously rather than silently passing
here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "measure_e2e_cost.py"


def _load_script() -> ModuleType:
    """Import ``scripts/measure_e2e_cost.py`` as a module.

    The script lives outside any importable package (``scripts/`` is a
    repo-root sibling of ``src/``), so we load it via
    :mod:`importlib.util` rather than a regular ``import`` statement.
    Cached on the module object so successive calls re-use the same
    instance.
    """
    spec = importlib.util.spec_from_file_location("_measure_e2e_cost", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Module-level cache so each test that calls ``_load_script()`` shares
# the same object (the script is pure-import-safe — no top-level side
# effects beyond function definitions).
_SCRIPT = _load_script()


# ---------------------------------------------------------------------------
# Engineered JSONL helpers (mirror ``tests/llm/cost/test_rollup.py``).
# ---------------------------------------------------------------------------


def _draft_record(
    *,
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_creation: int = 0,
    cache_read: int = 0,
    model_unique_id: str = "model.cost.fixture",
) -> dict[str, object]:
    """Build one ``LLMResponseEvent``-shaped dict with every required field."""
    return {
        "timestamp": "2026-05-29T00:00:00.000000Z",
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
    model: str = "gpt-4o-mini",
    input_tokens: int = 800,
    output_tokens: int = 200,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> dict[str, object]:
    """Build one ``GradeEvent``-shaped dict with every required field."""
    return {
        "audit_schema_version": 1,
        "signalforge_version": "0.0.0.test",
        "run_id": "00112233445566778899aabbccddeeff",
        "timestamp": "2026-05-29T00:00:00.000000Z",
        "model_unique_id": "model.cost.fixture",
        "artifact_id": "column.email.description",
        "criterion_id": "clarity",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec))
            fh.write("\n")


def _make_populated_project(tmp_path: Path) -> Path:
    """Create a tmp project with one drafter + one grader audit record."""
    project = tmp_path / "project"
    project.mkdir()
    audit = project / ".signalforge"
    audit.mkdir()
    _write_jsonl(audit / "llm_responses.jsonl", [_draft_record()])
    _write_jsonl(audit / "grade.jsonl", [_grade_record()])
    return project


# ---------------------------------------------------------------------------
# In-process happy-path tests.
# ---------------------------------------------------------------------------


def test_measure_e2e_cost_main_exits_0_on_valid_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Valid project with both JSONLs -> exit 0, non-empty stdout, empty stderr."""
    project = _make_populated_project(tmp_path)

    exit_code = _SCRIPT.main(["--project-dir", str(project)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out, "expected non-empty stdout on the happy path"
    assert captured.err == "", f"expected empty stderr, got: {captured.err!r}"
    assert "Traceback" not in captured.err


def test_measure_e2e_cost_main_exits_2_on_missing_audit_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty project (no audit JSONLs) -> exit 2 + remediation on stderr."""
    project = tmp_path / "empty_project"
    project.mkdir()

    exit_code = _SCRIPT.main(["--project-dir", str(project)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "no audit JSONLs found" in captured.err
    assert "↳ Remediation:" in captured.err
    assert "Traceback" not in captured.err


def test_measure_e2e_cost_main_exits_2_on_unknown_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A JSONL referencing a model absent from PRICES -> exit 2 + no traceback.

    The rollup engine routes an unknown model through
    :class:`CostRollupUnknownModelError`. The model id must start with a
    known provider prefix (``claude-`` / ``gpt-`` / ``gemini-``) so the
    rollup classifies it as a provider's SKU and then hits the
    PRICES-lookup path — otherwise it raises the same error via the
    provider-prefix branch, but going through the lookup branch better
    mirrors the bug class US-003 AC3 cares about.
    """
    project = tmp_path / "unknown_model_project"
    project.mkdir()
    audit = project / ".signalforge"
    audit.mkdir()
    _write_jsonl(
        audit / "llm_responses.jsonl",
        [_draft_record(model="claude-not-a-real-sku")],
    )

    exit_code = _SCRIPT.main(["--project-dir", str(project)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "unknown model id" in captured.err
    assert "↳ Remediation:" in captured.err
    assert "Traceback" not in captured.err


def test_measure_e2e_cost_format_json_emits_valid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--format=json`` emits JSON parseable by :func:`json.loads` with the
    canonical top-level keys."""
    project = _make_populated_project(tmp_path)

    exit_code = _SCRIPT.main(["--project-dir", str(project), "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert set(payload) >= {
        "per_provider",
        "total_usd",
        "pricing_table_version",
        "audit_files_consumed",
    }
    # Cross-check the data made it through: both providers should appear,
    # each with their respective model nested under per_model.
    assert "anthropic" in payload["per_provider"]
    assert "openai" in payload["per_provider"]
    assert "claude-sonnet-4-6" in payload["per_provider"]["anthropic"]["per_model"]
    assert "gpt-4o-mini" in payload["per_provider"]["openai"]["per_model"]


def test_measure_e2e_cost_format_text_emits_grand_total_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--format=text`` (default) prints the canonical ``TOTAL: $…`` line
    + the pricing-table-version + audit-files footer."""
    project = _make_populated_project(tmp_path)

    exit_code = _SCRIPT.main(["--project-dir", str(project)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "TOTAL:" in captured.out
    assert "$" in captured.out
    assert "pricing table" in captured.out
    assert "llm_responses.jsonl" in captured.out
    assert "grade.jsonl" in captured.out


# ---------------------------------------------------------------------------
# Gated subprocess smoke (mirrors tests/cli/test_subprocess_smoke.py).
# ---------------------------------------------------------------------------


@pytest.mark.cli_subprocess
def test_measure_e2e_cost_subprocess_smoke(tmp_path: Path) -> None:
    """``python scripts/measure_e2e_cost.py --project-dir <tmp>`` exits 0.

    Exercises the ``#!/usr/bin/env python3`` shebang + ``__main__`` block
    that the in-process ``main([...])`` smokes cannot reach. Mirrors the
    ``tests/cli/test_subprocess_smoke.py`` precedent: maintainer-only,
    gated by ``cli_subprocess`` (run with ``pytest -m cli_subprocess
    --no-cov``).
    """
    project = _make_populated_project(tmp_path)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--project-dir", str(project)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_REPO_ROOT),
    )

    assert result.returncode == 0, (
        f"unexpected exit code {result.returncode}; stderr={result.stderr!r}"
    )
    assert result.stdout, "expected non-empty stdout on happy path"
    # Stderr stays empty on the happy path; the no-traceback floor applies
    # to every code path (mirrors the ``signalforge --version`` smoke).
    assert "Traceback" not in result.stderr
