from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QSettings, QSignalBlocker, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QResizeEvent
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QStyle,
    QVBoxLayout, QWidget,
)

from .core import Adb, Backup, BackupError, Cancelled, Device, Inventory, Metadata, Report, checked_command, load_report
from .style import apply_theme
from .widgets import Banner, Card, CollapsibleSection, StatTile, label, set_role


def readable_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class Worker(QThread):
    result = Signal(object)
    error = Signal(str)
    cancelled = Signal()
    progress = Signal(int, int, str)
    scan_progress = Signal(int, object, str)

    def __init__(self, action: Callable[[Worker], object], parent: QObject):
        super().__init__(parent)
        self.action = action

    @Slot()
    def run(self) -> None:
        try:
            self.result.emit(self.action(self))
        except Cancelled:
            self.cancelled.emit()
        except (BackupError, OSError, sqlite3.Error) as exc:
            self.error.emit(str(exc))


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setup_layout = QHBoxLayout()
        self.setWindowTitle("Android-Medienbackup")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("backup.svg"))))
        self.resize(1000, 860)
        self.setMinimumSize(760, 600)
        self.cancel_event = threading.Event()
        self.worker: Worker | None = None
        self.result_handler: Callable[[object], None] | None = None
        self.inventory: Inventory | None = None
        self.close_requested = False
        self.settings = QSettings("AndroidMediaBackup", "AndroidMediaBackup")
        self.started_at = 0.0
        self.last_feedback = 0.0
        self.phase = ""
        self.pending_scan = False
        self.report_path = ""
        self.activity_timer = QTimer(self)
        self.activity_timer.setInterval(1000)
        self.activity_timer.timeout.connect(self.update_activity)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.dashboard_scroll = scroll
        self.setCentralWidget(scroll)
        body = QWidget()
        body.setObjectName("dashboard")
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(self.windowIcon().pixmap(48, 48))
        header.addWidget(icon)
        titles = QVBoxLayout()
        titles.addWidget(label("Android-Medienbackup", "title"))
        titles.addWidget(label("Lokal sichern. Vorhandenes behalten.", "muted"))
        header.addLayout(titles, 1)
        self.open_report = QPushButton("Bericht &öffnen")
        self.open_report.clicked.connect(self.choose_report)
        header.addWidget(self.open_report)
        info = QPushButton("&Info")
        info.clicked.connect(self.show_info)
        header.addWidget(info)
        layout.addLayout(header)
        self.banner = Banner()
        layout.addWidget(self.banner)

        setup = self.setup_layout
        setup.setSpacing(14)
        device_card = Card("1 · Smartphone verbinden")
        self.device_status = label("Geräte werden gesucht …")
        device_card.content.addWidget(self.device_status)
        device_label = QLabel("&Gerät:")
        self.devices = QComboBox()
        self.devices.setMinimumContentsLength(12)
        self.devices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.devices.setPlaceholderText("Kein Gerät erkannt – USB-Debugging prüfen")
        device_label.setBuddy(self.devices)
        self.refresh = QPushButton("Geräte &suchen")
        self.refresh.setIcon(self.theme_icon("view-refresh", QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh.clicked.connect(self.refresh_devices)
        self.devices.currentIndexChanged.connect(self.invalidate_inventory)
        device_card.content.addWidget(device_label)
        device_row = QHBoxLayout()
        device_row.addWidget(self.devices, 1)
        device_row.addWidget(self.refresh)
        device_card.content.addLayout(device_row)
        self.device_hint = label(
            "USB-Datenkabel anschließen, Smartphone entsperren und USB-Debugging erlauben.", "muted"
        )
        device_card.content.addWidget(self.device_hint)
        device_card.content.addStretch()
        setup.addWidget(device_card, 1)

        target_card = Card("2 · Ziel auswählen")
        target_card.content.addWidget(label("Eigener Backup-Ordner auf diesem Rechner", "muted"))
        target_label = QLabel("&Backup-Ziel:")
        self.target = QLineEdit()
        self.target.setPlaceholderText("Zielordner auf dem Linux-Rechner")
        self.target.setText(str(self.settings.value("backup_directory", "", type=str)))
        target_label.setBuddy(self.target)
        self.target.textChanged.connect(self.update_preview)
        self.target.editingFinished.connect(self.save_target)
        self.browse = QPushButton("Ordner &wählen")
        self.browse.setIcon(self.theme_icon("folder-open", QStyle.StandardPixmap.SP_DirOpenIcon))
        self.browse.clicked.connect(self.choose_target)
        target_card.content.addWidget(target_label)
        target_row = QHBoxLayout()
        target_row.addWidget(self.target, 1)
        target_row.addWidget(self.browse)
        target_card.content.addLayout(target_row)
        self.space_hint = label("Ziel wählen, um den freien Speicherplatz zu prüfen.", "muted")
        self.space_bar = QProgressBar()
        self.space_bar.setTextVisible(False)
        self.space_bar.setAccessibleName("Platzbedarf einer vollständigen neuen Kopie")
        self.space_bar.hide()
        target_card.content.addWidget(self.space_hint)
        target_card.content.addWidget(self.space_bar)
        target_card.content.addStretch()
        setup.addWidget(target_card, 1)
        layout.addLayout(setup)

        preview = Card("3 · Vorschau & Sicherung")
        stats = QHBoxLayout()
        self.file_stat = StatTile("Dateien erfasst")
        self.size_stat = StatTile("Datenmenge")
        self.issue_stat = StatTile("Hinweise zur Erfassung")
        for tile in (self.file_stat, self.size_stat, self.issue_stat):
            stats.addWidget(tile, 1)
        preview.content.addLayout(stats)
        self.summary = label("Bestand wird nach der Geräteauswahl automatisch eingelesen.", "muted")
        preview.content.addWidget(self.summary)
        self.scan_issues = CollapsibleSection("Hinweise zur Bestandsaufnahme")
        self.scan_issues.hide()
        preview.content.addWidget(self.scan_issues)
        exclusions = label("Keine Löschungen · App-Daten, Cache und Installationsdateien ausgeschlossen", "muted")
        exclusions.setToolTip(
            "Bilder, Videos, Audio, Dokumente und Downloads aus dem Android-Medienindex "
            "plus Download-Ordner. Die Auswahl kann von Files by Google abweichen. "
            "Android/data und Android/obb sind ausgeschlossen."
        )
        preview.content.addWidget(exclusions)
        self.full_verification = QCheckBox("Vollständige Integritätsprüfung (langsamer)")
        self.full_verification.setToolTip(
            "Prüft auch bekannte Dateien vollständig mit SHA-256 auf Smartphone und Ziel. "
            "Der Schnelllauf vergleicht bei bekannten Dateien nur Größe und Änderungszeit."
        )
        preview.content.addWidget(self.full_verification)
        mode_hint = label(
            "Standard: Schnelllauf nach Größe und Änderungszeit. Neue/geänderte Dateien werden "
            "vollständig geprüft. Gleiche Größe/Zeit garantiert keine Inhaltsgleichheit.", "muted"
        )
        preview.content.addWidget(mode_hint)
        actions = QHBoxLayout()
        self.scan = QPushButton("Bestand &neu einlesen")
        self.start = QPushButton("Sicherung s&tarten")
        self.start.setMinimumWidth(210)
        self.start.setProperty("variant", "primary")
        self.abort = self.start
        self.scan.clicked.connect(self.scan_device)
        self.start.clicked.connect(self.primary_action)
        actions.addWidget(self.scan)
        actions.addStretch()
        actions.addWidget(self.start)
        preview.content.addLayout(actions)
        layout.addWidget(preview)

        self.progress_card = Card("Aktueller Vorgang")
        self.phase_label = label("", "heading")
        self.progress_card.content.addWidget(self.phase_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Fortschritt des aktuellen Vorgangs")
        self.progress_card.content.addWidget(self.progress)
        self.current = label()
        self.progress_card.content.addWidget(self.current)
        self.activity = label("", "muted")
        self.progress_card.content.addWidget(self.activity)
        self.progress_card.hide()
        layout.addWidget(self.progress_card)

        self.result_card = Card("Ergebnis")
        self.result_status = label("", "heading")
        self.result_card.content.addWidget(self.result_status)
        self.result_context = label("", "muted")
        self.result_card.content.addWidget(self.result_context)
        result_stats = QHBoxLayout()
        self.result_stats = {}
        for key, title in (
            ("saved", "Neu gesichert"), ("existing", "Bereits vorhanden"),
            ("failed", "Fehlgeschlagen"), ("unprocessed", "Nicht bearbeitet"),
        ):
            tile = StatTile(title)
            self.result_stats[key] = tile
            result_stats.addWidget(tile, 1)
        self.result_card.content.addLayout(result_stats)
        self.report_summary = label("", "muted")
        self.result_card.content.addWidget(self.report_summary)
        self.report_errors = CollapsibleSection("Fehler – Sicherung nicht vollständig")
        self.report_empty = CollapsibleSection("Leere Quelldateien (bereits 0 Byte)")
        self.report_warnings = CollapsibleSection("Metadatenwarnungen (erfolgreich gesichert)")
        for section in (self.report_errors, self.report_empty, self.report_warnings):
            self.result_card.content.addWidget(section)
        self.report_folder = QPushButton("Berichtsordner anzeigen")
        self.report_folder.clicked.connect(self.open_report_folder)
        self.result_card.content.addWidget(self.report_folder)
        self.result_card.hide()
        layout.addWidget(self.result_card)
        self.log_section = CollapsibleSection("Technisches Protokoll")
        self.log = self.log_section.details
        self.log.setAccessibleName("Protokoll und Abschlussbericht")
        copy_log = QPushButton("Protokoll &kopieren")
        copy_log.clicked.connect(self.copy_log)
        log_header = QHBoxLayout()
        log_header.addWidget(self.log_section.toggle, 1)
        log_header.addStretch()
        log_header.addWidget(copy_log)
        layout.addLayout(log_header)
        layout.addWidget(self.log_section)
        layout.addStretch()
        hints = QApplication.styleHints()
        hints.colorSchemeChanged.connect(self.update_theme)
        self.update_theme(hints.colorScheme())
        self.set_busy(False)
        self.update_preview()
        QTimer.singleShot(0, self.refresh_devices)

    def resizeEvent(self, event: QResizeEvent) -> None:
        self.setup_layout.setDirection(
            QBoxLayout.Direction.TopToBottom if event.size().width() < 900
            else QBoxLayout.Direction.LeftToRight
        )
        super().resizeEvent(event)

    def theme_icon(self, name: str, fallback: QStyle.StandardPixmap) -> QIcon:
        return QIcon.fromTheme(name, self.style().standardIcon(fallback))

    @Slot(Qt.ColorScheme)
    def update_theme(self, scheme: Qt.ColorScheme) -> None:
        apply_theme(self, scheme)

    def append_log(self, message: str) -> None:
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {message}")

    def copy_log(self) -> None:
        QApplication.clipboard().setText(self.log.toPlainText())

    def show_info(self) -> None:
        QMessageBox.information(
            self, "Über die Sicherung",
            "Additive USB-Sicherung: Fotos und Videos nach Jahren, andere Dateien nach Typ.\n\n"
            "Quelldateien und vorhandene Backups werden nicht gelöscht. "
            "Technische App-Daten, Cache und Installationsdateien sind ausgeschlossen.\n\n"
            "Die Auswahl nutzt Android-Kategorien und Download-Ordner und kann von Files by Google abweichen. "
            "Gesperrte Bereiche werden gemeldet und sind nicht Bestandteil der Sicherung.\n\n"
            "Schnelllauf: Bekannte Dateien werden nach Größe/Änderungszeit verglichen. "
            "Für eine erneute Inhaltsprüfung die vollständige Integritätsprüfung wählen."
        )

    def open_report_folder(self) -> None:
        if self.report_path:
            folder = Path(self.report_path).expanduser().absolute().parent
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                self.show_error(f"Berichtsordner konnte nicht geöffnet werden: {folder}")

    def primary_action(self) -> None:
        if self.worker is not None:
            self.cancel()
        else:
            self.start_backup()

    def set_busy(self, busy: bool) -> None:
        for widget in (self.devices, self.refresh, self.target, self.browse, self.full_verification):
            widget.setEnabled(not busy)
        self.open_report.setEnabled(not busy)
        selected = self.devices.currentData()
        authorized = selected is not None and selected.state == "device"
        self.scan.setEnabled(not busy and authorized)
        self.start.setText("&Abbrechen" if busy else "Sicherung s&tarten")
        set_role(self.start, "variant", "danger" if busy else "primary")
        self.start.setEnabled(
            (busy and not self.cancel_event.is_set())
            or (not busy and selected is not None and authorized and self.inventory is not None
                and self.inventory.serial == selected.serial and bool(self.target.text().strip()))
        )
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)

    def launch(
        self, action: Callable[[Worker], object], result: Callable[[object], None],
        phase: str = "Vorgang läuft",
    ) -> None:
        if self.worker is not None:
            return
        self.cancel_event.clear()
        self.phase = phase
        self.phase_label.setText(phase)
        self.progress_card.show()
        self.banner.hide()
        self.progress.setValue(0)
        self.set_busy(True)
        self.result_handler = result
        self.worker = Worker(action, self)
        self.worker.result.connect(self.receive_result)
        self.worker.error.connect(self.show_error)
        self.worker.cancelled.connect(self.show_cancelled)
        self.worker.progress.connect(self.show_progress)
        self.worker.scan_progress.connect(self.show_scan_progress)
        self.worker.finished.connect(self.finished)
        self.started_at = self.last_feedback = time.monotonic()
        self.activity_timer.start()
        self.worker.start()
        self.update_activity()

    @Slot()
    def update_activity(self) -> None:
        if self.worker is None:
            return
        now = time.monotonic()
        elapsed = int(now - self.started_at)
        quiet = int(now - self.last_feedback)
        state = "Abbruch angefordert" if self.cancel_event.is_set() else "Vorgang läuft"
        feedback = f"Seit {quiet} s keine neue Rückmeldung" if quiet >= 10 else f"Letzte Rückmeldung vor {quiet} s"
        self.activity.setText(
            f"{state} · Laufzeit {elapsed // 60:02d}:{elapsed % 60:02d} · {feedback}"
        )

    @Slot(object)
    def receive_result(self, result: object) -> None:
        if self.result_handler is not None:
            self.result_handler(result)

    @Slot()
    def finished(self) -> None:
        thread = self.worker
        if thread is None:
            return
        # finished is emitted before native thread teardown; retain the wrapper until it has joined.
        thread.wait()
        self.activity_timer.stop()
        elapsed = int(time.monotonic() - self.started_at)
        self.activity.setText(f"Vorgang beendet · Laufzeit {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.progress_card.hide()
        scan_next = self.pending_scan and not self.cancel_event.is_set()
        self.pending_scan = False
        self.worker = None
        self.result_handler = None
        thread.deleteLater()
        self.set_busy(False)
        self.update_preview()
        if self.close_requested:
            self.pending_scan = False
            self.close()
        elif scan_next:
            self.scan_device()

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.pending_scan = False
        self.append_log("FEHLER: " + message)
        self.banner.show_message("Fehler: " + message, "error")
        self.current.setText(message)
        if self.worker is not None and self.phase == "Geräte suchen":
            self.device_status.setText("Gerätesuche fehlgeschlagen")
            set_role(self.device_status, "tone", "error")
            self.device_hint.setText("Fehlermeldung prüfen und anschließend erneut Geräte suchen.")

    @Slot()
    def show_cancelled(self) -> None:
        self.pending_scan = False
        self.append_log("Vorgang abgebrochen; keine vollständige Sicherung bestätigt.")
        self.banner.show_message(
            "Abgebrochen. Dieser Vorgang bestätigt keine vollständige Sicherung.", "warning"
        )
        self.current.setText("Abgebrochen")
        if self.phase == "Geräte suchen":
            self.update_device_status()

    @Slot(int, int, str)
    def show_progress(self, done: int, total: int, path: str) -> None:
        self.last_feedback = time.monotonic()
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.current.setText(f"{done:,} / {total:,} Dateien · {Path(path).name[:100]}")
        self.current.setToolTip(path)
        self.progress.setAccessibleDescription(f"{done} von {total} Dateien")

    @Slot(int, object, str)
    def show_scan_progress(self, count: int, size: int, path: str) -> None:
        self.last_feedback = time.monotonic()
        self.progress.setRange(0, 0)
        self.current.setText(
            f"Bestandsaufnahme: {count:,} Dateien · {readable_size(size)} erfasst\n{Path(path).name[:100]}"
        )
        self.current.setToolTip(path)
        self.file_stat.set_value(f"{count:,}")
        self.size_stat.set_value(readable_size(size))
        self.update_activity()

    def refresh_devices(self) -> None:
        if self.worker is not None:
            return
        self.pending_scan = False
        self.inventory = None
        with QSignalBlocker(self.devices):
            self.devices.clear()
        self.device_status.setText("Geräte werden gesucht …")
        set_role(self.device_status, "tone", "")
        self.device_hint.setText("Bitte warten. Anschließend wird der Bestand automatisch eingelesen.")
        self.update_preview()

        def action(worker: Worker) -> object:
            missing = [tool for tool in ("adb", "exiftool") if not shutil.which(tool)]
            if missing:
                raise BackupError("Fehlende Programme: " + ", ".join(missing) + ". Siehe Installationsanleitung.")
            checked_command(["exiftool", "-ver"], self.cancel_event)
            return Adb.devices(self.cancel_event)

        def received(result: object) -> None:
            if not isinstance(result, list):
                raise TypeError("Expected device list")
            with QSignalBlocker(self.devices):
                self.devices.clear()
                for device in result:
                    if not isinstance(device, Device):
                        raise TypeError("Expected device")
                    self.devices.addItem(f"{device.model} — {device.serial} [{device.state}]", device)
                for index in range(self.devices.count()):
                    if self.devices.itemData(index).state == "device":
                        self.devices.setCurrentIndex(index)
                        break
            if not self.devices.count():
                self.append_log("Kein Gerät gefunden. USB-Verbindung und USB-Debugging prüfen.")
            elif not any(self.devices.itemData(i).state == "device" for i in range(self.devices.count())):
                self.append_log(
                    "Kein autorisiertes Gerät. Smartphone entsperren, USB-Debugging bestätigen und erneut suchen."
                )
            self.update_device_status()
            device = self.devices.currentData()
            self.pending_scan = device is not None and device.state == "device"

        self.launch(action, received, "Geräte suchen")

    def invalidate_inventory(self) -> None:
        self.inventory = None
        self.update_device_status()
        self.update_preview()
        if self.worker is None:
            device = self.devices.currentData()
            if device is not None and device.state == "device":
                self.scan_device()

    def update_device_status(self) -> None:
        device = self.devices.currentData()
        if device is None:
            text, tone = "Kein Gerät erkannt", "warning"
            hint = "USB-Datenkabel anschließen, USB-Debugging aktivieren und Geräte suchen."
        elif device.state == "device":
            text, tone = f"Verbunden · {device.model}", "success"
            hint = f"Seriennummer: {device.serial} · Der Bestand wird automatisch eingelesen."
        elif device.state == "unauthorized":
            text, tone = "Freigabe auf dem Smartphone nötig", "warning"
            hint = "Smartphone entsperren, USB-Debugging bestätigen und erneut Geräte suchen."
        else:
            text, tone = f"Gerät nicht bereit · {device.state}", "warning"
            hint = "USB-Verbindung prüfen und erneut Geräte suchen."
        self.device_status.setText(text)
        set_role(self.device_status, "tone", tone)
        self.device_hint.setText(hint)

    def choose_target(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Backup-Ziel auswählen", self.target.text())
        if folder:
            self.target.setText(folder)
            self.save_target()

    @Slot()
    def save_target(self) -> None:
        value = self.target.text().strip()
        self.settings.setValue(
            "backup_directory", str(Path(value).expanduser().absolute()) if value else ""
        )
        self.settings.sync()
        if self.settings.status() != self.settings.Status.NoError:
            self.show_error("Der zuletzt gewählte Backup-Ordner konnte nicht gespeichert werden.")

    def update_preview(self) -> None:
        if self.worker is None:
            self.set_busy(False)
        self.update_space()
        if self.inventory is None:
            self.file_stat.set_value("–")
            self.size_stat.set_value("–")
            self.issue_stat.set_value("–")
            self.scan_issues.hide()
            self.summary.setText(
                "Noch keine Vorschau. Bestand wird bei Geräteauswahl automatisch eingelesen. "
                "Zum Wiederholen „Bestand neu einlesen“ wählen."
            )
            return
        self.file_stat.set_value(f"{len(self.inventory.files):,}")
        self.size_stat.set_value(readable_size(self.inventory.total_bytes))
        self.issue_stat.set_value(str(len(self.inventory.issues)))
        self.scan_issues.set_content("\n".join(self.inventory.issues), len(self.inventory.issues))
        summary = (
            f"{len(self.inventory.files):,} Dateien, {readable_size(self.inventory.total_bytes)}\n"
            f"Speicher: {', '.join(self.inventory.roots)}\n"
            f"{self.inventory.selection}\n"
            f"Zusätzlich erkannte ausgeschlossene Dateien: {self.inventory.excluded_files}"
        )
        self.summary.setText(summary)

    def update_space(self) -> None:
        self.space_bar.hide()
        set_role(self.space_hint, "tone", "")
        if not self.target.text().strip():
            self.space_hint.setText("Ziel wählen, um den freien Speicherplatz zu prüfen.")
            return
        path = Path(self.target.text().strip()).expanduser().absolute()
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            free = shutil.disk_usage(path).free
            text = f"{readable_size(free)} frei"
            if self.inventory is not None:
                needed = self.inventory.total_bytes + 16 * 1024 * 1024
                text += f" · Neue Kopie: {readable_size(self.inventory.total_bytes)} + 16 MiB Reserve"
                self.space_bar.setRange(0, 100)
                self.space_bar.setValue(min(100, round(100 * needed / max(1, free))))
                self.space_bar.show()
                low = free < needed
                set_role(self.space_bar, "tone", "warning" if low else "")
                if low:
                    text += "\nMöglicherweise zu wenig Platz für eine vollständige neue Kopie."
                    set_role(self.space_hint, "tone", "warning")
            self.space_hint.setText(text)
        except OSError as exc:
            self.space_hint.setText(f"Zielspeicher nicht prüfbar: {exc}")
            set_role(self.space_hint, "tone", "error")

    def scan_device(self) -> None:
        if self.worker is not None:
            return
        self.pending_scan = False
        device = self.devices.currentData()
        if device is None or device.state != "device":
            self.show_error("Bitte ein autorisiertes Gerät auswählen.")
            return
        self.inventory = None
        self.update_preview()
        serial = device.serial
        self.current.setText("Dateibestand wird erfasst …")

        def received(result: object) -> None:
            if not isinstance(result, Inventory):
                raise TypeError("Expected inventory")
            selected = self.devices.currentData()
            if selected is None or selected.serial != result.serial:
                self.show_error("Bestandsaufnahme gehört nicht zum ausgewählten Gerät. Bitte erneut einlesen.")
                return
            self.inventory = result
            self.append_log(f"Bestand: {len(result.files)} Dateien, {readable_size(result.total_bytes)}")
            self.append_log(
                f"{result.selection} "
                f"{result.excluded_files} zusätzlich erkannte App-Dateien ausgeschlossen."
            )
            if result.issues:
                self.append_log("\n".join("HINWEIS: " + issue for issue in result.issues))
                self.banner.show_message(
                    "Bestand mit Hinweisen erfasst. Gesperrte oder nicht lesbare Bereiche fehlen; "
                    "bitte die Hinweise zur Bestandsaufnahme prüfen.", "warning"
                )
            self.current.setText("Vorschau bereit. Die Dateien werden erst beim Start übertragen.")
            self.update_preview()

        self.launch(
            lambda worker: Adb(serial, self.cancel_event).scan(worker.scan_progress.emit),
            received, "Bestand einlesen",
        )

    def start_backup(self) -> None:
        if self.worker is not None:
            return
        inventory = self.inventory
        if inventory is None or not self.target.text().strip():
            self.show_error("Bitte Bestand einlesen und einen Backup-Zielordner auswählen.")
            return
        device = self.devices.currentData()
        if device is None or device.state != "device" or inventory.serial != device.serial:
            self.show_error("Bestandsaufnahme passt nicht zum ausgewählten Gerät. Bitte erneut einlesen.")
            return
        target = Path(self.target.text().strip()).expanduser().resolve()
        full_verification = self.full_verification.isChecked()
        mode = "Vollständige SHA-256-Prüfung" if full_verification else "Schnelllauf nach Größe/Änderungszeit"
        if target == Path(target.anchor) or target == Path.home():
            self.show_error("Bitte einen eigenen Backup-Unterordner statt Wurzel- oder Home-Verzeichnis wählen.")
            return
        answer = QMessageBox.question(
            self, "Sicherung starten",
            f"{len(inventory.files):,} Dateien · {readable_size(inventory.total_bytes)}\n"
            f"Ziel: {target}\n"
            f"Prüfmodus: {mode}\n"
            f"Hinweise aus der Erfassung: {len(inventory.issues)}\n\n"
            "Vorhandene Inhalte bleiben erhalten. Gesperrte Bereiche sind nicht Bestandteil der Sicherung.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.append_log(f"Sicherung gestartet: {target}")
        self.result_card.hide()

        def action(worker: Worker) -> object:
            return Backup(
                Adb(inventory.serial, self.cancel_event), Metadata(self.cancel_event),
                self.cancel_event, worker.progress.emit,
                full_verification=full_verification,
            ).run(inventory, target)

        def received(result: object) -> None:
            if not isinstance(result, Report):
                raise TypeError("Expected report")
            self.show_report(result)
            # A fresh scan is required for the next run, including newly created phone files.
            self.inventory = None
            self.pending_scan = result.status not in ("cancelled", "failed")
            if not self.isActiveWindow():
                QApplication.alert(self)

        self.launch(action, received, "Dateien sichern & prüfen")
        self.save_target()

    def choose_report(self) -> None:
        directory = str(Path(self.target.text()).expanduser() / ".backup") if self.target.text() else ""
        filename, _ = QFileDialog.getOpenFileName(self, "Sicherungsbericht öffnen", directory, "Berichte (*.json)")
        if filename:
            try:
                self.show_report(load_report(Path(filename)))
            except BackupError as exc:
                self.show_error(str(exc))

    def show_report(self, report: Report) -> None:
        messages = sum(len(items) for items in report.metadata_warnings.values())
        unprocessed = max(0, report.total - report.saved - report.existing - report.failed)
        tone = (
            "error" if report.status in ("failed", "running") or report.failed or report.errors
            else "warning" if report.status == "cancelled" or report.metadata_warnings or report.empty_files
            else "success"
        )
        outcome = (
            "Sicherung läuft – noch kein Abschlussbericht"
            if report.status == "running" else report.outcome
        )
        self.result_status.setText(outcome)
        set_role(self.result_status, "tone", tone)
        self.result_context.setText(
            f"Gerät: {report.serial}\n"
            f"Bericht: {report.report_path or 'Kein Berichtspfad verfügbar'}"
        )
        for key, value in (
            ("saved", report.saved), ("existing", report.existing),
            ("failed", report.failed), ("unprocessed", unprocessed),
        ):
            self.result_stats[key].set_value(f"{value:,}")
        mode = "Vollständige SHA-256-Prüfung" if report.verification == "full" else "Schnelllauf"
        self.report_summary.setText(
            f"{mode} · Ohne erneute Inhaltsprüfung übersprungen: {report.quick_skipped}\n"
            f"Ausgeschlossene Dateien: {report.excluded_files}\n"
            f"Metadatenwarnungen: {messages} Meldungen bei {len(report.metadata_warnings)} Dateien\n"
            + (
                "Schnelllauf: Gleiche Größe und Änderungszeit garantieren keine Inhaltsgleichheit.\n"
                if report.verification == "quick" else ""
            )
            + report.selection
        )
        self.report_errors.set_content("\n".join(report.errors), len(report.errors))
        self.report_empty.set_content("\n".join(report.empty_files), len(report.empty_files))
        self.report_warnings.set_content(
            "Metadatenwarnungen bedeuten keinen fehlgeschlagenen Dateitransfer.\n"
            "Ungültige Aufnahmedaten: Jahresordner nach Datei-Änderungsdatum, sonst Unbekannt.\n\n"
            + "\n\n".join(
                name + "\n  " + "\n  ".join(warnings)
                for name, warnings in report.metadata_warnings.items()
            ), messages
        )
        self.report_path = report.report_path
        self.report_folder.setEnabled(bool(self.report_path))
        self.result_card.show()
        QTimer.singleShot(0, lambda: self.dashboard_scroll.ensureWidgetVisible(self.result_card))
        self.current.setText(outcome)
        self.append_log(
            f"\n{outcome}\n"
            f"Neu gesichert: {report.saved}; bereits vorhanden: {report.existing}; "
            f"fehlgeschlagen: {report.failed}; "
            f"nicht bearbeitet: {max(0, report.total - report.saved - report.existing - report.failed)}\n"
            f"Fehler bei Erfassung/Sicherung: {len(report.errors)}\n"
            f"Prüfmodus: {'vollständige SHA-256-Prüfung' if report.verification == 'full' else 'Schnelllauf'}\n"
            f"Ohne erneute Inhaltsprüfung übersprungen: {report.quick_skipped}\n"
            f"Metadatenwarnungen: {messages} Meldungen bei {len(report.metadata_warnings)} Dateien\n"
            f"Leere Quelldateien: {len(report.empty_files)} – bereits auf dem Smartphone 0 Byte\n"
            "Metadatenwarnungen bedeuten keinen fehlgeschlagenen Dateitransfer.\n"
            "Ungültige Aufnahmedaten: Jahresordner nach Datei-Änderungsdatum, sonst Unbekannt.\n"
            f"Ausgeschlossene Dateien: {report.excluded_files}\nBericht: {report.report_path}"
        )
        if report.selection:
            self.append_log(report.selection)
        if report.verification == "quick":
            self.append_log(
                "Schnelllauf: Übersprungene Dateien wurden nicht erneut gehasht. "
                "Gleiche Größe und Änderungszeit garantieren keine Inhaltsgleichheit."
            )
        if report.errors:
            self.append_log("\nFEHLER – keine vollständige Sicherung:\n" + "\n".join(report.errors))
        if report.empty_files:
            self.append_log(
                "\nLEERE QUELLDATEIEN – kopiert, aber ohne Dateiinhalt:\n" + "\n".join(report.empty_files)
            )
        if report.metadata_warnings:
            self.append_log(
                "\nMETADATENWARNUNGEN – Dateien erfolgreich gesichert:\n"
                + "\n".join(
                    name + "\n  " + "\n  ".join(warnings)
                    for name, warnings in report.metadata_warnings.items()
                )
            )

    def cancel(self) -> None:
        self.pending_scan = False
        self.cancel_event.set()
        self.abort.setEnabled(False)
        self.current.setText("Abbruch angefordert; laufender Transfer wird gestoppt …")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker is not None:
            answer = QMessageBox.question(
                self, "Vorgang läuft", "Vorgang abbrechen und nach sicherem Abschluss schließen?"
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.close_requested = True
                self.cancel()
            event.ignore()
        else:
            self.save_target()
            event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Android-Medienbackup")
    app.setDesktopFileName("android-media-backup")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("backup.svg"))))
    window = Window()
    window.show()
    sys.exit(app.exec())
