"""Rich Console singleton with a content-width cap."""

from __future__ import annotations

from rich.console import Console

# The widest content any screen renders, whatever the terminal offers.
MAX_CONTENT_COLS = 140

console = Console(highlight=False)


def get_content_width() -> int:
    """Return the active UI context's resolved width or its uninitialized cap."""
    # The context configures this Console; defer its import until initialization ends.
    from tui_toolkit.context import get_ui

    ui = get_ui()
    return ui.content_width if ui.content_width is not None else ui.max_content_cols
