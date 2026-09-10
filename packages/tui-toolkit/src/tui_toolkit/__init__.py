"""tui-toolkit — terminal UI widgets with unified theming.

Workflow event types and reporting helpers live in sonata_engine.
"""

from __future__ import annotations

__version__ = "0.1.0"

# theming + setup
import tui_toolkit.console as console
from tui_toolkit.brand import DEFAULT_BRAND, AppBrand

# rendering primitives
from tui_toolkit.chrome import render_screen_frame
from tui_toolkit.console import get_content_width
from tui_toolkit.context import UIContext, bind_ui, get_ui, init_ui

# pickers
from tui_toolkit.pickers import Choice, Separator, multiselect, select
from tui_toolkit.theme import DEFAULT_THEME, Theme

# startup banner
from tui_toolkit.workflow import header

# Sorted (RUF022); the grouping comments live with the imports above.
__all__ = [
    "DEFAULT_BRAND",
    "DEFAULT_THEME",
    "AppBrand",
    "Choice",
    "Separator",
    "Theme",
    "UIContext",
    "__version__",
    "bind_ui",
    "console",
    "get_content_width",
    "get_ui",
    "header",
    "init_ui",
    "multiselect",
    "render_screen_frame",
    "select",
]
