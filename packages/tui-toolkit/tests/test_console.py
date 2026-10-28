"""Tests for tui_toolkit.console — Rich Console singleton + width helpers."""

from __future__ import annotations

from rich.console import Console

from tui_toolkit.console import console, get_content_width


def test_console_is_a_rich_console():
    assert isinstance(console, Console)


def test_get_content_width_default_is_max_cap():
    width = get_content_width()
    assert width == 140


def test_init_ui_updates_console_and_content_width(monkeypatch):
    import os

    from tui_toolkit.context import UIContext, bind_ui, init_ui

    with bind_ui(UIContext()):
        monkeypatch.setattr(
            "shutil.get_terminal_size", lambda *args: os.terminal_size((100, 24))
        )
        try:
            init_ui()
            assert get_content_width() == console.width == 100
        finally:
            console.width = None
