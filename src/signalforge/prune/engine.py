"""Prune engine entry point and per-test executor.

Hosts `prune_tests(candidates, model, manifest, adapter, *, config=None)` —
the public seam that compiles every CandidateTest to failing-rows SQL via the
compiler module, runs each through `WarehouseAdapter.run_test_sql`, and folds
the per-test verdicts into a typed `PruneResult`. The executor enforces the
per-test runtime budget, the total-budget cap, and the conservative
"keep-on-error" default that routes warehouse exceptions to
`kept-without-evidence` rather than dropping silently.

See plans/super/6-prune-engine.md for the full design.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)
