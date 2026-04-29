"""Default-mode regression suite — DEC-012.

If a future bug flips the default :class:`SamplingMode` away from
``SCHEMA_ONLY``, every team that hasn't authored ``signalforge.yml`` silently
leaks PII. These three tests are the load-bearing safety net at three layers:

* **DEC-012(a)** — the field default on :class:`SafetyPolicy` itself.
* **DEC-012(b)** — the loader fallback when no ``signalforge.yml`` is present.
* **DEC-012(c)** — :func:`build_llm_request` issues *zero* adapter calls under
  the default policy.

All three are also covered by their respective module test files
(``test_policy.py``, ``test_config.py``, ``test_request.py``); this file
clusters them so future-you can ``grep -r "default_mode"`` and find every
layer in one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.manifest.loader import load
from signalforge.safety.config import load_safety_config
from signalforge.safety.models import SamplingMode
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.request import build_llm_request
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.safety


_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "safety" / "manifest_with_pii_meta.json"
)


def test_safety_policy_no_args_is_schema_only() -> None:
    """DEC-012(a) — policy field default."""
    assert SafetyPolicy().mode is SamplingMode.SCHEMA_ONLY


def test_load_safety_config_no_file_is_schema_only(tmp_path: Path) -> None:
    """DEC-012(b) — loader fallback with no ``signalforge.yml``."""
    assert load_safety_config(tmp_path).mode is SamplingMode.SCHEMA_ONLY


def test_build_llm_request_default_policy_zero_warehouse_calls(tmp_path: Path) -> None:
    """DEC-012(c) — default policy must trigger zero adapter calls.

    A regression here means a default-mode flip silently issues sample/aggregate
    queries against the warehouse. ``FakeAdapter`` with no expectations queued
    raises ``AssertionError`` on any call, so this passes only if no warehouse
    method is invoked at all.
    """
    manifest = load(_FIXTURE.parent, manifest_path=_FIXTURE)
    customers = manifest.get_model("model.sf_demo.customers")

    fake = FakeAdapter()
    policy = SafetyPolicy(audit_path=tmp_path / "audit.jsonl")

    request = build_llm_request(customers, fake, policy)

    fake.assert_all_expectations_met()
    assert fake.enter_count == 0
    assert request.mode is SamplingMode.SCHEMA_ONLY
    assert request.sampled_rows is None
    assert request.aggregates is None
