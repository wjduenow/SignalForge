"""The ``read_schema`` orchestrator (US-005).

Public entry point for the ingest layer: parse an external dbt
``schema.yml`` for one model and return a typed
:class:`~signalforge.ingest.models.IngestResult` whose ``candidate``
(a :class:`~signalforge.draft.CandidateSchema`) feeds the prune stage
unchanged, plus the structured ``skipped`` records for every test the
reader could not convert (DEC-003).

This ties together the building blocks shipped by the earlier stories of
issue #104:

* :func:`signalforge.ingest.parser.parse_test_entry` — pure per-entry
  ``str | dict`` → ``CandidateTest | SkippedTest`` mapping (US-003,
  DEC-006/008/009).
* :func:`signalforge.ingest.anchor.validate_anchor_contract` — whole-file
  collect-all anchor check against the model's real columns (US-004,
  DEC-002/007).
* The :class:`~signalforge.ingest.errors.IngestError` hierarchy (US-001).

Steps (in order):

1. Resolve the input (see the ``schema`` contract on :func:`read_schema`).
2. Size-cap the raw bytes BEFORE any parse (DEC-005).
3. ``yaml.safe_load`` only — never ``yaml.load`` (DEC-005).
4. Select the ``models:`` entry by ``name == model.name`` (DEC-006).
5. Build a :class:`CandidateSchema`: union ``tests:`` + ``data_tests:``,
   dedupe identical entries (DEC-008); ``description`` defaults to ``""``
   (DEC-010).
6. Run the anchor check (DEC-002).
7. Return :class:`IngestResult`.

**No logging** anywhere in this module — ingest is a stage-0 reader
(``.claude/rules/manifest-readers.md`` rule #4). Observability lives in
the consuming prune / grade stages.
"""

from __future__ import annotations

from hashlib import blake2b
from pathlib import Path
from typing import Any

import yaml

from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTest,
    CandidateTestCustomSQL,
)
from signalforge.ingest.anchor import validate_anchor_contract
from signalforge.ingest.errors import (
    IngestModelNotFoundError,
    IngestSchemaNotFoundError,
    IngestSchemaParseError,
    IngestSchemaTooLargeError,
)
from signalforge.ingest.models import IngestResult, SkippedTest
from signalforge.ingest.parser import classify_singular_test, parse_test_entry
from signalforge.manifest import Manifest, Model

# DEC-005: size cap on the raw byte length checked BEFORE ``yaml.safe_load``
# so the parser never sees a billion-laughs / deeply-nested-anchor payload.
# Mirrors the diff layer's ``existing_schema`` cap order of magnitude
# (diff-renderer DEC-006 uses ~5 MB) — a single model's schema.yml block is
# kilobytes; 5 MB is generous headroom while still bounding the attack surface.
_INGEST_SCHEMA_SIZE_LIMIT_BYTES = 5_000_000


def read_schema(
    schema: str | Path,
    model: Model,
    *,
    project_dir: Path | None = None,
) -> IngestResult:
    """Parse an external dbt ``schema.yml`` for ``model`` into an ``IngestResult``.

    The ``schema`` argument is overloaded by *type* — this str-vs-Path split
    is the contract:

    * ``schema: pathlib.Path`` → a FILE path. It is canonicalised via
      :func:`signalforge._common.path_safety.canonicalise_path` (symlink /
      containment hardened) against ``project_dir`` (defaulting to the
      file's parent directory when ``project_dir`` is ``None``), then read.
      A path-containment failure (symlink loop / escape) re-raises as
      :class:`IngestSchemaParseError`; a missing file raises
      :class:`IngestSchemaNotFoundError`.
    * ``schema: str`` → RAW YAML CONTENT, not a path. No file read and no
      canonicalisation happen; the string is parsed directly.

    Args:
        schema: A ``Path`` to a ``schema.yml`` file, or a ``str`` of raw
            YAML content (see the contract above).
        model: The manifest :class:`~signalforge.manifest.Model` whose tests
            are being ingested. ``model.name`` selects the ``models:`` entry;
            ``model.columns`` is the column set the anchor check validates
            against.
        project_dir: Optional project root used to symlink-harden a ``Path``
            ``schema`` argument. Ignored when ``schema`` is a ``str``.
            Defaults to the schema file's parent directory.

    Returns:
        An :class:`IngestResult` carrying the converted ``candidate`` and
        the tuple of ``skipped`` records.

    Raises:
        IngestSchemaNotFoundError: ``schema`` is a ``Path`` that does not
            exist.
        IngestSchemaParseError: the file could not be read / canonicalised,
            or the YAML is malformed.
        IngestSchemaTooLargeError: the raw bytes exceed
            :data:`_INGEST_SCHEMA_SIZE_LIMIT_BYTES` (checked before parse).
        IngestModelNotFoundError: no ``models:`` entry matches ``model.name``.
        IngestAnchorContractError: one or more tests reference a column
            absent from ``model.columns`` (whole-file, collect-all).
    """
    content_bytes = _resolve_input_bytes(
        schema, project_dir, size_limit=_INGEST_SCHEMA_SIZE_LIMIT_BYTES
    )

    # DEC-005: size cap BEFORE any parse. For a ``Path`` input the cap is
    # ALSO enforced from ``stat().st_size`` before the file is read into
    # memory (see ``_resolve_input_bytes``); this post-resolve check covers
    # the ``str`` (already-in-memory) input and is a backstop for both.
    size = len(content_bytes)
    if size > _INGEST_SCHEMA_SIZE_LIMIT_BYTES:
        raise IngestSchemaTooLargeError(size, _INGEST_SCHEMA_SIZE_LIMIT_BYTES)

    # DEC-005: ``yaml.safe_load`` ONLY — never ``yaml.load``.
    try:
        document = yaml.safe_load(content_bytes)
    except yaml.YAMLError as exc:
        raise IngestSchemaParseError(
            f"schema.yml is not valid YAML: {exc}",
            cause=exc,
        ) from exc

    model_block = _select_model_block(document, model.name)
    candidate, skipped = _build_candidate(model_block, model.name)
    validate_anchor_contract(candidate, frozenset(model.columns.keys()))
    return IngestResult(candidate=candidate, skipped=tuple(skipped))


def _resolve_input_bytes(schema: str | Path, project_dir: Path | None, *, size_limit: int) -> bytes:
    """Resolve the ``schema`` argument to raw bytes per the str-vs-Path contract.

    For a ``Path`` input the ``size_limit`` cap is enforced from
    ``stat().st_size`` BEFORE the file is read into memory (DEC-005) — a
    multi-gigabyte ``schema.yml`` is rejected without first being slurped.
    """
    if isinstance(schema, Path):
        base = project_dir if project_dir is not None else schema.parent
        try:
            resolved = canonicalise_path(schema, base)
        except PathContainmentError as exc:
            raise IngestSchemaParseError(
                f"schema.yml path failed symlink-hardened canonicalisation: {exc}",
                cause=exc,
            ) from exc
        if not resolved.is_file():
            raise IngestSchemaNotFoundError(schema)
        # DEC-005: cap from the file's metadata BEFORE reading bytes, so an
        # oversize file never lands in memory.
        try:
            stat_size = resolved.stat().st_size
        except OSError as exc:
            raise IngestSchemaParseError(
                f"schema.yml metadata could not be read: {exc}",
                cause=exc,
            ) from exc
        if stat_size > size_limit:
            raise IngestSchemaTooLargeError(stat_size, size_limit)
        try:
            return resolved.read_bytes()
        except OSError as exc:
            raise IngestSchemaParseError(
                f"schema.yml could not be read: {exc}",
                cause=exc,
            ) from exc
    # ``str`` → raw YAML content. No file read, no canonicalisation.
    return schema.encode("utf-8")


def _select_model_block(document: Any, model_name: str) -> dict[str, Any]:
    """Return the ``models:`` entry whose ``name`` matches ``model_name``.

    Raises :class:`IngestModelNotFoundError` when the document has no
    matching entry (including a document that isn't a mapping, has no
    ``models:`` list, or whose entries are not mappings).
    """
    if isinstance(document, dict):
        models = document.get("models")
        if isinstance(models, (list, tuple)):
            for entry in models:
                if isinstance(entry, dict) and entry.get("name") == model_name:
                    return entry
    raise IngestModelNotFoundError(model_name)


def _collect_tests(block: dict[str, Any]) -> list[Any]:
    """Union the ``tests:`` and ``data_tests:`` lists from a YAML block (DEC-006/008).

    dbt renamed ``tests:`` → ``data_tests:`` in 1.8; accept both. Returns the
    concatenation in encounter order (``tests:`` first); per-test dedupe
    happens after parsing in :func:`_parse_and_dedupe`.
    """
    out: list[Any] = []
    for key in ("tests", "data_tests"):
        raw = block.get(key)
        if isinstance(raw, (list, tuple)):
            out.extend(raw)
    return out


def _test_dedupe_key(test: CandidateTest) -> tuple[Any, ...]:
    """Stable dedupe key for a parsed ``CandidateTest`` (DEC-008).

    Keyed by ``(type, column, sorted-args)`` so an identical test appearing
    under both ``tests:`` and ``data_tests:`` collapses to one entry.
    """
    if test.type == "accepted_values":
        # Sort so two identical value sets in different order dedupe (DEC-008).
        return (test.type, test.column, tuple(sorted(test.values)))
    if test.type == "relationships":
        return (test.type, test.column, test.to, test.field)
    return (test.type, test.column)


def _parse_and_dedupe(
    entries: list[Any], *, column: str | None, skipped: list[SkippedTest]
) -> tuple[CandidateTest, ...]:
    """Parse every entry, route skips to ``skipped``, dedupe supported tests."""
    seen: set[tuple[Any, ...]] = set()
    tests: list[CandidateTest] = []
    for entry in entries:
        parsed = parse_test_entry(entry, column=column)
        if isinstance(parsed, SkippedTest):
            skipped.append(parsed)
            continue
        key = _test_dedupe_key(parsed)
        if key in seen:
            continue
        seen.add(key)
        tests.append(parsed)
    return tuple(tests)


def _build_candidate(
    block: dict[str, Any], model_name: str
) -> tuple[CandidateSchema, list[SkippedTest]]:
    """Assemble the ``CandidateSchema`` + skip records from the model YAML block."""
    skipped: list[SkippedTest] = []

    columns: list[CandidateColumn] = []
    raw_columns = block.get("columns")
    if isinstance(raw_columns, (list, tuple)):
        for raw_col in raw_columns:
            if not isinstance(raw_col, dict):
                continue
            col_name = raw_col.get("name")
            if not isinstance(col_name, str) or not col_name:
                continue
            col_tests = _parse_and_dedupe(_collect_tests(raw_col), column=col_name, skipped=skipped)
            columns.append(
                CandidateColumn(
                    name=col_name,
                    description=_description_or_empty(raw_col),
                    tests=col_tests,
                )
            )

    model_tests = _parse_and_dedupe(_collect_tests(block), column=None, skipped=skipped)

    candidate = CandidateSchema(
        name=model_name,
        description=_description_or_empty(block),
        columns=tuple(columns),
        tests=model_tests,
    )
    return candidate, skipped


def _description_or_empty(block: dict[str, Any]) -> str:
    """Return ``block['description']`` as a string, defaulting to ``""`` (DEC-010)."""
    raw = block.get("description")
    return raw if isinstance(raw, str) else ""


def _custom_sql_hash(sql: str) -> str:
    """Stable 16-hex blake2b-8 fingerprint of a singular-test SQL body (DEC-013).

    The dedupe key for a custom SQL test is ``(model, "custom_sql", sql_hash)``
    — the model is fixed for a single ``read_test_files`` call, ``"custom_sql"``
    is the test type, so ``sql_hash`` is the only varying component. blake2b-8
    over the raw UTF-8 bytes mirrors the project's reproducibility-hash recipe
    (issue #55 — one hash family across the corpus); two byte-identical SQL
    bodies (whether from two ``.sql`` files or from a schema.yml ``custom_sql``)
    collapse to a single :class:`CandidateTestCustomSQL`.
    """
    return blake2b(sql.encode("utf-8"), digest_size=8).hexdigest()


def read_test_files(
    tests_dir: Path,
    model: Model,
    manifest: Manifest,
    *,
    project_dir: Path | None = None,
    existing: CandidateSchema | None = None,
) -> IngestResult:
    """Read an operator's singular dbt tests (``tests/*.sql``) for ``model`` (US-013).

    Enumerates every ``*.sql`` file directly under ``tests_dir`` (sorted by
    name for determinism), and for each one decides — via
    :func:`signalforge.ingest.parser.classify_singular_test` — whether it is a
    model-level :class:`~signalforge.draft.CandidateTestCustomSQL` for
    ``model``:

    * A ``.sql`` whose resolved ``ref()`` / ``source()`` / ``this`` references
      ``model`` becomes a ``CandidateTestCustomSQL(column=None, sql=<body>)``.
    * A ``.sql`` referencing some *other* model is simply not included (NOT
      skip-recorded — it is not a defect of this model's ingest, DEC-013).
    * A ``.sql`` carrying Jinja the bounded resolver cannot evaluate
      (``{% ... %}``, ``{{ var() }}``, macros, or an unresolved ``{{ }}``)
      becomes a :class:`SkippedTest` with ``reason="malformed-supported-test"``
      (the closed 3-value :data:`~signalforge.ingest.models.SkipReason` is not
      extended).

    Each ``.sql`` is size-capped from ``stat().st_size`` BEFORE it is read into
    memory (DEC-005, same cap as :func:`read_schema`), so an oversize file
    raises :class:`IngestSchemaTooLargeError` without first being slurped.

    Dedupe (DEC-013): associated tests dedupe by
    ``(model, "custom_sql", sql_hash)``. Because ``model`` is fixed and the
    type is constant, the effective key is the blake2b-8 of the SQL body
    (:func:`_custom_sql_hash`). When ``existing`` (the schema.yml-sourced
    candidate, supplied by the ``prune-existing`` merge in #105/US-014) is
    given, any ``.sql`` whose SQL matches an ``existing`` custom_sql test is
    dropped so the same test from both sources collapses to one.

    The returned :class:`IngestResult` carries a model-level-only
    ``CandidateSchema`` (no columns) holding just the associated custom SQL
    tests; the caller (US-014) merges it with the schema.yml-sourced candidate.
    No anchor-contract check runs here — singular tests are model-level and
    carry no column reference (``column=None``).

    Args:
        tests_dir: The directory to enumerate ``*.sql`` files in (typically
            ``<project_dir>/tests``). Canonicalised against ``project_dir``.
        model: The manifest model the singular tests are associated to.
        manifest: The manifest, used to resolve ``ref()`` / ``source()``.
        project_dir: Optional project root used to symlink-harden ``tests_dir``.
            Defaults to ``tests_dir`` itself.
        existing: Optional schema.yml-sourced candidate to dedupe against by
            ``(model, "custom_sql", sql_hash)``.

    Returns:
        An :class:`IngestResult` whose ``candidate`` holds the associated
        custom SQL tests (model-level) and whose ``skipped`` records every
        unsupported-Jinja file, in sorted-filename encounter order.

    Raises:
        IngestSchemaNotFoundError: ``tests_dir`` does not exist or is not a
            directory.
        IngestSchemaParseError: ``tests_dir`` failed canonicalisation, or a
            ``.sql`` file could not be read / stat'd.
        IngestSchemaTooLargeError: a ``.sql`` file exceeds
            :data:`_INGEST_SCHEMA_SIZE_LIMIT_BYTES` (checked before read).
    """
    base = project_dir if project_dir is not None else tests_dir
    try:
        resolved_dir = canonicalise_path(tests_dir, base)
    except PathContainmentError as exc:
        raise IngestSchemaParseError(
            f"tests directory path failed symlink-hardened canonicalisation: {exc}",
            cause=exc,
        ) from exc
    if not resolved_dir.is_dir():
        raise IngestSchemaNotFoundError(tests_dir)

    seen_hashes: set[str] = set()
    # Seed the dedupe set with the schema.yml-sourced custom_sql tests so a
    # ``.sql`` duplicating one of them collapses (DEC-013).
    if existing is not None:
        for test in existing.tests:
            if isinstance(test, CandidateTestCustomSQL):
                seen_hashes.add(_custom_sql_hash(test.sql))

    tests: list[CandidateTest] = []
    skipped: list[SkippedTest] = []

    for sql_path in sorted(resolved_dir.glob("*.sql"), key=lambda p: p.name):
        if not sql_path.is_file():
            continue
        sql = _read_sql_file(sql_path)
        outcome = classify_singular_test(
            sql, file_name=sql_path.name, model=model, manifest=manifest
        )
        if outcome is None:
            # Unrelated to this model — not included, not recorded.
            continue
        if isinstance(outcome, SkippedTest):
            skipped.append(outcome)
            continue
        # Associated CandidateTestCustomSQL — dedupe by sql_hash.
        sql_hash = _custom_sql_hash(outcome.sql)
        if sql_hash in seen_hashes:
            continue
        seen_hashes.add(sql_hash)
        tests.append(outcome)

    candidate = CandidateSchema(
        name=model.name,
        description="",
        columns=(),
        tests=tuple(tests),
    )
    return IngestResult(candidate=candidate, skipped=tuple(skipped))


def _read_sql_file(sql_path: Path) -> str:
    """Read a single ``.sql`` file, size-capped from ``stat()`` before read.

    Mirrors :func:`_resolve_input_bytes`'s Path branch: the cap is enforced
    from ``stat().st_size`` BEFORE the file is read into memory (DEC-005), so
    an oversize singular test never lands in memory.
    """
    try:
        stat_size = sql_path.stat().st_size
    except OSError as exc:
        raise IngestSchemaParseError(
            f"singular test file metadata could not be read: {exc}",
            cause=exc,
        ) from exc
    if stat_size > _INGEST_SCHEMA_SIZE_LIMIT_BYTES:
        raise IngestSchemaTooLargeError(stat_size, _INGEST_SCHEMA_SIZE_LIMIT_BYTES)
    try:
        return sql_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IngestSchemaParseError(
            f"singular test file could not be read: {exc}",
            cause=exc,
        ) from exc


__all__ = ("read_schema", "read_test_files")
