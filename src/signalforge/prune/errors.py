"""Prune-layer typed exception hierarchy.

`PruneError` is the module base; every distinct failure mode (config load,
test compilation, executor budget exhaustion, audit write) is a subclass.
Each carries a class-level `default_remediation` and accepts a `remediation`
kwarg; the base `__str__` renders both message and `↳ Remediation:` line so
the CLI (#9) and diff renderer (#8) can pattern-match on type rather than
sniffing message text. Mirrors the manifest, warehouse, safety, and draft
layers' error-hierarchy conventions.

See plans/super/6-prune-engine.md for the full design.
"""

from __future__ import annotations
