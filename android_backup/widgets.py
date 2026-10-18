from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from .core import Report


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


class ConfirmationDialog(QDialog):
    def __init__(
        self, parent: QWidget, title: str, details: list[tuple[str, str]],
        notice: str, action: str, default_accept: bool = True,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowIcon(parent.windowIcon())
        self.setModal(True)
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        layout.addWidget(label(title, "title"))
        card = Card("Auf einen Blick")
        for name, value in details:
            card.content.addWidget(label(name, "muted"))
            value_label = label(value)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            card.content.addWidget(value_label)
        layout.addWidget(card)
        layout.addWidget(label(notice, "muted"))
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton("&Abbrechen")
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button = QPushButton(action)
        self.confirm_button.setProperty("variant", "primary" if default_accept else "danger")
        self.confirm_button.clicked.connect(self.accept)
        self.cancel_button.setAutoDefault(False)
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.setDefault(default_accept)
        self.cancel_button.setDefault(not default_accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)
        (self.confirm_button if default_accept else self.cancel_button).setFocus()


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


class InfoDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Über die Sicherung")
        self.setWindowIcon(parent.windowIcon())
        self.resize(600, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        layout.addWidget(label("Über die Sicherung", "title"))
        layout.addWidget(label("Fotos, Videos und Dateien direkt auf Ihrem Rechner sichern.", "muted"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("dashboard")
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 0, 8, 0)
        content.setSpacing(14)
        for title, text in (
            ("Sichern ohne Löschen",
             "Additive USB-Sicherung: Fotos und Videos werden nach Jahren abgelegt, andere Dateien nach Typ. "
             "Quelldateien und vorhandene Backups werden nicht gelöscht."),
            ("Umfang & Grenzen",
             "Die Auswahl nutzt Android-Kategorien und Download-Ordner und kann von Files by Google abweichen. "
             "Technische App-Daten, Cache und Installationsdateien sind ausgeschlossen. "
             "Gesperrte Bereiche werden gemeldet und sind nicht Bestandteil der Sicherung."),
            ("Integrität prüfen",
             "Im Schnelllauf werden bekannte Dateien nach Größe und Änderungszeit verglichen. "
             "Gleiche Werte garantieren keine Inhaltsgleichheit. Neue und geänderte Dateien werden vollständig geprüft. "
             "Für eine erneute Inhaltsprüfung auch bekannter Dateien die vollständige Integritätsprüfung wählen."),
        ):
            card = Card(title)
            card.content.addWidget(label(text))
            content.addWidget(card)
        content.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.close_button = QPushButton("&Schließen")
        self.close_button.setProperty("variant", "primary")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignRight)
        self.close_button.setFocus()


class ProblemsDialog(QDialog):
    def __init__(self, parent: QWidget, report: Report):
        super().__init__(parent)
        self.setWindowTitle("Probleme der Sicherung")
        self.setWindowIcon(parent.windowIcon())
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        layout.addWidget(label("Probleme der Sicherung", "title"))
        layout.addWidget(label(report.outcome, "heading"))
        layout.addWidget(label(
            "Alle Probleme sind nach Art gruppiert. Zu jeder Meldung sehen Sie "
            "die betroffene Datei oder den Bereich und die Auswirkung auf Ihre Sicherung.", "muted"
        ))
        self.problems = QTextBrowser()
        self.problems.setAccessibleName("Probleme und Auswirkungen der Sicherung")
        self.problems.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.problems.setOpenLinks(False)
        layout.addWidget(self.problems, 1)
        sections: list[str] = []

        def group(title: str, impact: str, entries: list[tuple[str, str, str]]) -> None:
            sections.append(f"<h2>{escape(title)}</h2><p>{escape(impact)}</p>")
            for message, path, effect in entries:
                sections.append(
                    f"<p><b>Meldung:</b> {escape(message)}<br>"
                    f"<b>Datei / Bereich:</b> {escape(path)}<br>"
                    f"<b>Auswirkung:</b> {escape(effect)}</p>"
                )

        if report.status in ("cancelled", "failed", "running"):
            group(
                {"cancelled": "Sicherung abgebrochen", "failed": "Sicherung gestoppt",
                 "running": "Sicherung noch nicht abgeschlossen"}[report.status],
                "Eine vollständige Sicherung ist nicht bestätigt. Bereits gesicherte Dateien bleiben erhalten.", [],
            )
        if report.errors or report.failed:
            group(
                f"Fehler bei Erfassung / Sicherung – Meldungen: {len(report.errors)}",
                f"Fehlgeschlagene Dateien: {report.failed}. Nicht lesbare Bereiche können in der Auswahl fehlen.",
                [(message, "Siehe Meldung", error_impact(message))
                 for message in report.errors],
            )
        remaining = max(0, report.total - report.saved - report.existing - report.failed)
        if remaining:
            group(f"Nicht bearbeitete Dateien ({remaining})",
                  "Diese Dateien wurden in diesem Lauf nicht bearbeitet. Erneute Sicherung erforderlich.", [])
        if report.empty_files:
            group(
                f"Leere Quelldateien ({len(report.empty_files)})",
                "Schon auf dem Smartphone ohne Inhalt – die Kopie kann keine Nutzdaten wiederherstellen.",
                [("Quelldatei hat 0 Byte", path, "Kein nutzbarer Dateiinhalt vorhanden. Original auf dem Smartphone prüfen.")
                 for path in report.empty_files],
            )
        if report.metadata_warnings:
            entries = []
            for path, warnings in report.metadata_warnings.items():
                for message in warnings:
                    entries.append((message, path, metadata_impact(message)))
            group(f"Metadatenwarnungen ({len(entries)})",
                  "Kein fehlgeschlagener Dateitransfer. Details je Meldung beachten.", entries)
        if not sections:
            sections.append("<p>" + escape(
                "Keine Probleme gemeldet. Das gilt nur für den erfassten, zugänglichen Umfang. "
                "Im Schnelllauf übersprungene Dateien wurden nicht erneut auf Inhaltsgleichheit geprüft."
            ) + "</p>")
        self.problems.setHtml("".join(sections))
        close = QPushButton("&Schließen")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


def error_impact(message: str) -> str:
    if "Zu wenig freier Speicherplatz" in message or "No space left on device" in message:
        return "Für diese Datei fehlt eine bestätigte Sicherung. Am Ziel Platz schaffen und erneut sichern."
    if "Vorhandenes Backup fehlt oder ist beschädigt" in message:
        return "Eine frühere Kopie fehlt oder ist beschädigt. Sie wurde nicht überschrieben; Backup-Ziel prüfen."
    if "Bestandsaufnahme" in message or "Android-Indexeintrag" in message:
        return "Die Auswahl kann Dateien dieses Bereichs übersehen haben. Zugang prüfen und Bestand erneut einlesen."
    if "Permission denied" in message:
        return "Zugriff verweigert. Betroffene Dateien oder Bereiche sind nicht vollständig gesichert. Berechtigungen prüfen."
    if "Quelldatei" in message and "verändert" in message:
        return "Die Datei änderte sich während der Prüfung. Keine bestätigte Kopie; Smartphone ruhen lassen und erneut sichern."
    return (
        "Für die genannte Datei oder den genannten Bereich ist keine erfolgreiche Sicherung bestätigt. "
        "Ursache anhand der Meldung prüfen und erneut sichern."
    )


def metadata_impact(message: str) -> str:
    if message.startswith("Ungültiges Aufnahmedatum"):
        return (
            "Datei erfolgreich gesichert. Dieses Datum wird nicht zur Jahresablage verwendet. "
            "Ein anderes gültiges Aufnahmedatum, sonst das Änderungsdatum oder „Unbekannt“ bestimmt den Ordner."
        )
    if message.startswith("Kein verlässlich bestimmbares Jahr"):
        return "Datei erfolgreich gesichert, aber kein brauchbares Jahr gefunden. Ablage im Jahresordner „Unbekannt“."
    if "ICC_Profile" in message:
        return "Datei erfolgreich gesichert. Das Farbprofil ist auffällig; Farben können je nach Anzeigeprogramm abweichen."
    if "Invalid EXIF text encoding" in message:
        return (
            "Datei erfolgreich gesichert. EXIF-Text ist nicht korrekt codiert; "
            "z. B. Kommentare können unlesbar sein. Die Meldung bestätigt keinen Verlust des Bildinhalts."
        )
    return (
        "Datei erfolgreich gesichert. Die Metadaten sind auffällig; "
        "die genaue Auswirkung auf Anzeige oder Weiterverarbeitung ist aus dieser Meldung nicht sicher ableitbar."
    )


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
