"""Prune engine entry point and per-test executor (US-009).

Hosts :func:`prune_tests` — the public seam that compiles every
:class:`signalforge.draft.CandidateTest` to failing-rows SQL via the
compiler module (US-007), runs each through
:meth:`signalforge.warehouse.WarehouseAdapter.run_test_sql`, writes a
fail-closed JSONL audit receipt per decision (US-008), and folds the
per-test verdicts into a typed :class:`PruneResult` (US-004).

This is THE load-bearing differentiator (CLAUDE.md commitment #1): a
candidate test that always passes on warehouse samples is worse than
nothing because it consumes reviewer attention. The orchestrator drops
those tests AND the symmetric "fails on known-clean data" set, while
keeping every test it could not evaluate (fail-closed against silent
loss of signal).

Design commitments operationalised here:

* **DEC-002** — The signature is
  ``prune_tests(model, adapter, candidates, manifest, *, config,
  audit_path) -> PruneResult``. Mirrors
  :func:`signalforge.draft.draft_schema` so the CLI / wrapper layers
  see one consistent end-to-end shape across stages.
* **DEC-008** — :attr:`PruneConfig.trusted_models` is validated at
  orchestrator entry: every entry must resolve to a model in the
  loaded :class:`Manifest`. The first miss raises
  :class:`PruneTrustedModelNotFoundError` BEFORE any warehouse call is
  issued. Validating at entry (not at config load) is necessary
  because the manifest isn't loaded yet when
  :func:`load_prune_config` runs.
* **DEC-011** — Total-budget enforcement: every test consults a
  monotonic-ms wall clock; once the elapsed total ≥
  ``total_budget_seconds * 1000`` every remaining un-started test is
  marked ``kept-without-evidence`` with a budget-specific ``why`` and
  no warehouse call is issued.
* **DEC-016** — Fail-closed audit (mirrors safety / draft layers): a
  failed audit write aborts the prune run. ``OSError`` /
  ``PermissionError`` from the writer surface as
  :class:`PruneAuditWriteError` with the original cause attached;
  :class:`PruneAuditRecordTooLargeError` propagates as-is (it's
  already a typed :class:`PruneError` subclass).
  :class:`KeyboardInterrupt` / :class:`SystemExit` propagate untouched
  so a Ctrl-C is never demoted to an audit error.
* **DEC-019** — Module-level :data:`_sleep` and :data:`_now_monotonic_ms`
  aliases (mirrors :mod:`signalforge.llm.client`'s ``_sleep`` /
  ``_rand_uniform`` pattern). Tests reassign these to deterministic
  stand-ins so budget-exhaustion paths exercise without
  timing-dependent flake. The orchestrator's normal path does not
  invoke ``_sleep``; the alias is reserved for future budget-loop
  work and pinned by a test so the seam doesn't drift.

Per-test timeout-ms threading
-----------------------------

v0.1 relies on the adapter's default :class:`QueryJobConfig` (no per-test
timeout). Per-test budget enforcement requires an additional
``run_test_sql(..., timeout_ms=...)`` parameter, deferred to v0.2 — the
existing ``_make_query_job_config`` plumbing from US-002 is in place but
not exposed at :meth:`run_test_sql`. The ``kept-without-evidence``
*outcome* is still emitted via two paths:

* **Total-budget exceeded** — ``elapsed_total_ms ≥
  config.total_budget_seconds * 1000`` short-circuits every remaining
  test before dispatch.
* **Warehouse error** — any :class:`WarehouseError` subclass raised by
  ``run_test_sql`` (including a future BigQuery job-timeout mapping)
  is caught and routed to ``kept-without-evidence``.

This honours "tests we cannot evaluate are kept, not dropped" — the
conservative default that keeps a buggy adapter / oversized table from
silently losing signal-bearing tests.

See ``plans/super/6-prune-engine.md`` for the full design.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from signalforge import __version__ as _SIGNALFORGE_VERSION
from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.draft.models import CandidateSchema, CandidateTest
from signalforge.manifest.models import Manifest, Model
from signalforge.prune.audit import (
    _build_prune_event,
    _compute_config_hash,
    _write_prune_event,
)
from signalforge.prune.compiler import (
    _compile_test,
    _compute_compiled_sql_hash,
    _InvalidIdentifier,
    _RequiresFutureData,
)
from signalforge.prune.config import PruneConfig
from signalforge.prune.errors import (
    PruneAuditRecordTooLargeError,
    PruneAuditWriteError,
    PruneError,
    PruneTrustedModelNotFoundError,
)
from signalforge.prune.models import PruneDecision, PruneResult, Scope
from signalforge.warehouse.base import WarehouseAdapter
from signalforge.warehouse.errors import WarehouseError
from signalforge.warehouse.models import TableRef, TestResult

_LOGGER = logging.getLogger(__name__)

# Module-level aliases for deterministic test override (DEC-019).
# Mirrors src/signalforge/llm/client.py's _sleep / _rand_uniform pattern.
# `_sleep` is reserved for future budget-loop work (v0.2 per-test
# timeout enforcement); the alias is pinned by a test so the seam stays
# reassignable. `_now_monotonic_ms` IS used by the orchestrator's
# total-budget gate — tests reassign it to advance the clock past
# ``total_budget_seconds * 1000`` deterministically.
_sleep = time.sleep


def _now_monotonic_ms() -> int:
    """Return the current monotonic time in integer milliseconds.

    Defined as a module-level function (not a lambda) so test code can
    reassign ``signalforge.prune.engine._now_monotonic_ms`` without
    having to import :mod:`time` to construct a stand-in.
    """
    return int(time.monotonic() * 1000)


def _build_compiled_sql_hash_or_empty(sql: str) -> str:
    """Return the 16-hex-char compiled-SQL hash, or a stable empty-string
    hash for the no-SQL outcomes (``requires-future-data`` and the
    budget-exceeded variant of ``kept-without-evidence``).

    Using an empty-string hash for the no-SQL outcomes keeps
    :attr:`PruneDecision.compiled_sql_hash` non-optional in the read-back
    schema — every audit JSONL row carries 16 hex chars regardless of
    decision shape, so a downstream consumer never has to branch on a
    nullable hash.
    """
    return _compute_compiled_sql_hash(sql)


def _why_always_passes(sampled_rows: int | None, scope: Scope) -> str:
    rows = sampled_rows if sampled_rows is not None else 0
    return f"Test passed on {rows} {scope} rows; no failures."


def _why_failed_on_known_clean_data(
    failure_count: int,
    sampled_rows: int | None,
) -> str:
    rows = sampled_rows if sampled_rows is not None else 0
    return (
        f"Test failed with {failure_count} failures on {rows} sampled rows; "
        "model.unique_id is in trusted_models, so the test is presumed buggy."
    )


def _why_kept(failure_count: int, sampled_rows: int | None, scope: Scope) -> str:
    rows = sampled_rows if sampled_rows is not None else 0
    return (
        f"Test failed with {failure_count} failures on {rows} {scope} rows; "
        "reviewer should evaluate."
    )


def _why_kept_without_evidence_warehouse_error(exc: WarehouseError) -> str:
    return f"Test could not be evaluated: {type(exc).__name__}: {exc.message}"


def _why_kept_without_evidence_budget(budget_seconds: int) -> str:
    return f"Total prune budget ({budget_seconds}s) exceeded before evaluation."


def _why_materialisation_failed(exc: WarehouseError) -> str:
    """DEC-005 of issue #22 — ``why`` field shape on the materialisation
    failure path.

    Format: ``f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"``.
    The 200-byte truncation keeps each per-decision audit row under the
    4000-byte JSONL limit (mirrors safety / draft / prune fail-closed
    audit caps); the typed-class prefix lets a reviewer correlate with
    the underlying warehouse adapter's own logs without sniffing the
    truncated tail.
    """
    return f"sample materialisation failed: {type(exc).__name__}: {str(exc)[:200]}"


def _maybe_emit_kept_rate_warning(
    decisions: list[PruneDecision],
    *,
    model_unique_id: str,
    threshold: float,
) -> None:
    """Emit one WARNING when the kept rate is at or below ``threshold`` (issue #51).

    No-op when the candidate set is empty — division-by-zero on
    ``len(decisions) == 0`` would be a degenerate signal of its own
    (the drafter produced nothing) and is not the failure mode this
    warning is here to catch. The orchestrator calls this helper once at
    every return site so the disabled / materialisation-failed branches
    are covered too; on those paths ``kept_rate`` is ``1.0`` so the
    warning only fires when an operator explicitly sets
    ``min_kept_rate_warn`` to ``1.0`` ("always warn").
    """

    total = len(decisions)
    if total == 0:
        return
    kept = sum(1 for d in decisions if d.decision == "kept")
    kept_rate = kept / total
    if kept_rate <= threshold:
        _LOGGER.warning(
            "prune kept rate at or below configured threshold: %s",
            json.dumps(
                {
                    "model_unique_id": model_unique_id,
                    "total_tests": total,
                    "kept": kept,
                    "dropped": total - kept,
                    "kept_rate": round(kept_rate, 4),
                    "min_kept_rate_warn": threshold,
                }
            ),
        )


def _why_prune_disabled() -> str:
    """DEC-003 of issue #35 — locked verbatim; pinned by a stability test."""
    return "prune disabled in signalforge.yml"


def _validate_trusted_models(config: PruneConfig, manifest: Manifest) -> None:
    """Validate every ``config.trusted_models`` entry exists in the
    manifest (DEC-008).

    Raised at orchestrator entry — BEFORE any warehouse call is issued —
    so a typo'd unique_id surfaces loudly rather than silently losing
    its "treat clean-data failure as a real failure" semantics. The
    first miss wins (don't accumulate); the user should fix one typo
    and re-run rather than chase a list.
    """
    for unique_id in config.trusted_models:
        if unique_id not in manifest.nodes:
            raise PruneTrustedModelNotFoundError(unique_id=unique_id)


def _iter_candidate_tests(
    candidates: CandidateSchema,
) -> list[tuple[str, CandidateTest]]:
    """Flatten every candidate test (per-column then model-level) into a
    list of ``(test_anchor, test)`` pairs.

    Per-column tests carry ``test_anchor=f"column.{column.name}"``;
    model-level tests carry the literal ``"model"``. Iteration order is
    deterministic: columns in declared order first, then model-level
    tests — this lets a reviewer correlate the JSONL audit row order
    with the candidate-schema YAML output.
    """
    pairs: list[tuple[str, CandidateTest]] = []
    for column in candidates.columns:
        for test in column.tests:
            pairs.append((f"column.{column.name}", test))
    for test in candidates.tests:
        pairs.append(("model", test))
    return pairs


def _write_audit_or_abort(
    decision: PruneDecision,
    *,
    model_unique_id: str,
    config_hash: str,
    audit_path: Path,
) -> None:
    """Write one :class:`PruneEvent` for ``decision`` via the fail-closed
    writer; raise :class:`PruneAuditWriteError` on I/O failure.

    DEC-016 — propagation IS the defence. A failed audit write aborts
    the prune run; the orchestrator does NOT continue with subsequent
    tests. Mirrors safety/draft fail-closed audit semantics.

    Exception ladder (mirrors :func:`signalforge.draft.draft_from_request`):

    * :class:`PruneAuditRecordTooLargeError` — already a typed
      :class:`PruneError`; propagates as-is so downstream
      pattern-matching can branch on it.
    * :class:`KeyboardInterrupt` / :class:`SystemExit` — signal-shaped
      exits propagate untouched. Wrapping them would silently demote a
      Ctrl-C into an audit error.
    * Any other :class:`BaseException` (most commonly :class:`OSError` /
      :class:`PermissionError` from the underlying ``os.write`` /
      ``os.fsync``) wraps as :class:`PruneAuditWriteError` with the
      original cause attached.
    """
    event = _build_prune_event(
        decision=decision,
        model_unique_id=model_unique_id,
        config_hash=config_hash,
    )
    try:
        _write_prune_event(event, audit_path)
    except PruneAuditRecordTooLargeError:
        # Already typed; propagate as-is.
        raise
    except (KeyboardInterrupt, SystemExit):
        # Signal-shaped exits must propagate untouched.
        raise
    except BaseException as exc:
        raise PruneAuditWriteError(
            "Failed to durably persist a prune-decision audit record.",
            cause=exc,
        ) from exc


def _decide_from_test_result(
    *,
    test: CandidateTest,
    test_anchor: str,
    test_result: TestResult,
    compiled_sql: str,
    compiled_sql_hash: str,
    elapsed_ms: int,
    sampled_rows: int | None,
    scope: Scope,
    is_trusted: bool,
    capture_failure_rows: int,
) -> PruneDecision:
    """Route a successful :class:`TestResult` into a :class:`PruneDecision`.

    Routing matrix (per the plan's decision table):

    * ``failure_count == 0`` → ``decision="dropped"``,
      ``reason="always-passes"`` (no signal — drops the test).
    * ``failure_count > 0`` AND ``is_trusted`` → ``decision="dropped"``,
      ``reason="failed-on-known-clean-data"`` (test is presumed buggy
      against a model whose data is treated as known-clean).
    * ``failure_count > 0`` AND not ``is_trusted`` → ``decision="kept"``,
      ``reason="kept"`` (real failure on untrusted data — reviewer
      should evaluate).
    """
    failure_count = test_result.failure_count
    sample_failures = tuple(test_result.sample_failures) if test_result.sample_failures else None
    if failure_count == 0:
        return PruneDecision(
            test_anchor=test_anchor,
            test=test,
            decision="dropped",
            reason="always-passes",
            failures=0,
            sampled_rows=sampled_rows,
            scope=scope,
            elapsed_ms=elapsed_ms,
            compiled_sql_hash=compiled_sql_hash,
            compiled_sql=compiled_sql,
            why=_why_always_passes(sampled_rows, scope),
            sample_failures=None,
        )
    if is_trusted:
        return PruneDecision(
            test_anchor=test_anchor,
            test=test,
            decision="dropped",
            reason="failed-on-known-clean-data",
            failures=failure_count,
            sampled_rows=sampled_rows,
            scope=scope,
            elapsed_ms=elapsed_ms,
            compiled_sql_hash=compiled_sql_hash,
            compiled_sql=compiled_sql,
            why=_why_failed_on_known_clean_data(failure_count, sampled_rows),
            sample_failures=sample_failures if capture_failure_rows > 0 else None,
        )
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision="kept",
        reason="kept",
        failures=failure_count,
        sampled_rows=sampled_rows,
        scope=scope,
        elapsed_ms=elapsed_ms,
        compiled_sql_hash=compiled_sql_hash,
        compiled_sql=compiled_sql,
        why=_why_kept(failure_count, sampled_rows, scope),
        sample_failures=sample_failures if capture_failure_rows > 0 else None,
    )


def _decide_requires_future_data(
    *,
    test: CandidateTest,
    test_anchor: str,
    sentinel: _RequiresFutureData,
    elapsed_ms: int,
    scope: Scope,
) -> PruneDecision:
    """Build a :class:`PruneDecision` for a ``relationships`` test whose
    parent isn't in the manifest (DEC-026).

    No warehouse call has been issued; ``compiled_sql`` is empty and
    ``compiled_sql_hash`` is the stable hash of the empty string so the
    audit JSONL shape stays uniform across decision types.
    """
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision="dropped",
        reason="requires-future-data",
        failures=0,
        sampled_rows=None,
        scope=scope,
        elapsed_ms=elapsed_ms,
        compiled_sql_hash=_build_compiled_sql_hash_or_empty(""),
        compiled_sql="",
        why=sentinel.reason,
        sample_failures=None,
    )


def _decide_kept_without_evidence_invalid_identifier(
    *,
    test: CandidateTest,
    test_anchor: str,
    sentinel: _InvalidIdentifier,
    elapsed_ms: int,
    scope: Scope,
) -> PruneDecision:
    """Build a :class:`PruneDecision` for a test whose ``column`` /
    ``field`` failed the SQL-identifier shape check (defence-in-depth).

    No warehouse call has been issued; ``compiled_sql`` is empty and
    ``compiled_sql_hash`` is the stable hash of the empty string. Routed
    to ``kept-without-evidence`` (decision="kept") rather than
    ``"dropped"`` because a malformed identifier MAY still be
    signal-bearing once the operator fixes the upstream prompt /
    manifest — conservative default avoids silently losing the test.
    """
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision="kept",
        reason="kept-without-evidence",
        failures=0,
        sampled_rows=None,
        scope=scope,
        elapsed_ms=elapsed_ms,
        compiled_sql_hash=_build_compiled_sql_hash_or_empty(""),
        compiled_sql="",
        why=sentinel.reason,
        sample_failures=None,
    )


def _decide_kept_without_evidence_warehouse_error(
    *,
    test: CandidateTest,
    test_anchor: str,
    exc: WarehouseError,
    compiled_sql: str,
    compiled_sql_hash: str,
    elapsed_ms: int,
    scope: Scope,
) -> PruneDecision:
    """Build a :class:`PruneDecision` for a test that raised a typed
    :class:`WarehouseError` during execution.

    Conservative default (DEC-006): keep tests we cannot evaluate.
    ``decision="kept"`` rather than ``"dropped"`` so a transient adapter
    failure / table-not-found / auth blip doesn't silently lose a
    signal-bearing test. The ``why`` carries the typed error class name
    so a reviewer can correlate with the warehouse adapter's own logs.
    """
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision="kept",
        reason="kept-without-evidence",
        failures=0,
        sampled_rows=None,
        scope=scope,
        elapsed_ms=elapsed_ms,
        compiled_sql_hash=compiled_sql_hash,
        compiled_sql=compiled_sql,
        why=_why_kept_without_evidence_warehouse_error(exc),
        sample_failures=None,
    )


def _decide_kept_without_evidence_materialisation_failed(
    *,
    test: CandidateTest,
    test_anchor: str,
    exc: WarehouseError,
    scope: Scope,
) -> PruneDecision:
    """Build a :class:`PruneDecision` for a test that never compiled because
    the per-run sample materialisation failed (DEC-005 / DEC-009 of issue
    #22).

    Conservative-bias routing: every candidate test in the run drains to
    ``decision="kept", reason="kept-without-evidence"`` regardless of
    which test it is, because we have no warehouse evidence for any of
    them. ``compiled_sql`` is empty (compilation never happened) and
    ``elapsed_ms`` is 0 (the test never ran). The audit JSONL row is
    still written so a reviewer sees exactly which tests would have been
    evaluated had the materialisation succeeded.
    """
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision="kept",
        reason="kept-without-evidence",
        failures=0,
        sampled_rows=None,
        scope=scope,
        elapsed_ms=0,
        compiled_sql_hash=_build_compiled_sql_hash_or_empty(""),
        compiled_sql="",
        why=_why_materialisation_failed(exc),
        sample_failures=None,
    )


def _decide_kept_without_evidence_disabled(
    *,
    test: CandidateTest,
    test_anchor: str,
    scope: Scope,
) -> PruneDecision:
    """DEC-001/DEC-007 of issue #35 — operator-chosen disable path.

    The operator set ``prune.enabled: false`` in ``signalforge.yml``. We
    route every candidate to ``kept-without-evidence`` (no
    :data:`DropReason` expansion — the 5-value literal stays locked per
    DEC-007 of #35) so the diff still surfaces every candidate, and the
    audit JSONL still records one :class:`PruneEvent` per candidate
    (fail-closed audit preserved per DEC-016 of #6). No warehouse call,
    no LLM call, ``compiled_sql`` is empty.

    Mirrors :func:`_decide_kept_without_evidence_materialisation_failed`
    structurally — the only differences are the ``why`` text and the
    absence of a triggering exception (the operator chose this path).
    """
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision="kept",
        reason="kept-without-evidence",
        failures=0,
        sampled_rows=None,
        scope=scope,
        elapsed_ms=0,
        compiled_sql_hash=_build_compiled_sql_hash_or_empty(""),
        compiled_sql="",
        why=_why_prune_disabled(),
        sample_failures=None,
    )


def _decide_kept_without_evidence_budget(
    *,
    test: CandidateTest,
    test_anchor: str,
    budget_seconds: int,
    scope: Scope,
) -> PruneDecision:
    """Build a :class:`PruneDecision` for a test that was never evaluated
    because the total prune budget was exhausted (DEC-011).

    No compilation has happened; ``compiled_sql`` is empty and
    ``elapsed_ms`` is 0 (the test never ran). The audit JSONL row is
    still written so a reviewer sees exactly which tests would have
    been evaluated next had the budget allowed.
    """
    return PruneDecision(
        test_anchor=test_anchor,
        test=test,
        decision="kept",
        reason="kept-without-evidence",
        failures=0,
        sampled_rows=None,
        scope=scope,
        elapsed_ms=0,
        compiled_sql_hash=_build_compiled_sql_hash_or_empty(""),
        compiled_sql="",
        why=_why_kept_without_evidence_budget(budget_seconds),
        sample_failures=None,
    )


def _resolve_sample_bucket(
    *,
    adapter: WarehouseAdapter,
    table_ref: TableRef,
    scope: Scope,
    sample_size: int,
) -> int | None:
    """Return the deterministic-sample bucket for ``scope=sample``,
    or ``None`` when ``scope=full``.

    The bucket is computed as ``max(num_rows // sample_size, 1)`` —
    matches the warehouse adapter's :meth:`sample_rows` derivation
    (DEC-006 of issue #3) so samples drawn through this seam align with
    samples the adapter would draw on its own.

    Reaches through the warehouse adapter's
    :func:`signalforge.warehouse.adapters._client._get_client` path to
    fetch ``Table.num_rows``. The adapter's own :meth:`sample_rows`
    encapsulates this lookup but does not surface row count separately;
    the prune layer needs the row count BEFORE issuing tests, so we
    accept the minor encapsulation crack here. v0.2 may add an explicit
    :meth:`WarehouseAdapter.get_table_metadata` seam.

    Raises :class:`PruneError` when ``num_rows`` is unknown — sampling
    against an unsized table would silently degrade to "every row" and
    defeat US-003's cost model. The fail-loud signal lands in front of
    the operator.
    """
    if scope != "sample":
        return None
    # Reach through the SDK shim to fetch row count. ``_get_client`` is
    # the documented internal seam on the BigQuery adapter (DEC-019 of
    # issue #3 — every SDK-noise comment lives in
    # ``adapters/_client.py``); the abstract base does NOT declare it,
    # so we route through ``getattr`` to keep pyright clean. v0.2 may
    # add an explicit ``WarehouseAdapter.get_table_metadata`` seam to
    # avoid the encapsulation crack; for now the prune layer needs
    # ``num_rows`` BEFORE issuing tests and the adapter's public
    # ``sample_rows`` does not surface it.
    get_client = getattr(adapter, "_get_client", None)
    if get_client is None:  # pragma: no cover - defensive; v0.1 only ships BigQuery
        raise PruneError(
            "sample-mode prune requires the adapter to expose `_get_client`; "
            f"{type(adapter).__name__} does not.",
            remediation=(
                "Set `prune.scope: full` in signalforge.yml until the v0.2 "
                "WarehouseAdapter.get_table_metadata seam lands."
            ),
        )
    try:
        client = get_client()
        meta = client.get_table(table_ref)
    except WarehouseError:
        # Adapter mapped the SDK exception. Re-raise as PruneError so
        # the caller's catch surface stays homogeneous: any failure to
        # establish the deterministic-sample bucket is a prune-layer
        # configuration / setup error.
        raise
    num_rows = getattr(meta, "num_rows", None)
    if num_rows is None or num_rows == 0:
        raise PruneError(
            f"sample-mode prune requires Table.num_rows for "
            f"{table_ref.qualified_name!r} but the warehouse returned None.",
            remediation=(
                "Verify the table is materialised and accessible. "
                "If it is genuinely empty (or num_rows is unavailable on the "
                "warehouse type), set `prune.scope: full` in signalforge.yml "
                "to bypass the deterministic-sample CTE."
            ),
        )
    return max(num_rows // sample_size, 1)


def prune_tests(
    model: Model,
    adapter: WarehouseAdapter,
    candidates: CandidateSchema,
    manifest: Manifest,
    *,
    config: PruneConfig | None = None,
    audit_path: Path | None = None,
    project_dir: Path | None = None,
) -> PruneResult:
    """Drop always-pass and known-clean-fail candidate tests.

    End-to-end orchestrator that integrates the prune layer's building
    blocks (compiler US-007, audit writer US-008, typed value objects
    US-004, error hierarchy US-006, config loader US-005) into one
    public seam. Mirrors :func:`signalforge.draft.draft_schema` so the
    CLI / wrapper layers see a consistent end-to-end shape across
    pipeline stages.

    Pipeline:

    1. Resolve config (``None`` → defaults). Compute ``config_hash`` via
       ``blake2b(canonical_json, digest_size=8)`` for audit-row provenance
       (16 hex chars; migrated from ``SHA-256[:16]`` by issue #55 so the
       audit corpus reads one hash recipe across every writer).
    2. Validate :attr:`PruneConfig.trusted_models` against ``manifest``
       (DEC-008). The first miss raises
       :class:`PruneTrustedModelNotFoundError`; no warehouse calls have
       been issued.
    3. Resolve ``TableRef.from_model(model)`` for the model under
       prune. May raise :class:`ManifestProjectNotFoundError` /
       :class:`ManifestSchemaNotFoundError` — those are manifest-shape
       problems and propagate as-is.
    4. Iterate every :class:`CandidateTest` (per-column, then
       model-level). Track a running monotonic-ms clock; once
       ``elapsed_total_ms ≥ config.total_budget_seconds * 1000`` mark
       every remaining test ``kept-without-evidence`` and stop
       dispatching warehouse calls (DEC-011).
    5. For each test:

       a. Compile via :func:`signalforge.prune.compiler._compile_test`.
          ``_RequiresFutureData`` sentinel → ``requires-future-data``
          drop (no warehouse call). Otherwise → continue with the
          compiled SQL string.
       b. Run via :meth:`adapter.run_test_sql` inside a
          ``try/except WarehouseError`` block. A typed
          :class:`WarehouseError` routes to ``kept-without-evidence``
          with the error class name in ``why``.
       c. Inspect :attr:`TestResult.failure_count`:

          * ``0`` → ``always-passes`` (drop).
          * ``> 0`` AND ``model.unique_id`` in ``config.trusted_models``
            → ``failed-on-known-clean-data`` (drop).
          * ``> 0`` otherwise → ``kept`` (keep).
       d. Write the audit JSONL row via the fail-closed writer.
          A failed audit write aborts the run (DEC-016).
    6. Build the aggregate :class:`PruneResult` and return.

    Caller responsibilities:

    * Loading config from ``signalforge.yml`` via
      :func:`signalforge.prune.load_prune_config`.
    * Opening the adapter via
      :meth:`signalforge.warehouse.WarehouseAdapter.from_profile` or
      direct construction.
    * Ensuring the loaded :class:`Manifest` is consistent with the
      ``model`` under prune.

    Context-manager ownership (US-005 of issue #22):
        ``prune_tests`` invokes ``adapter`` inside a ``with`` block
        itself, so :meth:`WarehouseAdapter.__exit__` always runs —
        including on the materialisation-failure path. **Callers MUST
        pass an adapter that has not already been entered**; double
        ``__enter__`` is undefined behaviour. Do not wrap the
        ``prune_tests`` call in your own ``with adapter:`` — the
        engine owns the context. Without ``__exit__`` running,
        BigQuery materialised sessions rely on the server-side TTL
        for cleanup (~24 hours) instead of the explicit
        ``CALL BQ.ABORT_SESSION()`` the adapter issues from
        ``__exit__`` (DEC-013 of issue #22). A non-BigQuery adapter
        that does not maintain session state still sees its own
        cleanup hooks fire correctly because every
        :class:`WarehouseAdapter` subclass implements
        ``__enter__`` / ``__exit__``.

    Args:
        model: the manifest :class:`Model` under prune.
        adapter: the :class:`WarehouseAdapter` (BigQuery in v0.1).
        candidates: the :class:`CandidateSchema` from the LLM drafter
            (#5).
        manifest: the parent :class:`Manifest` — used by the compiler's
            relationships-resolver (DEC-026) and the trusted-models
            validator (DEC-008).
        config: optional :class:`PruneConfig`; ``None`` resolves to
            :class:`PruneConfig` defaults from DEC-009. The caller is
            responsible for loading from disk via
            :func:`load_prune_config`.
        audit_path: optional override for the prune-audit JSONL path.
            ``None`` resolves to
            ``<project_dir> / ".signalforge" / "prune.jsonl"`` (default
            ``project_dir`` is :func:`pathlib.Path.cwd`); the parent
            directory is created if missing. The resolved path is
            symlink-hardened via
            :func:`signalforge._common.path_safety.canonicalise_path`
            before any write so a symlinked
            ``<project>/.signalforge/prune.jsonl`` cannot redirect
            writes outside the project tree (DEC-016).
        project_dir: optional project-root override used to resolve the
            default ``audit_path``. ``None`` resolves to
            :func:`pathlib.Path.cwd`. Mirrors the other layers'
            project-relative resolution so the prune audit lands in the
            right place regardless of which sub-directory the CLI
            invokes from.

    Returns:
        A :class:`PruneResult` carrying every per-test
        :class:`PruneDecision`, the run's elapsed time, and the version
        stamp.

    Raises:
        PruneTrustedModelNotFoundError: a ``trusted_models`` entry does
            not match any model in ``manifest.nodes`` (DEC-008). Raised
            at entry — no warehouse calls issued.
        PruneAuditRecordTooLargeError: a per-decision audit record
            exceeded the POSIX-atomic-append size cap. Aborts the run
            (DEC-016). Propagates as-is.
        PruneAuditWriteError: any other I/O / encoding failure in the
            audit writer. Aborts the run (DEC-016) and wraps the
            underlying exception on ``cause``.
        ManifestProjectNotFoundError, ManifestSchemaNotFoundError: the
            ``model`` under prune lacks ``database`` / ``schema``;
            propagated from :meth:`TableRef.from_model`.
    """
    resolved_config = config if config is not None else PruneConfig()

    # Resolve audit path. Default: <project_dir>/.signalforge/prune.jsonl
    # (project_dir defaults to cwd). Resolving relative to project_dir
    # rather than cwd matches the safety + draft layers — when the CLI
    # is invoked from a sub-directory, the audit lands next to the
    # project, not next to wherever the user happened to be.
    #
    # DEC-002 of issue #35 — audit-path resolution + symlink-hardening
    # MUST run before the ``enabled=False`` short-circuit (we still
    # write one PruneEvent per candidate on the disabled path per
    # DEC-001 of #35), but BEFORE ``_validate_trusted_models`` and
    # ``TableRef.from_model`` (an operator who disabled prune shouldn't
    # need a valid trusted_models list or a manifest-shape-clean model).
    resolved_project_dir = project_dir if project_dir is not None else Path.cwd()
    raw_audit_path = (
        audit_path
        if audit_path is not None
        else resolved_project_dir / ".signalforge" / "prune.jsonl"
    )
    # Create the parent directory if missing — the writer itself does
    # not mkdir (it expects the parent to exist; the orchestrator owns
    # that contract per the writer's docstring). mkdir runs BEFORE
    # canonicalise_path so the directory exists for the resolve(strict).
    raw_audit_path.parent.mkdir(parents=True, exist_ok=True)

    # Symlink-harden the audit path (DEC-016) — a symlinked
    # `.signalforge/prune.jsonl` pointing outside the project tree
    # could redirect writes to an attacker-controlled location.
    # ``canonicalise_path`` requires project_dir to exist; it raises
    # :class:`PathContainmentError` (layer-neutral, defined in
    # :mod:`signalforge._common.path_safety`) on cycle / missing-dir /
    # containment-violation. Wrap as :class:`PruneAuditWriteError` so the
    # prune layer's catch surface stays homogeneous — every "we couldn't
    # durably persist the audit" condition raises a single typed error.
    resolved_project_dir.mkdir(parents=True, exist_ok=True)
    try:
        resolved_audit_path = canonicalise_path(raw_audit_path, resolved_project_dir)
    except PathContainmentError as exc:
        raise PruneAuditWriteError(
            "Prune-audit path failed symlink-hardened canonicalisation; "
            "refusing to write outside the project tree.",
            cause=exc,
        ) from exc

    # Compute the config hash once per run so every audit row in this
    # run shares a single config_hash; mirrors the safety-layer
    # policy_hash convention.
    config_hash = _compute_config_hash(
        resolved_config.model_dump_json(),
    )

    pairs = _iter_candidate_tests(candidates)
    decisions: list[PruneDecision] = []
    start_ms = _now_monotonic_ms()

    # DEC-001/DEC-002/DEC-003/DEC-007 of issue #35 — operator-chosen
    # disable short-circuit. Fires AFTER audit-path symlink-hardening
    # and ``config_hash`` computation (so every audit row is durable
    # and provenance-stamped), but BEFORE ``_validate_trusted_models``,
    # ``TableRef.from_model``, ``adapter.dialect``, and ``with adapter:``.
    # An operator who disabled prune shouldn't be blocked by a stale
    # ``trusted_models`` entry or a manifest-shape problem on a model
    # whose warehouse we're not going to touch.
    #
    # Conservative-bias routing (mirrors the materialisation-failed
    # branch verbatim per DEC-009 of #22): every candidate routes to
    # ``kept-without-evidence`` with the locked ``why`` text from
    # DEC-003. One PruneEvent per candidate (fail-closed audit
    # preserved per DEC-016 of #6) — skipping the audit on a "fast
    # path" would violate the invariant.
    if not resolved_config.enabled:
        scope_disabled: Scope = resolved_config.scope
        for test_anchor, test in pairs:
            decision = _decide_kept_without_evidence_disabled(
                test=test,
                test_anchor=test_anchor,
                scope=scope_disabled,
            )
            _write_audit_or_abort(
                decision,
                model_unique_id=model.unique_id,
                config_hash=config_hash,
                audit_path=resolved_audit_path,
            )
            decisions.append(decision)

        total_elapsed_ms = max(0, _now_monotonic_ms() - start_ms)
        _maybe_emit_kept_rate_warning(
            decisions,
            model_unique_id=model.unique_id,
            threshold=resolved_config.min_kept_rate_warn,
        )
        return PruneResult(
            model_unique_id=model.unique_id,
            decisions=tuple(decisions),
            elapsed_ms=total_elapsed_ms,
            signalforge_version=_SIGNALFORGE_VERSION,
        )

    # Empty-candidate short-circuit. When there are zero candidate tests
    # to evaluate (e.g. every test in an externally-authored schema.yml
    # was skip-recorded by the ingest layer — issue #105's
    # ``prune-existing`` all-unsupported case), there is nothing to
    # compile, run, or materialise. Return an empty PruneResult WITHOUT
    # entering ``with adapter:`` — on the default ``materialised`` +
    # ``sample`` path the adapter would otherwise issue a real
    # ``CREATE TEMP TABLE ... AS SELECT`` to materialise a sample for
    # zero tests, incurring warehouse cost for no signal. Mirrors the
    # disabled short-circuit's no-warehouse-contact posture above; the
    # fail-closed audit invariant is preserved trivially (zero decisions
    # → zero PruneEvents).
    if not pairs:
        # Call the kept-rate WARNING helper at this return site too, per
        # the "called at every prune_tests return site" contract (issue
        # #51) — it's a no-op here (the helper early-returns on
        # ``total == 0``), but routing every return site through it keeps
        # a future change to the empty-candidate path from silently
        # dropping the signal.
        _maybe_emit_kept_rate_warning(
            [],
            model_unique_id=model.unique_id,
            threshold=resolved_config.min_kept_rate_warn,
        )
        return PruneResult(
            model_unique_id=model.unique_id,
            decisions=(),
            elapsed_ms=max(0, _now_monotonic_ms() - start_ms),
            signalforge_version=_SIGNALFORGE_VERSION,
        )

    # Validate trusted_models BEFORE any warehouse call (DEC-008).
    _validate_trusted_models(resolved_config, manifest)

    source_table_ref = TableRef.from_model(model)
    dialect = adapter.dialect()
    is_trusted = model.unique_id in resolved_config.trusted_models
    scope: Scope = resolved_config.scope

    total_budget_ms = resolved_config.total_budget_seconds * 1000

    # US-005 of issue #22 — wrap the adapter in ``with`` so
    # :meth:`WarehouseAdapter.__exit__` always runs (DEC-013 of #22 —
    # explicit ``CALL BQ.ABORT_SESSION();`` cleanup for any session
    # minted by ``materialise_sample``). The ``with`` block covers the
    # OneShot path too because DEC-008/DEC-025 of #3 already require it
    # for ``column_stats`` batching state.
    with adapter:
        # ----------------------------------------------------------------
        # Strategy dispatch (DEC-006 / Q3 / Q7 of issue #22).
        #
        # ``"materialised"`` (default per DEC-006):
        #   1. Materialise a single sample table once via
        #      :meth:`adapter.materialise_sample`.
        #   2. Compile every per-test failing-rows SQL against the
        #      returned :class:`TableRef` (``_SESSION._sf_sample_<run_id>``)
        #      with effective ``scope="full"`` so the compiler does NOT
        #      add a redundant deterministic-sample CTE on top of an
        #      already-sampled table. The decision's ``scope`` field still
        #      records the user-facing ``config.scope`` so the audit row
        #      tells a reviewer "this test ran against a (materialised)
        #      sample".
        #   3. On any :class:`WarehouseError` (the typed
        #      :class:`MaterialisationFailedError`,
        #      :class:`UnknownTableSizeError`,
        #      :class:`SamplingRequiresPartitionFilterError`,
        #      :class:`MaterialisationNotSupportedError`, or any other
        #      subclass): emit ONE DEC-009 degraded-run WARNING, then
        #      route every candidate to ``kept-without-evidence`` with
        #      the DEC-005 ``why`` shape and write ONE PruneEvent per
        #      candidate (fail-closed audit preserved per DEC-016 of #6).
        #
        # ``"oneshot"``: v0.1 path unchanged — bucket via
        # :func:`_resolve_sample_bucket`, compile per-test with the
        # ``scope`` / ``sample_size`` / ``sample_bucket`` arguments the
        # compiler already knows how to wrap.
        # ----------------------------------------------------------------
        compile_table_ref: TableRef
        compile_scope: Scope
        sample_bucket: int | None
        compile_partition_filter = resolved_config.partition_filter

        if resolved_config.sample_strategy == "materialised" and scope == "sample":
            try:
                materialised_ref = adapter.materialise_sample(
                    source_table_ref,
                    resolved_config.sample_size,
                    partition_filter=resolved_config.partition_filter,
                )
            except WarehouseError as exc:
                # DEC-009 of issue #22 — single degraded-run WARNING
                # fires at the head of the conservative-bias routing
                # path BEFORE any audit-write iteration. Lazy-format
                # JSON per the layer-wide DEC-017 logger gate; never
                # f-string-interpolate user-controlled values.
                _LOGGER.warning(
                    "materialisation failed; routing all tests to kept-without-evidence: %s",
                    json.dumps(
                        {
                            "model_unique_id": model.unique_id,
                            "candidate_count": len(pairs),
                            "error_class": type(exc).__name__,
                            "error_message": str(exc)[:200],
                        }
                    ),
                )
                # Fail-closed audit preserved (DEC-016 of #6): one
                # PruneEvent per candidate, even on the all-failed path.
                # The audit-write loop runs to completion unless the
                # writer itself fails — no early return.
                for test_anchor, test in pairs:
                    decision = _decide_kept_without_evidence_materialisation_failed(
                        test=test,
                        test_anchor=test_anchor,
                        exc=exc,
                        scope=scope,
                    )
                    _write_audit_or_abort(
                        decision,
                        model_unique_id=model.unique_id,
                        config_hash=config_hash,
                        audit_path=resolved_audit_path,
                    )
                    decisions.append(decision)

                total_elapsed_ms = max(0, _now_monotonic_ms() - start_ms)
                _maybe_emit_kept_rate_warning(
                    decisions,
                    model_unique_id=model.unique_id,
                    threshold=resolved_config.min_kept_rate_warn,
                )
                return PruneResult(
                    model_unique_id=model.unique_id,
                    decisions=tuple(decisions),
                    elapsed_ms=total_elapsed_ms,
                    signalforge_version=_SIGNALFORGE_VERSION,
                )

            # Materialisation succeeded — every per-test compile
            # references the temp table directly. Effective compile
            # scope is "full" so the compiler does NOT wrap a
            # redundant deterministic-sample CTE on top of an
            # already-sampled table; the decision's user-facing
            # ``scope`` field stays at ``config.scope``.
            compile_table_ref = materialised_ref
            compile_scope = "full"
            sample_bucket = None
            # The materialisation already filtered the partitions
            # (Q5 of issue #22 — partition_filter applies once inside
            # the CTAS WHERE clause, not on every per-test query).
            compile_partition_filter = None
        else:
            # ``oneshot`` strategy OR ``scope="full"`` (no sampling at
            # all) — v0.1 path. The bucket lookup runs only when
            # scope=sample.
            compile_table_ref = source_table_ref
            compile_scope = scope
            sample_bucket = _resolve_sample_bucket(
                adapter=adapter,
                table_ref=source_table_ref,
                scope=scope,
                sample_size=resolved_config.sample_size,
            )

        budget_exhausted = False

        for test_anchor, test in pairs:
            # Total-budget gate (DEC-011 of #6 / DEC-010 of #22 — the
            # watchdog ticks across both materialisation AND the
            # per-test loop). Checked BEFORE any compile or warehouse
            # call. Once exceeded, every remaining test drains to
            # ``kept-without-evidence`` without dispatching.
            if not budget_exhausted:
                elapsed_total_ms = _now_monotonic_ms() - start_ms
                if elapsed_total_ms >= total_budget_ms:
                    budget_exhausted = True

            if budget_exhausted:
                decision = _decide_kept_without_evidence_budget(
                    test=test,
                    test_anchor=test_anchor,
                    budget_seconds=resolved_config.total_budget_seconds,
                    scope=scope,
                )
                _write_audit_or_abort(
                    decision,
                    model_unique_id=model.unique_id,
                    config_hash=config_hash,
                    audit_path=resolved_audit_path,
                )
                decisions.append(decision)
                continue

            # Compile the candidate test to failing-rows SQL. Returns
            # either a string (the SELECT), a ``_RequiresFutureData``
            # sentinel, or an ``_InvalidIdentifier`` sentinel.
            compile_result = _compile_test(
                test,
                compile_table_ref,
                dialect,
                manifest,
                scope=compile_scope,
                sample_size=(resolved_config.sample_size if compile_scope == "sample" else None),
                sample_bucket=sample_bucket,
                partition_filter=compile_partition_filter,
            )
            if isinstance(compile_result, _RequiresFutureData):
                decision = _decide_requires_future_data(
                    test=test,
                    test_anchor=test_anchor,
                    sentinel=compile_result,
                    elapsed_ms=0,
                    scope=scope,
                )
                _write_audit_or_abort(
                    decision,
                    model_unique_id=model.unique_id,
                    config_hash=config_hash,
                    audit_path=resolved_audit_path,
                )
                decisions.append(decision)
                continue

            if isinstance(compile_result, _InvalidIdentifier):
                decision = _decide_kept_without_evidence_invalid_identifier(
                    test=test,
                    test_anchor=test_anchor,
                    sentinel=compile_result,
                    elapsed_ms=0,
                    scope=scope,
                )
                _write_audit_or_abort(
                    decision,
                    model_unique_id=model.unique_id,
                    config_hash=config_hash,
                    audit_path=resolved_audit_path,
                )
                decisions.append(decision)
                continue

            compiled_sql: str = compile_result
            compiled_sql_hash = _build_compiled_sql_hash_or_empty(compiled_sql)

            # Per-test timing wraps the warehouse call.
            test_start_ms = _now_monotonic_ms()
            try:
                test_result = adapter.run_test_sql(
                    compiled_sql,
                    capture_failures=resolved_config.capture_failure_rows,
                )
            except WarehouseError as exc:
                elapsed_ms = max(0, _now_monotonic_ms() - test_start_ms)
                # Lazy-format JSON per DEC-017 — never f-string-interpolate
                # user-controlled values into a logger call.
                _LOGGER.warning(
                    "kept-without-evidence: %s",
                    json.dumps(
                        {
                            "model_unique_id": model.unique_id,
                            "test_anchor": test_anchor,
                            "error_class": type(exc).__name__,
                        }
                    ),
                )
                decision = _decide_kept_without_evidence_warehouse_error(
                    test=test,
                    test_anchor=test_anchor,
                    exc=exc,
                    compiled_sql=compiled_sql,
                    compiled_sql_hash=compiled_sql_hash,
                    elapsed_ms=elapsed_ms,
                    scope=scope,
                )
                _write_audit_or_abort(
                    decision,
                    model_unique_id=model.unique_id,
                    config_hash=config_hash,
                    audit_path=resolved_audit_path,
                )
                decisions.append(decision)
                continue

            elapsed_ms = max(0, _now_monotonic_ms() - test_start_ms)
            # ``sampled_rows`` is the size of the row set the test
            # ran against. v0.1 does not yet thread the sample-size
            # from the adapter's TABLESAMPLE / hash-mod path through
            # to the prune layer (see DEC-026 deferral notes); we
            # record None for ``"sample"`` scope until US-011 wires
            # it through. For ``"full"`` scope sampled_rows is None
            # by design (the model contract:
            # ``scope == "full"`` ↔ sampled_rows is None).
            sampled_rows: int | None = None
            decision = _decide_from_test_result(
                test=test,
                test_anchor=test_anchor,
                test_result=test_result,
                compiled_sql=compiled_sql,
                compiled_sql_hash=compiled_sql_hash,
                elapsed_ms=elapsed_ms,
                sampled_rows=sampled_rows,
                scope=scope,
                is_trusted=is_trusted,
                capture_failure_rows=resolved_config.capture_failure_rows,
            )
            _write_audit_or_abort(
                decision,
                model_unique_id=model.unique_id,
                config_hash=config_hash,
                audit_path=resolved_audit_path,
            )
            decisions.append(decision)

        total_elapsed_ms = max(0, _now_monotonic_ms() - start_ms)
        _maybe_emit_kept_rate_warning(
            decisions,
            model_unique_id=model.unique_id,
            threshold=resolved_config.min_kept_rate_warn,
        )
        return PruneResult(
            model_unique_id=model.unique_id,
            decisions=tuple(decisions),
            elapsed_ms=total_elapsed_ms,
            signalforge_version=_SIGNALFORGE_VERSION,
        )


__all__ = ("prune_tests",)
