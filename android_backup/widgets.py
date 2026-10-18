from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget,
)


def set_role(widget: QWidget, name: str, value: str) -> None:
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def label(text: str = "", role: str = "") -> QLabel:
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    if role:
        result.setProperty("role", role)
    return result


class Card(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setProperty("card", True)
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(20, 16, 20, 16)
        self.content.setSpacing(10)
        self.content.addWidget(label(title, "heading"))


class StatTile(QWidget):
    def __init__(self, title: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 12, 4)
        self.value = label("–", "stat")
        layout.addWidget(self.value)
        layout.addWidget(label(title, "muted"))

    def set_value(self, value: str) -> None:
        self.value.setText(value)


class Banner(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("banner")
        layout = QHBoxLayout(self)
        self.message = label()
        self.dismiss = QToolButton()
        self.dismiss.setText("Schließen")
        self.dismiss.setAccessibleName("Mitteilung schließen")
        self.dismiss.clicked.connect(self.hide)
        layout.addWidget(self.message, 1)
        layout.addWidget(self.dismiss)
        self.hide()

    def show_message(self, text: str, tone: str) -> None:
        self.message.setText(text)
        set_role(self.message, "tone", tone)
        self.show()


class CollapsibleSection(QWidget):
    def __init__(self, title: str):
        super().__init__()
        self.title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.details.setAccessibleName(title)
        self.details.setMinimumHeight(130)
        self.details.setMaximumHeight(230)
        self.details.hide()
        self.toggle.toggled.connect(self.set_expanded)
        layout.addWidget(self.toggle)
        layout.addWidget(self.details)

    def set_expanded(self, expanded: bool) -> None:
        self.details.setVisible(expanded)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    def set_content(self, text: str, count: int) -> None:
        self.details.setPlainText(text)
        self.toggle.setText(f"{self.title} ({count:,})")
        self.toggle.setChecked(False)
        self.setVisible(bool(count))
