"""AppBrand — application identity passed through UIContext."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppBrand:
    """Application identity the TUI renders: name, logo art and default chrome text.

    Consumed from the active ``UIContext.brand`` by the startup banner and the
    full-screen pickers, which fall back to ``default_breadcrumb`` and
    ``default_footer_hint`` when a screen supplies neither.
    """

    name: str = "App"
    wordmark: str = ""
    ascii_logo: str = ""
    default_breadcrumb: str = "Main"
    default_footer_hint: str = "Esc back | Ctrl+C exit"


DEFAULT_BRAND = AppBrand()
