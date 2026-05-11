"""Selector grammar for ``signalforge generate --select`` (issue #37).

Pure manifest-layer code; no warehouse / LLM contact.

Public surface
--------------

* :func:`parse_selector` — split a user expression into typed atoms.
* :func:`select_models` — resolve a selector expression against a
  :class:`signalforge.manifest.models.Manifest`, returning deduplicated,
  ``unique_id``-sorted matches.
* :class:`SelectorAtom` — discriminated-union alias over the three concrete
  atom shapes (:class:`TagAtom`, :class:`PathAtom`, :class:`BareAtom`).

Grammar (locked by issue #37 DEC-001)
-------------------------------------

``<atom>[,<atom>]*`` where ``<atom>`` is one of:

* ``tag:<name>`` — non-empty ``<name>``. Match if
  ``<name> in (set(Model.tags) | set(Model.config.tags))``.
* ``path:<glob>`` — non-empty ``<glob>``. Match via
  :func:`fnmatch.fnmatchcase` against ``Model.original_file_path``.
* bare ``<value>`` — non-empty. If ``<value>.startswith("model.")`` route as
  ``unique_id``; otherwise exact-match against ``Model.original_file_path``
  (mirrors existing positional ``<model>`` semantics).

Whitespace around the comma is stripped. Empty atoms / empty tag / empty
glob / empty bare → :class:`SelectorParseError`. Multi-expression is union
(set-OR). Results are deduplicated by ``unique_id`` and ordered by
``unique_id`` lexicographic sort for deterministic downstream consumption
(CLI summary, integration tests).

dbt's space-separated convention diverges (intentionally — comma is
unambiguous in argparse). dbt's intersections / exclusions / graph
operators are out of scope for v0.2.

Module-shape notes
------------------

* Atoms are **user-input typed**, not read-back-from-disk: typos must fail
  loud. Every atom uses ``extra="forbid"`` (deviates from the manifest
  layer's default ``extra="ignore"`` — see `manifest-readers.md` for the
  rationale on config-shaped vs reader-shaped extras).
* Atoms are frozen Pydantic v2 models with a ``kind: Literal[...]``
  discriminator. ``SelectorAtom`` is an ``Annotated[Union[...], Field(
  discriminator="kind")]`` alias so callers can pattern-match exhaustively.
"""

from __future__ import annotations

import fnmatch
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from signalforge.manifest.errors import SelectorParseError
from signalforge.manifest.models import Manifest, Model

_ATOM_CONFIG = ConfigDict(frozen=True, extra="forbid")


class TagAtom(BaseModel):
    """Matches a model if ``name`` appears in ``Model.tags ∪ Model.config.tags``."""

    model_config = _ATOM_CONFIG

    kind: Literal["tag"] = "tag"
    name: str


class PathAtom(BaseModel):
    """Matches a model if ``fnmatch.fnmatchcase(Model.original_file_path, glob)``."""

    model_config = _ATOM_CONFIG

    kind: Literal["path"] = "path"
    glob: str


class BareAtom(BaseModel):
    """Bare atom: routes by prefix.

    * ``value.startswith("model.")`` → ``unique_id`` exact match.
    * Else → ``original_file_path`` exact match.

    Mirrors the existing positional ``<model>`` argument semantics in
    :meth:`signalforge.manifest.Manifest.get_model`.
    """

    model_config = _ATOM_CONFIG

    kind: Literal["bare"] = "bare"
    value: str


SelectorAtom = Annotated[
    TagAtom | PathAtom | BareAtom,
    Field(discriminator="kind"),
]
"""Discriminated union over the three concrete atom shapes."""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_atom(raw: str, *, original_expr: str) -> TagAtom | PathAtom | BareAtom:
    """Classify a single (whitespace-stripped) atom string.

    The caller has already validated that ``raw`` is non-empty (empty atoms
    are detected at the comma-split layer so the error message can name the
    multi-expression case explicitly).
    """
    if raw.startswith("tag:"):
        payload = raw[len("tag:") :]
        if not payload:
            raise SelectorParseError(
                f"empty tag payload in selector expression: {original_expr!r}",
            )
        return TagAtom(name=payload)
    if raw.startswith("path:"):
        payload = raw[len("path:") :]
        if not payload:
            raise SelectorParseError(
                f"empty path payload in selector expression: {original_expr!r}",
            )
        return PathAtom(glob=payload)
    return BareAtom(value=raw)


def parse_selector(expr: str) -> tuple[TagAtom | PathAtom | BareAtom, ...]:
    """Split ``expr`` on ``,`` and classify each atom.

    See module docstring for the locked grammar (issue #37 DEC-001).

    Raises:
        SelectorParseError: empty expression, empty atom (leading / trailing
            / consecutive commas), or empty payload on ``tag:`` / ``path:``.
    """
    if expr == "":
        raise SelectorParseError(
            "empty selector expression",
        )
    parts = [p.strip() for p in expr.split(",")]
    if any(p == "" for p in parts):
        raise SelectorParseError(
            f"empty atom in selector expression: {expr!r}",
        )
    return tuple(_parse_atom(p, original_expr=expr) for p in parts)


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


def _match_tag(model: Model, name: str) -> bool:
    return name in (set(model.tags) | set(model.config.tags))


def _match_path(model: Model, glob: str) -> bool:
    return fnmatch.fnmatchcase(model.original_file_path, glob)


def _match_bare(model: Model, value: str) -> bool:
    if value.startswith("model."):
        return model.unique_id == value
    return model.original_file_path == value


def _match(model: Model, atom: TagAtom | PathAtom | BareAtom) -> bool:
    # Exhaustive over the three concrete atom kinds; mypy/pyright narrows on
    # the literal discriminator.
    if isinstance(atom, TagAtom):
        return _match_tag(model, atom.name)
    if isinstance(atom, PathAtom):
        return _match_path(model, atom.glob)
    return _match_bare(model, atom.value)


def select_models(manifest: Manifest, expr: str) -> tuple[Model, ...]:
    """Return models in ``manifest`` matching the selector ``expr``.

    Atoms are unioned (set-OR). The result is deduplicated by ``unique_id``
    and ordered by ``unique_id`` lexicographic sort (deterministic for the
    CLI summary + integration tests).

    Zero-match returns an empty tuple — the manifest layer does not raise
    on empty matches. The CLI layer is responsible for converting empty to
    its own typed error (``CliSelectorNoMatchError``) so the manifest
    layer's catch surface stays homogeneous.

    Raises:
        SelectorParseError: same shape rules as :func:`parse_selector`.
    """
    atoms = parse_selector(expr)
    matched: dict[str, Model] = {}
    for model in manifest.iter_models():
        for atom in atoms:
            if _match(model, atom):
                matched[model.unique_id] = model
                break
    return tuple(matched[uid] for uid in sorted(matched))
