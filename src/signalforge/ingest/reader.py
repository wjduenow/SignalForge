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

import re
from hashlib import blake2b
from pathlib import Path
from typing import Any, Final

import yaml

from signalforge._common.path_safety import PathContainmentError, canonicalise_path
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTest,
    CandidateTestCustomSQL,
)
from signalforge.ingest._compiled_sql import (
    is_deterministic_sql,
    is_prunable_count_scalar,
    is_row_returning,
    validate_ingested_sql,
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
from signalforge.manifest import GenericTest, Manifest, Model, associate_test_model

# DEC-005: size cap on the raw byte length checked BEFORE ``yaml.safe_load``
# so the parser never sees a billion-laughs / deeply-nested-anchor payload.
# Mirrors the diff layer's ``existing_schema`` cap order of magnitude
# (diff-renderer DEC-006 uses ~5 MB) — a single model's schema.yml block is
# kilobytes; 5 MB is generous headroom while still bounding the attack surface.
_INGEST_SCHEMA_SIZE_LIMIT_BYTES = 5_000_000

# #268 DEC-012(2) — size cap on a manifest test node's ``compiled_code``,
# checked BEFORE any sqlglot parse. The 5 MB cap above guards *file* reads
# (``read_schema`` / ``read_test_files``) only; ``read_manifest_tests`` takes an
# already-parsed ``Manifest``, so a pathological body (a dbt-utils ``equality``
# on a 400-column model, an unrolled ``accepted_values`` with thousands of
# literals) previously reached sqlglot unbounded. 256 KiB sits far above any
# realistic dbt-expectations / dbt-utils compiled body (the largest observed are
# single-digit KB) and far below anything that stresses the parser. An over-cap
# body is skip-recorded (``malformed-supported-test`` — the CLOSED 3-value
# ``SkipReason``, never grown), never a hard abort.
_COMPILED_CODE_SIZE_LIMIT_BYTES: Final[int] = 262_144


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

    Each parameterised variant extends the key with its discriminating
    args so two ``row_count_between`` / ``accepted_values`` /
    ``relationships`` entries with the SAME ``(type, column)`` but
    DIFFERENT args (bounds / values / target) survive as distinct
    candidates rather than collapsing to one (#169 — row_count_between
    is model-level only, so ``column`` is always ``None``; without the
    bound-aware key two row_count_between entries on the same model
    silently merged).
    """
    if test.type == "accepted_values":
        # Sort so two identical value sets in different order dedupe (DEC-008).
        return (test.type, test.column, tuple(sorted(test.values)))
    if test.type == "relationships":
        return (test.type, test.column, test.to, test.field)
    if test.type == "row_count_between":
        return (test.type, test.column, test.minimum, test.maximum, test.where)
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


# ---------------------------------------------------------------------------
# Manifest-test bridge: dbt-compiled test node -> CandidateTestCustomSQL (US-003)
# ---------------------------------------------------------------------------
#
# DEC-001 — a dbt-compiled test node's already-Jinja-resolved ``compiled_code``
# flows through the existing ``custom_sql`` prune pipeline as a model-level
# ``CandidateTestCustomSQL(column=None)``. No 7th ``CandidateTest`` variant.
#
# DEC-011 — the ``<ARTIFACT>`` fence the grade layer wraps artifact text in is
# broken by a literal ``</ARTIFACT>`` in the payload, which fails the WHOLE
# grade run closed. A hostile macro arg (a regex / value-list containing that
# tag) must NOT be able to do that, so the synthesized rationale is scrubbed of
# the close tag — the literal form AND the whitespace-split ``</  ARTIFACT>``
# variant — at construction time, before it becomes the frozen candidate's
# ``rationale``.
_ENVELOPE_CLOSE_RE = re.compile(r"</\s*ARTIFACT>")

# DEC-010 — the ingest-layer surface for the "not a silent skip" AC. Each
# no-``compiled_code`` test node is skip-recorded with this per-node detail
# naming ``dbt compile``; when EVERY associated node lacks ``compiled_code`` a
# single summary ``SkippedTest`` (see :func:`_all_missing_summary`) is prepended
# so the operator gets one prominent "run ``dbt compile``" pointer rather than
# only N per-node lines. Mirrors the catalog.json / ``data_type`` guidance
# (#159): the manifest READER tolerates null ``compiled_code`` silently
# (stage-0), and the surfacing is this bridge's concern.
_MISSING_COMPILED_CODE_DETAIL = (
    "no compiled_code on the manifest test node — dbt parse does not populate it. "
    "Run `dbt compile` (or `dbt build` / `dbt docs generate`) and commit "
    "target/manifest.json so SignalForge can prune this test."
)
_ALL_MISSING_SUMMARY_TEST_NAME = "(manifest tests)"
_ALL_MISSING_SUMMARY_DETAIL = (
    "None of this model's manifest test nodes carry compiled_code — nothing "
    "could be pruned. dbt parse does not populate compiled_code; run "
    "`dbt compile` (or `dbt build` / `dbt docs generate`) and commit "
    "target/manifest.json, then re-run."
)

# DEC-004 (#267) / DEC-012 — the two structural skip causes, both routed to
# ``malformed-supported-test`` (structurally unusable for the prune COUNT-wrap
# or an irreproducible verdict). No-compiled_code routes to
# ``custom-or-generic-test`` (a namespaced test with no body to evaluate).
_AGGREGATE_SKIP_DETAIL = (
    "non-count aggregate/scalar-shaped compiled body (single-row) — a non-count "
    "aggregate (AVG / SUM / MIN / MAX), arithmetic-on-count (COUNT(*) + 1), or a "
    "multi-aggregate SELECT: it cannot be soundly re-interpreted as a "
    "failing-rows count, so wrapping it in SELECT COUNT(*) AS failures FROM "
    "(<sql>) would report a silent wrong verdict. Count-of-rows scalar bodies "
    "(SELECT COUNT(*) / COUNT(col) / COUNT(DISTINCT col)) ARE now pruned as of "
    "#267 and no longer skip; only these non-count residues remain unsupported."
)
_NONDETERMINISTIC_SKIP_DETAIL = (
    "non-deterministic compiled body (TABLESAMPLE / RAND / CURRENT_TIMESTAMP / "
    "NOW / UUID / …): the prune verdict would not be reproducible, violating "
    "explainable-diffs."
)
# #268 DEC-012(2) — the over-cap disposition. Routed to the existing
# ``malformed-supported-test`` (structurally unusable: the body is never parsed,
# so no verdict can be reached); the closed 3-value ``SkipReason`` is NOT grown.
_OVERSIZE_SKIP_DETAIL = (
    f"compiled_code exceeds the {_COMPILED_CODE_SIZE_LIMIT_BYTES}-byte ingest cap "
    "and was not parsed. A body this large is far outside the realistic "
    "dbt-expectations / dbt-utils range; parsing it unbounded risks exhausting "
    "the parser. Narrow the test (e.g. fewer columns on a dbt_utils.equality) or "
    "drop it."
)

_UNENCODABLE_SKIP_DETAIL = (
    "compiled_code is not valid UTF-8 (it carries a lone surrogate, likely from a "
    "corrupt or hand-edited manifest.json). SignalForge cannot hash, audit or run "
    "a body it cannot encode, so it is skip-recorded rather than aborting the run. "
    "Re-run `dbt compile` to regenerate the manifest."
)


def read_manifest_tests(
    manifest: Manifest,
    model: Model,
    *,
    project_dir: Path | None = None,
    dialect: str = "bigquery",
) -> IngestResult:
    """Bridge dbt-compiled manifest test nodes for ``model`` into an ``IngestResult``.

    Walks :attr:`~signalforge.manifest.Manifest.tests` (the
    ``resource_type == "test"`` nodes), selects the ones associated to ``model``
    via :func:`signalforge.manifest.associate_test_model`, and for each one that
    is **row-returning AND deterministic AND carries ``compiled_code``** builds a
    model-level :class:`~signalforge.draft.CandidateTestCustomSQL` whose ``sql``
    is the node's already-Jinja-resolved ``compiled_code`` and whose
    ``rationale`` names the source macro + args (DEC-001, DEC-011).

    Nodes that cannot be pruned are **skip-recorded, never silently dropped**
    (DEC-010) into :attr:`IngestResult.skipped`, each with an actionable
    ``detail``, using the closed 3-value :data:`~signalforge.ingest.SkipReason`
    (never grown — DEC-014):

    * absent / null ``compiled_code`` → ``custom-or-generic-test`` (a namespaced
      test with no body to evaluate; ``detail`` names ``dbt compile``).
    * NOT row-returning AND not a count-of-rows scalar (a non-count aggregate /
      arithmetic-on-count / multi-aggregate body) → ``malformed-supported-test``
      (it cannot be soundly re-interpreted as a failing-rows count). A
      count-of-rows scalar (``COUNT(*)`` / ``COUNT(col)`` / ``COUNT(DISTINCT col)``)
      IS graduated to a candidate as of #267 (DEC-003) — it carries the compiled
      body verbatim and the compiler does the ``COUNT``-wrap restructure.
    * NOT deterministic (TABLESAMPLE / RAND / NOW / …) → ``malformed-supported-test``
      (the verdict would be irreproducible; DEC-012).
    * ``compiled_code`` that fails the comment-tolerant safety scan
      (:func:`~signalforge.ingest._compiled_sql.validate_ingested_sql`) →
      ``malformed-supported-test``.
    * ``compiled_code`` over :data:`_COMPILED_CODE_SIZE_LIMIT_BYTES` →
      ``malformed-supported-test``, checked BEFORE any sqlglot parse (#268
      DEC-012(2)). ``read_manifest_tests`` takes an already-parsed ``Manifest``,
      so the file-read cap that guards ``read_schema`` never applies here.

    When EVERY associated node lacks ``compiled_code`` a single summary
    :class:`SkippedTest` is prepended (DEC-010) so the operator gets one
    prominent "run ``dbt compile``" pointer. This is a **soft** surface — never
    a hard abort.

    Macro identity (DEC-015): the synthesized ``rationale`` begins with the
    source macro label (``dbt-expectations expect_column_values_to_be_between(…)``),
    so it flows through the diff ``why`` cascade unchanged (rationale → evidence
    → fallback) and the operator sees which manifest test to remove. The macro
    identity therefore rides on the candidate's ``rationale``; no new field is
    needed on :class:`CandidateTestCustomSQL`.

    Stage-0 reader: no logging, no warehouse / LLM calls, no SQL building — the
    raw ``compiled_code`` string is carried verbatim; identifier / SQL safety is
    (re-)validated in the prune compiler (#154 US-004).

    Args:
        manifest: The loaded manifest whose ``tests`` are walked.
        model: The manifest model whose associated tests are ingested;
            ``model.unique_id`` selects associated nodes.
        project_dir: Accepted for adjacent-stage signature parity
            (``read_schema`` / ``prune_tests`` / ``grade_artifacts``); this
            bridge does no path I/O, so it is unused.
        dialect: The sqlglot dialect name the classification gates parse
            ``compiled_code`` under (#268 DEC-013). Defaults to ``"bigquery"``,
            preserving pre-#268 behaviour for callers that have no adapter in
            hand. Callers that DO know the live warehouse should pass
            ``adapter.dialect().name`` — the prune compiler parses the same body
            under the live dialect, and two parses under *different* dialects can
            disagree, which would let the engine and the compiler reach opposite
            verdicts on the same test. An unknown name does not raise: every gate
            degrades to its conservative verdict.

    Returns:
        An :class:`IngestResult` whose ``candidate`` is a model-level-only
        :class:`CandidateSchema` (no columns) holding the ingested
        ``custom_sql`` tests, plus the ``skipped`` records in
        ``unique_id``-sorted encounter order.
    """
    del project_dir  # no path I/O in this bridge — accepted for API parity only.

    associated: list[GenericTest] = [
        test
        for _, test in sorted(manifest.tests.items())
        if associate_test_model(test) == model.unique_id
    ]

    tests: list[CandidateTest] = []
    skipped: list[SkippedTest] = []
    for test in associated:
        outcome = _classify_manifest_test(test, dialect=dialect)
        if isinstance(outcome, SkippedTest):
            skipped.append(outcome)
        else:
            tests.append(outcome)

    # DEC-010 — all-missing summary: at least one associated node, and every one
    # lacks compiled_code. Prepend one prominent remediation pointer.
    if associated and all(not _has_compiled_code(t) for t in associated):
        skipped.insert(
            0,
            SkippedTest(
                test_name=_ALL_MISSING_SUMMARY_TEST_NAME,
                column=None,
                reason="custom-or-generic-test",
                detail=_ALL_MISSING_SUMMARY_DETAIL,
            ),
        )

    candidate = CandidateSchema(
        name=model.name,
        description="",
        columns=(),
        tests=tuple(tests),
    )
    return IngestResult(candidate=candidate, skipped=tuple(skipped))


def _has_compiled_code(test: GenericTest) -> bool:
    """Return ``True`` iff ``test.compiled_code`` is present and non-blank."""
    return test.compiled_code is not None and test.compiled_code.strip() != ""


def _classify_manifest_test(
    test: GenericTest, *, dialect: str = "bigquery"
) -> CandidateTestCustomSQL | SkippedTest:
    """Route one associated manifest test node to a candidate or a skip record.

    The gate order is deliberate (cheapest / most-specific first): presence →
    **size cap** → row-returning-or-count-scalar → deterministic →
    comment-tolerant safety scan. The first failing gate wins; only a body that
    clears every gate becomes a :class:`CandidateTestCustomSQL`. A scalar
    (one-row) body clears the third gate only when it is a graduatable
    count-of-rows scalar (#267 DEC-003) — a non-count scalar skip-records
    ``malformed-supported-test``.

    The size cap (#268 DEC-012(2)) precedes every sqlglot gate so a pathological
    body is never handed to the parser. ``dialect`` (#268 DEC-013) is threaded
    into each sqlglot gate so the ingest verdict is reached under the same
    dialect the prune compiler will use.
    """
    label = _macro_label(test)
    cc = test.compiled_code
    if cc is None or not cc.strip():
        return SkippedTest(
            test_name=label,
            column=test.column_name,
            reason="custom-or-generic-test",
            detail=_MISSING_COMPILED_CODE_DETAIL,
        )
    # #268 DEC-012(2) — bound the body BEFORE any sqlglot parse. Byte length (not
    # character count) so a multi-byte payload cannot smuggle past the cap.
    # The encode ALSO screens un-encodable bodies: a lone surrogate from a
    # manifest JSON escape (``\ud800``) raises ``UnicodeEncodeError`` — not a
    # ``_PARSE_FAILURES`` type, so it would escape this stage-0 reader and abort
    # the whole prune run (the class of bug US-001 closed for the sqlglot gates).
    # It cannot be skipped by encoding through it, either: a surrogate body IS a
    # valid row-returning candidate to sqlglot, so it would resurface and crash
    # `compiled_sql_hash` at prune time. A body SignalForge cannot UTF-8 encode
    # cannot be safely hashed / audited / run, so skip-record it here.
    try:
        body_byte_len = len(cc.encode("utf-8"))
    except UnicodeEncodeError:
        return SkippedTest(
            test_name=label,
            column=test.column_name,
            reason="malformed-supported-test",
            detail=_UNENCODABLE_SKIP_DETAIL,
        )
    if body_byte_len > _COMPILED_CODE_SIZE_LIMIT_BYTES:
        return SkippedTest(
            test_name=label,
            column=test.column_name,
            reason="malformed-supported-test",
            detail=_OVERSIZE_SKIP_DETAIL,
        )
    # A scalar (one-row) body only skips when it is NOT a graduatable
    # count-of-rows scalar (#267 DEC-003/DEC-005). A COUNT(*) / COUNT(col) /
    # COUNT(DISTINCT col) scalar is soundly re-interpretable as a failing-rows
    # count, so it FALLS THROUGH to the common determinism → safety → candidate
    # tail (carrying the compiled body verbatim — the compiler, not ingest, does
    # the COUNT-wrap restructure). A non-count scalar still skip-records.
    if not is_row_returning(cc, dialect=dialect) and not is_prunable_count_scalar(
        cc, dialect=dialect
    ):
        return SkippedTest(
            test_name=label,
            column=test.column_name,
            reason="malformed-supported-test",
            detail=_AGGREGATE_SKIP_DETAIL,
        )
    if not is_deterministic_sql(cc, dialect=dialect):
        return SkippedTest(
            test_name=label,
            column=test.column_name,
            reason="malformed-supported-test",
            detail=_NONDETERMINISTIC_SKIP_DETAIL,
        )
    try:
        validate_ingested_sql(cc)
    except Exception as exc:  # noqa: BLE001 — QuerySyntaxError (warehouse layer).
        # Lazy import of the specific type keeps the cross-layer coupling out of
        # module scope (mirrors _compiled_sql's posture); re-raise anything that
        # is NOT the expected safety rejection.
        from signalforge.warehouse.errors import QuerySyntaxError

        if not isinstance(exc, QuerySyntaxError):
            raise
        return SkippedTest(
            test_name=label,
            column=test.column_name,
            reason="malformed-supported-test",
            detail=f"compiled_code failed the ingested-SQL safety scan: {exc}",
        )

    return CandidateTestCustomSQL(
        sql=cc,
        column=None,  # DEC-001 — model-level; the compiled body is self-contained.
        rationale=_synthesize_rationale(test),
        # #154 US-004 / DEC-007 — mark the compiled body as manifest-ingested so
        # the prune engine routes it to full-scope-against-source (dbt's own
        # quoted relation cannot bind the {{ this }} sample substitution) and
        # validates it comment-tolerantly (dbt compiled_code carries SQL
        # comments the #116 validator rejects). A drafted custom_sql leaves this
        # False and keeps its existing sample behaviour.
        from_manifest=True,
    )


def _macro_label(test: GenericTest) -> str:
    """Human-facing label for a skip record's ``test_name``.

    Prefers the generic test's macro name (``test_metadata.name``); falls back
    to the node ``unique_id`` for a singular test (no ``test_metadata``).
    """
    if test.test_metadata is not None:
        return test.test_metadata.name
    return test.unique_id


def _synthesize_rationale(test: GenericTest) -> str:
    """Build the envelope-safe synthesized rationale (DEC-011, DEC-015).

    Shape: ``<namespace-label> <macro>(<args>)`` — e.g.
    ``dbt-expectations expect_column_values_to_be_between(column=amount,
    min_value=1000, max_value=2000)``. A built-in test (no namespace) drops the
    label prefix; a singular test (no ``test_metadata``) falls back to the node
    ``unique_id``. The whole string is scrubbed of the ``</ARTIFACT>`` close tag
    (literal + whitespace-split) so a hostile macro arg cannot fail-close the
    grade run.
    """
    tm = test.test_metadata
    if tm is None:
        return _sanitize_envelope(test.unique_id)
    prefix = f"{tm.namespace.replace('_', '-')} {tm.name}" if tm.namespace else tm.name
    args = _format_kwargs(tm.kwargs, column_name=test.column_name)
    return _sanitize_envelope(f"{prefix}({args})")


def _format_kwargs(kwargs: dict[str, Any], *, column_name: str | None) -> str:
    """Render a generic test's rendered macro kwargs into an arg summary.

    ``column_name`` is surfaced first as ``column=<value>`` (from the kwargs
    ``column_name`` if present, else the node's ``column_name``); the dbt-internal
    ``model`` kwarg (a ``{{ get_where_subquery(ref(...)) }}`` Jinja string) is
    dropped; every other kwarg renders ``key=value`` in kwargs-declaration order.
    """
    parts: list[str] = []
    kw_col = kwargs.get("column_name")
    col_value = kw_col if isinstance(kw_col, str) and kw_col else column_name
    if col_value:
        parts.append(f"column={col_value}")
    for key, value in kwargs.items():
        if key in ("model", "column_name"):
            continue
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def _sanitize_envelope(text: str) -> str:
    """Strip the ``</ARTIFACT>`` grade-envelope close tag from ``text`` (DEC-011).

    Removes both the literal ``</ARTIFACT>`` and the whitespace-split
    ``</  ARTIFACT>`` variant so the synthesized rationale can never reconstruct
    the fence-terminating tag when it is later wrapped in ``<ARTIFACT>...`` by
    the grade layer. The open tag alone is harmless data and is left untouched.
    """
    return _ENVELOPE_CLOSE_RE.sub("", text)


__all__ = ("read_manifest_tests", "read_schema", "read_test_files")
