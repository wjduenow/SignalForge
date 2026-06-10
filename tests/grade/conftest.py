"""Shared grade-layer test fixtures.

The #202 US-005 (DEC-206) transient-recovery sweep is ALWAYS-ON, with a
production default cool-down of ``GradeConfig.sweep_cooldown_seconds = 2.0``
between rounds. Pre-existing grade tests that drive a transient degrade
would otherwise pay that real wall-clock wait (and the sweep's extra
re-grade calls) on every run.

This autouse fixture neutralises the cool-down by default — it patches the
test-overridable :data:`signalforge.grade.engine._async_sleep` seam to a
no-op so the suite runs instantly. Tests that specifically verify the
cool-down seam re-patch the seam with ``monkeypatch.setattr`` AFTER this
fixture (monkeypatch unwinds last-set-first, so their override wins). A
test that needs the UNPATCHED import-time alias (the alias-identity pin)
opts out via ``@pytest.mark.no_sweep_cooldown_patch``.
"""

from __future__ import annotations

import pytest

from signalforge.grade import engine as _engine_module


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_sweep_cooldown_patch: opt out of the autouse sweep cool-down no-op patch",
    )


@pytest.fixture(autouse=True)
def _neutralise_sweep_cooldown(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace the sweep cool-down sleep with a no-op coroutine by default."""
    if request.node.get_closest_marker("no_sweep_cooldown_patch") is not None:
        return

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_engine_module, "_async_sleep", _noop)
