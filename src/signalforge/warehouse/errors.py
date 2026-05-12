"""Typed exception hierarchy for the warehouse adapter layer.

Implements DEC-026 (15-class hierarchy rooted at :class:`WarehouseError`)
and DEC-022 (user-supplied strings rendered via ``repr()`` so adversarial
input — embedded quotes, control chars — cannot smuggle special characters
into log viewers or error messages). Mirrors the style established by
:mod:`signalforge.manifest.errors`: every error carries a class-level
``default_remediation`` that the base ``__str__`` renders on a separate
``↳ Remediation:`` line.

The remediation pattern operationalises the README's "explainable diffs"
commitment at the warehouse layer's failure surface; every distinct failure
mode the adapter can produce gets a typed exception so the prune/CLI layers
can pattern-match without sniffing message text.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages (DEC-022).

    Embedding raw user input in error strings is a log-injection seam: a
    crafted dataset name like ``foo'\\nINFO: spoofed log line`` could pollute
    log viewers or stack traces. Routing every user-controlled value through
    ``repr()`` quotes the string, escapes control characters, and makes
    whitespace visible.
    """
    return repr(v)


class WarehouseError(Exception):
    """Base class for all warehouse-adapter errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log output
    and CLI output both read cleanly.
    """

    default_remediation: ClassVar[str] = "(no remediation set — this is the base class)"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = (
            remediation if remediation is not None else type(self).default_remediation
        )

    def __str__(self) -> str:
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class WarehouseAuthError(WarehouseError):
    """Wraps :class:`google.auth.exceptions.DefaultCredentialsError` and
    :class:`google.auth.exceptions.RefreshError` so callers can catch a
    SignalForge-typed exception instead of a Google-internal one."""

    default_remediation: ClassVar[str] = (
        "Run `gcloud auth application-default login` to set up Application Default Credentials."
    )


class UnsupportedProfileTypeError(WarehouseError):
    """The dbt profile's ``type`` field is not ``"bigquery"``.

    v0.1 ships the BigQuery adapter only; Snowflake/Postgres land in v0.2.
    """

    default_remediation: ClassVar[str] = (
        "v0.1 supports `type: bigquery` only. Snowflake and Postgres adapters are tracked for v0.2."
    )

    def __init__(self, profile_type: str, *, remediation: str | None = None) -> None:
        self.profile_type = profile_type
        message = f"Unsupported profile type: {_format_value(profile_type)}"
        super().__init__(message, remediation=remediation)


class UnsupportedAuthMethodError(WarehouseError):
    """The dbt profile's ``method`` field is not ``"oauth"`` (or ``None``).

    v0.1 supports OAuth / Application Default Credentials only; service-account
    JSON, impersonation, and token-file methods land later.
    """

    default_remediation: ClassVar[str] = (
        "v0.1 supports `method: oauth` (or unset) only. Run "
        "`gcloud auth application-default login` to set up ADC."
    )

    def __init__(self, method: str, *, remediation: str | None = None) -> None:
        self.method = method
        message = f"Unsupported auth method: {_format_value(method)}"
        super().__init__(message, remediation=remediation)


class ProfileNotFoundError(WarehouseError):
    """None of the three search paths yielded a ``profiles.yml`` file.

    The remediation lists every path searched so the user can see exactly
    where we looked.
    """

    default_remediation: ClassVar[str] = (
        "Create a `profiles.yml` at one of the searched paths, or set the "
        "DBT_PROFILES_DIR environment variable."
    )

    def __init__(
        self,
        searched_paths: list[Path],
        *,
        remediation: str | None = None,
    ) -> None:
        self.searched_paths = list(searched_paths)
        rendered_paths = ", ".join(_format_value(str(p)) for p in self.searched_paths)
        message = f"No profiles.yml found. Searched: [{rendered_paths}]"
        if remediation is None:
            remediation = (
                f"Create a `profiles.yml` at one of: [{rendered_paths}], or set "
                "the DBT_PROFILES_DIR environment variable."
            )
        super().__init__(message, remediation=remediation)


class ProfileEnvVarUnsetError(ProfileNotFoundError):
    """The profile references ``env_var('NAME')`` for a variable that is
    not set in the environment and has no default supplied.

    Inherits from :class:`ProfileNotFoundError` so existing callers
    catching "profile won't load" with one except keep working
    (init-demo's documented happy path falls back to ``profiles.yml``
    edits when this fires, so the operator sees one error type for
    "profile broken").

    The dbt convention ``env_var('NAME', 'default')`` resolves to the
    default and never raises; only the no-default form trips this.
    Added by issue #47 to support the demo's
    ``{{ env_var('GOOGLE_CLOUD_PROJECT') }}`` profile.
    """

    default_remediation: ClassVar[str] = (
        "Set the environment variable named in the message, or supply a default "
        "to the env_var(...) call (e.g. env_var('NAME', 'fallback'))."
    )

    def __init__(
        self,
        var_name: str,
        profiles_path: Path,
        *,
        remediation: str | None = None,
    ) -> None:
        self.var_name = var_name
        self.profiles_path = profiles_path
        message = (
            f"profiles.yml at {profiles_path} references env_var({_format_value(var_name)}) "
            f"but environment variable {_format_value(var_name)} is not set "
            "and no default was supplied."
        )
        if remediation is None:
            remediation = (
                f"Set the environment variable: `export {var_name}=<value>`, "
                f"or edit {profiles_path} to supply a default: "
                f"`env_var('{var_name}', '<default>')`."
            )
        # Track searched_paths so parent contract holds.
        self.searched_paths: list[Path] = [profiles_path]
        WarehouseError.__init__(self, message, remediation=remediation)


class ProfileTargetNotFoundError(ProfileNotFoundError):
    """The profile resolved but the requested ``target`` field is missing.

    Inherits from :class:`ProfileNotFoundError` so callers can catch both
    "no profile" and "wrong target" with a single ``except`` clause when
    they don't care which it is.
    """

    default_remediation: ClassVar[str] = (
        "Add the requested target to the profile in profiles.yml, or pass an "
        "explicit `target=` that exists in the profile."
    )

    def __init__(
        self,
        profile_name: str,
        target: str,
        *,
        available: list[str] | None = None,
        profiles_path: Path | None = None,
        remediation: str | None = None,
    ) -> None:
        self.profile_name = profile_name
        self.target = target
        self.available = list(available) if available is not None else []
        self.profiles_path = profiles_path
        # Bypass ProfileNotFoundError.__init__ — we have a different message
        # shape — and call WarehouseError.__init__ directly.
        message = (
            f"Target {_format_value(target)} not found in profile {_format_value(profile_name)}."
        )
        if remediation is None:
            available_str = (
                ", ".join(_format_value(a) for a in self.available) if self.available else "(none)"
            )
            location = f" in {profiles_path}" if profiles_path is not None else ""
            remediation = (
                f"Available targets for profile `{profile_name}`{location}: "
                f"[{available_str}]. Pass an explicit `target=` matching one "
                "of these, or add the requested target to profiles.yml."
            )
        # Track searched_paths so the parent's contract holds for callers
        # that introspect it.
        self.searched_paths: list[Path] = [profiles_path] if profiles_path is not None else []
        WarehouseError.__init__(self, message, remediation=remediation)


class ManifestProjectNotFoundError(WarehouseError):
    """``Model.database`` is ``None`` so we cannot construct a fully-qualified
    BigQuery :class:`TableRef`. Raised by ``TableRef.from_model`` (US-004)."""

    default_remediation: ClassVar[str] = (
        "Set `database:` (BigQuery project) for this model in dbt, or pass an "
        "explicit project= when constructing TableRef."
    )

    def __init__(self, model_unique_id: str, *, remediation: str | None = None) -> None:
        self.model_unique_id = model_unique_id
        message = (
            f"Model {_format_value(model_unique_id)} has no `database` (BigQuery "
            "project) set in the manifest."
        )
        super().__init__(message, remediation=remediation)


class ManifestSchemaNotFoundError(WarehouseError):
    """``Model.schema_`` is ``None`` so we cannot construct a fully-qualified
    BigQuery :class:`TableRef`. Raised by ``TableRef.from_model`` (US-004)."""

    default_remediation: ClassVar[str] = (
        "Set `schema:` (BigQuery dataset) for this model in dbt, or pass an "
        "explicit dataset= when constructing TableRef."
    )

    def __init__(self, model_unique_id: str, *, remediation: str | None = None) -> None:
        self.model_unique_id = model_unique_id
        message = (
            f"Model {_format_value(model_unique_id)} has no `schema` (BigQuery "
            "dataset) set in the manifest."
        )
        super().__init__(message, remediation=remediation)


class InvalidIdentifierError(WarehouseError):
    """A SQL identifier (project / dataset / table / column) failed the
    DEC-013 regex ``[A-Za-z_][A-Za-z0-9_]*``.

    The offending value is rendered via :func:`_format_value` (i.e. ``repr()``)
    so adversarial input cannot smuggle special characters into log viewers.
    """

    default_remediation: ClassVar[str] = "Identifiers must match [A-Za-z_][A-Za-z0-9_]*."

    def __init__(self, field: str, value: str, *, remediation: str | None = None) -> None:
        self.field = field
        self.value = value
        message = f"Invalid identifier for field {_format_value(field)}: {_format_value(value)}"
        super().__init__(message, remediation=remediation)


class BytesBilledExceededError(WarehouseError):
    """The BigQuery ``maximum_bytes_billed`` cap rejected a query.

    Wraps the underlying :class:`google.api_core.exceptions.BadRequest`.
    The ``job_id`` field lets ops docs cross-link to BigQuery's job history.
    """

    default_remediation: ClassVar[str] = (
        "Either narrow the query (add a partition filter or smaller sample) "
        "or raise `max_bytes_billed_per_query` in the adapter config."
    )

    def __init__(
        self,
        job_id: str | None,
        bytes_billed: int | None,
        limit: int,
        *,
        remediation: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.bytes_billed = bytes_billed
        self.limit = limit
        message = (
            f"Query exceeded max_bytes_billed (limit={limit}, "
            f"billed={bytes_billed}, job_id={_format_value(job_id)})."
        )
        super().__init__(message, remediation=remediation)


class TableNotFoundError(WarehouseError):
    """Typed wrapper around BigQuery's 404-for-table.

    Raised when ``get_table`` / ``sample_rows`` / ``column_stats`` cannot
    resolve the requested :class:`TableRef`.
    """

    default_remediation: ClassVar[str] = (
        "Verify the project.dataset.table exists and that the active credentials have read access."
    )

    def __init__(self, table: str, *, remediation: str | None = None) -> None:
        self.table = table
        message = f"Table not found: {_format_value(table)}"
        super().__init__(message, remediation=remediation)


class ColumnNotFoundError(WarehouseError):
    """Typed wrapper around BigQuery's 404-for-column / a column reference
    that does not exist on the resolved table schema."""

    default_remediation: ClassVar[str] = (
        "Verify the column name against the table schema "
        "(`SELECT column_name FROM `<dataset>`.INFORMATION_SCHEMA.COLUMNS`)."
    )

    def __init__(self, table: str, column: str, *, remediation: str | None = None) -> None:
        self.table = table
        self.column = column
        message = f"Column {_format_value(column)} not found on table {_format_value(table)}."
        super().__init__(message, remediation=remediation)


class QuerySyntaxError(WarehouseError):
    """Wraps BigQuery's ``BadRequest`` for SQL parse errors so callers can
    distinguish "your SQL is malformed" from other ``BadRequest`` flavours
    (e.g. :class:`BytesBilledExceededError`)."""

    default_remediation: ClassVar[str] = (
        "Inspect the BigQuery error detail and fix the SQL. The drafter's "
        "prompt should be updated if this recurs."
    )

    def __init__(self, detail: str, *, remediation: str | None = None) -> None:
        self.detail = detail
        message = f"BigQuery rejected the query: {_format_value(detail)}"
        super().__init__(message, remediation=remediation)


class SamplingError(WarehouseError):
    """Parent for sampling-time failures.

    Subclasses cover the two distinct ways ``sample_rows`` can fail loudly
    (DEC-024); a ``except SamplingError`` catches both.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the specific subclass's remediation; sampling failed and "
        "fail-loud is preferred to silent over-spend."
    )


class SamplingRequiresPartitionFilterError(SamplingError):
    """A table large enough to trip the ``_LARGE_TABLE_THRESHOLD`` was
    sampled without a ``PartitionFilter``. DEC-024 fails loud rather than
    let an unscoped sample scan terabytes."""

    default_remediation: ClassVar[str] = (
        "Pass a `PartitionFilter` to scope the sample to a specific partition; "
        "unscoped sampling on tables this large risks excessive bytes billed."
    )

    def __init__(self, table: str, num_rows: int, *, remediation: str | None = None) -> None:
        self.table = table
        self.num_rows = num_rows
        message = (
            f"Table {_format_value(table)} has {num_rows} rows; sampling "
            "requires a PartitionFilter."
        )
        super().__init__(message, remediation=remediation)


class UnknownTableSizeError(SamplingError):
    """``Table.num_rows`` is ``None`` / 0 and no ``PartitionFilter`` was
    supplied. DEC-024 refuses to guess a bucket size in this case."""

    default_remediation: ClassVar[str] = (
        "Provide partition_filter to scope the sample, or call "
        "adapter.refresh_table_metadata(table) once num_rows is populated."
    )

    def __init__(self, table: str, *, remediation: str | None = None) -> None:
        self.table = table
        message = (
            f"Table {_format_value(table)} has unknown num_rows and no "
            "PartitionFilter was supplied."
        )
        super().__init__(message, remediation=remediation)


class MaterialisationFailedError(WarehouseError):
    """The per-run sample-materialisation query (BigQuery CTAS / equivalent)
    failed at the SDK / network / quota seam (DEC-008 of issue #22).

    Wraps the underlying SDK exception via the ``cause=`` kwarg pattern
    (mirrors ``LLMResponseAuditWriteError``). The orchestrator routes
    every remaining test to ``kept-without-evidence`` per Q3 of the
    plan; the typed exception lets the prune/CLI layers pattern-match
    on materialisation failure without sniffing message text.

    Tier-3 in the CLI exit-code taxonomy (external-dep failure) via
    inheritance from :class:`WarehouseError`.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the underlying warehouse error (job_id / quota / auth). "
        "To bypass materialisation entirely, set "
        "'prune.sample_strategy: oneshot' in signalforge.yml."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(message, remediation=remediation)


class EstimateNotSupportedError(WarehouseError):
    """The :class:`WarehouseAdapter` ABC default impl of
    ``estimate_query_bytes`` raises this; concrete adapters override the
    method to provide a real implementation (US-002 of issue #36).

    v0.2 ships the BigQuery override; non-BigQuery adapters
    (Snowflake/Postgres in v0.3) inherit the default raise until each
    grows its own override. The remediation is verbatim DEC-004 of the
    plan: it tells the operator how to opt out by using a BigQuery
    profile or waiting for v0.3 multi-warehouse estimation support.

    Tier-3 in the CLI exit-code taxonomy via inheritance from
    :class:`WarehouseError`.
    """

    # Locked verbatim per DEC-004 of plans/super/36-estimate-cost-preview.md;
    # changing this text breaks a contract test pinning the byte-equal
    # remediation string (test_estimatenotsupportederror_remediation_locked_verbatim).
    default_remediation: ClassVar[str] = (
        "Use --estimate with a BigQuery profile, "
        "or wait for v0.3 multi-warehouse estimation support."
    )

    def __init__(self, adapter_name: str, *, remediation: str | None = None) -> None:
        self.adapter_name = adapter_name
        message = f"Adapter {_format_value(adapter_name)} does not support query-bytes estimation."
        super().__init__(message, remediation=remediation)


class MaterialisationNotSupportedError(WarehouseError):
    """The :class:`WarehouseAdapter` ABC default impl of
    ``materialise_sample`` raises this; concrete adapters override the
    method to provide a real implementation (DEC-008 of issue #22).

    v0.1 ships the BigQuery override in US-003; non-BigQuery adapters
    (Snowflake/Postgres in v0.2) inherit the default raise until each
    grows its own override. The remediation is verbatim DEC-006 of the
    plan: it tells the operator how to opt out via
    ``signalforge.yml prune.sample_strategy: oneshot``.

    Tier-3 in the CLI exit-code taxonomy via inheritance from
    :class:`WarehouseError`.
    """

    default_remediation: ClassVar[str] = (
        "Set 'prune.sample_strategy: oneshot' in signalforge.yml to fall "
        "back to per-test sampling, or wait for v0.3 multi-warehouse "
        "materialisation support."
    )

    def __init__(self, adapter_name: str, *, remediation: str | None = None) -> None:
        self.adapter_name = adapter_name
        message = f"Adapter {_format_value(adapter_name)} does not support sample materialisation."
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (verified by tests/warehouse/test_errors.py).
__all__ = [
    "BytesBilledExceededError",
    "ColumnNotFoundError",
    "EstimateNotSupportedError",
    "InvalidIdentifierError",
    "ManifestProjectNotFoundError",
    "ManifestSchemaNotFoundError",
    "MaterialisationFailedError",
    "MaterialisationNotSupportedError",
    "ProfileEnvVarUnsetError",
    "ProfileNotFoundError",
    "ProfileTargetNotFoundError",
    "QuerySyntaxError",
    "SamplingError",
    "SamplingRequiresPartitionFilterError",
    "TableNotFoundError",
    "UnknownTableSizeError",
    "UnsupportedAuthMethodError",
    "UnsupportedProfileTypeError",
    "WarehouseAuthError",
    "WarehouseError",
]
