"""SignalForge ingest layer — read external dbt ``schema.yml`` tests into a
``CandidateSchema``.

This subpackage extends Architectural Commitment #1 ("signal over volume")
beyond SignalForge's own LLM draft layer: point SignalForge at an existing
dbt ``schema.yml`` (hand-written, or output from dbt-codegen / dbt Copilot
/ DinoAI / datapilot) and let the warehouse tell you which of its tests add
no signal. The reader parses standard dbt test syntax and emits a typed
``CandidateSchema`` that ``signalforge.prune.prune_tests`` consumes
unchanged.

US-001 (this story) ships the package scaffold + the typed-error surface
only; the reader / parser / result models land in later stories of issue
#104. The CLI ``prune-existing`` subcommand is the fast-follow #105
(DEC-004) — the ``IngestError`` hierarchy is wired into the CLI exit-code
taxonomy now so #105 inherits it with no rework.

Public API surface (US-001):

* The :class:`IngestError` hierarchy: :class:`IngestError`,
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

__all__ = [
    # Errors (6)
    "IngestError",
    "IngestSchemaNotFoundError",
    "IngestSchemaParseError",
    "IngestSchemaTooLargeError",
    "IngestModelNotFoundError",
    "IngestAnchorContractError",
]
