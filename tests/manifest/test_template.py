"""Tests for the bounded dbt-Jinja reference resolver (US-002 of #116).

``signalforge.manifest.resolve_template_refs(sql, model, manifest)`` substitutes
``{{ this }}`` / ``{{ ref(...) }}`` / ``{{ source(...) }}`` in singular-test SQL
for the qualified table names the prune compiler / ingest reader consume, with
NO Jinja engine. Whitespace and quote-style variations are tolerated; control-
flow, var()/env_var(), macro calls, and any residual ``{{ }}`` fail loud.

These exercise the three supported forms, pkg/version disambiguation, whitespace
tolerance, the rejection paths, and propagation of the underlying resolver
errors (RefNotFoundError / AmbiguousRefError / SourceNotFoundError).
"""

from __future__ import annotations

from typing import Any

import pytest

from signalforge.manifest import (
    AmbiguousRefError,
    Manifest,
    Model,
    RefNotFoundError,
    SourceNotFoundError,
    TemplateResolutionError,
    UnsupportedJinjaError,
    resolve_template_refs,
)

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
                "model.pkg.stg_users": _model_dict(
                    "model.pkg.stg_users", name="stg_users", package="pkg"
                ),
                "model.pkg.dim_users": _model_dict(
                    "model.pkg.dim_users", name="dim_users", package="pkg"
                ),
            },
            "sources": {
                "source.pkg.raw.users": {
                    "unique_id": "source.pkg.raw.users",
                    "source_name": "raw",
                    "name": "users",
                    "resource_type": "source",
                    "database": _PROJECT,
                    "schema": "raw",
                    "identifier": "users",
                }
            },
        }
    )


def _model(manifest: Manifest) -> Model:
    return manifest.get_model("model.pkg.stg_users")


# ---------------------------------------------------------------------------
# {{ this }}
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_this_resolves_to_model_qualified_name() -> None:
    manifest = _manifest()
    sql = "select * from {{ this }} where id is null"
    out = resolve_template_refs(sql, _model(manifest), manifest)
    assert out == f"select * from {_PROJECT}.{_DATASET}.stg_users where id is null"


@pytest.mark.unit
def test_this_no_inner_whitespace() -> None:
    manifest = _manifest()
    out = resolve_template_refs("from {{this}}", _model(manifest), manifest)
    assert out == f"from {_PROJECT}.{_DATASET}.stg_users"


# ---------------------------------------------------------------------------
# {{ ref(...) }}
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ref_single_arg_resolves() -> None:
    manifest = _manifest()
    out = resolve_template_refs("join {{ ref('dim_users') }}", _model(manifest), manifest)
    assert out == f"join {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_double_quotes_tolerated() -> None:
    manifest = _manifest()
    out = resolve_template_refs('join {{ ref("dim_users") }}', _model(manifest), manifest)
    assert out == f"join {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_two_arg_package_form_resolves() -> None:
    manifest = _manifest()
    out = resolve_template_refs("from {{ ref('pkg', 'dim_users') }}", _model(manifest), manifest)
    assert out == f"from {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_version_kwarg_form_resolves() -> None:
    """A version kwarg is tolerated for grammar parity; last positional is the name."""
    manifest = _manifest()
    out = resolve_template_refs(
        "from {{ ref('dim_users', version=2) }}", _model(manifest), manifest
    )
    assert out == f"from {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_quoted_version_kwarg_does_not_become_positional() -> None:
    """Item-1 regression: a QUOTED ``version='1'`` kwarg must NOT be mistaken
    for a positional name. The old resolver collected every quoted fragment,
    so ``ref('dim_users', version='1')`` resolved the model name to ``'1'``.
    The kwarg is split out; the only positional (``dim_users``) is the name."""
    manifest = _manifest()
    out = resolve_template_refs(
        "from {{ ref('dim_users', version='1') }}", _model(manifest), manifest
    )
    assert out == f"from {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_two_arg_with_quoted_version_kwarg_resolves_name_and_package() -> None:
    """Item-1 regression: package + name positionals plus a quoted version
    kwarg. The kwarg is ignored; the last positional is the name and the
    leading positional is the package disambiguator."""
    manifest = _manifest()
    out = resolve_template_refs(
        "from {{ ref('pkg', 'dim_users', version='2') }}", _model(manifest), manifest
    )
    assert out == f"from {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_trailing_comma_empty_token_skipped() -> None:
    """Item-3 (PR #117) coverage: an empty fragment in the ref() arg list — a
    trailing comma (``ref('dim_users', )``) — splits to an empty token that the
    resolver must ``continue`` past (template.py empty-token guard) rather than
    treat as a positional. The single real positional resolves the name."""
    manifest = _manifest()
    out = resolve_template_refs("from {{ ref('dim_users', ) }}", _model(manifest), manifest)
    assert out == f"from {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_interior_empty_token_skipped() -> None:
    """An interior empty fragment (``ref('pkg', , 'dim_users')``) likewise
    splits to an empty token that is skipped; package + name still resolve."""
    manifest = _manifest()
    out = resolve_template_refs("from {{ ref('pkg', , 'dim_users') }}", _model(manifest), manifest)
    assert out == f"from {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_whitespace_variations_tolerated() -> None:
    manifest = _manifest()
    out = resolve_template_refs("from {{   ref(  'dim_users'  )   }}", _model(manifest), manifest)
    assert out == f"from {_PROJECT}.{_DATASET}.dim_users"


@pytest.mark.unit
def test_ref_package_disambiguates_collision() -> None:
    """The two-arg form picks the right model out of a cross-package collision."""
    manifest = Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {
                "model.pkg_a.shared": _model_dict(
                    "model.pkg_a.shared", name="shared", package="pkg_a"
                ),
                "model.pkg_b.shared": _model_dict(
                    "model.pkg_b.shared", name="shared", package="pkg_b"
                ),
            },
        }
    )
    model = manifest.get_model("model.pkg_a.shared")
    out = resolve_template_refs("from {{ ref('pkg_b', 'shared') }}", model, manifest)
    assert out == f"from {_PROJECT}.{_DATASET}.shared"


# ---------------------------------------------------------------------------
# {{ source(...) }}
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_source_resolves() -> None:
    manifest = _manifest()
    out = resolve_template_refs("from {{ source('raw', 'users') }}", _model(manifest), manifest)
    assert out == f"from {_PROJECT}.raw.users"


@pytest.mark.unit
def test_source_double_quotes_tolerated() -> None:
    manifest = _manifest()
    out = resolve_template_refs('from {{ source("raw", "users") }}', _model(manifest), manifest)
    assert out == f"from {_PROJECT}.raw.users"


# ---------------------------------------------------------------------------
# Multiple refs in one string
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multiple_refs_in_one_string() -> None:
    manifest = _manifest()
    sql = (
        "select a.id from {{ this }} a "
        "join {{ ref('dim_users') }} b on a.id = b.id "
        "join {{ source('raw', 'users') }} c on a.id = c.id"
    )
    out = resolve_template_refs(sql, _model(manifest), manifest)
    assert f"{_PROJECT}.{_DATASET}.stg_users a" in out
    assert f"{_PROJECT}.{_DATASET}.dim_users b" in out
    assert f"{_PROJECT}.raw.users c" in out
    assert "{{" not in out and "}}" not in out


@pytest.mark.unit
def test_no_jinja_passes_through_unchanged() -> None:
    manifest = _manifest()
    sql = "select count(*) from analytics.stg_users where id is null"
    assert resolve_template_refs(sql, _model(manifest), manifest) == sql


# ---------------------------------------------------------------------------
# Rejection paths — fail loud
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_statement_block_rejected() -> None:
    manifest = _manifest()
    sql = "select * from {{ this }} {% if true %} where 1=1 {% endif %}"
    with pytest.raises(UnsupportedJinjaError) as excinfo:
        resolve_template_refs(sql, _model(manifest), manifest)
    assert "Remediation:" in str(excinfo.value)


@pytest.mark.unit
def test_var_call_rejected() -> None:
    manifest = _manifest()
    with pytest.raises(UnsupportedJinjaError):
        resolve_template_refs("where x = {{ var('cutoff') }}", _model(manifest), manifest)


@pytest.mark.unit
def test_env_var_call_rejected() -> None:
    manifest = _manifest()
    with pytest.raises(UnsupportedJinjaError):
        resolve_template_refs("where x = {{ env_var('DBT_X') }}", _model(manifest), manifest)


@pytest.mark.unit
def test_macro_call_rejected() -> None:
    manifest = _manifest()
    with pytest.raises(UnsupportedJinjaError):
        resolve_template_refs("select {{ my_macro('a') }}", _model(manifest), manifest)


@pytest.mark.unit
def test_bare_identifier_rejected() -> None:
    manifest = _manifest()
    with pytest.raises(UnsupportedJinjaError):
        resolve_template_refs("select {{ some_global }}", _model(manifest), manifest)


@pytest.mark.unit
def test_unsupported_jinja_is_template_resolution_error_subclass() -> None:
    """One ``except TemplateResolutionError`` catches the unsupported case too."""
    manifest = _manifest()
    with pytest.raises(TemplateResolutionError):
        resolve_template_refs("{{ var('x') }}", _model(manifest), manifest)


@pytest.mark.unit
def test_malformed_unclosed_expression_rejected() -> None:
    """A residual opener (no closing }}) fails loud rather than passing through."""
    manifest = _manifest()
    with pytest.raises(TemplateResolutionError) as excinfo:
        resolve_template_refs("from {{ ref('dim_users')", _model(manifest), manifest)
    assert "Remediation:" in str(excinfo.value)


@pytest.mark.unit
def test_ref_with_no_quoted_arg_rejected() -> None:
    manifest = _manifest()
    with pytest.raises(UnsupportedJinjaError):
        resolve_template_refs("from {{ ref(some_dynamic) }}", _model(manifest), manifest)


@pytest.mark.unit
def test_source_with_wrong_arity_rejected() -> None:
    manifest = _manifest()
    with pytest.raises(UnsupportedJinjaError):
        resolve_template_refs("from {{ source('raw') }}", _model(manifest), manifest)


# ---------------------------------------------------------------------------
# Propagation of underlying resolver errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_ref_propagates_ref_not_found() -> None:
    manifest = _manifest()
    with pytest.raises(RefNotFoundError):
        resolve_template_refs("from {{ ref('nope') }}", _model(manifest), manifest)


@pytest.mark.unit
def test_unknown_source_propagates_source_not_found() -> None:
    manifest = _manifest()
    with pytest.raises(SourceNotFoundError):
        resolve_template_refs("from {{ source('raw', 'nope') }}", _model(manifest), manifest)


@pytest.mark.unit
def test_ambiguous_ref_propagates_ambiguous_error() -> None:
    manifest = Manifest.model_validate(
        {
            "metadata": {},
            "nodes": {
                "model.pkg_a.shared": _model_dict(
                    "model.pkg_a.shared", name="shared", package="pkg_a"
                ),
                "model.pkg_b.shared": _model_dict(
                    "model.pkg_b.shared", name="shared", package="pkg_b"
                ),
            },
        }
    )
    model = manifest.get_model("model.pkg_a.shared")
    with pytest.raises(AmbiguousRefError):
        resolve_template_refs("from {{ ref('shared') }}", model, manifest)
