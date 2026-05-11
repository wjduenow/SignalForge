"""Manifest subpackage — read-only access to dbt's ``target/manifest.json``.

Public surface (DEC-017):

- :func:`load` — entry point that reads, validates, and indexes a manifest.
- :class:`Manifest`, :class:`Model` — Pydantic models surfaced to callers.
- The full :class:`ManifestError` hierarchy, so callers can catch typed
  failures without reaching into private modules.
- :func:`parse_selector`, :func:`select_models`, :data:`SelectorAtom`
  (issue #37 DEC-012) — the typed selector grammar for multi-model batch.

Anything not re-exported here is an implementation detail. Internal helpers
(e.g. ``_canonicalise_path``, ``_detect_version``) remain reachable via their
dotted module paths but are deliberately not promoted to the package's
top-level namespace.
"""

from signalforge.manifest.errors import (
    ManifestError,
    ManifestNotFoundError,
    ModelDisabledError,
    ModelMissingSqlError,
    ModelNotFoundError,
    ModelPathOutsideProjectError,
    SelectorParseError,
    UnsupportedManifestVersionError,
)
from signalforge.manifest.loader import load
from signalforge.manifest.models import Manifest, Model
from signalforge.manifest.select import SelectorAtom, parse_selector, select_models

__all__ = [
    "load",
    "Manifest",
    "Model",
    "ManifestError",
    "ManifestNotFoundError",
    "UnsupportedManifestVersionError",
    "ModelNotFoundError",
    "ModelDisabledError",
    "ModelPathOutsideProjectError",
    "ModelMissingSqlError",
    "SelectorParseError",
    "SelectorAtom",
    "parse_selector",
    "select_models",
]
