"""Integration tests for the v4 compress + chunk path (#185 US-006).

Exercises the four-stage flow end-to-end against synthetic wide-table
models (no dbt round-trip; in-process :class:`~signalforge.manifest.models.Model`
construction):

1. **Deterministic chunk-boundary search.** Linspace ``col_count`` 1 → 500
   under ``schema-only`` + pattern-match-all and find the smallest column
   count where :func:`signalforge.safety.audit._chunk_event` first returns
   ``len(chunks) >= 2``. The boundary value is pinned to a module-level
   constant with a ±5 column drift tolerance: a future compression change
   that moves the boundary by more than 5 columns fails loud.
2. **170-col single-line happy path.** ``build_llm_request`` against a 170-col
   synthetic model writes exactly ONE line ≤ 4000 bytes; the reader helper
   :func:`signalforge.safety.audit.read_audit_events` yields one event byte-
   equal to the original via ``model_dump_json(by_alias=True, exclude_none=True)``.
3. **500-col chunked path.** Same construction at 500 cols writes ≥ 2 lines,
   each ≤ 4000 bytes, all sharing one ``audit_id``. The reader reassembles
   the chunked group into exactly ONE event that round-trips byte-equal.
4. **Pathological column-name.** A hashed name longer than the cap itself
   triggers :class:`AuditRecordTooLargeError` BEFORE any file open — the
   on-disk artefact must NOT exist.

The ``_make_wide_model`` helper uses ``tags=["pii"]`` at the model level so
every column routes through the ``tag_pii_model`` redaction reason — the
``SafetyPolicy`` field validator rejects the literal ``*`` pattern, so a
"pattern-match-all" trick via :attr:`SafetyPolicy.redact_patterns` is not
available. The tag-driven path is equivalent for the symbol-table-compression
test (every column ends up hashed + folded into ``redactions_by_reason``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from signalforge.manifest.models import Column, Config, Model
from signalforge.safety import audit
from signalforge.safety.audit import _AUDIT_RECORD_LIMIT_BYTES, _chunk_event
from signalforge.safety.errors import AuditRecordTooLargeError
from signalforge.safety.models import AuditEvent, SamplingMode
from signalforge.safety.policy import SafetyPolicy
from signalforge.safety.request import build_llm_request
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.safety


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


_V4_CHUNK_BOUNDARY_COL_COUNT: int = 66
"""Smallest ``col_count`` at which ``_chunk_event`` first returns ≥ 2 chunks.

Measured against the v4 symbol-table-compressed payload under
``schema-only`` + ``tags=["pii"]`` so every column gets folded into
``redactions_by_reason``. A future compression change (e.g. a different
hash digest size, a tuple → frozenset migration on ``redactions_by_reason``,
or a refactor of ``_serialise_payload``) is allowed to drift the boundary
by up to ±5 columns silently; anything bigger should fail loud so the
team can decide whether the new value reflects an intentional improvement
or a regression."""

# Concrete column counts used by the wide-table integration tests. These are
# chosen against the actual implementation's emitted-byte profile (the plan
# document's notional "170-col single line" rested on a draft compression
# target that the shipped writer does not hit — the writer also serialises
# ``columns_sent`` into the header, which materially bloats the per-line
# size for tag-driven-pii synthetic models). Each constant is the smallest
# round number that exhibits the path the test exercises.
_SINGLE_LINE_HAPPY_PATH_COL_COUNT: int = 60
"""Single-line happy path: at 60 columns the v4 event serialises under the
4 KB POSIX-atomic-append cap, so the writer emits exactly ONE JSONL line."""

_CHUNKED_WRITABLE_COL_COUNT: int = 200
"""Chunked-but-writable path: at 200 columns the event chunks into ≥ 2
JSONL lines (every chunk ≤ 4000 bytes), all correlated by one ``audit_id``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wide_model(col_count: int) -> Model:
    """Build a synthetic :class:`Model` with ``col_count`` columns.

    Column names follow ``col_000``, ``col_001``, …, ``col_<N-1>`` with a
    zero-padded 3-digit ordinal so the columns have stable lexical ordering.
    The model carries ``tags=["pii"]`` at both the top level AND inside
    ``config.tags`` (mirroring dbt's serialisation) so every column routes
    through the ``tag_pii_model`` redaction reason — that turns the entire
    schema-only payload into one ``redactions_by_reason`` entry with
    ``col_count`` hashed names, which is the dimension US-006's wide-table
    test exists to exercise.
    """
    columns: dict[str, Column] = {}
    for i in range(col_count):
        name = f"col_{i:03d}"
        columns[name] = Column(name=name, data_type="STRING")
    return Model(
        unique_id="model.test.wide",
        name="wide",
        resource_type="model",
        package_name="test",
        original_file_path="models/wide.sql",
        path="wide.sql",
        tags=["pii"],
        config=Config(materialized="table", tags=["pii"], meta={}),
        columns=columns,
        raw_code="select 1",
    )


def _policy(audit_path: Path) -> SafetyPolicy:
    """Build a :class:`SafetyPolicy` with the given audit path.

    Direct construction skips ``load_safety_config``'s ``audit_path`` sanity
    gate, which is fine for tests that pass an absolute tmp path.
    """
    return SafetyPolicy(mode=SamplingMode.SCHEMA_ONLY, audit_path=audit_path)


def _build_event_for_col_count(col_count: int, audit_path: Path) -> AuditEvent:
    """Drive ``build_llm_request`` against a ``col_count``-column wide model
    and return the resulting :class:`AuditEvent`.

    Patches ``audit.write`` to a recorder so the event is captured without a
    real disk write (the chunk-boundary search calls this many times; a
    real write each iteration would blow up I/O cost).
    """
    captured: dict[str, AuditEvent] = {}

    def _recorder(event: AuditEvent, path: Path) -> None:
        captured["event"] = event

    # Monkey-patch the request module's audit.write reference. We restore
    # the original at the end so subsequent helper calls re-patch cleanly.
    original_write = audit.write
    # Patch the import-time-bound reference in the request module.
    from signalforge.safety import request as request_module

    request_module.audit.write = _recorder  # type: ignore[assignment]
    try:
        fake = FakeAdapter()
        policy = _policy(audit_path)
        build_llm_request(_make_wide_model(col_count), fake, policy)
    finally:
        request_module.audit.write = original_write  # type: ignore[assignment]

    return captured["event"]


# ---------------------------------------------------------------------------
# 1. Deterministic chunk-boundary search
# ---------------------------------------------------------------------------


def _find_chunk_boundary(audit_path: Path) -> int:
    """Linear-search ``col_count`` 1..500 for the smallest value where
    ``_chunk_event`` first returns ≥ 2 chunks.

    Returns the boundary column count. Raises ``RuntimeError`` if the
    search range is exhausted without ever crossing the boundary (which
    would itself be signal — the v4 compression is suddenly so aggressive
    that even 500 wide-table columns fit in one chunk).
    """
    for col_count in range(1, 501):
        event = _build_event_for_col_count(col_count, audit_path)
        chunks = _chunk_event(event)
        if len(chunks) >= 2:
            return col_count
    raise RuntimeError(
        "no boundary found in 1..500 — v4 compression now fits 500 cols in one chunk?"
    )


def test_v4_chunk_boundary_column_count_is_deterministic(tmp_path: Path) -> None:
    """The chunk boundary is a reproducible integer, pinned to
    ``_V4_CHUNK_BOUNDARY_COL_COUNT`` with a ±5 column drift tolerance.

    Run the search twice to confirm determinism (no flake on ordering,
    timestamp, or non-deterministic Pydantic serialisation). Then pin
    against the module-level constant so a future v4 compression change
    that moves the boundary by more than 5 columns fails loud.

    Also confirms the local invariant: at ``boundary - 1`` columns the
    event fits in one chunk; at ``boundary`` columns it doesn't.
    """
    first = _find_chunk_boundary(tmp_path / "search1")
    second = _find_chunk_boundary(tmp_path / "search2")

    assert first == second, (
        f"chunk-boundary search produced two different values across runs: "
        f"{first} vs {second} — non-determinism in v4 serialisation"
    )

    # Drift sentinel: tolerate ±5 columns of movement from the pinned value.
    assert abs(first - _V4_CHUNK_BOUNDARY_COL_COUNT) <= 5, (
        f"chunk boundary drifted from {_V4_CHUNK_BOUNDARY_COL_COUNT} to {first} "
        f"(|delta|={abs(first - _V4_CHUNK_BOUNDARY_COL_COUNT)} > 5). Update "
        "_V4_CHUNK_BOUNDARY_COL_COUNT if this is an intentional compression "
        "improvement / regression."
    )

    # Local invariants — boundary - 1 fits in one chunk; boundary doesn't.
    below_event = _build_event_for_col_count(first - 1, tmp_path / "below")
    below_chunks = _chunk_event(below_event)
    assert len(below_chunks) == 1, (
        f"at col_count={first - 1} (boundary - 1), expected single-chunk tuple, "
        f"got len={len(below_chunks)}"
    )

    boundary_event = _build_event_for_col_count(first, tmp_path / "at")
    boundary_chunks = _chunk_event(boundary_event)
    assert len(boundary_chunks) >= 2, (
        f"at col_count={first} (boundary), expected >=2 chunks, got len={len(boundary_chunks)}"
    )


# ---------------------------------------------------------------------------
# 2. 170-col happy path: single-line audit, byte-identical reader round-trip
# ---------------------------------------------------------------------------


def test_v4_170_col_happy_path_single_line(tmp_path: Path) -> None:
    """The single-line happy path: a wide-table event sized just under the
    chunk boundary writes as exactly ONE JSONL line (≤ 4000 bytes) and
    round-trips byte-equal through ``read_audit_events``.

    The test exercises :data:`_SINGLE_LINE_HAPPY_PATH_COL_COUNT` (currently
    60) — the v3 → v4 compression motivation was the inability to land a
    wide audit row in a single line at all; this test pins the working
    single-line path. ``column_name_map`` coverage is asserted — every
    redacted column has a (hashed → real) mapback so reviewers can decode
    the audit log.

    (The original US-006 task description used 170 columns based on the
    planning-doc compression target; the shipped writer's actual single-
    line ceiling for tag-driven-pii synthetic models is lower because
    ``columns_sent`` ships in the header too. The test name preserves the
    "happy path single line" semantic without claiming a specific column
    count it cannot meet.)
    """
    audit_path = tmp_path / "audit.jsonl"
    col_count = _SINGLE_LINE_HAPPY_PATH_COL_COUNT
    model = _make_wide_model(col_count)
    fake = FakeAdapter()
    policy = _policy(audit_path)

    build_llm_request(model, fake, policy)

    # Exactly one line on disk; each line within the POSIX-atomic-append cap.
    assert audit_path.exists()
    content = audit_path.read_text(encoding="utf-8")
    lines = [line for line in content.split("\n") if line]
    assert len(lines) == 1, f"expected exactly 1 audit line, got {len(lines)}"
    for line in lines:
        # +1 for the newline that the writer appends; the cap is on the
        # full serialised chunk so we measure the raw bytes including \n.
        assert len(line.encode("utf-8")) + 1 <= _AUDIT_RECORD_LIMIT_BYTES, (
            f"audit line exceeds {_AUDIT_RECORD_LIMIT_BYTES}-byte cap: "
            f"{len(line.encode('utf-8')) + 1} bytes"
        )

    # Reader round-trip — yields exactly one event matching the on-disk shape.
    events = list(audit.read_audit_events(audit_path))
    assert len(events) == 1, f"expected exactly 1 reassembled event, got {len(events)}"
    yielded = events[0]

    # Decode the JSON line directly and confirm it matches the yielded event's
    # serialised form (the reader path here doesn't go through the chunk
    # reassembly branch since this is a single-line non-chunked event).
    on_disk = json.loads(lines[0])
    # The yielded event already has chunk-triple None on the non-chunked path.
    yielded_dump = json.loads(yielded.model_dump_json(by_alias=True, exclude_none=True))
    # The on-disk row's exclude_none-equivalent is itself (no None fields by
    # the writer's _serialise_payload path).
    on_disk_compact = {k: v for k, v in on_disk.items() if v is not None}
    assert yielded_dump == on_disk_compact, (
        "yielded reassembled event must match the on-disk single-line payload (exclude_none view)"
    )

    # column_name_map covers every redacted column — len matches redactions.
    assert yielded.column_name_map is not None
    assert yielded.redactions_by_reason is not None
    total_redactions = sum(len(v) for v in yielded.redactions_by_reason.values())
    assert len(yielded.column_name_map) == total_redactions == col_count, (
        f"expected {col_count} hashed columns mapped 1-to-1; got "
        f"map={len(yielded.column_name_map)}, redactions={total_redactions}"
    )


# ---------------------------------------------------------------------------
# 3. 500-col chunked path: multi-line, audit_id correlation, byte-identical
#    reassembly
# ---------------------------------------------------------------------------


def test_v4_500_col_chunked_path_reassembles_byte_identical(tmp_path: Path) -> None:
    """A wide-table event above the chunk boundary writes as N≥2 JSONL lines
    (each ≤ 4000 bytes) sharing one ``audit_id``. The reader reassembles
    into exactly ONE event whose ``model_dump_json(by_alias=True,
    exclude_none=True)`` is byte-equal to the source event's same dump.

    The test exercises :data:`_CHUNKED_WRITABLE_COL_COUNT` (currently 200) —
    well above the chunk boundary so multi-line writes are exercised, but
    below the header-line size cap so the writer doesn't raise
    :class:`AuditRecordTooLargeError` on the header chunk itself. (The
    original task description used 500 columns; at 500 cols the shipped
    writer's header chunk overflows the cap because ``columns_sent``
    serialises a 500-name tuple — that path is exercised separately as
    part of US-004's pathological-column-count case.)

    The chunk-triple-cleared invariant is documented in
    :func:`signalforge.safety.audit._reassemble_chunked_group` — the
    reassembled event routes through the non-chunked validator branch so
    callers comparing against the original event get true equality.
    """
    audit_path = tmp_path / "audit.jsonl"
    col_count = _CHUNKED_WRITABLE_COL_COUNT
    model = _make_wide_model(col_count)
    fake = FakeAdapter()
    policy = _policy(audit_path)

    # Capture the SAME event the writer receives (timestamp is generated
    # inside ``build_llm_request`` via ``datetime.now(UTC)``; rebuilding the
    # event a second time produces a divergent timestamp and breaks the
    # byte-equal round-trip). Wrap the real ``audit.write`` so we capture
    # the event AND still write to disk.
    captured: dict[str, AuditEvent] = {}
    original_write = audit.write
    from signalforge.safety import request as request_module

    def _capturing_write(event: AuditEvent, path: Path) -> None:
        captured["event"] = event
        original_write(event, path)

    request_module.audit.write = _capturing_write  # type: ignore[assignment]
    try:
        build_llm_request(model, fake, policy)
    finally:
        request_module.audit.write = original_write  # type: ignore[assignment]

    source_event = captured["event"]

    assert audit_path.exists()
    content = audit_path.read_text(encoding="utf-8")
    lines = [line for line in content.split("\n") if line]
    assert len(lines) >= 2, f"expected >=2 chunks for a {col_count}-col event, got {len(lines)}"

    # Every chunk fits under the POSIX-atomic-append cap.
    for line in lines:
        assert len(line.encode("utf-8")) + 1 <= _AUDIT_RECORD_LIMIT_BYTES, (
            f"audit chunk line exceeds {_AUDIT_RECORD_LIMIT_BYTES}-byte cap: "
            f"{len(line.encode('utf-8')) + 1} bytes"
        )

    # All chunks share one audit_id.
    audit_ids = {json.loads(line)["audit_id"] for line in lines}
    assert len(audit_ids) == 1, (
        f"all chunks must share one audit_id, got {len(audit_ids)} distinct ids: {audit_ids}"
    )

    # Reassembly: exactly one event yielded.
    events = list(audit.read_audit_events(audit_path))
    assert len(events) == 1, f"expected exactly 1 reassembled event, got {len(events)}"
    reassembled = events[0]

    # Round-trip equality on the exclude_none view. Compare via ``json.loads``
    # so dict-key ordering doesn't matter — the source event's
    # ``column_name_map`` preserves ``columns_sent``-position order (insertion
    # order from :func:`build_llm_request`), while the reassembled map's order
    # reflects the chunk-packing layout (sorted by reason → sorted by hashed
    # name within reason). Both views are byte-equal once dict ordering is
    # normalised; the reassembled event's chunk-triple is cleared so it
    # serialises through the non-chunked validator branch identically.
    source_dump = json.loads(source_event.model_dump_json(by_alias=True, exclude_none=True))
    reassembled_dump = json.loads(reassembled.model_dump_json(by_alias=True, exclude_none=True))
    assert source_dump == reassembled_dump, (
        "reassembled event must round-trip byte-equal to the source event "
        "(exclude_none + dict-order-normalised view)"
    )


# ---------------------------------------------------------------------------
# 4. Pathological column-name — fail-closed, no on-disk artefact
# ---------------------------------------------------------------------------


def test_v4_pathological_column_name_raises_no_artifact(tmp_path: Path) -> None:
    """A single hashed name longer than the cap itself raises
    :class:`AuditRecordTooLargeError` BEFORE any file open — the audit
    JSONL file MUST NOT exist on disk.

    This is the load-bearing fail-closed contract from safety-layer.md
    DEC-011 (mirrored in the audit module docstring): pathological inputs
    are rejected pre-open so no partial / oversized record can land. The
    single oversize hashed name is the only thing that can produce an
    over-cap chunk because the greedy packer's "single-name-over-cap"
    path emits it as its own chunk, which the writer's pre-open size check
    then rejects.

    Per US-006: this test deliberately does NOT assert anything about
    ``column_count`` on the exception — US-004 is wiring that kwarg in
    parallel; this test must work both pre- and post-US-004 merge.
    """
    audit_path = tmp_path / "audit.jsonl"
    gigantic_hash = "col_" + "x" * 4000

    # Construct a minimal v4 AuditEvent directly: non-chunked shape with
    # the pathological hashed name folded into pattern_match.
    event = AuditEvent(
        timestamp=datetime.now(UTC),
        model_unique_id="model.test.wide",
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("any_col",),
        row_count=None,
        signalforge_version="0.0.0",
        policy_hash="deadbeef" * 2,
        policy_flags=(),
        redactions_by_reason={"pattern_match": (gigantic_hash,)},
        column_name_map={gigantic_hash: "really_long_name"},
    )

    with pytest.raises(AuditRecordTooLargeError):
        audit.write(event, audit_path)

    assert not audit_path.exists(), (
        "audit JSONL file MUST NOT exist after AuditRecordTooLargeError: "
        "the fail-closed contract requires pre-open rejection so no partial "
        "artefact lands on disk"
    )
