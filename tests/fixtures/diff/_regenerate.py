"""Regenerate the diff renderer snapshot fixtures (US-011 of #8).

Invoked by :file:`tests/fixtures/diff/regenerate.sh`. Walks every entry
in :data:`tests.diff._snapshot_inputs.CASES`, renders the report, and
writes the result to :file:`tests/fixtures/diff/<filename>` byte-for-byte.

Idempotent — re-running the script produces identical bytes (the
:class:`signalforge.diff.models.DiffReport` builders are deterministic;
the renderers are pure functions of the report).

Why a Python helper rather than inline shell:

* The fixture content depends on the in-tree :mod:`signalforge.diff`
  source — running the renderer is the only way to keep fixtures and
  source in lockstep when DEC-007/008/013/021 evolve.
* The recipe (which renderer, which kwargs) lives alongside the
  builders in :mod:`tests.diff._snapshot_inputs`; the shell script
  only orchestrates; the heavy lifting is here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the test helpers importable when the script is run from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tests.diff._snapshot_inputs import CASES, render_for_case  # noqa: E402

_FIXTURES_DIR = Path(__file__).resolve().parent


def regenerate_all() -> None:
    """Render and write every fixture in :data:`CASES`.

    One ``write_text`` per case; encoding is UTF-8 with no BOM. The
    output filename is taken from the recipe; the directory is the
    fixtures dir adjacent to this script.
    """
    for name, (_builder, recipe) in CASES.items():
        filename = recipe["filename"]
        rendered = render_for_case(name)
        out_path = _FIXTURES_DIR / filename
        out_path.write_text(rendered, encoding="utf-8")
        print(f"  wrote {out_path.name} ({len(rendered)} chars)", file=sys.stderr)


if __name__ == "__main__":
    regenerate_all()
