"""Candidate-test SQL compiler.

Translates each variant of the drafter's `CandidateTest` discriminated union
(`not_null`, `unique`, `accepted_values`, `relationships`) into a failing-rows
SELECT statement using `WarehouseAdapter.dialect()` for quote character and
identifier casing. Matches dbt-core's NULL-exclusion conventions verbatim so
prune verdicts agree with `dbt test` runtime verdicts. Quote-escapes
user-controlled values (notably `accepted_values.values`) before SQL
interpolation; trusts adapter-validated identifiers on `TableRef`.

See plans/super/6-prune-engine.md for the full design.
"""

from __future__ import annotations
