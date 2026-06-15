"""Tests for ``signalforge.airflow.errors`` (issue #230 / US-003 / DEC-003).

These tests import ONLY the Airflow-free error seam — never the real
``apache-airflow`` package — so they run in the **default** pytest suite
(NO ``airflow`` marker). They pin two contracts:

1. ``AirflowConfigError`` renders its message + a ``↳ Remediation:`` line
   in ``str()`` (the layer-base rendering convention from
   ``manifest-readers.md``).
2. ``AirflowConfigError`` maps to exit code 2 (input-validation) via
   ``signalforge.cli._helpers.map_exception_to_exit_code`` — the same
   contract ``tests/cli/test_exit_codes.py`` pins for the other stages.
"""

from __future__ import annotations

from signalforge.airflow import AirflowConfigError, AirflowIntegrationError
from signalforge.cli._helpers import map_exception_to_exit_code


def test_airflow_config_error_renders_message_and_remediation() -> None:
    """``str()`` shows the message and the explicit remediation on its
    own ``↳ Remediation:`` line."""
    exc = AirflowConfigError("bad project_dir", remediation="do x")
    rendered = str(exc)
    assert "bad project_dir" in rendered
    assert "↳ Remediation: do x" in rendered


def test_airflow_config_error_uses_default_remediation() -> None:
    """When no explicit remediation is passed, the concrete's
    ``default_remediation`` is rendered."""
    exc = AirflowConfigError("bad model")
    rendered = str(exc)
    assert "bad model" in rendered
    assert "↳ Remediation:" in rendered
    assert exc.remediation == AirflowConfigError.default_remediation
    # The base-class placeholder must NOT leak onto a concrete leaf.
    assert "this is the base class" not in rendered


def test_airflow_config_error_is_airflow_integration_error() -> None:
    """The concrete inherits the abstract base so the MRO walk in
    :func:`map_exception_to_exit_code` resolves it."""
    assert issubclass(AirflowConfigError, AirflowIntegrationError)
    assert isinstance(AirflowConfigError("x"), AirflowIntegrationError)


def test_airflow_config_error_maps_to_exit_code_2() -> None:
    """``AirflowConfigError`` is tier 2 (input-validation)."""
    assert map_exception_to_exit_code(AirflowConfigError("bad config")) == 2
