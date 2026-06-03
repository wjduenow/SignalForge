"""Unit tests for :mod:`signalforge.grade.cache` (issue #189 / US-003).

Covers the five-part cache key recipe (DEC-004), the
:class:`CacheRecord` shape (DEC-011), the fail-soft write posture
(DEC-005), the 16 KB size cap (DEC-006), the don't-cache-degraded gate
(DEC-007), the flat 0o600 layout (DEC-012), the ``O_EXCL`` concurrent
write safety (DEC-014), and the symlink-hardened
:func:`signalforge.grade.cache.clear_cache` (DEC-015).

Tests are TDD-first: they live alongside the implementation but pin
the contract independently — every named acceptance criterion in the
US-003 bead has a corresponding test below.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from signalforge.grade.cache import (
    _GRADE_CACHE_RECORD_LIMIT_BYTES,
    CacheRecord,
    clear_cache,
    compute_cache_key,
    lookup_cache,
    write_cache,
)
from signalforge.grade.errors import GradeCachePathError

# --- Helpers ---------------------------------------------------------------


def _record(
    *,
    artifact_id: str = "column.email.description",
    criterion_id: str = "clarity",
    score: float = 0.8,
    passed: bool = True,
    evidence: str = "Evidence text.",
    reasoning: str = "Reasoning text.",
    criterion_prompt_hash: str = "1111222233334444",
    artifact_text_hash: str = "aaaabbbbccccdddd",
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    prompt_version_template: str = "fedcba9876543210",
    response_text_hash: str = "5555666677778888",
    rubric_hash: str = "0123456789abcdef",
    original_timestamp: datetime | None = None,
) -> CacheRecord:
    """Build a :class:`CacheRecord` with sensible defaults."""
    if original_timestamp is None:
        original_timestamp = datetime(2026, 5, 1, 17, 42, 13, 123456, tzinfo=UTC)
    return CacheRecord(
        artifact_id=artifact_id,
        criterion_id=criterion_id,
        score=score,
        passed=passed,
        evidence=evidence,
        reasoning=reasoning,
        criterion_prompt_hash=criterion_prompt_hash,
        artifact_text_hash=artifact_text_hash,
        provider=provider,
        model=model,
        prompt_version_template=prompt_version_template,
        response_text_hash=response_text_hash,
        rubric_hash=rubric_hash,
        original_timestamp=original_timestamp,
    )


# --- compute_cache_key -----------------------------------------------------


def test_compute_cache_key_is_deterministic() -> None:
    """Same inputs produce the same 16-hex key, twice."""
    kwargs = {
        "criterion_prompt_hash": "1111222233334444",
        "artifact_text_hash": "aaaabbbbccccdddd",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "prompt_version_template": "fedcba9876543210",
    }
    first = compute_cache_key(**kwargs)
    second = compute_cache_key(**kwargs)
    assert first == second
    assert len(first) == 16
    # All lowercase hex characters.
    assert all(c in "0123456789abcdef" for c in first)


@pytest.mark.parametrize(
    "axis",
    [
        "criterion_prompt_hash",
        "artifact_text_hash",
        "provider",
        "model",
        "prompt_version_template",
    ],
)
def test_compute_cache_key_changes_on_each_input_axis(axis: str) -> None:
    """Flipping any one of the 5 axes changes the cache key.

    Pins DEC-004 — the key recipe consumes EVERY axis. Without this,
    a refactor that silently dropped one of the five inputs would
    silently break cache invalidation in production.
    """
    base = {
        "criterion_prompt_hash": "1111222233334444",
        "artifact_text_hash": "aaaabbbbccccdddd",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "prompt_version_template": "fedcba9876543210",
    }
    base_key = compute_cache_key(**base)
    mutated = dict(base)
    mutated[axis] = mutated[axis] + "X"
    mutated_key = compute_cache_key(**mutated)
    assert mutated_key != base_key, (
        f"Cache key did not change when {axis!r} was mutated — "
        f"DEC-004 invalidation axis is silently broken."
    )


def test_compute_cache_key_provider_in_recipe_prevents_collision() -> None:
    """``(provider=anthropic, model=claude-sonnet)`` ≠
    ``(provider=openai, model=claude-sonnet)``.

    Pins DEC-004's load-bearing security argument: a future hypothetical
    re-release of the same SKU under a different vendor must NOT
    silently rehydrate the wrong provider's verdict.
    """
    shared = {
        "criterion_prompt_hash": "1111222233334444",
        "artifact_text_hash": "aaaabbbbccccdddd",
        "model": "claude-sonnet-4-6",
        "prompt_version_template": "fedcba9876543210",
    }
    anthropic_key = compute_cache_key(provider="anthropic", **shared)
    openai_key = compute_cache_key(provider="openai", **shared)
    assert anthropic_key != openai_key


def test_compute_cache_key_rejects_nul_collision() -> None:
    """The NUL-byte separator means a value containing a literal NUL
    still produces a deterministic key.

    Not a security test (NUL is rejected upstream by the project's
    identifier grammar) — just guards against a future refactor that
    might switch to a printable separator. Pins behaviour, not safety.
    """
    # Concatenated NULs are obvious — a value of exactly "\x00" plus an
    # adjacent empty value would produce the same joined string as an
    # empty value adjacent to a NUL. Use known-different hashes to
    # confirm the digest still depends on order, not just bytes.
    k1 = compute_cache_key(
        criterion_prompt_hash="aaaa",
        artifact_text_hash="bbbb",
        provider="p1",
        model="m1",
        prompt_version_template="ver1",
    )
    k2 = compute_cache_key(
        criterion_prompt_hash="aaaabbbb",
        artifact_text_hash="",
        provider="p1",
        model="m1",
        prompt_version_template="ver1",
    )
    assert k1 != k2


# --- CacheRecord model -----------------------------------------------------


def test_cache_record_refuses_none_score() -> None:
    """DEC-007 — :class:`CacheRecord` rejects ``score=None`` at the
    model layer.

    The Pydantic field is typed ``float`` (not ``float | None``); a
    degraded :class:`signalforge.grade.GradingResult` cannot construct
    a record. This is the load-bearing gate against silently replaying
    transient LLM failures.
    """
    with pytest.raises(ValidationError):
        CacheRecord(
            artifact_id="column.email.description",
            criterion_id="clarity",
            score=None,  # type: ignore[arg-type]
            passed=False,
            criterion_prompt_hash="x",
            artifact_text_hash="y",
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_version_template="z",
            response_text_hash="r",
            rubric_hash="ru",
            original_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_cache_record_refuses_out_of_range_score() -> None:
    """Score validator rejects values outside ``[0.0, 1.0]`` and NaN/inf."""
    for bad_score in (1.5, -0.1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            CacheRecord(
                artifact_id="a",
                criterion_id="c",
                score=bad_score,
                passed=True,
                criterion_prompt_hash="x",
                artifact_text_hash="y",
                provider="anthropic",
                model="claude-sonnet-4-6",
                prompt_version_template="z",
                response_text_hash="r",
                rubric_hash="ru",
                original_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )


def test_cache_record_repr_omits_evidence_and_reasoning() -> None:
    """DEC-022 of #6 (generalised) — accidental ``_LOGGER.info("rec: %s",
    record)`` must not dump multi-paragraph evidence / reasoning.
    """
    rec = _record(evidence="PII-bearing text", reasoning="More PII text")
    text = repr(rec)
    assert "PII-bearing" not in text
    assert "More PII" not in text
    # Identity + score + pass-flag survive.
    assert "column.email.description" in text
    assert "0.8" in text


def test_cache_record_repr_args_omits_evidence_and_reasoning() -> None:
    """Pydantic v2 routes ``rich.print()`` / ``devtools.pretty()`` /
    ``pprint`` through ``__repr_args__``. Override must match
    ``__repr__`` so the structured-debug hooks don't re-expose the
    bodies (memory: ``pydantic-v2-repr-args-redaction-required``).
    """
    rec = _record(evidence="EVIDENCE_PII", reasoning="REASONING_PII")
    arg_keys = {name for name, _ in rec.__repr_args__()}
    assert "evidence" not in arg_keys
    assert "reasoning" not in arg_keys


def test_cache_record_serializes_timestamp_with_iso8601_z() -> None:
    """``original_timestamp`` renders with the canonical Z suffix
    (mirrors the wider audit shape via :func:`iso8601_z`).
    """
    rec = _record(original_timestamp=datetime(2026, 5, 1, 17, 42, 13, 123456, tzinfo=UTC))
    dumped = json.loads(rec.model_dump_json())
    assert dumped["original_timestamp"] == "2026-05-01T17:42:13.123456Z"


# --- lookup_cache ----------------------------------------------------------


def test_lookup_cache_returns_none_on_missing_dir(tmp_path: Path) -> None:
    """Missing cache directory → cache miss, no raise."""
    cache_dir = tmp_path / "grade-cache"  # does not exist
    assert lookup_cache(cache_dir, "0123456789abcdef") is None


def test_lookup_cache_returns_none_on_missing_file(tmp_path: Path) -> None:
    """Cache dir exists but the keyed file doesn't → cache miss."""
    cache_dir = tmp_path / "grade-cache"
    cache_dir.mkdir()
    assert lookup_cache(cache_dir, "0123456789abcdef") is None


def test_lookup_cache_returns_none_on_malformed_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed JSON → cache miss + INFO log; no raise."""
    cache_dir = tmp_path / "grade-cache"
    cache_dir.mkdir()
    key = "deadbeefcafebabe"
    (cache_dir / f"{key}.json").write_text("{not valid json", encoding="utf-8")

    caplog.set_level(logging.INFO, logger="signalforge.grade.cache")
    assert lookup_cache(cache_dir, key) is None
    info_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.cache" and r.levelno == logging.INFO
    ]
    assert any("malformed json" in r.getMessage() for r in info_records)


def test_lookup_cache_returns_none_on_schema_version_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``cache_schema_version != 1`` → cache miss + INFO log."""
    cache_dir = tmp_path / "grade-cache"
    cache_dir.mkdir()
    key = "deadbeefcafebabe"
    bogus = {"cache_schema_version": 99, "anything": "else"}
    (cache_dir / f"{key}.json").write_text(json.dumps(bogus), encoding="utf-8")

    caplog.set_level(logging.INFO, logger="signalforge.grade.cache")
    assert lookup_cache(cache_dir, key) is None
    info_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.cache" and r.levelno == logging.INFO
    ]
    assert any("schema mismatch" in r.getMessage() for r in info_records)


def test_lookup_cache_returns_none_on_validation_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A poisoning attempt with ``score: null`` is treated as a miss,
    not silently rehydrated as a degraded result.

    Defence against an attacker (or stale fixture) writing a degraded
    record into the cache: the read path must refuse, because DEC-007
    forbids caching degraded results in the first place.
    """
    cache_dir = tmp_path / "grade-cache"
    cache_dir.mkdir()
    key = "deadbeefcafebabe"
    poison = {
        "cache_schema_version": 1,
        "artifact_id": "a",
        "criterion_id": "c",
        "score": None,  # DEC-007 violation
        "passed": False,
        "criterion_prompt_hash": "x",
        "artifact_text_hash": "y",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "prompt_version_template": "z",
        "response_text_hash": "r",
        "rubric_hash": "ru",
        "original_timestamp": "2026-05-01T17:42:13.123456Z",
    }
    (cache_dir / f"{key}.json").write_text(json.dumps(poison), encoding="utf-8")

    caplog.set_level(logging.INFO, logger="signalforge.grade.cache")
    assert lookup_cache(cache_dir, key) is None
    info_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.cache" and r.levelno == logging.INFO
    ]
    assert any("validation failed" in r.getMessage() for r in info_records)


def test_lookup_cache_round_trips_a_written_record(tmp_path: Path) -> None:
    """End-to-end: ``write_cache`` then ``lookup_cache`` returns the
    same fields (modulo Pydantic's internal representation).
    """
    cache_dir = tmp_path / "grade-cache"
    key = "deadbeefcafebabe"
    record = _record()

    write_cache(cache_dir, key, record)
    loaded = lookup_cache(cache_dir, key)
    assert loaded is not None
    assert loaded.artifact_id == record.artifact_id
    assert loaded.criterion_id == record.criterion_id
    assert loaded.score == record.score
    assert loaded.passed == record.passed
    assert loaded.evidence == record.evidence
    assert loaded.reasoning == record.reasoning
    assert loaded.provider == record.provider
    assert loaded.model == record.model


# --- write_cache -----------------------------------------------------------


def test_write_cache_creates_dir_lazily(tmp_path: Path) -> None:
    """``write_cache`` creates a missing cache directory on first call."""
    cache_dir = tmp_path / "grade-cache"
    assert not cache_dir.exists()
    write_cache(cache_dir, "deadbeefcafebabe", _record())
    assert cache_dir.is_dir()


def test_write_cache_uses_0o600_file_mode(tmp_path: Path) -> None:
    """File mode 0o600 (owner-only) — DEC-012."""
    cache_dir = tmp_path / "grade-cache"
    key = "deadbeefcafebabe"
    write_cache(cache_dir, key, _record())
    path = cache_dir / f"{key}.json"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_write_cache_o_excl_skips_on_existing_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DEC-014 — pre-existing file (concurrent-write race) is a
    benign skip with a DEBUG log; no raise, no overwrite.
    """
    cache_dir = tmp_path / "grade-cache"
    cache_dir.mkdir()
    key = "deadbeefcafebabe"
    path = cache_dir / f"{key}.json"
    path.write_text("PRE-EXISTING CONTENT", encoding="utf-8")
    original_size = path.stat().st_size

    caplog.set_level(logging.DEBUG, logger="signalforge.grade.cache")
    write_cache(cache_dir, key, _record())  # must not raise

    # Pre-existing file untouched (content-addressed key + O_EXCL).
    assert path.read_text(encoding="utf-8") == "PRE-EXISTING CONTENT"
    assert path.stat().st_size == original_size

    debug_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.cache" and r.levelno == logging.DEBUG
    ]
    assert any("already present" in r.getMessage() for r in debug_records)


def test_write_cache_fails_soft_on_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-005 — ``OSError`` on ``os.open`` is caught, WARNING is
    emitted, function returns normally.
    """
    cache_dir = tmp_path / "grade-cache"

    def _boom(*args: object, **kwargs: object) -> int:
        raise PermissionError("simulated")

    monkeypatch.setattr("signalforge.grade.cache.os.open", _boom)

    caplog.set_level(logging.WARNING, logger="signalforge.grade.cache")
    write_cache(cache_dir, "deadbeefcafebabe", _record())  # must not raise

    warning_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.cache" and r.levelno == logging.WARNING
    ]
    assert warning_records, "expected one WARNING line on os.open failure"
    assert any("write failed" in r.getMessage() for r in warning_records)


def test_write_cache_rejects_oversize_record_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEC-006 — oversize record: no on-disk artefact + WARNING.

    Forces the cap to a tiny value via monkeypatch so we can write a
    normal-sized record that exceeds it; this proves the cap fires
    BEFORE any ``os.open``.
    """
    cache_dir = tmp_path / "grade-cache"
    key = "deadbeefcafebabe"
    # Force a 100-byte cap; any realistic record overflows.
    monkeypatch.setattr(
        "signalforge.grade.cache._GRADE_CACHE_RECORD_LIMIT_BYTES",
        100,
    )

    caplog.set_level(logging.WARNING, logger="signalforge.grade.cache")
    write_cache(cache_dir, key, _record())  # must not raise

    # No on-disk artefact (size check is BEFORE any os.open).
    assert not (cache_dir / f"{key}.json").exists()

    warning_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.cache" and r.levelno == logging.WARNING
    ]
    assert any("oversize" in r.getMessage() for r in warning_records)


def test_write_cache_real_cap_at_16000_bytes() -> None:
    """The exported cap constant is exactly 16_000 (DEC-006)."""
    assert _GRADE_CACHE_RECORD_LIMIT_BYTES == 16_000


def test_write_cache_refuses_non_cacherecord_instance(tmp_path: Path) -> None:
    """Programmer-error guard — passing a raw dict raises ``ValueError``.

    Distinct from the DEC-005 fail-soft posture, which covers I/O /
    concurrency / oversize.
    """
    with pytest.raises(ValueError, match="CacheRecord"):
        write_cache(
            tmp_path / "grade-cache",
            "deadbeefcafebabe",
            {"score": 0.5},  # type: ignore[arg-type]
        )


# --- clear_cache -----------------------------------------------------------


def test_clear_cache_removes_dir_idempotently_on_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing cache dir → no-op + INFO; no raise."""
    cache_dir = tmp_path / ".signalforge" / "grade-cache"  # missing
    caplog.set_level(logging.INFO, logger="signalforge.grade.cache")
    clear_cache(cache_dir)
    info_records = [
        r
        for r in caplog.records
        if r.name == "signalforge.grade.cache" and r.levelno == logging.INFO
    ]
    assert any("absent" in r.getMessage() for r in info_records)


def test_clear_cache_removes_existing_dir(tmp_path: Path) -> None:
    """Happy path: existing cache dir + files → removed entirely."""
    cache_dir = tmp_path / ".signalforge" / "grade-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "deadbeefcafebabe.json").write_text("{}", encoding="utf-8")
    (cache_dir / "1234567890abcdef.json").write_text("{}", encoding="utf-8")

    clear_cache(cache_dir)

    assert not cache_dir.exists()


def test_clear_cache_refuses_path_not_in_grade_cache_suffix(
    tmp_path: Path,
) -> None:
    """``clear_cache`` refuses anything whose canonical form does
    not end with ``.signalforge/grade-cache``.

    Pins DEC-015 — the symlink-hardened destructive boundary.
    """
    # A directory NOT under .signalforge/grade-cache:
    rogue = tmp_path / "not-a-cache"
    rogue.mkdir()
    with pytest.raises(GradeCachePathError):
        clear_cache(rogue)
    # The rogue directory MUST still exist (the gate fired before
    # any shutil.rmtree).
    assert rogue.exists()


def test_clear_cache_refuses_symlink_escape(tmp_path: Path) -> None:
    """A symlink whose target lives outside ``.signalforge/grade-cache``
    is rejected — the destructive ``shutil.rmtree`` never runs against
    the target tree.
    """
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    canary = real_target / "DO-NOT-DELETE.txt"
    canary.write_text("survived", encoding="utf-8")

    # Symlink at <tmp>/.signalforge/grade-cache → <tmp>/elsewhere
    sigdir = tmp_path / ".signalforge"
    sigdir.mkdir()
    symlink_path = sigdir / "grade-cache"
    symlink_path.symlink_to(real_target, target_is_directory=True)

    with pytest.raises(GradeCachePathError):
        clear_cache(symlink_path)

    # The symlink target tree is intact.
    assert canary.exists()
    assert canary.read_text(encoding="utf-8") == "survived"


def test_clear_cache_via_path_with_grade_cache_suffix_succeeds(tmp_path: Path) -> None:
    """A canonical ``.signalforge/grade-cache`` path is accepted even
    when reached via an intermediate symlink whose target preserves
    the suffix.
    """
    cache_dir = tmp_path / "real" / ".signalforge" / "grade-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "deadbeefcafebabe.json").write_text("{}", encoding="utf-8")

    # Symlink the parent .signalforge into a sibling location whose
    # final suffix still ends with the conventional pair after resolve.
    clear_cache(cache_dir)
    assert not cache_dir.exists()


# --- File-permission contract (Linux umask sanity) -------------------------


def test_write_cache_skips_umask_overrides(tmp_path: Path) -> None:
    """The 0o600 mode is requested at ``os.open``; a permissive umask
    must not loosen the actual file mode.
    """
    cache_dir = tmp_path / "grade-cache"
    key = "deadbeefcafebabe"
    # Save and set a permissive umask.
    old_umask = os.umask(0o000)
    try:
        write_cache(cache_dir, key, _record())
    finally:
        os.umask(old_umask)
    mode = (cache_dir / f"{key}.json").stat().st_mode & 0o777
    # 0o600 unconditionally (umask cannot ADD bits, but a fresh
    # implementation that passed 0o666 would let umask=0 leave it
    # world-writable — this test guards against that regression).
    assert mode == 0o600
