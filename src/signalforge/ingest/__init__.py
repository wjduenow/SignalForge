"""SignalForge ingest layer — read external dbt ``schema.yml`` tests into a
``CandidateSchema``.

This subpackage extends Architectural Commitment #1 ("signal over volume")
beyond SignalForge's own LLM draft layer: point SignalForge at an existing
dbt ``schema.yml`` (hand-written, or output from dbt-codegen / dbt Copilot
/ DinoAI / datapilot) and let the warehouse tell you which of its tests add
no signal. The reader parses standard dbt test syntax and emits a typed
``CandidateSchema`` that ``signalforge.prune.prune_tests`` consumes
unchanged.

Issue #104 ships this full library seam: the package scaffold + typed-error
hierarchy (US-001), the result models (US-002), the pure dbt test-entry
parser (US-003), the fail-loud anchor validator (US-004), and the
``read_schema`` orchestrator (US-005). The operator-facing CLI
``prune-existing`` subcommand is the fast-follow #105 (DEC-004) — the
``IngestError`` hierarchy is wired into the CLI exit-code taxonomy now so
#105 inherits it with no rework.

Public API surface:

* :func:`read_schema` (US-005) — the public entry point: parse an
  external ``schema.yml`` for one model and return an
  :class:`IngestResult`.
* :func:`read_test_files` (US-013) — read an operator's singular dbt
  tests (``tests/*.sql``) for one model into model-level
  :class:`~signalforge.draft.CandidateTestCustomSQL` records, so
  ``prune-existing`` can later prune them (DEC-013).
* :class:`IngestResult` / :class:`SkippedTest` / :data:`SkipReason`
  (US-002) — the reader's return shapes.
* The :class:`IngestError` hierarchy (US-001): :class:`IngestError`,
  :class:`IngestSchemaNotFoundError`, :class:`IngestSchemaParseError`,
  :class:`IngestSchemaTooLargeError`, :class:`IngestModelNotFoundError`,
  :class:`IngestAnchorContractError`.

See ``plans/super/104-ingest-external-tests.md`` for the full design.
"""

from __future__ import annotations

from signalforge.ingest.errors import (
    IngestAnchorContractError,
    IngestError,
    IngestModelNotFoundError,
    IngestSchemaNotFoundError,
    IngestSchemaParseError,
    IngestSchemaTooLargeError,
)
from signalforge.ingest.models import IngestResult, SkippedTest, SkipReason
from signalforge.ingest.reader import read_schema, read_test_files

__all__ = [
    # Errors (6)
    "IngestError",
    "IngestSchemaNotFoundError",
    "IngestSchemaParseError",
    "IngestSchemaTooLargeError",
    "IngestModelNotFoundError",
    "IngestAnchorContractError",
    # Result models (US-002)
    "IngestResult",
    "SkippedTest",
    "SkipReason",
    # Reader orchestrators — the public entry points
    "read_schema",  # schema.yml (US-005)
    "read_test_files",  # tests/*.sql singular tests (US-013)
]
