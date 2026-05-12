"""Parity test between ``src/signalforge/_demo/`` and ``tests/fixtures/dbt_project_austin/``.

Implements DEC-008 of ``plans/super/47-init-demo.md``: the shipped demo tree
must stay byte-for-byte equal to the e2e-smoke fixture tree EXCEPT for two
documented rewrites:

1. ``profiles.yml`` — the shipped copy uses dbt's ``env_var('GOOGLE_CLOUD_PROJECT')``
   macro for the BigQuery project field and drops the maintainer-only
   "DO NOT signalforge against this" header (DEC-009).
2. ``.gitignore`` — the shipped copy is slimmed to a single ``.signalforge/``
   exclusion; the test-fixture copy keeps the issue-#10 / DEC-021 maintainer
   commentary.

Additional invariants:

* ``regenerate.sh`` lives only in the test fixture (maintainer-only;
  documented in DEC-015 — the shipped tree does not include it).
* The shipped ``_demo/`` tree contains zero symlinks (DEC-005).

The maintainer-only ``tests/fixtures/dbt_project_austin/regenerate.sh`` script
updates BOTH trees in lockstep so this parity gate fires only on uncommanded
drift.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "dbt_project_austin"
_DEMO_DIR = _REPO_ROOT / "src" / "signalforge" / "_demo"

# Files allowed to diverge between the two trees. Every other file in the demo
# tree MUST be byte-equal to its test-fixture counterpart. Adding to this list
# is a deliberate widening of the rewrite surface and MUST be accompanied by an
# updated DEC entry in plans/super/47-init-demo.md.
_ALLOWED_REWRITES = frozenset({"profiles.yml", ".gitignore"})

# Files allowed to live only in the test-fixture tree (maintainer-only artefacts
# that DO NOT ship in the wheel). The shipped demo intentionally omits these.
_TEST_FIXTURE_ONLY = frozenset({"regenerate.sh"})


def _relative_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_demo_fixture_parity_holds_byte_for_byte_except_documented_files() -> None:
    """The two trees must agree byte-for-byte except for the two named rewrites.

    Implements DEC-008. The exceptions documented inline are:
        - profiles.yml (DEC-009 — env_var('GOOGLE_CLOUD_PROJECT') swap)
        - .gitignore   (DEC-008 — slim out issue-#10 / DEC-021 references)
    """
    test_files = _relative_files(_TEST_FIXTURE_DIR)
    demo_files = _relative_files(_DEMO_DIR)

    # The shipped tree must contain every test-fixture file except the
    # maintainer-only set.
    expected_in_demo = test_files - _TEST_FIXTURE_ONLY
    missing_from_demo = expected_in_demo - demo_files
    assert not missing_from_demo, (
        f"shipped demo is missing files present in test fixture: {sorted(missing_from_demo)}"
    )

    # The shipped tree must NOT carry unexpected extras (anything not in the
    # test fixture). Net-new shipped files require an explicit DEC.
    unexpected_extras = demo_files - test_files
    assert not unexpected_extras, (
        f"shipped demo has files not in test fixture: {sorted(unexpected_extras)}"
    )

    # Maintainer-only files (regenerate.sh) must NOT ship in the demo tree.
    leaked_maintainer_files = demo_files & _TEST_FIXTURE_ONLY
    assert not leaked_maintainer_files, (
        f"maintainer-only files leaked into shipped demo: {sorted(leaked_maintainer_files)}"
    )

    # Every shared file must be byte-equal except for the two documented
    # rewrites. The rewrites are still asserted to BE DIFFERENT below so a
    # silent identical copy doesn't pass the parity gate while violating
    # DEC-009.
    drift: list[str] = []
    for rel in sorted(expected_in_demo):
        test_bytes = (_TEST_FIXTURE_DIR / rel).read_bytes()
        demo_bytes = (_DEMO_DIR / rel).read_bytes()
        if rel in _ALLOWED_REWRITES:
            if test_bytes == demo_bytes:
                drift.append(
                    f"{rel} is identical between trees but must be rewritten per DEC-008/DEC-009"
                )
        else:
            if test_bytes != demo_bytes:
                drift.append(f"{rel} differs between trees (uncommanded drift)")
    assert not drift, "parity drift detected:\n  - " + "\n  - ".join(drift)


def test_demo_fixture_contains_no_symlinks() -> None:
    """Codifies DEC-005: the shipped demo tree is symlink-free.

    Walks ``src/signalforge/_demo/`` and asserts ``not p.is_symlink()`` for
    every entry. Drift here (e.g. an accidental ``ln -s`` during a regen)
    breaks the test loudly.
    """
    assert _DEMO_DIR.is_dir(), f"demo tree missing at {_DEMO_DIR}"
    offending = [str(p.relative_to(_DEMO_DIR)) for p in _DEMO_DIR.rglob("*") if p.is_symlink()]
    assert not offending, f"shipped demo contains symlinks (DEC-005 violation): {sorted(offending)}"


def test_demo_profiles_yml_uses_env_var_macro() -> None:
    """Pins DEC-009: the shipped profile uses ``env_var('GOOGLE_CLOUD_PROJECT')``.

    A PyPI user with the env var set runs the demo with zero file edits.
    """
    profile_text = (_DEMO_DIR / "profiles.yml").read_text()
    assert "env_var('GOOGLE_CLOUD_PROJECT')" in profile_text, (
        "shipped profiles.yml must use env_var('GOOGLE_CLOUD_PROJECT') per DEC-009; "
        f"got:\n{profile_text}"
    )
    # The shipped copy must NOT carry the maintainer-only "DO NOT signalforge"
    # warning header — that header is specifically about the billing-broken
    # bigquery-public-data placeholder which the shipped copy replaces.
    assert "DO NOT" not in profile_text, (
        "shipped profiles.yml must not carry the maintainer-only DO NOT header"
    )
    # The shipped copy must NOT carry the broken billing placeholder.
    assert "project: bigquery-public-data" not in profile_text, (
        "shipped profiles.yml must not pin project: bigquery-public-data "
        "(it's the billing-broken placeholder; use env_var(...) instead)"
    )


def test_test_fixture_profiles_yml_retains_maintainer_header() -> None:
    """Confirms the DEC-009 rewrite is one-way.

    The test-fixture copy keeps the "do not signalforge against this" header
    plus the billing-broken ``bigquery-public-data`` placeholder so the e2e
    smoke test (issue #10) overwrites it in ``tmp_path`` with the operator's
    real billing project. The shipped copy does the opposite swap (DEC-009).
    """
    profile_text = (_TEST_FIXTURE_DIR / "profiles.yml").read_text()
    assert "project: bigquery-public-data" in profile_text, (
        "test-fixture profiles.yml must retain the bigquery-public-data "
        "placeholder so the e2e smoke test exercises the overwrite path; "
        f"got:\n{profile_text}"
    )
    # The maintainer-only warning header must remain in the test-fixture copy.
    assert "WRONG for query-time use" in profile_text, (
        "test-fixture profiles.yml must retain the maintainer-only warning "
        "header explaining why running signalforge directly against it fails"
    )
