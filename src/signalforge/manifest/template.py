"""Bounded dbt-Jinja reference resolver — NO Jinja engine (US-002 of #116).

Turns the dbt-Jinja references in a *singular* (SQL-file) test into real
qualified table names so the prune compiler (US-007) and ingest reader (US-013)
can run the test against the warehouse. The three supported forms are:

* ``{{ this }}`` → the model's own qualified name (:meth:`Model.resolve_this`);
* ``{{ ref('name') }}`` / ``{{ ref('pkg', 'name') }}`` (and version forms) →
  :meth:`Manifest.resolve_ref` (the last positional is the model name; the
  two-arg form's first positional is the package disambiguator);
* ``{{ source('src', 'table') }}`` → :meth:`Manifest.resolve_source`.

The substitution is a **bounded regex pass**, deliberately NOT a Jinja engine
(DEC-004 of the plan), mirroring the unwrap style already used in
:func:`signalforge.ingest.parser._unwrap_ref_or_source`. Whitespace inside the
``{{ }}`` is tolerated and both quote styles are accepted.

Anything the resolver does not recognise **fails loud** rather than reaching
the warehouse as broken SQL:

* ``{% ... %}`` statement blocks, ``{{ var(...) }}`` / ``{{ env_var(...) }}``
  lookups, and macro calls raise :class:`UnsupportedJinjaError`;
* any residual ``{{ ... }}`` left unresolved after substitution raises
  :class:`TemplateResolutionError`.

This module lives in the manifest layer (stage-0): it depends only on the
manifest's resolvers, returns a ``str``, emits ZERO logs, and is deterministic
for a given input (``docs/rules/manifest-readers.md``). The
:class:`RefNotFoundError` / :class:`AmbiguousRefError` / :class:`SourceNotFoundError`
raised by the underlying resolvers propagate unchanged.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from signalforge.manifest.errors import (
    TemplateResolutionError,
    UnsupportedJinjaError,
)

if TYPE_CHECKING:
    from signalforge.manifest.models import Manifest, Model

# A ``{% ... %}`` statement block: any Jinja control-flow / tag. The bounded
# resolver has no engine for these, so their mere presence is a hard fail.
_STATEMENT_RE = re.compile(r"\{%.*?%\}", re.DOTALL)

# A single ``{{ ... }}`` expression. Captures the inner body so the dispatcher
# can classify it (this / ref / source / unsupported). Non-greedy so adjacent
# expressions don't merge.
_EXPR_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.DOTALL)

# ``this`` — exactly the bare keyword (after the surrounding whitespace strip).
_THIS_RE = re.compile(r"^this$")

# ``ref('m')`` / ``ref("pkg", "m")`` — captures the parenthesised arg list.
_REF_RE = re.compile(r"^ref\s*\(\s*(.*?)\s*\)$", re.DOTALL)

# ``source('s', 't')`` — captures the parenthesised arg list.
_SOURCE_RE = re.compile(r"^source\s*\(\s*(.*?)\s*\)$", re.DOTALL)

# A single quoted positional arg inside a ref()/source() call.
_QUOTED_ARG_RE = re.compile(r"""['"]([^'"]*)['"]""")

# A keyword argument token (``version='1'``, ``v = 2``). Used to split the
# kwargs out of a ref() arg list so they don't get mistaken for positional
# quoted names. Matches a leading identifier followed by ``=``.
_KWARG_RE = re.compile(r"^\s*\w+\s*=")


def resolve_template_refs(sql: str, model: Model, manifest: Manifest) -> str:
    """Resolve the dbt-Jinja references in ``sql`` to qualified table names.

    ``sql`` is a singular-test SQL string (the body of a ``.sql`` test file).
    ``model`` is the model the test is attached to (drives ``{{ this }}``);
    ``manifest`` resolves ``ref()`` / ``source()``.

    Returns the SQL with every supported reference substituted for its
    qualified name.

    Raises:
        :class:`UnsupportedJinjaError`: the SQL contains a ``{% ... %}`` block,
            a ``{{ var(...) }}`` / ``{{ env_var(...) }}`` lookup, or a macro
            call — none of which the bounded resolver can evaluate.
        :class:`TemplateResolutionError`: a residual ``{{ ... }}`` could not be
            classified as one of the three supported forms.
        :class:`RefNotFoundError` / :class:`AmbiguousRefError` /
            :class:`SourceNotFoundError`: propagated unchanged from the
            manifest resolvers.

    Pure: no I/O, no logging, deterministic for a given input.
    """
    # Reject control-flow first: a ``{% if %}`` we silently dropped could flip
    # the test's meaning. The presence of a statement block is itself the fail.
    if _STATEMENT_RE.search(sql):
        raise UnsupportedJinjaError(
            "Singular-test SQL contains a Jinja statement block ({% ... %}), "
            "which the bounded resolver cannot evaluate.",
        )

    def _replace(match: re.Match[str]) -> str:
        return _resolve_expression(match.group(1), model=model, manifest=manifest)

    resolved = _EXPR_RE.sub(_replace, sql)

    # Defence in depth: a malformed ``{{`` with no closing ``}}`` (or any shape
    # the expression regex didn't match) would slip through the sub. If the
    # opener token survives, the SQL still carries an unresolved reference.
    if "{{" in resolved or "}}" in resolved:
        raise TemplateResolutionError(
            "Singular-test SQL contains an unresolved or malformed Jinja "
            "expression ({{ ... }}) after substitution.",
        )

    return resolved


def _resolve_expression(body: str, *, model: Model, manifest: Manifest) -> str:
    """Resolve one ``{{ ... }}`` inner body to a qualified name.

    ``body`` is whitespace-stripped by the capturing regex. Dispatches on the
    three supported forms; everything else (``var(...)``, ``env_var(...)``,
    macro calls, bare identifiers) raises :class:`UnsupportedJinjaError`.
    """
    if _THIS_RE.match(body):
        return model.resolve_this().qualified_name

    ref_match = _REF_RE.match(body)
    if ref_match is not None:
        return _resolve_ref_args(ref_match.group(1), manifest=manifest)

    source_match = _SOURCE_RE.match(body)
    if source_match is not None:
        return _resolve_source_args(source_match.group(1), manifest=manifest)

    raise UnsupportedJinjaError(
        f"Unsupported Jinja expression in singular-test SQL: {{{{ {body} }}}}. "
        "Only this, ref(), and source() are supported.",
    )


def _resolve_ref_args(arglist: str, *, manifest: Manifest) -> str:
    """Resolve a ``ref(...)`` arg list to a qualified name.

    The last quoted POSITIONAL is the model name; a leading positional (the
    two-arg form) is the package disambiguator. Keyword arguments such as
    ``version='1'`` are split out and ignored — only positionals participate
    in name/package resolution, mirroring
    :func:`signalforge.manifest.loader.resolve_ref`. Without this split a
    ``ref('orders', version='1')`` would otherwise treat ``'1'`` as a
    positional and resolve the model name to ``"1"``.
    """
    positionals: list[str] = []
    for raw_token in arglist.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if _KWARG_RE.match(token):
            # A kwarg (``version='1'``) — not a positional name/package arg.
            continue
        quoted = _QUOTED_ARG_RE.search(token)
        if quoted is not None:
            positionals.append(quoted.group(1))
    if not positionals:
        raise UnsupportedJinjaError(
            "ref() in singular-test SQL has no quoted positional name argument; "
            "the bounded resolver cannot evaluate dynamic ref() calls.",
        )
    name = positionals[-1]
    package = positionals[0] if len(positionals) >= 2 else None
    return manifest.resolve_ref(name, package=package).qualified_name


def _resolve_source_args(arglist: str, *, manifest: Manifest) -> str:
    """Resolve a ``source('s', 't')`` arg list to a qualified name.

    Requires exactly two quoted positionals (source name + table name); any
    other shape is not a static source() call the bounded resolver can map.
    """
    args = _QUOTED_ARG_RE.findall(arglist)
    if len(args) != 2:
        raise UnsupportedJinjaError(
            "source() in singular-test SQL must take exactly two quoted "
            "arguments, source('<source>', '<table>'); the bounded resolver "
            "cannot evaluate other forms.",
        )
    return manifest.resolve_source(args[0], args[1]).qualified_name
