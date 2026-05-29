"""Parametrized exit-code contract for every typed exception (US-008).

Pairs the 7th AST scan in :mod:`tests.test_audit_completeness` (which
asserts every ``*Error`` class declared in any
``src/signalforge/*/errors.py`` appears in the
:data:`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` mapping) with
the runtime contract: for each entry in the mapping, raise the exception
from a stage and assert the CLI's exit code matches the table value, the
stderr starts with ``ERROR: ``, and no traceback ever leaks (DEC-016).

The pattern of patching the first stage entry (``manifest.load``) to
raise the candidate exception works for every exception class because
the CLI's boundary catch is shape-blind — every typed exception is
routed through :func:`format_error_to_stderr` and
:func:`map_exception_to_exit_code` regardless of which stage raised it.
A future story that wants to assert a specific stage produces a
specific exception (e.g. only the warehouse adapter can raise
:class:`TableNotFoundError` in production) belongs in
``tests/cli/test_generate.py`` — this file is the *taxonomy* contract.

Traces to: DEC-008 (stderr shape — single line for tiers 1/3, header +
bullets for tier 2), DEC-019 (the taxonomy itself), DEC-024 (the AST
scan + this parametrized loop are paired).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from signalforge.cli import main
from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE
from tests.cli._factories import make_fake_dbt_project
from tests.cli.test_generate import _install_happy_patches

# ---------------------------------------------------------------------------
# Synthetic-instance factory
# ---------------------------------------------------------------------------
#
# Most typed exceptions in the project follow one of three shapes:
#
# * ``Cls(message, *, remediation=None)`` — the layer-base default.
# * ``Cls(positional_arg, *, remediation=None)`` — domain-specific (e.g.
#   ``TableNotFoundError(table)``).
# * ``Cls(*, kwargs..., remediation=None)`` — kw-only multi-field (e.g.
#   ``LLMOutputAnchorContractError(violations=...)``,
#   ``GradeBelowThresholdError(pass_rate=...)``).
#
# We can't introspect ``inspect.signature`` reliably for every class
# (some derive their __init__ via the layer-base mixin and the signature
# objects look identical despite different required kwargs at runtime).
# Instead, the factory below maps each class name to a small
# ``construct(cls)`` lambda. New typed exceptions added to the mapping
# table need an entry here; the parametrized smoke test raises a clear
# AssertionError naming the missing class.

_SENTINEL_PATH = Path("/tmp/signalforge-test-sentinel")
_SENTINEL_MESSAGE = "synthetic test instance"
_SENTINEL_CAUSE = RuntimeError("synthetic cause")


def _construct_exception(exc_cls: type[BaseException]) -> BaseException:
    """Construct a synthetic instance of ``exc_cls``.

    Per-class branches handle the kw-only and multi-positional shapes;
    the catch-all path falls through to ``exc_cls(_SENTINEL_MESSAGE)``
    for the layer-base default. ``TypeError`` from a constructor that
    doesn't accept the catch-all shape is re-raised with a clear hint
    so the test failure names the offending class.

    The local ``cls`` alias is typed ``Any`` so pyright doesn't flag the
    per-class kw-only constructor arguments (``violations=...``,
    ``pass_rate=...``, etc.) as unknown parameters. The runtime
    contract is enforced by the parametrized test below.
    """
    name = exc_cls.__name__
    cls: Any = exc_cls

    # Path-shaped constructors.
    if name in {"ConfigNotFoundError", "DraftConfigNotFoundError"}:
        return cls(_SENTINEL_PATH)

    # Profile-shape constructors.
    if name == "ProfileNotFoundError":
        return cls([_SENTINEL_PATH])
    if name == "ProfileTargetNotFoundError":
        return cls("default", "dev")
    if name in {"UnsupportedProfileTypeError", "UnsupportedAuthMethodError"}:
        return cls("synthetic")
    if name == "IncompleteProfileError":
        return cls("snowflake", ["account", "warehouse"])
    if name in {"ManifestProjectNotFoundError", "ManifestSchemaNotFoundError"}:
        return cls("model.test.x")

    # Warehouse identifier / table / column shapes.
    if name == "InvalidIdentifierError":
        return cls("table", "bad name")
    if name == "TableNotFoundError":
        return cls("project.dataset.tbl")
    if name == "ColumnNotFoundError":
        return cls("project.dataset.tbl", "missing")
    if name == "BytesBilledExceededError":
        return cls("job-id", 1_000_000_000, 100_000_000)
    if name == "QuerySyntaxError":
        return cls("synthetic syntax detail")
    if name == "SamplingRequiresPartitionFilterError":
        return cls("project.dataset.tbl", 100_000_000)
    if name == "UnknownTableSizeError":
        return cls("project.dataset.tbl")
    if name == "AuditWriteError":
        return cls(_SENTINEL_PATH, _SENTINEL_CAUSE)
    if name in {
        "AuditRecordTooLargeError",
        "PruneAuditRecordTooLargeError",
        "GradeAuditRecordTooLargeError",
        "LLMResponseAuditRecordTooLargeError",
        "DiffSidecarRecordTooLargeError",
        "DiffTestFileRecordTooLargeError",
        "DiffInputTooLargeError",
        "IngestSchemaTooLargeError",
    }:
        return cls(5000, 4000)

    # Ingest layer (issue #104) — anchor-contract collect-all takes a
    # positional tuple of violation strings.
    if name == "IngestAnchorContractError":
        return cls(("a", "b", "c"))

    # Safety policy / config edge shapes.
    if name == "ColumnNotInModelError":
        return cls("model.test.x", "missing_col")
    if name == "InvalidSamplingModeError":
        return cls("nope", allowed=("schema-only",))
    if name == "InvalidPatternError":
        return cls("[bad", reason="invalid glob")
    if name == "UnknownConfigKeyError":
        return cls("foo", "safety")
    if name == "PolicyValidationError":
        return cls("mode", "bogus", "not allowed")

    # Drafter LLM-output family — every ``LLMOutput*`` carries the
    # full evidence bundle of fields. We synthesise the minimal set.
    _llm_output_kwargs: dict[str, Any] = {
        "raw_text": "{}",
        "prompt_version": "abcdef0123456789",
        "model": "claude-fake",
        "cache_hit": False,
        "input_tokens": 100,
        "output_tokens": 10,
    }
    if name == "LLMOutputError":
        return cls(_SENTINEL_MESSAGE, **_llm_output_kwargs)
    if name == "LLMOutputJSONError":
        import json

        try:
            json.loads("not json")
        except json.JSONDecodeError as cause:
            return cls(_SENTINEL_MESSAGE, cause=cause, **_llm_output_kwargs)
        raise RuntimeError("unreachable: json.loads('not json') should have raised")
    if name == "LLMOutputValidationError":
        from pydantic import BaseModel, ValidationError

        class _Probe(BaseModel):
            x: int

        try:
            _Probe(x="not an int")  # type: ignore[arg-type]
        except ValidationError as cause:
            return cls(_SENTINEL_MESSAGE, cause=cause, **_llm_output_kwargs)
        raise RuntimeError("unreachable: ValidationError did not fire")
    if name == "LLMOutputAnchorContractError":
        return cls(
            _SENTINEL_MESSAGE,
            violations=("a", "b", "c"),
            **_llm_output_kwargs,
        )
    if name == "PromptEnvelopeBreachError":
        return cls("model.test.x")
    if name == "DraftConfigInvalidError":
        return cls(_SENTINEL_MESSAGE, cause=_SENTINEL_CAUSE)

    # Prune-config opt-in shapes.
    if name == "PruneTrustedModelNotFoundError":
        return cls("model.test.missing")

    # Grade-layer shapes.
    if name == "GradePromptEnvelopeBreachError":
        return cls("artifact.test.x")
    if name == "GradeOutputError":
        return cls(_SENTINEL_MESSAGE, violation_type="schema_invalid")
    if name == "GradeBelowThresholdError":
        return cls(
            pass_rate=0.4,
            mean_score=0.55,
            min_pass_rate=0.6,
            min_mean_score=0.6,
            aggregate_complete=True,
        )

    # Diff-layer mismatch shapes.
    if name == "DiffCandidateModelMismatchError":
        return cls("cand", "model")
    if name == "DiffPruneResultModelMismatchError":
        return cls("prune.id", "model.id")
    if name == "DiffGradingReportModelMismatchError":
        return cls("grade.id", "model.id")

    # LLM-helper kwargs.
    if name == "LLMRateLimitError":
        return cls(_SENTINEL_MESSAGE, attempts=3)
    if name in {
        "LLMHelperError",
        "LLMAuthError",
        "LLMServerError",
        "LLMConnectionError",
        "LLMResponseFormatError",
    }:
        return cls(_SENTINEL_MESSAGE, cause=_SENTINEL_CAUSE)
    if name == "LLMCacheTooLargeError":
        return cls(9000, 8000)

    # Audit-write durability shapes (per-layer paired writers).
    if name in {
        "PruneAuditWriteError",
        "GradeAuditWriteError",
        "LLMResponseAuditWriteError",
        "GradeLLMError",
        "DiffSidecarWriteError",
        "DiffTestFileWriteError",
    }:
        return cls(_SENTINEL_MESSAGE, cause=_SENTINEL_CAUSE)

    # Sample-materialisation seam (issue #22 / DEC-008 of US-007 of the
    # plan). ``MaterialisationFailedError`` follows the ``cause=`` kwarg
    # pattern (mirrors ``LLMResponseAuditWriteError``);
    # ``MaterialisationNotSupportedError`` takes a positional adapter
    # name. Both registered at tier 3 (external-dep) per DEC-008.
    if name == "MaterialisationFailedError":
        return cls(_SENTINEL_MESSAGE, cause=_SENTINEL_CAUSE)
    if name == "MaterialisationNotSupportedError":
        return cls("SyntheticAdapter")

    # Query-bytes estimation produced no usable figure (issue #130 /
    # DEC-003). Keyword-only ``detail`` (the diagnostic). Tier 3.
    if name == "EstimateUnavailableError":
        return cls(detail="EXPLAIN plan lacked GlobalStats")

    # Selector-failure CLI wrappers (issue #37 / DEC-007 — US-002).
    # ``CliSelectorParseError`` accepts an optional ``cause`` (the
    # underlying ``SelectorParseError`` from the manifest layer);
    # ``CliSelectorNoMatchError`` is expr-only. Both tier 2.
    if name == "CliSelectorParseError":
        return cls(expr="tag:")
    if name == "CliSelectorNoMatchError":
        return cls(expr="tag:nonexistent")

    # init-demo CLI wrappers (issue #47 / DEC-012, DEC-013 — US-004).
    # Each wrapper takes keyword-only kwargs (``dest=`` / ``cause=`` /
    # ``remediation=``); the dest-exists and dest-unsafe variants are
    # tier 2 (input-validation), fixture-missing and copy-error are
    # tier 1 (broken install / generic filesystem failure).
    if name in {"CliInitDemoDestExistsError", "CliInitDemoDestUnsafeError"}:
        return cls(dest="/tmp/synthetic", cause=_SENTINEL_CAUSE)
    if name == "CliInitDemoFixtureMissingError":
        return cls(cause=_SENTINEL_CAUSE)
    if name == "CliInitDemoCopyError":
        return cls(dest="/tmp/synthetic", cause=_SENTINEL_CAUSE)

    # Warehouse profile env_var failure (issue #47 — supports init-demo's
    # bundled `{{ env_var('GOOGLE_CLOUD_PROJECT') }}` profile). Requires
    # (var_name, profiles_path) as positional args.
    if name == "ProfileEnvVarUnsetError":
        from pathlib import Path

        return cls(var_name="SYNTHETIC_VAR", profiles_path=Path("/tmp/synthetic/profiles.yml"))

    # LLM cost-rollup errors (issue #157 / US-001). Each concrete carries
    # positional args specific to the rollup failure mode (missing JSONLs,
    # malformed JSONL line, unknown model id); the catch-all message shape
    # doesn't fit.
    if name == "CostRollupAuditMissingError":
        return cls(project_dir="/tmp/synthetic", audit_dir=".signalforge")
    if name == "CostRollupMalformedRecordError":
        return cls(
            path="/tmp/synthetic/.signalforge/llm_responses.jsonl",
            line_num=1,
            reason="synthetic JSONDecodeError",
        )
    if name == "CostRollupUnknownModelError":
        return cls(model_id="synthetic-model-id")

    # Catch-all: layer-base default ``Cls(message, *, remediation=None)``.
    try:
        return cls(_SENTINEL_MESSAGE)
    except TypeError as exc:
        raise AssertionError(
            f"Cannot construct {name} via the catch-all "
            f"`{name}({_SENTINEL_MESSAGE!r})` path: {exc}. "
            "Add a per-class branch to _construct_exception in "
            "tests/cli/test_exit_codes.py with the correct constructor "
            "shape."
        ) from exc


# ---------------------------------------------------------------------------
# Parametrized contract: raise → main → assert exit + stderr shape
# ---------------------------------------------------------------------------


_PARAMS: list[tuple[type[BaseException], int]] = sorted(
    _EXCEPTION_TO_EXIT_CODE.items(),
    key=lambda item: (item[1], item[0].__name__),
)


@pytest.mark.parametrize(
    ("exc_cls", "expected_exit"),
    _PARAMS,
    ids=[cls.__name__ for cls, _ in _PARAMS],
)
def test_typed_exception_maps_to_correct_exit_code(
    exc_cls: type[BaseException],
    expected_exit: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """For every entry in :data:`_EXCEPTION_TO_EXIT_CODE`: construct a
    synthetic instance, raise it from the manifest-load stage (the
    earliest patchable seam), call ``main(["generate", ...])``, and
    assert:

    * exit code matches the table value;
    * stderr starts with ``"ERROR: "`` (DEC-008);
    * no ``"Traceback"`` appears anywhere in stderr (DEC-016).

    The boundary catch in :func:`signalforge.cli.generate.cmd_generate`
    is shape-blind — patching the first stage to raise is sufficient
    for every typed exception in the layer.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    exc_instance = _construct_exception(exc_cls)
    mocks["manifest_load"].side_effect = exc_instance

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == expected_exit, (
        f"{exc_cls.__name__}: expected exit {expected_exit}, got {code}. stderr={captured.err!r}"
    )
    assert captured.err.startswith("ERROR: "), (
        f"{exc_cls.__name__}: stderr must start with 'ERROR: '; got {captured.err!r}"
    )
    assert "Traceback" not in captured.err, (
        f"{exc_cls.__name__}: traceback leaked to stderr (DEC-016): {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Tier-2 bullet shape (DEC-008 + DEC-017)
# ---------------------------------------------------------------------------


def test_anchor_contract_violations_render_as_header_plus_bullets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """:class:`LLMOutputAnchorContractError` with three violations →
    stderr matches the canonical header + 3 bullets shape (DEC-008 of
    #9 + DEC-017 of #5). The whole-draft fail-loud contract from #5
    surfaces every violation in one CLI message; CI parsers key on the
    leading ``  - `` bullet prefix.
    """
    import re

    from signalforge.draft.errors import LLMOutputAnchorContractError

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["manifest_load"].side_effect = LLMOutputAnchorContractError(
        "draft response violated anchor contract",
        violations=(
            "column 'phantom' not in model columns",
            "test references missing column 'ghost'",
            "duplicate not_null on 'id'",
        ),
        raw_text="{}",
        prompt_version="abcdef0123456789",
        model="claude-fake",
        cache_hit=False,
        input_tokens=100,
        output_tokens=10,
    )

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 2, f"expected tier-2 (input) exit; got {code}"
    # Header + exactly three bullets, in order. The remediation footer
    # may follow on a subsequent line; the bullet shape is the contract.
    assert re.search(
        r"^ERROR: .+\n  - .+\n  - .+\n  - .+",
        captured.err,
        flags=re.MULTILINE,
    ), f"stderr did not match header+bullets shape:\n{captured.err}"
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Panic path (DEC-016)
# ---------------------------------------------------------------------------


def test_untyped_runtime_error_exits_one_with_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare :class:`RuntimeError` from a stage → exit 1 (load tier
    fallback per :func:`map_exception_to_exit_code`) and no traceback
    leaks to stderr (DEC-016). The boundary catch in
    :func:`cmd_generate` formats the message via
    :func:`format_error_to_stderr` so the message body still surfaces.
    """
    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["manifest_load"].side_effect = RuntimeError("kaboom")

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 1, f"expected panic-tier exit 1; got {code}"
    assert captured.err.startswith("ERROR: "), (
        f"panic-path stderr must start with 'ERROR: '; got {captured.err!r}"
    )
    assert "kaboom" in captured.err, (
        f"panic-path stderr must surface the exception message; got {captured.err!r}"
    )
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Explicit per-class call-out: TableNotFoundError → tier 2 (DEC-012)
# ---------------------------------------------------------------------------


def test_table_not_found_error_exits_tier_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """:class:`TableNotFoundError` is tier 2 (input) per DEC-012. The
    parametrized loop above covers every entry in the table; this
    non-parametrized test calls the contract out by name so the diff
    on a future tier change is easy to read in code review.
    """
    from signalforge.warehouse.errors import TableNotFoundError

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["manifest_load"].side_effect = TableNotFoundError("project.dataset.bogus_table")

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 2, f"TableNotFoundError must exit tier 2; got {code}"
    assert captured.err.startswith("ERROR: ")
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Explicit per-class call-outs: materialisation seam errors → tier 3
# (US-007 / DEC-008 of plans/super/22-temp-table-sample.md)
# ---------------------------------------------------------------------------


def test_materialisation_failed_error_maps_to_tier_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """:class:`MaterialisationFailedError` is tier 3 (external-dep) per
    DEC-008 of US-007. It wraps any SDK / network / quota failure during
    the per-run materialisation query (BigQuery CTAS / equivalent), so
    it slots alongside the other warehouse-connectivity / quota errors
    in the four-tier taxonomy.

    The parametrized loop above covers every entry in
    :data:`_EXCEPTION_TO_EXIT_CODE`; this non-parametrized test calls
    the contract out by name so a future tier-change diff is easy to
    read in code review (mirrors the precedent set by
    :func:`test_table_not_found_error_exits_tier_two`).
    """
    from signalforge.warehouse.errors import MaterialisationFailedError

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["manifest_load"].side_effect = MaterialisationFailedError(
        "synthetic materialise failure",
        cause=_SENTINEL_CAUSE,
    )

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 3, f"MaterialisationFailedError must exit tier 3; got {code}"
    assert captured.err.startswith("ERROR: ")
    assert "Traceback" not in captured.err


def test_materialisation_not_supported_error_maps_to_tier_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """:class:`MaterialisationNotSupportedError` is tier 3 (external-dep)
    per DEC-008 of US-007. The :class:`WarehouseAdapter` ABC default
    impl raises this when a concrete adapter has not overridden
    ``materialise_sample`` — for v0.1 only BigQuery overrides; v0.2
    Snowflake/Postgres adapters inherit the default raise until each
    grows its own override.

    Tier 3 (rather than tier 1) because the failure is "the active
    adapter cannot do what we asked of it" — an external-dep state we
    cannot recover by retrying or by fixing user input — not a
    configuration / load failure.
    """
    from signalforge.warehouse.errors import MaterialisationNotSupportedError

    project_dir = make_fake_dbt_project(tmp_path)
    monkeypatch.chdir(project_dir)
    mocks = _install_happy_patches(monkeypatch)

    mocks["manifest_load"].side_effect = MaterialisationNotSupportedError("SyntheticAdapter")

    code = main(["generate", "model.shop.customers"])
    captured = capsys.readouterr()

    assert code == 3, f"MaterialisationNotSupportedError must exit tier 3; got {code}"
    assert captured.err.startswith("ERROR: ")
    assert "Traceback" not in captured.err


def test_audit_completeness_scan_passes_for_new_errors() -> None:
    """The 7th AST scan
    (:func:`tests.test_audit_completeness.test_every_typed_error_is_in_exit_code_mapping_table`)
    walks every ``src/signalforge/*/errors.py`` and asserts every
    concrete ``class <Name>Error`` declaration appears in
    :data:`_EXCEPTION_TO_EXIT_CODE`. US-007 of issue #22 lands the two
    new materialisation-seam errors in lockstep with the mapping; this
    test pins the scan to **pass** specifically for both new classes so
    a future regression that drops one (or moves them to a different
    errors module without updating the mapping) breaks loud at the
    callout, not just inside the broader 7th-scan failure list.
    """
    from signalforge.cli._helpers import _EXCEPTION_TO_EXIT_CODE
    from signalforge.warehouse.errors import (
        MaterialisationFailedError,
        MaterialisationNotSupportedError,
    )

    assert MaterialisationFailedError in _EXCEPTION_TO_EXIT_CODE, (
        "MaterialisationFailedError missing from _EXCEPTION_TO_EXIT_CODE; "
        "the 7th AST scan would catch this, but the per-class assertion "
        "names the offending class up front."
    )
    assert MaterialisationNotSupportedError in _EXCEPTION_TO_EXIT_CODE, (
        "MaterialisationNotSupportedError missing from "
        "_EXCEPTION_TO_EXIT_CODE; same scan fail-loud as above."
    )
    assert _EXCEPTION_TO_EXIT_CODE[MaterialisationFailedError] == 3, (
        "MaterialisationFailedError must map to tier 3 (external-dep) per DEC-008 of US-007."
    )
    assert _EXCEPTION_TO_EXIT_CODE[MaterialisationNotSupportedError] == 3, (
        "MaterialisationNotSupportedError must map to tier 3 (external-dep) per DEC-008 of US-007."
    )


def test_estimate_unavailable_error_maps_to_tier_3() -> None:
    """``EstimateUnavailableError`` (issue #130 / DEC-003) is registered at
    tier 3 (external-dep) — the estimation seam ran but produced no usable
    figure, so it routes through ``--estimate``'s degrade path, not an
    input-shape failure. The explicit table entry is required by the 7th AST
    scan even though ``WarehouseError``'s fallback is also tier 3; this
    per-class assertion names the offending class up front if the entry is
    ever dropped. ``map_exception_to_exit_code`` resolves a constructed
    instance via the MRO walk to confirm the table entry is live."""
    from signalforge.cli._helpers import map_exception_to_exit_code
    from signalforge.warehouse import EstimateUnavailableError

    assert EstimateUnavailableError in _EXCEPTION_TO_EXIT_CODE, (
        "EstimateUnavailableError missing from _EXCEPTION_TO_EXIT_CODE; the "
        "7th AST scan would catch this, but the per-class assertion names "
        "the offending class up front."
    )
    assert _EXCEPTION_TO_EXIT_CODE[EstimateUnavailableError] == 3, (
        "EstimateUnavailableError must map to tier 3 (external-dep) per DEC-003 of #130."
    )
    assert map_exception_to_exit_code(EstimateUnavailableError(detail="no GlobalStats")) == 3
