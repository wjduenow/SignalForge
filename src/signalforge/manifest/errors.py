"""Typed exception hierarchy for the manifest layer.

Implements DEC-013 (seven-class hierarchy rooted at ``ManifestError``) and
DEC-014 (every error carries an actionable ``remediation`` rendered in
``__str__``). The remediation pattern operationalises the README's
"explainable diffs" commitment at the loader's failure surface.
"""

from __future__ import annotations


class ManifestError(Exception):
    """Base class for all manifest-layer errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log output
    and CLI output both read cleanly.
    """

    default_remediation: str = "(no remediation set — this is the base class)"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation if remediation is not None else self.default_remediation

    def __str__(self) -> str:
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class ManifestNotFoundError(ManifestError):
    """``target/manifest.json`` (or the explicit override path) is absent."""

    default_remediation = "Run `dbt parse` or check that `project_dir` is correct."


class UnsupportedManifestVersionError(ManifestError):
    """The manifest's schema version is outside the v9–v12 supported range.

    Fusion v20 manifests trip this error in v0.1; support is tracked as
    future work.
    """

    default_remediation = (
        "Supported manifest schemas: v9 through v12 (dbt 1.5 through 1.11). "
        "Fusion v20 is tracked as future work."
    )


class ModelNotFoundError(ManifestError):
    """The requested ``unique_id`` or file path is not present in ``nodes``
    (and is not present in the parallel ``disabled`` dict either)."""

    default_remediation = (
        "Verify the unique_id or file path. Use `Manifest.iter_models()` to list known models."
    )


class ModelDisabledError(ModelNotFoundError):
    """The requested model exists but lives in the ``disabled`` parallel
    dict — it was disabled in dbt config and is therefore not loaded into
    ``nodes``. Subclassing ``ModelNotFoundError`` lets callers catch both
    with a single ``except`` clause when they don't care which it is."""

    default_remediation = (
        "The model is in the `disabled` parallel dict. Enable it in dbt config to use it."
    )


class ModelPathOutsideProjectError(ManifestError):
    """The supplied (or resolved) path escapes ``project_dir`` — typically
    via a symlink or a ``..``-laden relative path. Raised by the
    symlink-hardened path resolver (DEC-007)."""

    default_remediation = (
        "Pass a path relative to project_dir, or an absolute path that "
        "resolves under it (no symlink escapes)."
    )


class ModelMissingSqlError(ManifestError):
    """The resolved model's ``raw_code`` is null or empty.

    Per DEC-004 we surface ``raw_code`` only — we do not silently fall
    back to ``compiled_code``. An empty ``raw_code`` almost always means
    the manifest pre-dates a re-parse, hence the remediation message.
    """

    default_remediation = (
        "Run `dbt parse` first — the manifest's `raw_code` field is empty for this model."
    )


class SelectorParseError(ManifestError):
    """The ``--select`` expression supplied to :func:`parse_selector` is syntactically invalid.

    Issued for empty atoms (``""``, leading / trailing / consecutive commas),
    empty payloads (``tag:``, ``path:``), or any unparseable shape. Issue #37
    DEC-001 (grammar) + DEC-012 (module location): selector parsing lives in
    :mod:`signalforge.manifest.select`, and the manifest layer raises this
    typed error so the CLI layer can wrap it as a tier-2 input-validation
    failure (``CliSelectorParseError``) without sniffing message text.
    """

    default_remediation = (
        "Selector grammar: <atom>[,<atom>]*, where <atom> is 'tag:<name>', "
        "'path:<glob>', or a bare unique_id / file path. Example: "
        "'tag:staging,path:models/marts/*'."
    )
