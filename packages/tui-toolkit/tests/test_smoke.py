"""Bootstrap smoke test — verifies the package can be imported."""

import importlib.metadata as metadata

import tui_toolkit


def test_package_imports():
    # Asserted against the installed metadata rather than a literal. The version
    # already lives in pyproject.toml and in `__init__.py`, and a third copy here
    # could only ever disagree with those two in silence.
    assert tui_toolkit.__version__ == metadata.version("tui-toolkit")


def test_version_comes_from_installed_distribution_metadata(monkeypatch):
    import importlib

    actual_version = metadata.version
    with monkeypatch.context() as patched:
        patched.setattr(
            metadata,
            "version",
            lambda name: "9.8.7" if name == "tui-toolkit" else actual_version(name),
        )
        try:
            assert importlib.reload(tui_toolkit).__version__ == "9.8.7"
        finally:
            patched.undo()
            importlib.reload(tui_toolkit)
