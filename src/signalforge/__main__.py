"""``python -m signalforge`` entry point.

Mirrors the ``[project.scripts] signalforge = "signalforge.cli:main"`` console
script, but reachable via ``python -m signalforge`` — the PATH-independent form
:mod:`signalforge.airflow.runner` uses for its ``invocation="subprocess"`` mode
(``[sys.executable, "-m", "signalforge", ...]``), which always resolves as long
as the package is importable, regardless of whether the ``signalforge`` script
is on ``PATH`` (e.g. inside an isolated Airflow worker venv).

This is the *top-level package* ``__main__`` — distinct from the
``signalforge.cli`` subpackage, which deliberately ships no ``__main__.py`` (its
``main`` is called in-process by tests; see ``.claude/rules/cli-layer.md``).
"""

from __future__ import annotations

import sys

from signalforge.cli import main

if __name__ == "__main__":  # pragma: no cover - exercised via subprocess only
    sys.exit(main())
