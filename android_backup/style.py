from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget


def apply_theme(widget: QWidget, scheme: Qt.ColorScheme) -> None:
    dark = scheme == Qt.ColorScheme.Dark
    background, surface, text, muted, border = (
        ("#111827", "#1c2535", "#f0f4fa", "#b5c1d4", "#3b4a61") if dark
        else ("#f2f5fa", "#ffffff", "#17243b", "#53647d", "#cbd5e3")
    )
    accent, hover = ("#739dff", "#94b4ff") if dark else ("#285ad6", "#1948bb")
    accent_text = "#101a30" if dark else "#ffffff"
    success, warning, error = (
        ("#8adbb7", "#ffda86", "#ffb1b9") if dark
        else ("#116541", "#805000", "#a52335")
    )
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, background),
        (QPalette.ColorRole.WindowText, text),
        (QPalette.ColorRole.Base, surface),
        (QPalette.ColorRole.AlternateBase, background),
        (QPalette.ColorRole.Text, text),
        (QPalette.ColorRole.Button, surface),
        (QPalette.ColorRole.ButtonText, text),
        (QPalette.ColorRole.Highlight, accent),
        (QPalette.ColorRole.HighlightedText, accent_text),
        (QPalette.ColorRole.ToolTipBase, surface),
        (QPalette.ColorRole.ToolTipText, text),
        (QPalette.ColorRole.PlaceholderText, muted),
    ):
        palette.setColor(role, QColor(color))
    widget.setPalette(palette)
    widget.setProperty("theme", "dark" if dark else "light")
    widget.setStyleSheet(f"""
        QMainWindow, QDialog, QScrollArea, QWidget#dashboard {{ background: {background}; }}
        QWidget {{ color: {text}; font-size: 14px; }}
        QFrame[card="true"] {{
            background: {surface}; border: 1px solid {border}; border-radius: 12px;
        }}
        QLabel {{ background: transparent; border: none; }}
        QLabel[role="title"] {{ font-size: 25px; font-weight: 700; }}
        QLabel[role="heading"] {{ font-size: 17px; font-weight: 600; }}
        QTextBrowser[role="filename"] {{ font-size: 17px; font-weight: 600; }}
        QLabel[role="muted"] {{ color: {muted}; }}
        QLabel[role="stat"] {{ font-size: 25px; font-weight: 700; }}
        QLabel[tone="success"] {{ color: {success}; }}
        QLabel[tone="warning"] {{ color: {warning}; }}
        QLabel[tone="error"] {{ color: {error}; }}
        QFrame#banner {{ border: 1px solid {border}; border-radius: 8px; }}
        QPushButton {{
            background: {surface}; border: 1px solid {border}; border-radius: 7px;
            padding: 9px 13px; font-weight: 600;
        }}
        QPushButton:hover {{ border-color: {accent}; }}
        QPushButton:pressed {{ background: {background}; }}
        QPushButton:focus, QToolButton:focus, QCheckBox:focus {{
            border: 2px solid {accent};
        }}
        QPushButton[variant="primary"] {{
            background: {accent}; color: {accent_text}; border-color: {accent};
        }}
        QPushButton[variant="primary"]:hover {{ background: {hover}; }}
        QPushButton[variant="danger"] {{ color: {error}; border-color: {error}; }}
        QPushButton:disabled {{
            background: {background}; color: {muted}; border-color: {border};
        }}
        QLineEdit, QComboBox, QPlainTextEdit, QTextBrowser {{
            background: {surface}; border: 1px solid {border}; border-radius: 6px;
            padding: 8px; selection-background-color: {accent};
            selection-color: {accent_text};
        }}
        QLineEdit:focus, QComboBox:focus {{ border: 2px solid {accent}; }}
        QComboBox QAbstractItemView {{
            background: {surface}; color: {text};
            selection-background-color: {accent}; selection-color: {accent_text};
        }}
        QProgressBar {{
            background: {background}; border: 1px solid {border};
            border-radius: 5px; min-height: 10px; max-height: 10px;
        }}
        QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
        QProgressBar[tone="warning"]::chunk {{ background: {warning}; }}
        QToolButton {{
            border: 1px solid transparent; border-radius: 5px;
            padding: 5px; text-align: left; color: {muted};
        }}
        QToolButton:hover {{ color: {accent}; }}
        QCheckBox {{ spacing: 8px; padding: 4px; }}
        QCheckBox::indicator {{ width: 18px; height: 18px; }}
        QScrollArea {{ border: none; }}
    """)
