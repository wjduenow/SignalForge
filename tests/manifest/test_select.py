"""Tests for :mod:`signalforge.manifest.select` (issue #37, US-001).

Covers the typed ``SelectorAtom`` discriminated union, the
:func:`parse_selector` grammar, and the :func:`select_models` matcher.

Traces to DEC-001 (grammar), DEC-012 (module location), DEC-016 (fnmatch).
"""

from __future__ import annotations

from typing import Any

import pytest

from signalforge.manifest import (
    Manifest,
    SelectorAtom,
    SelectorParseError,
    parse_selector,
    select_models,
)
from signalforge.manifest.select import BareAtom, PathAtom, TagAtom


def _minimal_manifest_metadata() -> dict[str, Any]:
    return {
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
    }


def _model_dict(
    *,
    unique_id: str,
    name: str,
    original_file_path: str,
    tags: list[str] | None = None,
    config_tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "unique_id": unique_id,
        "name": name,
        "resource_type": "model",
        "package_name": "proj",
        "original_file_path": original_file_path,
        "path": original_file_path,
        "schema": "analytics",
        "raw_code": "select 1 as id",
        "tags": tags if tags is not None else [],
        "config": {
            "materialized": "view",
            "tags": config_tags if config_tags is not None else [],
            "meta": {},
        },
    }


def _build_manifest(*models: dict[str, Any]) -> Manifest:
    nodes = {m["unique_id"]: m for m in models}
    return Manifest.model_validate(
        {
            "metadata": _minimal_manifest_metadata(),
            "nodes": nodes,
            "disabled": {},
        }
    )


# ---------------------------------------------------------------------------
# parse_selector — grammar happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_selector_single_tag() -> None:
    atoms = parse_selector("tag:staging")
    assert atoms == (TagAtom(name="staging"),)


@pytest.mark.unit
def test_parse_selector_single_path() -> None:
    atoms = parse_selector("path:models/marts/*")
    assert atoms == (PathAtom(glob="models/marts/*"),)


@pytest.mark.unit
def test_parse_selector_bare_unique_id() -> None:
    atoms = parse_selector("model.proj.x")
    assert atoms == (BareAtom(value="model.proj.x"),)


@pytest.mark.unit
def test_parse_selector_bare_filepath() -> None:
    atoms = parse_selector("models/x.sql")
    assert atoms == (BareAtom(value="models/x.sql"),)


@pytest.mark.unit
def test_parse_selector_multi_expression_union() -> None:
    atoms = parse_selector("tag:staging,path:models/marts/*")
    assert atoms == (TagAtom(name="staging"), PathAtom(glob="models/marts/*"))


@pytest.mark.unit
def test_parse_selector_strips_whitespace() -> None:
    atoms = parse_selector(" tag:staging , path:models/marts/* ")
    assert atoms == (TagAtom(name="staging"), PathAtom(glob="models/marts/*"))


# ---------------------------------------------------------------------------
# parse_selector — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.error
@pytest.mark.parametrize("expr", ["", ",tag:foo", "tag:foo,", "tag:foo,,path:x"])
def test_parse_selector_rejects_empty_atom(expr: str) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector(expr)


@pytest.mark.unit
@pytest.mark.error
def test_parse_selector_rejects_empty_tag() -> None:
    with pytest.raises(SelectorParseError):
        parse_selector("tag:")


@pytest.mark.unit
@pytest.mark.error
def test_parse_selector_rejects_empty_path() -> None:
    with pytest.raises(SelectorParseError):
        parse_selector("path:")


# ---------------------------------------------------------------------------
# select_models — matching semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_select_models_tag_match_union_of_tags_and_config_tags() -> None:
    manifest = _build_manifest(
        _model_dict(
            unique_id="model.proj.a",
            name="a",
            original_file_path="models/staging/a.sql",
            tags=["staging"],
        ),
        _model_dict(
            unique_id="model.proj.b",
            name="b",
            original_file_path="models/staging/b.sql",
            config_tags=["staging"],
        ),
        _model_dict(
            unique_id="model.proj.c",
            name="c",
            original_file_path="models/marts/c.sql",
            tags=["marts"],
        ),
    )
    matched = select_models(manifest, "tag:staging")
    assert tuple(m.unique_id for m in matched) == ("model.proj.a", "model.proj.b")


@pytest.mark.unit
def test_select_models_path_glob_match() -> None:
    manifest = _build_manifest(
        _model_dict(
            unique_id="model.proj.a",
            name="a",
            original_file_path="models/staging/a.sql",
        ),
        _model_dict(
            unique_id="model.proj.b",
            name="b",
            original_file_path="models/staging/b.sql",
        ),
        _model_dict(
            unique_id="model.proj.c",
            name="c",
            original_file_path="models/marts/c.sql",
        ),
    )
    matched = select_models(manifest, "path:models/staging/*")
    assert tuple(m.unique_id for m in matched) == ("model.proj.a", "model.proj.b")


@pytest.mark.unit
def test_select_models_bare_routes_unique_id_for_model_prefix() -> None:
    manifest = _build_manifest(
        _model_dict(
            unique_id="model.proj.a",
            name="a",
            original_file_path="models/staging/a.sql",
        ),
        _model_dict(
            unique_id="model.proj.b",
            name="b",
            original_file_path="models/staging/b.sql",
        ),
    )
    # unique_id path
    matched = select_models(manifest, "model.proj.a")
    assert tuple(m.unique_id for m in matched) == ("model.proj.a",)
    # round-trips against get_model
    assert matched[0] is manifest.get_model("model.proj.a")
    # file-path path
    matched = select_models(manifest, "models/staging/b.sql")
    assert tuple(m.unique_id for m in matched) == ("model.proj.b",)


@pytest.mark.unit
def test_select_models_multi_expression_union_is_deduped() -> None:
    manifest = _build_manifest(
        _model_dict(
            unique_id="model.proj.a",
            name="a",
            original_file_path="models/staging/a.sql",
            tags=["staging"],
        ),
        _model_dict(
            unique_id="model.proj.b",
            name="b",
            original_file_path="models/staging/b.sql",
            tags=["staging"],
        ),
    )
    # Both atoms match both models -> result must be deduped.
    matched = select_models(manifest, "tag:staging,path:models/staging/*")
    assert tuple(m.unique_id for m in matched) == ("model.proj.a", "model.proj.b")


@pytest.mark.unit
def test_select_models_ordered_by_unique_id() -> None:
    # Insert in reverse-sorted order; expect lexicographic output.
    manifest = _build_manifest(
        _model_dict(
            unique_id="model.proj.zz",
            name="zz",
            original_file_path="models/staging/zz.sql",
            tags=["staging"],
        ),
        _model_dict(
            unique_id="model.proj.aa",
            name="aa",
            original_file_path="models/staging/aa.sql",
            tags=["staging"],
        ),
        _model_dict(
            unique_id="model.proj.mm",
            name="mm",
            original_file_path="models/staging/mm.sql",
            tags=["staging"],
        ),
    )
    matched = select_models(manifest, "tag:staging")
    assert tuple(m.unique_id for m in matched) == (
        "model.proj.aa",
        "model.proj.mm",
        "model.proj.zz",
    )


@pytest.mark.unit
def test_select_models_zero_match_returns_empty_tuple() -> None:
    manifest = _build_manifest(
        _model_dict(
            unique_id="model.proj.a",
            name="a",
            original_file_path="models/staging/a.sql",
            tags=["staging"],
        ),
    )
    assert select_models(manifest, "tag:nonexistent") == ()


# ---------------------------------------------------------------------------
# Atom typing — discriminated union + frozen + extra=forbid
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_selector_atom_is_discriminated_union_alias() -> None:
    """``SelectorAtom`` must accept every concrete atom shape."""
    atoms: tuple[SelectorAtom, ...] = (
        TagAtom(name="x"),
        PathAtom(glob="models/*"),
        BareAtom(value="model.proj.x"),
    )
    # Smoke: each carries its discriminator.
    assert {a.kind for a in atoms} == {"tag", "path", "bare"}


@pytest.mark.unit
def test_atoms_are_frozen() -> None:
    from pydantic import ValidationError

    atom = TagAtom(name="staging")
    with pytest.raises(ValidationError):
        atom.name = "marts"  # type: ignore[misc]


@pytest.mark.unit
@pytest.mark.error
def test_atoms_reject_extra_keys() -> None:
    """Atoms are user-input typed; typos must fail loud (extra='forbid')."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TagAtom.model_validate({"kind": "tag", "name": "x", "extra_key": "boom"})
