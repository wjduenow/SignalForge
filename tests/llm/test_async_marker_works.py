"""``@pytest.mark.asyncio`` is wired and runs async tests under strict mode.

Proves that ``pytest-asyncio`` is installed via ``uv sync --dev`` and that
``[tool.pytest.ini_options].asyncio_mode = 'strict'`` plus the registered
``asyncio`` marker collect and execute an ``async def`` test body. Without
this smoke a missing dev-dep would silently skip every future async test
rather than failing collection. Traces to DEC-013 of
``plans/super/186-grade-asyncio-parallel.md``.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_smoke() -> None:
    """A trivial ``async def`` body runs to completion under the asyncio marker."""

    await asyncio.sleep(0)
    assert True
