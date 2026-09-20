"""Bootstrap smoke test — verifies the package can be imported."""

import importlib.metadata as metadata

import tui_toolkit


def test_package_imports():
    # Asserted against the installed metadata rather than a literal. The version
    # already lives in pyproject.toml and in `__init__.py`, and a third copy here
    # could only ever disagree with those two in silence.
    assert tui_toolkit.__version__ == metadata.version("tui-toolkit")
