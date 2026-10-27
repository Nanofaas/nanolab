"""Rich Console singleton with a content-width cap."""

from __future__ import annotations

from rich.console import Console

# The widest content any screen renders, whatever the terminal offers.
MAX_CONTENT_COLS = 140

console = Console(highlight=False)

_content_width: int = MAX_CONTENT_COLS


def get_content_width() -> int:
    """Active content width (after _apply_width) or the bare cap."""
    return _content_width


def _apply_width(width: int) -> None:
    """Set the Rich Console width and the cached helper width."""
    global _content_width
    _content_width = width
    console.width = width
