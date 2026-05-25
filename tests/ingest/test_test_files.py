"""Tests for the ``read_test_files`` singular-test reader (US-013).

Exercises the stage-0 ``tests/*.sql`` reader end-to-end: a ``.sql`` referencing
the target model → a model-level ``CandidateTestCustomSQL``; a ``.sql``
referencing a *different* model → not included (and not skip-recorded); an
unsupported-Jinja ``.sql`` → ``SkippedTest(malformed-supported-test)``; dedupe
against a schema.yml-sourced custom_sql of identical SQL; and the
size-cap-before-read defence.

``read_test_files`` returns an ``IngestResult`` produced in-process and handed
to prune; it is NOT read back from disk, so no ``extra="forbid"`` drift
detector is needed (see ``tests/ingest/test_models.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from signalforge.draft.models import CandidateSchema, CandidateTestCustomSQL
from signalforge.ingest import (
    IngestResult,
    IngestSchemaNotFoundError,
    IngestSchemaParseError,
    IngestSchemaTooLargeError,
    SkippedTest,
    read_test_files,
)
from signalforge.ingest.parser import classify_singular_test
from signalforge.ingest.reader import _INGEST_SCHEMA_SIZE_LIMIT_BYTES
from signalforge.manifest import Manifest, Model

# ``TableRef`` validates ``project`` against GCP's 6–30-char grammar.
_PROJECT = "my_project_dev"
_DATASET = "analytics"


def _model_dict(unique_id: str, *, name: str, package: str) -> dict[str, Any]:
    return {
        "unique_id": unique_id,
        "name": name,
        "resource_type": "model",
        "package_name": package,
        "original_file_path": f"models/{name}.sql",
        "path": f"{name}.sql",
        "database": _PROJECT,
        "schema": _DATASET,
        "alias": name,
        "raw_code": "select 1 as id",
    }


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {
                "model.shop.orders": _model_dict(
                    "model.shop.orders", name="orders", package="shop"
                ),
                "model.shop.customers": _model_dict(
                    "model.shop.customers", name="customers", package="shop"
                ),
            },
            "sources": {
                "source.shop.raw.events": {
                    "unique_id": "source.shop.raw.events",
                    "source_name": "raw",
                    "name": "events",
                    "resource_type": "source",
                    "database": _PROJECT,
                    "schema": "raw",
                    "identifier": "events",
                }
            },
        }
    )


def _orders(manifest: Manifest) -> Model:
    return manifest.get_model("model.shop.orders")


def _write(tests_dir: Path, name: str, body: str) -> None:
    (tests_dir / name).write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# classify_singular_test — the pure per-file classifier
# ---------------------------------------------------------------------------


def test_classify_associated_when_sql_references_target_model() -> None:
    manifest = _manifest()
    sql = "select * from {{ ref('orders') }} where amount <= 0"
    out = classify_singular_test(
        sql, file_name="amt.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)
    assert out.type == "custom_sql"
    assert out.column is None  # singular tests are model-level
    assert out.sql == sql  # raw body carried verbatim
    assert out.rationale is None


def test_classify_unrelated_when_sql_references_other_model() -> None:
    manifest = _manifest()
    sql = "select * from {{ ref('customers') }} where email is null"
    out = classify_singular_test(
        sql, file_name="email.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None  # unrelated → not included, not recorded


def test_classify_unsupported_jinja_is_skip_recorded() -> None:
    manifest = _manifest()
    sql = "{% set t = 0 %}\nselect * from {{ ref('orders') }} where amount < {{ t }}"
    out = classify_singular_test(
        sql, file_name="macro.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, SkippedTest)
    assert out.reason == "malformed-supported-test"
    assert out.test_name == "macro.sql"
    assert out.column is None


def test_classify_var_lookup_is_skip_recorded() -> None:
    # {{ var(...) }} is unsupported Jinja the bounded resolver can't evaluate.
    manifest = _manifest()
    sql = "select * from {{ ref('orders') }} where region = '{{ var('region') }}'"
    out = classify_singular_test(
        sql, file_name="var.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, SkippedTest)
    assert out.reason == "malformed-supported-test"


def test_classify_ref_to_unknown_model_is_unrelated_not_skip() -> None:
    # A well-formed ref() to a model absent from the manifest is not THIS
    # model's test — unrelated (None), not skip-recorded.
    manifest = _manifest()
    sql = "select * from {{ ref('ghost_model') }} where 1=1"
    out = classify_singular_test(
        sql, file_name="ghost.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None


def test_classify_this_resolves_to_target_and_associates() -> None:
    # {{ this }} resolves to the model itself, so a test using it associates.
    manifest = _manifest()
    sql = "select * from {{ this }} where amount is null"
    out = classify_singular_test(
        sql, file_name="this.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)
    assert out.sql == sql


def test_classify_this_plus_unresolvable_ref_associates_with_raw_sql() -> None:
    # Item 1 (PR #117): a body referencing THIS model ({{ this }}) AND an
    # unresolvable OTHER ref() must NOT be silently dropped — it targets us.
    # The RAW unresolved SQL is carried so the prune compiler defers it.
    manifest = _manifest()
    sql = "select t.id from {{ this }} t join {{ ref('missing_model') }} m on t.id = m.id"
    out = classify_singular_test(
        sql, file_name="join.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)
    assert out.sql == sql  # raw, unresolved — prune re-resolves + routes it
    assert out.column is None


def test_classify_target_ref_plus_unresolvable_ref_associates() -> None:
    # Same defect via a resolvable ref('orders') (the target) alongside an
    # unknown ref(): the unknown one makes the whole-body resolve raise, but
    # the heuristic still detects the target reference.
    manifest = _manifest()
    sql = "select o.id from {{ ref('orders') }} o join {{ ref('ghost') }} g on o.id = g.id"
    out = classify_singular_test(
        sql, file_name="join2.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)
    assert out.sql == sql


def test_classify_source_sibling_then_target_ref_associates() -> None:
    # A source() sibling (never the model target — skipped by the heuristic),
    # an unresolvable sibling ref() (swallowed), then the target ref('orders')
    # (associates). The unknown ref makes the whole-body resolve raise, so the
    # heuristic decides.
    manifest = _manifest()
    sql = (
        "select o.id from {{ source('raw', 'events') }} e "
        "join {{ ref('ghost') }} g on e.id = g.id "
        "join {{ ref('orders') }} o on e.id = o.id"
    )
    out = classify_singular_test(
        sql, file_name="src_join.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)
    assert out.sql == sql


def test_classify_source_sibling_only_stays_unrelated() -> None:
    # A source() sibling (never the model target — skipped) + an unresolvable
    # ref() (swallowed), no target reference → genuinely unrelated (False).
    manifest = _manifest()
    sql = "select * from {{ source('raw', 'events') }} e join {{ ref('ghost') }} g on e.id = g.id"
    out = classify_singular_test(
        sql, file_name="src_only.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None


def test_classify_only_unresolvable_other_ref_stays_unrelated() -> None:
    # No {{ this }}, no target ref — only unknown refs → genuinely unrelated.
    manifest = _manifest()
    sql = "select * from {{ ref('ghost_a') }} a join {{ ref('ghost_b') }} b on a.id = b.id"
    out = classify_singular_test(
        sql, file_name="ghosts.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None


def test_classify_target_as_substring_fragment_in_comment_not_associated() -> None:
    # Item 2 (PR #117): the target qualified name appearing as a SUBSTRING of a
    # longer token (here inside a comment word ``orders_legacy``) must NOT
    # word-boundary-match; the real FROM is a different (resolvable) model.
    # A bare ``target in resolved`` substring check would have false-matched.
    manifest = _manifest()
    target = _orders(manifest).resolve_this().qualified_name
    sql = (
        f"-- legacy comparison vs {target}_legacy table\n"
        "select * from {{ ref('customers') }} where x is null"
    )
    out = classify_singular_test(
        sql, file_name="comment.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None


def test_classify_target_as_dotted_fragment_not_associated() -> None:
    # The target qualified name as a fragment of a longer dotted identifier
    # (``<target>_archive``) must NOT match. Here the body resolves to a
    # different model; the bare-substring check would have false-matched.
    manifest = _manifest()
    target = _orders(manifest).resolve_this().qualified_name
    sql = (
        f"select * from {target}_archive a "
        "join {{ ref('customers') }} c on a.id = c.id where c.x is null"
    )
    out = classify_singular_test(
        sql, file_name="archive.sql", model=_orders(manifest), manifest=manifest
    )
    assert out is None


def test_classify_normal_from_this_associates() -> None:
    # Sanity: a standalone ``FROM {{ this }}`` still associates (word-boundary
    # match on the resolved target succeeds).
    manifest = _manifest()
    sql = "select * from {{ this }} where amount < 0"
    out = classify_singular_test(
        sql, file_name="ok.sql", model=_orders(manifest), manifest=manifest
    )
    assert isinstance(out, CandidateTestCustomSQL)


# ---------------------------------------------------------------------------
# read_test_files — the directory reader
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "ingest" / "custom_sql_files"


def test_read_test_files_associates_and_skips_and_excludes() -> None:
    manifest = _manifest()
    result = read_test_files(_FIXTURE_DIR, _orders(manifest), manifest)
    assert isinstance(result, IngestResult)

    # The orders-referencing well-formed test is included.
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1
    assert "ref('orders')" in custom[0].sql
    assert "amount <= 0" in custom[0].sql

    # The customers-referencing file is NOT included and NOT skip-recorded.
    assert all("customers" not in t.sql for t in custom)

    # The macro file referencing orders is skip-recorded.
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "malformed-supported-test"
    assert result.skipped[0].test_name == "assert_orders_with_macro.sql"

    # Candidate is model-level only: no columns.
    assert result.candidate.columns == ()
    assert result.candidate.name == "orders"


def test_read_test_files_dedupes_identical_sql_files(tmp_path: Path) -> None:
    manifest = _manifest()
    body = "select * from {{ ref('orders') }} where amount <= 0\n"
    _write(tmp_path, "a.sql", body)
    _write(tmp_path, "b.sql", body)  # byte-identical → must collapse
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1


def test_read_test_files_dedupes_against_existing_schema_yml_custom_sql(tmp_path: Path) -> None:
    # DEC-013: a .sql duplicating a schema.yml-sourced custom_sql collapses to
    # one when ``existing`` is supplied.
    manifest = _manifest()
    body = "select * from {{ ref('orders') }} where amount <= 0\n"
    _write(tmp_path, "dup.sql", body)
    existing = CandidateSchema(
        name="orders",
        description="",
        columns=(),
        tests=(CandidateTestCustomSQL(column=None, sql=body, rationale=None),),
    )
    result = read_test_files(
        tmp_path, _orders(manifest), manifest, project_dir=tmp_path, existing=existing
    )
    # The .sql collapses into the existing one → zero NEW custom_sql tests.
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert custom == []


def test_read_test_files_distinct_sql_against_existing_is_kept(tmp_path: Path) -> None:
    manifest = _manifest()
    _write(tmp_path, "new.sql", "select * from {{ ref('orders') }} where amount is null\n")
    existing = CandidateSchema(
        name="orders",
        description="",
        columns=(),
        tests=(
            CandidateTestCustomSQL(
                column=None,
                sql="select * from {{ ref('orders') }} where amount <= 0\n",
                rationale=None,
            ),
        ),
    )
    result = read_test_files(
        tmp_path, _orders(manifest), manifest, project_dir=tmp_path, existing=existing
    )
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1
    assert "amount is null" in custom[0].sql


def test_read_test_files_empty_dir_yields_empty_candidate(tmp_path: Path) -> None:
    manifest = _manifest()
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    assert result.candidate.tests == ()
    assert result.skipped == ()


def test_read_test_files_non_utf8_sql_raises_parse_error(tmp_path: Path) -> None:
    """Item-2 regression: a ``.sql`` file with non-UTF-8 bytes raises a typed
    :class:`IngestSchemaParseError` rather than escaping as an unhandled
    ``UnicodeDecodeError``. ``read_text(encoding="utf-8")`` raises
    ``UnicodeDecodeError``, which was previously NOT caught by the
    ``except OSError`` branch in ``_read_sql_file``."""
    manifest = _manifest()
    # 0xFF is not valid UTF-8; ``read_text(encoding="utf-8")`` raises
    # UnicodeDecodeError on it.
    (tmp_path / "latin1.sql").write_bytes(b"select * from t where col = '\xff'\n")
    with pytest.raises(IngestSchemaParseError) as excinfo:
        read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    assert "could not be read" in str(excinfo.value)


def test_read_test_files_ignores_non_sql_files(tmp_path: Path) -> None:
    manifest = _manifest()
    _write(tmp_path, "readme.md", "not a test")
    _write(tmp_path, "notes.txt", "select * from {{ ref('orders') }}")
    _write(tmp_path, "real.sql", "select * from {{ ref('orders') }} where amount <= 0\n")
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1


def test_read_test_files_oversize_file_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DEC-005: the cap is enforced from stat().st_size BEFORE the file is read.
    monkeypatch.setattr("signalforge.ingest.reader._INGEST_SCHEMA_SIZE_LIMIT_BYTES", 10)
    manifest = _manifest()
    big = tmp_path / "big.sql"
    big.write_text("select * from {{ ref('orders') }} where amount <= 0\n")  # > 10 bytes
    with pytest.raises(IngestSchemaTooLargeError) as excinfo:
        read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    assert excinfo.value.limit == 10
    assert excinfo.value.size == big.stat().st_size


def test_read_test_files_real_cap_constant_is_5mb() -> None:
    # Guard the cap matches read_schema's (mirrors diff DEC-006 order of magnitude).
    assert _INGEST_SCHEMA_SIZE_LIMIT_BYTES == 5_000_000


def test_read_test_files_missing_dir_raises_not_found(tmp_path: Path) -> None:
    manifest = _manifest()
    missing = tmp_path / "no_such_dir"
    with pytest.raises(IngestSchemaNotFoundError):
        read_test_files(missing, _orders(manifest), manifest, project_dir=tmp_path)


def test_read_test_files_tests_dir_outside_project_raises_parse_error(tmp_path: Path) -> None:
    # A tests_dir that escapes the symlink-hardened containment of project_dir
    # re-raises PathContainmentError as IngestSchemaParseError (reader 375-376).
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    outside = tmp_path / "outside_tests"
    outside.mkdir()
    manifest = _manifest()
    with pytest.raises(IngestSchemaParseError) as excinfo:
        read_test_files(outside, _orders(manifest), manifest, project_dir=project_dir)
    assert "canonicalisation" in str(excinfo.value)


def test_read_test_files_skips_directory_named_dot_sql(tmp_path: Path) -> None:
    # A directory whose name ends in ``.sql`` is matched by the glob but is not
    # a file — the ``not is_file()`` continue skips it (reader line 396).
    manifest = _manifest()
    (tmp_path / "a_dir.sql").mkdir()
    _write(tmp_path, "real.sql", "select * from {{ ref('orders') }} where amount <= 0\n")
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 1
    assert "amount <= 0" in custom[0].sql


def test_read_test_files_stat_oserror_raises_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An OSError from ``stat()`` (metadata read) wraps as IngestSchemaParseError
    # (reader 432-433).
    manifest = _manifest()
    _write(tmp_path, "real.sql", "select * from {{ ref('orders') }} where amount <= 0\n")

    real_is_file = Path.is_file
    real_stat = Path.stat

    # Decouple the is_file() pre-step from the broken stat() so this is robust
    # across Python versions: in 3.13 ``Path.is_file()`` calls
    # ``stat(follow_symlinks=...)`` while in 3.11/3.12 it calls a bare
    # ``stat()`` — keying on the kwarg breaks on 3.12. Instead, force is_file()
    # True for the target and let the size-check stat() in _read_sql_file be the
    # only call that blows up (reader 431-433).
    def _force_is_file(self: Path, *args: object, **kwargs: object) -> bool:
        if self.name == "real.sql":
            return True
        return real_is_file(self, *args, **kwargs)  # type: ignore[arg-type]

    def _boom(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "real.sql":
            raise OSError("stat blew up")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "is_file", _force_is_file)
    monkeypatch.setattr(Path, "stat", _boom)
    with pytest.raises(IngestSchemaParseError) as excinfo:
        read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    assert "metadata could not be read" in str(excinfo.value)


def test_read_test_files_read_oserror_raises_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An OSError from ``read_text()`` (after the size check passes) wraps as
    # IngestSchemaParseError (reader 441-442).
    manifest = _manifest()
    _write(tmp_path, "real.sql", "select * from {{ ref('orders') }} where amount <= 0\n")

    real_read_text = Path.read_text

    def _boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "real.sql":
            raise OSError("read blew up")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(IngestSchemaParseError) as excinfo:
        read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    assert "could not be read" in str(excinfo.value)


def test_read_test_files_deterministic_sorted_order(tmp_path: Path) -> None:
    # Two distinct associated tests come back in sorted-filename order.
    manifest = _manifest()
    _write(tmp_path, "z_last.sql", "select * from {{ ref('orders') }} where amount is null\n")
    _write(tmp_path, "a_first.sql", "select * from {{ ref('orders') }} where amount <= 0\n")
    result = read_test_files(tmp_path, _orders(manifest), manifest, project_dir=tmp_path)
    custom = [t for t in result.candidate.tests if isinstance(t, CandidateTestCustomSQL)]
    assert len(custom) == 2
    # a_first.sql (amount <= 0) sorts before z_last.sql (amount is null).
    assert "amount <= 0" in custom[0].sql
    assert "amount is null" in custom[1].sql
