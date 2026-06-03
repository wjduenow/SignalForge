"""Persistent grade cache (issue #189 / DEC-018) — keys, record, I/O.

Content-addressed cache for ``(artifact, criterion)`` grade verdicts so a
re-run of ``signalforge generate`` over a model whose drafted artefacts
have not changed can skip the LLM judge call entirely. Mirrors the
"derived state, fail-soft" posture of warehouse-adapter session cleanup
(``warehouse-adapters.md`` § "Cleanup-boundary fail-soft pattern") —
**the inverse of the fail-closed audit writers** under
:mod:`signalforge.grade.audit` / :mod:`signalforge.diff._sidecar`. A
cache write failure is a one-line WARNING and a no-op; the live grade
still completes and the next run re-grades.

Three load-bearing properties (per the DECs in ``plans/super/189-no-grade-cache.md``):

* **Cache key composition (DEC-004) is five-part, NUL-separated**:
  ``criterion_prompt_hash``, ``artifact_text_hash``, ``provider``,
  ``model``, ``prompt_version_template``. ``provider`` between the
  artifact-text hash and the model id is load-bearing — different
  providers may ship the same SKU name (a hypothetical re-release of
  ``claude-sonnet-4-6`` under another vendor) and a cross-provider
  collision would silently rehydrate the wrong verdict.
* **Fail-soft write (DEC-005)**: any ``OSError`` (disk full, permission
  denied, …), oversize record (DEC-006: 16 KB cap), or
  concurrent-write race (DEC-014: ``O_EXCL`` plus ``FileExistsError``)
  collapses to a single WARNING line via the lazy-format JSON logger;
  ``write_cache`` returns normally. The live grade run is NEVER aborted
  by a cache write failure.
* **Don't cache degraded results (DEC-007)**: a ``score=None`` record
  would silently replay the failure on every re-run and prevent
  recovery from transient LLM blips. :class:`CacheRecord` types
  ``score: float`` (not ``float | None``), so a degraded
  :class:`signalforge.grade.GradingResult` cannot construct a record
  in the first place — the gate fires at the model layer before any
  on-disk artefact.

Public surface (per DEC-018):

* :class:`CacheRecord` — frozen Pydantic v2 model, flat duplication of
  the fields needed for verdict + key-verification + forensic replay.
* :func:`compute_cache_key` — 16-hex ``blake2b-8`` of the five-part
  recipe.
* :func:`lookup_cache` — best-effort read; every failure mode returns
  ``None`` (cache miss).
* :func:`write_cache` — fail-soft write; never raises (degraded-result
  argument is a programmer error and IS raised, but the engine pre-checks
  before calling).
* :func:`clear_cache` — symlink-hardened ``shutil.rmtree`` for the
  ``cache clear --grade`` subcommand (US-008 / DEC-015).

See ``plans/super/189-no-grade-cache.md`` DEC-004, DEC-005, DEC-006,
DEC-007, DEC-011, DEC-012, DEC-013, DEC-014, DEC-018 for the design.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from signalforge._common.timestamp import iso8601_z
from signalforge.grade.errors import GradeCachePathError, GradeCacheRecordTooLargeError

_LOGGER = logging.getLogger(__name__)


# DEC-006 — 16 KB pre-open size cap. Locked at 16_000 (not 16_384) for
# the same operator-math reason ``_GRADE_AUDIT_RECORD_LIMIT_BYTES = 4000``
# is locked: a flat decimal value lets reviewers eyeball the cap in
# WARNING lines. 4× the audit cap because cache files are *not*
# concurrent-append targets — the ``PIPE_BUF`` atomicity constraint that
# pins the audit cap doesn't apply, and a verbose ``evidence`` +
# ``reasoning`` body (~10 KB observed worst case) fits comfortably.
_GRADE_CACHE_RECORD_LIMIT_BYTES: Final[int] = 16_000

# DEC-012 — the conventional cache-directory leaf inside
# ``<project>/.signalforge/``. :func:`clear_cache` validates the
# resolved canonical path against this suffix so a misrouted
# ``cache_dir`` (e.g. a symlink target outside ``.signalforge/``)
# fails loud instead of ``shutil.rmtree``-ing an arbitrary tree.
_GRADE_CACHE_DIR_SUFFIX: Final[tuple[str, str]] = (".signalforge", "grade-cache")


class CacheRecord(BaseModel):
    """One persistent grade-cache record per ``(artifact, criterion)`` verdict.

    DEC-011 — **flat duplication, not wrapping** (no
    ``CacheRecord.result: GradingResult``). Three reasons:

    * Operator UX: ``jq '.score' <cache file>`` reads top-level.
    * One Pydantic source of truth for field types (no nested validator
      drift).
    * ~20 bytes per record saved on JSON nesting overhead.

    The forensic input-side hashes (``criterion_prompt_hash``,
    ``artifact_text_hash``, ``provider``, ``model``,
    ``prompt_version_template``) verify the cache file matches the
    16-hex key in its filename. ``response_text_hash`` and
    ``rubric_hash`` carry over to the rehydrated
    :class:`signalforge.grade.GradeEvent` so the audit corpus stays
    reproducibility-stable across cache-hit re-runs. The
    ``original_timestamp`` is forensic only — the rehydrated
    :class:`GradeEvent` gets the current run's timestamp.

    DEC-007 — ``score`` is typed ``float`` (NOT ``float | None``). A
    degraded :class:`signalforge.grade.GradingResult` (where
    ``score is None``) cannot construct a :class:`CacheRecord`;
    Pydantic raises :class:`pydantic.ValidationError` at the model
    layer before any on-disk artefact. This is the load-bearing gate
    against silently replaying transient LLM failures.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    cache_schema_version: int = 1

    # Verdict body — mirrors :class:`signalforge.grade.GradingResult`
    # field-for-field except for ``score`` (degraded path refused per
    # DEC-007).
    artifact_id: str
    criterion_id: str
    score: float
    passed: bool
    evidence: str = ""
    reasoning: str = ""

    # Forensic input-side hashes — verify cache file matches the key it
    # lives under. ``provider`` is the registered provider name
    # (``anthropic`` / ``openai`` / ``gemini`` / a custom plugin name),
    # NOT the resolved SDK class.
    criterion_prompt_hash: str
    artifact_text_hash: str
    provider: str
    model: str
    prompt_version_template: str

    # Output-side reproducibility — carried into the reconstructed
    # :class:`signalforge.grade.GradeEvent` so the audit corpus is
    # byte-identical (modulo the current-run ``timestamp`` and the
    # ``cache_hit: true`` flag) to a live-grade record.
    response_text_hash: str
    rubric_hash: str

    # Original-write timestamp — forensic only; the rehydrated
    # :class:`GradeEvent` gets the current run's timestamp so audit
    # JSONL ordering reflects when the verdict was *consumed*, not when
    # it was first computed.
    original_timestamp: datetime

    @field_serializer("original_timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return iso8601_z(value)

    @field_validator("score")
    @classmethod
    def _score_must_be_finite_in_range(cls, value: float) -> float:
        """Refuse NaN/inf and out-of-range values.

        Mirrors :meth:`signalforge.grade.GradingResult._score_in_range_or_none`
        but rejects ``None`` outright — a degraded grade verdict has no
        place in the cache (DEC-007).
        """
        # NaN check first — ``NaN < 0.0`` is ``False`` (every
        # comparison with NaN is False) so the range check would
        # silently accept NaN.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"score must be a finite real number in [0.0, 1.0]; got {value!r}")
        if value < 0.0 or value > 1.0:
            raise ValueError(f"score must be in [0.0, 1.0]; got {value!r}")
        return value

    def __repr__(self) -> str:
        """Minimal repr — omits ``evidence``/``reasoning``.

        DEC-022 of #6 (generalised) — ``evidence`` / ``reasoning`` may
        quote PII-bearing artifact text via the LLM judge's response.
        Collapsed repr keeps accidental ``_LOGGER.info("rec: %s", rec)``
        / ``rich.print(rec)`` / ``devtools.pretty(rec)`` from dumping
        the bodies; full payload remains accessible via
        :meth:`pydantic.BaseModel.model_dump`.
        """
        return (
            f"CacheRecord(artifact_id={self.artifact_id!r}, "
            f"criterion_id={self.criterion_id!r}, "
            f"score={self.score!r}, passed={self.passed!r}, "
            f"cache_schema_version={self.cache_schema_version!r})"
        )

    def __repr_args__(self):  # type: ignore[no-untyped-def]
        """Mirror :meth:`__repr__` for Pydantic's structured-debug hooks.

        Pydantic v2 routes ``rich.print()`` / ``devtools.pretty()`` /
        ``pprint`` through ``__repr_args__`` (Memory:
        ``pydantic-v2-repr-args-redaction-required``). Without this
        override the structured hooks would re-expose ``evidence`` /
        ``reasoning`` despite the custom ``__repr__``.
        """
        return [
            ("artifact_id", self.artifact_id),
            ("criterion_id", self.criterion_id),
            ("score", self.score),
            ("passed", self.passed),
            ("cache_schema_version", self.cache_schema_version),
        ]


def compute_cache_key(
    *,
    criterion_prompt_hash: str,
    artifact_text_hash: str,
    provider: str,
    model: str,
    prompt_version_template: str,
) -> str:
    """Compose the 16-hex cache key for one ``(artifact, criterion)`` pair.

    DEC-004 — five-part NUL-separated ``blake2b-8`` digest:

    .. code-block:: text

        key = blake2b(
            criterion_prompt_hash    + "\\x00" +
            artifact_text_hash       + "\\x00" +
            provider                 + "\\x00" +
            model                    + "\\x00" +
            prompt_version_template,
            digest_size=8,
        ).hexdigest()  # 16 hex chars

    Cache invalidation axes:

    * criterion text change → rotates ``criterion_prompt_hash``
    * artifact text change → rotates ``artifact_text_hash``
    * provider swap (anthropic↔openai↔gemini) → rotates ``provider``
    * model swap (Sonnet↔Haiku) → rotates ``model``
    * system-prompt / rubric-list change → rotates
      ``prompt_version_template``

    NUL-byte separator (not pipe or colon) defends against
    concatenation collisions where one component contains the
    separator character. NUL is rejected by every realistic identifier
    grammar in the project, so the joined input is unambiguously
    decomposable.

    Returns:
        16-character lowercase hex digest.
    """
    joined = (
        criterion_prompt_hash
        + "\x00"
        + artifact_text_hash
        + "\x00"
        + provider
        + "\x00"
        + model
        + "\x00"
        + prompt_version_template
    )
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=8).hexdigest()


def _cache_path(cache_dir: Path, key: str) -> Path:
    """Compose the on-disk path ``<cache_dir>/<key>.json``.

    Flat layout per DEC-012; the 16-hex key is short enough that a
    single directory level supports the ~few-thousand-entries per
    project that v0.3 expects. Sharding (``<key[:2]>/<key>.json``) is
    deferred to a future ticket if entry counts cross ~10 K.
    """
    return cache_dir / f"{key}.json"


def lookup_cache(cache_dir: Path, key: str) -> CacheRecord | None:
    """Best-effort read of the cache entry under ``cache_dir / key.json``.

    Every degenerate path returns ``None`` (cache miss) so the engine
    can fall through to a live grade. Specifically:

    * missing directory → ``None`` (DEC-012 lazy-mkdir on first write)
    * missing file → ``None``
    * empty file / malformed JSON → ``None`` + INFO log
    * ``cache_schema_version`` mismatch → ``None`` + INFO log
    * Pydantic validation error (any shape mismatch incl. degraded
      ``score: null`` poisoning) → ``None`` + INFO log
    * unreadable file (``PermissionError`` etc.) → ``None`` + INFO log

    No exceptions escape this function. The fail-soft posture mirrors
    :func:`write_cache` — both directions of the cache are derived
    state.

    Args:
        cache_dir: the cache root directory (typically
            ``<project>/.signalforge/grade-cache``).
        key: the 16-hex cache key from :func:`compute_cache_key`.

    Returns:
        the :class:`CacheRecord` on a hit, ``None`` on every miss.
    """
    path = _cache_path(cache_dir, key)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _LOGGER.info(
            "grade cache read failed: %s",
            json.dumps(
                {
                    "key": key,
                    "error_class": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
            ),
        )
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.info(
            "grade cache malformed json: %s",
            json.dumps({"key": key}),
        )
        return None

    # cache_schema_version mismatch is a forward/backward-compat signal
    # rather than corruption — log INFO and return a miss so the live
    # grade overwrites with the current shape on the next write.
    if isinstance(payload, dict):
        version = payload.get("cache_schema_version")
        if version != 1:
            _LOGGER.info(
                "grade cache schema mismatch: %s",
                json.dumps(
                    {
                        "key": key,
                        "expected_version": 1,
                        "found_version": version,
                    }
                ),
            )
            return None

    try:
        return CacheRecord.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError, etc.
        _LOGGER.info(
            "grade cache validation failed: %s",
            json.dumps(
                {
                    "key": key,
                    "error_class": type(exc).__name__,
                }
            ),
        )
        return None


def write_cache(cache_dir: Path, key: str, record: CacheRecord) -> None:
    """Fail-soft write of ``record`` to ``cache_dir / key.json``.

    DEC-005 — the inverse of the fail-closed audit writers. Any
    ``OSError`` (disk full, permission denied, …), oversize record
    (DEC-006 / ``_GRADE_CACHE_RECORD_LIMIT_BYTES``), or
    concurrent-write race (DEC-014 / ``O_EXCL`` + ``FileExistsError``)
    is caught here and surfaced as a single WARNING (or DEBUG, for the
    benign EEXIST race) via the lazy-format JSON logger. The live grade
    run is NEVER aborted; the next run re-grades and re-attempts the
    write.

    Steps:

    1. Refuse a degraded result (programmer-error guard). ``score=None``
       cannot reach here because :class:`CacheRecord` already refuses
       it at the model layer per DEC-007, but a defensive check on
       ``record.score`` makes the gate explicit if a future refactor
       relaxes the field type.
    2. Serialise via :meth:`pydantic.BaseModel.model_dump_json`
       (``by_alias=True``, ``indent=2`` for human-readable on-disk
       review).
    3. **Size check BEFORE any file open** (DEC-006): if the encoded
       byte length exceeds :data:`_GRADE_CACHE_RECORD_LIMIT_BYTES`,
       construct a :class:`GradeCacheRecordTooLargeError` and route it
       to the fail-soft WARNING (no on-disk artefact). The error class
       exists for diagnostic naming; it never propagates.
    4. ``cache_dir.mkdir(parents=True, exist_ok=True)`` — lazy creation
       on first write per DEC-012.
    5. **Concurrent-write safety** (DEC-014): ``os.open(path,
       O_WRONLY | O_CREAT | O_EXCL, 0o600)``. ``O_EXCL`` makes the
       open fail with ``FileExistsError`` when a parallel run got there
       first; because the cache is content-addressed (same key → same
       contents) the existing file is correct and we DEBUG-log and
       return. **No ``O_TRUNC`` fallback** — that would risk partial
       reads under concurrency.
    6. Short-write loop ``os.write``, then ``os.fsync``,
       ``os.close`` in ``try/finally``.

    Args:
        cache_dir: the cache root directory.
        key: the 16-hex cache key from :func:`compute_cache_key`.
        record: the :class:`CacheRecord` to persist. Must carry a
            non-``None`` finite score in ``[0.0, 1.0]`` (the model
            layer enforces this; the function-level guard is
            defensive).

    Raises:
        ValueError: programmer error — ``record`` is not a
            :class:`CacheRecord` (e.g. a raw dict with
            ``score: null``). Distinct from the DEC-005 fail-soft
            posture, which covers I/O / concurrency / oversize.
    """
    # Defensive guard — DEC-007. ``CacheRecord`` itself refuses
    # ``score=None`` at the model layer (``score: float``), so a
    # well-formed caller never trips this. Keeping the runtime check
    # here makes the contract explicit at the function boundary in
    # case a future refactor widens the field type.
    if not isinstance(record, CacheRecord):
        raise ValueError(
            f"write_cache requires a CacheRecord instance; received {type(record).__name__}"
        )

    body = record.model_dump_json(by_alias=True, indent=2) + "\n"
    encoded = body.encode("utf-8")

    if len(encoded) > _GRADE_CACHE_RECORD_LIMIT_BYTES:
        oversize = GradeCacheRecordTooLargeError(
            size=len(encoded),
            limit=_GRADE_CACHE_RECORD_LIMIT_BYTES,
        )
        _LOGGER.warning(
            "grade cache write skipped (oversize): %s",
            json.dumps(
                {
                    "key": key,
                    "size": len(encoded),
                    "limit": _GRADE_CACHE_RECORD_LIMIT_BYTES,
                    "error_class": type(oversize).__name__,
                }
            ),
        )
        return

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOGGER.warning(
            "grade cache write failed (mkdir): %s",
            json.dumps(
                {
                    "key": key,
                    "error_class": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
            ),
        )
        return

    path = _cache_path(cache_dir, key)
    try:
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        # DEC-014 — content-addressed key means the existing file is
        # byte-identical. Log DEBUG (a normal benign race, not an
        # error) and return.
        _LOGGER.debug(
            "grade cache entry already present: %s",
            json.dumps({"key": key}),
        )
        return
    except OSError as exc:
        _LOGGER.warning(
            "grade cache write failed (open): %s",
            json.dumps(
                {
                    "key": key,
                    "error_class": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
            ),
        )
        return

    try:
        try:
            # Short-write loop. ``os.write`` may return fewer bytes
            # than requested on some kernels / filesystems; loop until
            # the full payload lands.
            written = 0
            while written < len(encoded):
                n = os.write(fd, encoded[written:])
                if n == 0:
                    raise OSError("os.write returned 0 — disk full or other I/O failure")
                written += n
            os.fsync(fd)
        except OSError as exc:
            _LOGGER.warning(
                "grade cache write failed (write/fsync): %s",
                json.dumps(
                    {
                        "key": key,
                        "error_class": type(exc).__name__,
                        "errno": getattr(exc, "errno", None),
                    }
                ),
            )
            # Best-effort cleanup of the partial file so the next run
            # sees a clean miss rather than reading a truncated entry.
            with contextlib.suppress(OSError):
                path.unlink()
            return
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def clear_cache(cache_dir: Path) -> None:
    """Remove the grade-cache directory recursively.

    Idempotent on a missing directory (no-op + INFO log).
    Symlink-hardened: canonicalises ``cache_dir`` via
    ``Path.resolve(strict=False)`` and rejects anything whose
    canonical form does not end with the conventional
    ``.signalforge/grade-cache`` suffix (DEC-012, DEC-015). This
    refuses to ``shutil.rmtree`` an arbitrary tree even when the
    caller passes a symlinked path.

    Args:
        cache_dir: the cache root directory. Conventionally
            ``<project>/.signalforge/grade-cache``.

    Raises:
        GradeCachePathError: ``cache_dir`` canonicalises to a path
            whose final two components are not
            ``.signalforge/grade-cache``. Refuses to remove anything
            outside that suffix. Mapped to CLI tier 1 (load-time /
            operator-config problem).
    """
    cache_dir = Path(cache_dir)

    # ``Path.resolve(strict=False)`` is correct for the
    # "directory might not exist" idempotent case (DEC-015).
    # Containment check is against the conventional suffix because
    # ``clear_cache`` takes no anchor argument; the load-bearing
    # boundary is "the resolved canonical path lives inside a
    # ``.signalforge/grade-cache/`` tree somewhere on disk".
    try:
        resolved = cache_dir.resolve(strict=False)
    except (RuntimeError, OSError) as exc:
        # Python <= 3.12 raises RuntimeError on symlink cycles
        # regardless of strict=; Python >= 3.13 raises
        # OSError(ELOOP) under strict=True (gh-108958). strict=False
        # generally swallows both, but a pathological filesystem can
        # still surface a non-ELOOP OSError here.
        if isinstance(exc, OSError) and exc.errno not in (None, errno.ELOOP):
            raise
        raise GradeCachePathError(
            f"Grade cache path {cache_dir!r} could not be canonicalised ({type(exc).__name__})."
        ) from exc

    suffix = _GRADE_CACHE_DIR_SUFFIX
    parts = resolved.parts
    # Containment check: the LAST two path components must be
    # ``.signalforge`` and ``grade-cache``. Mirrors the spirit of the
    # ``signalforge._common.path_safety`` containment helper without
    # requiring an anchor argument (DEC-015 — ``clear_cache`` only
    # takes ``cache_dir``).
    if len(parts) < 2 or parts[-2:] != suffix:
        raise GradeCachePathError(
            f"Grade cache path {cache_dir!r} canonicalises to {resolved!r}, "
            f"whose final components are not {suffix!r}. Refusing to remove."
        )

    if not resolved.exists():
        _LOGGER.info(
            "grade cache clear: directory absent (no-op): %s",
            json.dumps({"path": str(resolved)}),
        )
        return

    shutil.rmtree(resolved, ignore_errors=False)
    _LOGGER.info(
        "grade cache cleared: %s",
        json.dumps({"path": str(resolved)}),
    )


__all__ = (
    "CacheRecord",
    "_GRADE_CACHE_RECORD_LIMIT_BYTES",
    "clear_cache",
    "compute_cache_key",
    "lookup_cache",
    "write_cache",
)
