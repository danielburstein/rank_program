"""Dark theme and status colours."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

BACKGROUND = "#1e1f24"
PANEL = "#26272e"
PANEL_ALT = "#2d2e36"
BORDER = "#3a3b45"
ACCENT = "#5b8def"
TEXT = "#e6e6e6"
TEXT_DIM = "#9a9ca6"
WARNING = "#e0a526"
ERROR = "#e05252"
PROVISIONAL = "#a66ff0"
CONFIRMED = "#4fc47a"
MANUAL = "#f08a3c"
CSV = "#2fb8b0"
INFO = "#5b8def"

STATUS_COLOURS = {
    "warning": WARNING,
    "error": ERROR,
    "provisional": PROVISIONAL,
    "confirmed": CONFIRMED,
    "waived": MANUAL,
    "manual": MANUAL,
    "manual_override": MANUAL,
    "manual_screenshot": MANUAL,
    "csv": CSV,
    "trusted_csv": CSV,
    "info": INFO,
    "auto": TEXT_DIM,
    "stale": WARNING,
    "resolved": TEXT_DIM,
    "accepted": CONFIRMED,
    "rejected": ERROR,
    "pending": WARNING,
    "needs_review": WARNING,
    "partial": WARNING,
    "unreadable": ERROR,
    "verified": CONFIRMED,
    "lookup_failed": ERROR,
    "ambiguous": ERROR,
    "unresolved": WARNING,
    "completed": CONFIRMED,
    "failed": ERROR,
    "running": ACCENT,
    "queued": TEXT_DIM,
    "cancelled": TEXT_DIM,
    "paused": WARNING,
}


def status_colour(kind: str | None) -> QColor:
    return QColor(STATUS_COLOURS.get((kind or "").lower(), TEXT))


def base_font() -> QFont:
    font = QFont()
    for family in ("Segoe UI", "Inter", "Noto Sans", "DejaVu Sans"):
        font.setFamily(family)
        if font.exactMatch():
            break
    font.setPointSize(10)
    return font


def mono_font() -> QFont:
    font = QFont()
    for family in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Monospace"):
        font.setFamily(family)
        if font.exactMatch():
            break
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(9)
    return font


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BACKGROUND))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL_ALT))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(PANEL_ALT))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL_ALT))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_DIM))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(TEXT_DIM))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_DIM))
    app.setPalette(palette)
    app.setFont(base_font())
    qss = Path(__file__).with_name("theme_dark.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))
