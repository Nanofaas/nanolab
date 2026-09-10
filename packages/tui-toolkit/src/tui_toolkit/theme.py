"""Theme model and Rich → prompt_toolkit style adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace

import questionary


@dataclass(frozen=True, slots=True)
class Theme:
    """Style and icon tokens shared by the Rich chrome and the questionary pickers.

    Style fields hold Rich markup (e.g. ``"bold cyan"``) and are rewritten into
    prompt_toolkit selectors by :func:`to_questionary_style`. ``DEFAULT_THEME``
    carries the cyan palette; derive a variant with :meth:`with_overrides`.
    """

    # Rich-format style strings (e.g., "bold cyan", "cyan dim").
    accent: str = "cyan"
    accent_strong: str = "bold cyan"
    accent_dim: str = "cyan dim"
    success: str = "green"
    warning: str = "yellow"
    error: str = "red"
    muted: str = "dim"
    text: str = ""
    brand: str = "bold cyan"

    icon_running: str = "▸"
    icon_completed: str = "✓"
    icon_failed: str = "✗"
    icon_warning: str = "⚠"
    icon_skipped: str = "⊘"
    icon_updated: str = "↺"
    icon_cancelled: str = "⊘"

    def with_overrides(self, **changes) -> Theme:
        """Return a copy of this theme with the named tokens replaced.

        ``changes`` is forwarded to :func:`dataclasses.replace`, so the keys
        must be ``Theme`` field names; the receiver itself is left untouched.
        """
        return replace(self, **changes)


DEFAULT_THEME = Theme()


def _to_pt(rich_style: str) -> str:
    """Translate a Rich style string into prompt_toolkit format.

    Examples:
        "bold cyan"   → "fg:cyan bold"
        "cyan dim"    → "fg:cyan"   (dim as modifier is dropped)
        "dim"         → "fg:grey"   (legacy alias for muted text)
        ""            → ""

    """
    if not rich_style:
        return ""
    tokens = rich_style.split()
    fg: str | None = None
    flags: list[str] = []
    for token in tokens:
        low = token.lower()
        if low in {"bold", "italic", "underline"}:
            flags.append(low)
        elif low == "dim":
            # 'dim' alone → grey foreground (legacy muted mapping)
            # 'dim' as modifier (e.g., "cyan dim") → dropped
            if fg is None:
                fg = "grey"
        else:
            fg = low
    parts: list[str] = []
    if fg is not None:
        parts.append(f"fg:{fg}")
    parts.extend(flags)
    return " ".join(parts)


def to_questionary_style(theme: Theme) -> questionary.Style:
    """Map theme tokens into the 13 questionary/prompt_toolkit selectors."""
    return questionary.Style(
        [
            ("brand", _to_pt(theme.brand)),
            ("breadcrumb", _to_pt(theme.muted)),
            ("footer", _to_pt(theme.muted)),
            ("qmark", _to_pt(theme.accent_strong)),
            ("question", "bold"),
            ("answer", _to_pt(theme.accent_strong)),
            ("pointer", _to_pt(theme.accent_strong)),
            ("highlighted", _to_pt(theme.accent_strong)),
            ("selected", _to_pt(theme.accent)),
            ("text", _to_pt(theme.text)),
            ("disabled", _to_pt(theme.muted)),
            ("separator", _to_pt(theme.muted)),
            ("instruction", _to_pt(theme.muted)),
        ]
    )
