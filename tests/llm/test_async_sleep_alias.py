"""``_async_sleep`` is the module-level alias for :func:`asyncio.sleep`.

Mirrors the ``_sleep`` / ``_rand_uniform`` aliases in
:mod:`signalforge.llm.client` per ``llm-drafter.md`` DEC-004 — tests reassign
the alias at module scope to fast-forward async retry backoff without
monkey-patching :func:`asyncio.sleep` globally. Traces to DEC-011 of
``plans/super/186-grade-asyncio-parallel.md``.
"""

from __future__ import annotations

import asyncio

from signalforge.llm import client as client_module


def test_async_sleep_is_asyncio_sleep() -> None:
    """The module-level alias is identity-equal to :func:`asyncio.sleep`."""

    assert client_module._async_sleep is asyncio.sleep
