"""Smoke test: verifies signalforge installs and imports with a valid __version__."""

import re

import signalforge


def test_version_is_non_empty_string() -> None:
    assert isinstance(signalforge.__version__, str)
    assert signalforge.__version__ != ""


def test_version_matches_pep440_shape() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", signalforge.__version__)


def test_import_has_no_error_chain() -> None:
    # Re-import via importlib and assert the version matches the direct import
    # — sanity check that the package resolves to the same module object regardless
    # of import mechanism. (Editable installs go via .pth, not the wheel target;
    # wheel packaging is verified at build time, not here.)
    import importlib

    module = importlib.import_module("signalforge")
    assert module.__version__ == signalforge.__version__
