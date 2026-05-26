"""US-002 (#119) DEC-005 — Snowflake SDK type-ignore confinement.

Every ``# type: ignore`` / ``# pyright: ignore`` line in the warehouse
``adapters/`` tree that ALSO mentions "snowflake" must live ONLY in
``_snowflake_client.py`` — the one-shim-per-vendor SDK seam. Mirrors the
spirit of the Anthropic-SDK confinement scan (scan 3) in
``tests/test_audit_completeness.py``; a simple file/line scan suffices here.
"""

from __future__ import annotations

from pathlib import Path

_ADAPTERS_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "signalforge" / "warehouse" / "adapters"
)
_SHIM_FILENAME = "_snowflake_client.py"


def _snowflake_type_ignore_lines(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, text) for lines carrying a snowflake-mentioning
    type/pyright ignore directive.
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        lowered = line.lower()
        has_ignore = "type: ignore" in lowered or "pyright: ignore" in lowered
        if has_ignore and "snowflake" in lowered:
            hits.append((lineno, line.strip()))
    return hits


def test_snowflake_type_ignores_only_in_shim() -> None:
    """No ``.py`` under ``adapters/`` other than ``_snowflake_client.py``
    may carry a snowflake-mentioning type-ignore.
    """
    offenders: list[str] = []
    for py in sorted(_ADAPTERS_DIR.glob("*.py")):
        if py.name == _SHIM_FILENAME:
            continue
        for lineno, text in _snowflake_type_ignore_lines(py):
            offenders.append(f"{py.name}:{lineno}: {text}")

    assert not offenders, (
        "snowflake-connector-python type-ignore must live only in "
        f"{_SHIM_FILENAME}, but found:\n" + "\n".join(offenders)
    )


def test_shim_actually_carries_snowflake_type_ignore() -> None:
    """Sanity: the shim itself DOES carry at least one snowflake-mentioning
    type-ignore. Without this, the confinement scan above could pass
    vacuously after a refactor that dropped the seam.
    """
    shim = _ADAPTERS_DIR / _SHIM_FILENAME
    assert _snowflake_type_ignore_lines(shim), (
        f"{_SHIM_FILENAME} should confine the snowflake SDK type-ignore; "
        "the confinement scan is only meaningful if the seam exists"
    )
