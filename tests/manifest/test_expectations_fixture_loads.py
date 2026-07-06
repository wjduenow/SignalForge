"""In-process smoke test for the #154 dbt-expectations compiled fixture (US-006).

Validates that the committed
``tests/fixtures/dbt_project_expectations/target/manifest.json`` loads cleanly
via :func:`signalforge.manifest.load` without any network access or environment
variables, and that its committed ``resource_type == "test"`` nodes carry
populated ``compiled_code`` — the property #154's manifest-test bridge reads.

Unlike every other committed manifest fixture (all ``dbt parse`` output, whose
test nodes are absent and whose ``compiled_code`` is ``null``), this fixture is
built via real ``dbt deps && dbt compile`` on dbt-duckdb + dbt-expectations (see
``tests/fixtures/regenerate.sh`` and DEC-017 of
``plans/super/154-dbt-expectations-prune.md``).

The six test nodes exercise all #154 / #267 prune outcomes (engineered through
the dbt-expectations macro ARGS or a singular test body, which ``dbt compile``
resolves statically):

* ``expect_column_values_to_be_between`` on ``amount`` with **impossible** bounds
  (every row 100/200 is outside [1000, 2000]) → returns failing rows → KEPT.
* ``expect_column_values_to_be_between`` on ``amount`` with **vacuous** bounds
  ([0, 1e6]) → zero failing rows → ALWAYS-PASSES drop.
* ``expect_column_values_to_not_be_null`` on the natural-NOT-NULL ``order_id``
  → zero failing rows → ALWAYS-PASSES drop.
* ``expect_table_row_count_to_be_between`` → row-returning ``validation_errors``
  wrapper → prunable candidate.
* ``expect_row_values_to_have_recent_data`` → compiled SQL references ``now()``
  → non-deterministic → SKIP.
* ``no_orders_above_threshold`` (#267 US-005) → a **singular** test with a single
  ``ref('orders')`` whose compiled body is a bare scalar ``SELECT count(*) …``
  → graduated to a ``from_manifest`` candidate by the count-of-rows path.

Tolerant of the sibling ``Manifest.tests`` surface (US-001, bead .1) not yet
being merged: the load-success + model checks always run, the ``Manifest.tests``
assertions run only when that attribute exists, and the committed-fixture-shape
assertions read the JSON directly so they hold either way.
"""

from __future__ import annotations

import json
from pathlib import Path

from signalforge.manifest import load
from signalforge.manifest.models import Manifest

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "dbt_project_expectations"
_MANIFEST_PATH = _FIXTURE_DIR / "target" / "manifest.json"

_EXPECTED_MACROS = {
    "expect_column_values_to_not_be_null",
    "expect_column_values_to_be_between",
    "expect_table_row_count_to_be_between",
    "expect_row_values_to_have_recent_data",
}


def test_expectations_manifest_loads_via_signalforge() -> None:
    """The committed dbt-expectations manifest loads and resolves ``orders``."""
    manifest = load(_FIXTURE_DIR)
    assert isinstance(manifest, Manifest)

    model = manifest.get_model("model.signalforge_test_expectations.orders")
    assert model.name == "orders"
    assert model.package_name == "signalforge_test_expectations"
    assert model.original_file_path == "models/orders.sql"
    assert set(model.columns.keys()) == {"order_id", "amount"}

    # If the sibling US-001 `Manifest.tests` surface has merged, the six test
    # nodes (five dbt-expectations + the #267-US-005 scalar-count singular test)
    # must surface with populated `compiled_code`. Otherwise this fixture still
    # exercises the load path (assertions above) and the committed-shape guard
    # below carries the real-SQL signal.
    tests = getattr(manifest, "tests", None)
    if tests is not None:
        assert len(tests) == 6
        for node in tests.values():
            compiled = getattr(node, "compiled_code", None)
            assert compiled is not None and compiled.strip()


def test_expectations_fixture_carries_compiled_test_nodes() -> None:
    """The committed manifest itself has six ``resource_type == "test"`` nodes —
    five dbt-expectations generic tests (covering the four #154 macros) plus the
    #267-US-005 scalar-count singular test — each with non-null ``compiled_code``.

    This reads the JSON directly (independent of the ``Manifest.tests`` surface)
    so it guards the fixture from silent corruption — a ``dbt parse`` re-run, a
    stale commit, or a scrub that dropped ``compiled_code`` would fail here.
    """
    raw = json.loads(_MANIFEST_PATH.read_text())
    assert raw["metadata"]["dbt_schema_version"].endswith("v12.json")

    test_nodes = [node for node in raw["nodes"].values() if node.get("resource_type") == "test"]
    # Five dbt-expectations generic tests + one #267-US-005 scalar-count singular
    # test (`tests/no_orders_above_threshold.sql`, no `test_metadata`).
    assert len(test_nodes) == 6

    generic_nodes = [node for node in test_nodes if node.get("test_metadata")]
    singular_nodes = [node for node in test_nodes if not node.get("test_metadata")]
    assert len(generic_nodes) == 5
    assert len(singular_nodes) == 1

    macros_seen: set[str] = set()
    for node in generic_nodes:
        compiled = node.get("compiled_code")
        assert compiled is not None and compiled.strip(), (
            f"test node {node.get('unique_id')} has empty compiled_code — "
            "the fixture must be built via `dbt compile`, not `dbt parse`"
        )
        meta = node.get("test_metadata") or {}
        assert meta.get("namespace") == "dbt_expectations"
        macros_seen.add(meta.get("name", ""))
        # Every dbt-expectations test attaches to the single `orders` model.
        assert node.get("attached_node") == ("model.signalforge_test_expectations.orders")

    assert macros_seen == _EXPECTED_MACROS

    # The #267-US-005 singular test: bare scalar-count `compiled_code`, no
    # `attached_node` (singular tests aren't attached), and a single `orders`
    # model dependency that `associate_test_model` resolves via `depends_on`.
    singular = singular_nodes[0]
    assert singular.get("unique_id") == (
        "test.signalforge_test_expectations.no_orders_above_threshold"
    )
    singular_compiled = singular.get("compiled_code") or ""
    assert singular_compiled.strip(), (
        "singular test node has empty compiled_code — the fixture must be built "
        "via `dbt compile`, not `dbt parse`"
    )
    assert "count(*)" in singular_compiled.lower()
    assert singular.get("attached_node") is None
    assert singular.get("depends_on", {}).get("nodes") == [
        "model.signalforge_test_expectations.orders"
    ]

    # The non-deterministic recent-data body must carry a `now()` reference —
    # this is the signal #154's determinism filter routes to a skip.
    recent = next(
        node
        for node in test_nodes
        if (node.get("test_metadata") or {}).get("name") == "expect_row_values_to_have_recent_data"
    )
    assert "now()" in (recent.get("compiled_code") or "")
