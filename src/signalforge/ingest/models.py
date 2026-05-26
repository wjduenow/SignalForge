"""Typed ingest result models (US-002).

Defines the in-process result shapes the ingest reader (a later story)
emits when it parses an external dbt ``schema.yml`` into a
:class:`~signalforge.draft.CandidateSchema`:

* :class:`SkippedTest` — one structured record per test the reader could
  not convert to a supported :class:`~signalforge.draft.CandidateTest`,
  carrying the closed :data:`SkipReason` literal and a free-text detail.
* :class:`IngestResult` — the reader's return value: the converted
  ``candidate`` plus the tuple of ``skipped`` records (DEC-003).

DEC-003 — unsupported / malformed / custom tests are *skipped + recorded*
(never silently dropped), supporting the explainability commitment and the
future ``prune-existing`` CLI's "N tests skipped, here's why" report.

These models are produced **in process** and handed straight to the prune
stage; they are NOT serialised to a JSONL audit / sidecar and read back
from disk. Per the read-back drift-detector convention
(``docs/rules/testing-signal.md`` § "Drift detection"), they therefore
need NO ``extra="forbid"`` drift detector — ``extra="ignore"`` is for
forward-compat with a future field, not for tolerating an upstream
on-disk schema we don't control.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from signalforge.draft import CandidateSchema

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore")

SkipReason = Literal[
    "unsupported-test-type",
    "custom-or-generic-test",
    "malformed-supported-test",
]
"""Closed reason set for a test the ingest reader could not convert.

* ``"unsupported-test-type"`` — a recognised dbt test name SignalForge
  does not model (e.g. a bare-string test that isn't ``not_null`` /
  ``unique``).
* ``"custom-or-generic-test"`` — a namespaced or project-defined test
  (``dbt_utils.*``, ``dbt_expectations.*``, a singular/custom generic).
* ``"malformed-supported-test"`` — a supported type whose args are
  missing or empty (e.g. ``accepted_values`` with no ``values``).

Adding a fourth skip cause requires extending this literal AND the
reader's classification logic in lockstep.
"""


class SkippedTest(BaseModel):
    """One structured skip record (DEC-003).

    ``column`` is ``None`` for a model-level test. ``detail`` carries a
    short free-text diagnostic (e.g. the raw test name or the missing
    argument) for the operator-facing skip report; it defaults to ``""``.
    """

    model_config = _BASE_CONFIG

    test_name: str
    column: str | None
    reason: SkipReason
    detail: str = ""


class IngestResult(BaseModel):
    """The ingest reader's return value (DEC-003).

    ``candidate`` is the converted schema the prune stage consumes
    unchanged; ``skipped`` records every test the reader could not
    convert, in encounter order.
    """

    model_config = _BASE_CONFIG

    candidate: CandidateSchema
    skipped: tuple[SkippedTest, ...] = ()

    def __repr__(self) -> str:
        """Minimal repr — surfaces identity + skip count, not nested content.

        Mirrors the prune / grade / diff ``__repr__`` convention: the full
        ``candidate`` (columns, tests, descriptions) and the ``skipped``
        tuple stay accessible via field access / ``result.model_dump()``;
        the custom repr only keeps the candidate's column / test payload
        out of a casual debug-print or ``_LOGGER`` interpolation.
        """
        return f"IngestResult(candidate={self.candidate.name!r}, skipped={len(self.skipped)})"


__all__ = (
    "IngestResult",
    "SkipReason",
    "SkippedTest",
)
