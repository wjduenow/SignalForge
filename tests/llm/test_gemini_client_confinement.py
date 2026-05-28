"""#137 US-001 / DEC-001 — Google Gemini SDK type-ignore confinement.

Every ``# type: ignore`` / ``# pyright: ignore`` line in the
``signalforge.llm`` package that ALSO mentions ``google.genai`` / ``genai``
must live ONLY in ``_gemini_client.py`` — the one-shim-per-vendor SDK seam.
Mirrors :mod:`tests.warehouse.test_snowflake_client_confinement` (DEC-005
of #119) and complements the AST-level construction-confinement scan in
:func:`tests.test_audit_completeness.test_gemini_client_construction_only_in_llm_client_shim`.

The two gates check different shapes:

* The AST scan rejects ``genai.Client(...)`` *constructions* elsewhere in
  the package. A bare ``import google.genai`` with no construction would
  pass.
* This file/line scan rejects any ``# type: ignore`` / ``# pyright: ignore``
  line that mentions the SDK. An ``import google.genai`` without a typed
  ignore stub would currently pass — pyright is the second gate that
  surfaces such drift via ``reportMissingImports`` (the SDK is not in the
  base install per DEC-010 / DEC-015).

Both gates together pin the rule: vendor SDK noise stays in
``_gemini_client.py``, full stop.
"""

from __future__ import annotations

from pathlib import Path

_LLM_DIR = Path(__file__).resolve().parents[2] / "src" / "signalforge" / "llm"
_SHIM_FILENAME = "_gemini_client.py"


def _gemini_type_ignore_lines(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, text)`` for lines carrying a Gemini-mentioning
    ``# type: ignore`` / ``# pyright: ignore`` directive.

    Matches any of three mention forms — ``google.genai`` (canonical),
    ``google-genai`` (the PyPI package name, may appear in comments), or
    bare ``genai`` (the imported namespace). All three are folded to
    lowercase before the substring check so an upper-case mention (e.g. in
    a comment header) still trips the scan. Single hits per line; a line
    carrying two distinct mentions is recorded once.
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        lowered = line.lower()
        has_ignore = "type: ignore" in lowered or "pyright: ignore" in lowered
        if not has_ignore:
            continue
        mentions_gemini = (
            "google.genai" in lowered or "google-genai" in lowered or "genai" in lowered
        )
        if mentions_gemini:
            hits.append((lineno, line.strip()))
    return hits


def test_gemini_type_ignores_only_in_shim() -> None:
    """No ``.py`` under ``signalforge.llm`` other than ``_gemini_client.py``
    may carry a Gemini-mentioning ``# type: ignore`` / ``# pyright: ignore``.

    Walks the LLM directory recursively to cover any future nested module
    (e.g. ``llm/providers/gemini.py`` if the registry split ever lands —
    today's flat layout is also covered, since ``rglob('*.py')`` is the
    superset of ``glob('*.py')``).
    """
    offenders: list[str] = []
    for py in sorted(_LLM_DIR.rglob("*.py")):
        if py.name == _SHIM_FILENAME:
            continue
        for lineno, text in _gemini_type_ignore_lines(py):
            offenders.append(f"{py.relative_to(_LLM_DIR)}:{lineno}: {text}")

    assert not offenders, (
        "google-genai-related type-ignore must live only in "
        f"{_SHIM_FILENAME}; found offenders:\n" + "\n".join(offenders)
    )


def test_shim_actually_carries_gemini_type_ignore() -> None:
    """Sanity: the shim itself DOES carry at least one Gemini-mentioning
    ``# type: ignore`` / ``# pyright: ignore``. Without this, the
    confinement scan above could pass vacuously after a refactor that
    dropped the seam.
    """
    shim = _LLM_DIR / _SHIM_FILENAME
    assert _gemini_type_ignore_lines(shim), (
        f"{_SHIM_FILENAME} should confine the google-genai SDK type-ignore; "
        "the confinement scan is only meaningful if the seam exists."
    )


def test_shim_imports_cleanly_without_sdk_installed() -> None:
    """DEC-015: importing the shim must NOT require the ``[gemini]`` extra.

    The ``google.genai`` SDK ships under the ``[gemini]`` optional-dependency
    extra (DEC-010, US-003); a base install will not have it. The shim's
    every SDK import is therefore lazy (inside function bodies), so the
    module-import path stays clean — and :func:`_load_gemini_exception_classes`
    returns a frozen dataclass of empty tuples when the SDK is absent, so
    the orchestrator's retry loop routes every exception to NO_RETRY
    rather than crashing at startup.

    This test pins the lazy-import contract: a fresh import of the shim
    must always succeed, and :func:`_load_gemini_exception_classes` must
    return a populated dataclass either way (real SDK classes when the
    extra is installed; empty tuples when not).
    """
    from signalforge.llm._gemini_client import (
        GeminiClientProtocol,
        _GeminiExceptionClasses,
        _load_gemini_exception_classes,
    )

    # Module-level constructs are reachable without the SDK.
    assert GeminiClientProtocol is not None
    classes = _load_gemini_exception_classes()
    assert isinstance(classes, _GeminiExceptionClasses)
    # All four bucket attrs are populated as tuples (possibly empty).
    assert isinstance(classes.rate_limit, tuple)
    assert isinstance(classes.api_status, tuple)
    assert isinstance(classes.auth, tuple)
    assert isinstance(classes.connection, tuple)
