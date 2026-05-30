#!/usr/bin/env python3
"""Maintainer-only audit-cost rollup wrapper.

Thin argparse entrypoint around
:func:`signalforge.llm.cost.rollup_audit_dir`. Established by US-003 of
``plans/super/157-e2e-cost-and-parallel.md`` so the maintainer can
re-measure the live e2e cost figure (the "~$0.30/full-suite run"
documented in CHANGELOG / runbook) from real audit JSONLs rather than
reasoning about it.

The script is NOT a ``signalforge`` subcommand — it does not register in
:mod:`signalforge.cli` and does not ship in the built wheel
(``tests/test_wheel_packaging.py`` gates that exclusion). It lives
under repo-root ``scripts/`` and is invoked via
``python scripts/measure_e2e_cost.py …``.

Usage::

    python scripts/measure_e2e_cost.py --project-dir /path/to/proj
    python scripts/measure_e2e_cost.py --project-dir /path/to/proj --format json
    python scripts/measure_e2e_cost.py --project-dir /path/to/proj --audit-dir .signalforge

Exit codes mirror the CLI taxonomy in ``.claude/rules/cli-layer.md``:

* ``0`` — success.
* ``2`` — any :class:`signalforge.llm.cost.CostError` subclass (input /
  state validation: audit dir missing, malformed JSONL, unknown model).
* ``1`` — any other unexpected ``Exception`` (panic-path equivalent).

The boundary ``try / except`` in :func:`main` is the single sink. No
``Traceback`` ever leaks to stderr — the no-traceback floor from
``cli-layer.md`` § "No traceback ever leaks" applies here too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from signalforge.llm.cost import (
    CostError,
    CostReport,
    ModelRollup,
    ProviderRollup,
    rollup_audit_dir,
)


def _model_to_jsonable(rollup: ModelRollup) -> dict[str, object]:
    """Convert one :class:`ModelRollup` to a plain JSON-serialisable dict.

    Walking the fields explicitly (instead of :func:`dataclasses.asdict`)
    sidesteps the fact that ``asdict`` invokes :func:`copy.deepcopy` on
    every container, and :class:`types.MappingProxyType` — used by the
    rollup shapes for read-only mappings — is not pickle / deepcopy
    safe.
    """
    return {
        "model": rollup.model,
        "input_tokens": rollup.input_tokens,
        "output_tokens": rollup.output_tokens,
        "cache_creation_input_tokens": rollup.cache_creation_input_tokens,
        "cache_read_input_tokens": rollup.cache_read_input_tokens,
        "total_usd": rollup.total_usd,
        "call_count": rollup.call_count,
    }


def _provider_to_jsonable(rollup: ProviderRollup) -> dict[str, object]:
    """Convert one :class:`ProviderRollup` to a JSON-serialisable dict.

    ``per_model`` is rebuilt as a plain dict keyed alphabetically so two
    runs over the same inputs produce byte-identical JSON (mirrors
    Architectural Commitment #5, "explainable diffs").
    """
    return {
        "provider": rollup.provider,
        "per_model": {
            model_id: _model_to_jsonable(rollup.per_model[model_id])
            for model_id in sorted(rollup.per_model)
        },
        "subtotal_usd": rollup.subtotal_usd,
    }


def _report_to_jsonable(report: CostReport) -> dict[str, object]:
    """Convert a :class:`CostReport` to a plain JSON-serialisable dict.

    Keys sorted at every level so two runs with the same inputs produce
    byte-identical JSON.
    """
    return {
        "per_provider": {
            provider_name: _provider_to_jsonable(report.per_provider[provider_name])
            for provider_name in sorted(report.per_provider)
        },
        "total_usd": report.total_usd,
        "pricing_table_version": report.pricing_table_version,
        "audit_files_consumed": list(report.audit_files_consumed),
    }


def _print_json(report: CostReport) -> None:
    """Emit the report as indented JSON on stdout."""
    json.dump(_report_to_jsonable(report), sys.stdout, indent=2)
    sys.stdout.write("\n")


def _print_text(report: CostReport) -> None:
    """Emit a human-readable per-provider per-model table on stdout.

    No external table library — plain ``print`` with column alignment.
    Layout:

    * One block per provider (alphabetical), header line +
      per-model rows + provider subtotal.
    * Trailing ``TOTAL: $X.XXXX (pricing table YYYY-MM-DD; audit
      files: ...)`` line.
    """
    header = (
        f"{'model':<40} {'calls':>6} {'input':>10} {'output':>10} "
        f"{'cache_w':>10} {'cache_r':>10} {'usd':>12}"
    )
    if not report.per_provider:
        # Edge case: no provider rolled up at all. Still emit the TOTAL
        # line so downstream tooling sees the canonical footer; the
        # rollup helper would have raised CostRollupAuditMissingError
        # before reaching here if BOTH JSONLs were absent, so this path
        # is reachable only if both files exist but contain no records.
        print("(no priced records found in the audit JSONLs)")
    for provider_name in sorted(report.per_provider):
        provider = report.per_provider[provider_name]
        print(f"\nprovider: {provider_name}")
        print(header)
        print("-" * len(header))
        for model_id in sorted(provider.per_model):
            m = provider.per_model[model_id]
            print(
                f"{m.model:<40} {m.call_count:>6} {m.input_tokens:>10} "
                f"{m.output_tokens:>10} {m.cache_creation_input_tokens:>10} "
                f"{m.cache_read_input_tokens:>10} ${m.total_usd:>11.4f}"
            )
        print(
            f"{'  subtotal':<40} {'':>6} {'':>10} {'':>10} {'':>10} {'':>10} "
            f"${provider.subtotal_usd:>11.4f}"
        )

    audit_list = ", ".join(report.audit_files_consumed)
    print(
        f"\nTOTAL: ${report.total_usd:.4f} "
        f"(pricing table {report.pricing_table_version}; audit files: {audit_list})"
    )


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse surface.

    Kept in a helper so tests can introspect / re-parse without invoking
    :func:`main` (mirrors the ``cli-layer.md`` pattern for the public
    CLI's ``add_parser`` helpers).
    """
    parser = argparse.ArgumentParser(
        prog="measure_e2e_cost.py",
        description=(
            "Roll up per-provider per-model USD cost from the SignalForge "
            "audit JSONLs under <project-dir>/<audit-dir>/."
        ),
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        required=True,
        help="Path to the SignalForge project root whose audit JSONLs will be rolled up.",
    )
    parser.add_argument(
        "--audit-dir",
        type=str,
        default=".signalforge",
        help=("Audit subdirectory name under --project-dir (default: %(default)s)."),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output shape (default: %(default)s).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code.

    The single boundary ``try / except`` is the only place errors are
    caught — no inner stage wraps its own ``except``. Mirrors
    ``cli-layer.md`` § "No traceback ever leaks": every routed exit
    code is one of ``{0, 1, 2}`` and stderr never carries a
    ``Traceback`` line on the failure paths.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        report = rollup_audit_dir(args.project_dir, audit_dir=args.audit_dir)
        if args.format == "json":
            _print_json(report)
        else:
            _print_text(report)
        return 0
    except CostError as exc:
        # ``LLMError.__str__`` renders ``message\n  ↳ Remediation: …``
        # so the operator sees a single, readable two-line message on
        # stderr without any traceback noise.
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001  — panic-path single sink
        # Tier-1 / panic-path equivalent of cli-layer.md § "No traceback
        # ever leaks". ``type(exc).__name__`` keeps the operator pointed
        # at the failing class without leaking a full traceback.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
