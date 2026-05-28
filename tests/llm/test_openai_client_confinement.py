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
    for py in sorted(_LLM_DIR.glob("*.py")):
        if py.name == _SHIM_FILENAME:
            continue
        for lineno, text in _openai_type_ignore_lines(py):
            offenders.append(f"{py.name}:{lineno}: {text}")

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
