from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from .core import Adb, Backup, BackupError, Cancelled, Device, Inventory, Metadata, Report, checked_command, load_report


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
        self.setWindowTitle("Android-Medienbackup")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("backup.svg"))))
        self.resize(850, 650)
        self.cancel_event = threading.Event()
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.result_handler: Callable[[object], None] | None = None
        self.inventory: Inventory | None = None
        self.close_requested = False
        self.settings = QSettings("AndroidMediaBackup", "AndroidMediaBackup")
        self.started_at = 0.0
        self.last_feedback = 0.0
        self.activity_timer = QTimer(self)
        self.activity_timer.setInterval(1000)
        self.activity_timer.timeout.connect(self.update_activity)

        body = QWidget()
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        introduction = QLabel(
            "Additive USB-Sicherung: Fotos und Videos nach Jahren, andere Dateien nach Typ.\n"
            "Es werden keine Quelldateien und keine vorhandenen Backups gelöscht.\n"
            "Technische App-Daten, Cache und Installationsdateien werden ausgeschlossen.\n"
            "Auswahl: Android-Kategorien Bilder, Videos, Audio, Dokumente und Downloads.\n"
            "Ergänzt durch Download-Ordner; die Auswahl kann von Files by Google abweichen."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)

        device_row = QHBoxLayout()
        device_label = QLabel("&Gerät:")
        self.devices = QComboBox()
        self.devices.setMinimumWidth(300)
        self.devices.setPlaceholderText("Kein Gerät erkannt – USB-Debugging prüfen")
        device_label.setBuddy(self.devices)
        self.refresh = QPushButton("Geräte &suchen")
        self.refresh.clicked.connect(self.refresh_devices)
        self.devices.currentIndexChanged.connect(self.invalidate_inventory)
        device_row.addWidget(device_label)
        device_row.addWidget(self.devices, 1)
        device_row.addWidget(self.refresh)
        layout.addLayout(device_row)

        target_row = QHBoxLayout()
        target_label = QLabel("&Backup-Ziel:")
        self.target = QLineEdit()
        self.target.setPlaceholderText("Zielordner auf dem Linux-Rechner")
        self.target.setText(self.settings.value("backup_directory", "", type=str))
        target_label.setBuddy(self.target)
        self.target.textChanged.connect(self.update_preview)
        self.target.editingFinished.connect(self.save_target)
        self.browse = QPushButton("Ordner &wählen")
        self.browse.clicked.connect(self.choose_target)
        target_row.addWidget(target_label)
        target_row.addWidget(self.target, 1)
        target_row.addWidget(self.browse)
        layout.addLayout(target_row)

        self.full_verification = QCheckBox("Vollständige Integritätsprüfung (langsamer)")
        self.full_verification.setToolTip(
            "Prüft auch bekannte Dateien vollständig mit SHA-256 auf Smartphone und Ziel."
        )
        layout.addWidget(self.full_verification)
        mode_hint = QLabel(
            "Standard: Schnelllauf nach Größe und Änderungszeit. Gleiche Größe/Zeit kann "
            "Inhaltsänderungen oder Beschädigungen verbergen. Neue/geänderte Dateien werden vollständig geprüft."
        )
        mode_hint.setWordWrap(True)
        layout.addWidget(mode_hint)
        self.summary = QLabel("Gerät verbinden, USB-Debugging autorisieren und Geräte suchen.")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        actions = QHBoxLayout()
        self.scan = QPushButton("&Bestand einlesen")
        self.start = QPushButton("Sicherung s&tarten")
        self.abort = QPushButton("&Abbrechen")
        self.open_report = QPushButton("Bericht &öffnen")
        self.open_report.clicked.connect(self.choose_report)
        self.scan.clicked.connect(self.scan_device)
        self.start.clicked.connect(self.start_backup)
        self.abort.clicked.connect(self.cancel)
        for button in (self.scan, self.start, self.abort, self.open_report):
            actions.addWidget(button)
        layout.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.current = QLabel("")
        self.current.setWordWrap(True)
        self.current.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.current)
        self.activity = QLabel("")
        self.activity.setWordWrap(True)
        layout.addWidget(self.activity)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setAccessibleName("Protokoll und Abschlussbericht")
        layout.addWidget(self.log, 1)
        self.set_busy(False)
        QTimer.singleShot(0, self.refresh_devices)

    def set_busy(self, busy: bool) -> None:
        for widget in (self.devices, self.refresh, self.target, self.browse, self.full_verification):
            widget.setEnabled(not busy)
        self.abort.setEnabled(busy)
        self.open_report.setEnabled(not busy)
        selected = self.devices.currentData()
        authorized = selected is not None and selected.state == "device"
        self.scan.setEnabled(not busy and authorized)
        self.start.setEnabled(not busy and self.inventory is not None and bool(self.target.text().strip()))
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)

    def launch(self, action: Callable[[Worker], object], result: Callable[[object], None]) -> None:
        if self.thread is not None:
            return
        self.cancel_event.clear()
        self.set_busy(True)
        self.result_handler = result
        self.worker = Worker(action, self)
        self.thread = self.worker
        self.worker.result.connect(self.receive_result)
        self.worker.error.connect(self.show_error)
        self.worker.cancelled.connect(self.show_cancelled)
        self.worker.progress.connect(self.show_progress)
        self.worker.scan_progress.connect(self.show_scan_progress)
        self.thread.finished.connect(self.finished)
        self.started_at = self.last_feedback = time.monotonic()
        self.activity_timer.start()
        self.thread.start()
        self.update_activity()

    @Slot()
    def update_activity(self) -> None:
        if self.thread is None:
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
        thread = self.thread
        if thread is None:
            return
        # finished is emitted before native thread teardown; retain the wrapper until it has joined.
        thread.wait()
        self.activity_timer.stop()
        elapsed = int(time.monotonic() - self.started_at)
        self.activity.setText(f"Vorgang beendet · Laufzeit {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.thread = None
        self.worker = None
        self.result_handler = None
        thread.deleteLater()
        self.set_busy(False)
        self.update_preview()
        if self.close_requested:
            self.close()

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.log.appendPlainText("FEHLER: " + message)
        self.current.setText(message)
        if not self.close_requested:
            QMessageBox.warning(self, "Vorgang nicht abgeschlossen", message)

    @Slot()
    def show_cancelled(self) -> None:
        self.log.appendPlainText("Vorgang abgebrochen; keine vollständige Sicherung bestätigt.")
        self.current.setText("Abgebrochen")

    @Slot(int, int, str)
    def show_progress(self, done: int, total: int, path: str) -> None:
        self.last_feedback = time.monotonic()
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.current.setText(f"{done} / {total}: {path}")

    @Slot(int, object, str)
    def show_scan_progress(self, count: int, size: int, path: str) -> None:
        self.last_feedback = time.monotonic()
        self.progress.setRange(0, 0)
        self.current.setText(
            f"Bestandsaufnahme: {count:,} Dateien · {readable_size(size)} erfasst\n{path}"
        )
        self.update_activity()

    def refresh_devices(self) -> None:
        if self.thread is not None:
            return
        self.inventory = None
        self.devices.clear()

        def action(worker: Worker) -> object:
            missing = [tool for tool in ("adb", "exiftool") if not shutil.which(tool)]
            if missing:
                raise BackupError("Fehlende Programme: " + ", ".join(missing) + ". Siehe Installationsanleitung.")
            checked_command(["exiftool", "-ver"], self.cancel_event)
            return Adb.devices(self.cancel_event)

        def received(result: object) -> None:
            if not isinstance(result, list):
                raise TypeError("Expected device list")
            self.devices.clear()
            for device in result:
                if not isinstance(device, Device):
                    raise TypeError("Expected device")
                self.devices.addItem(f"{device.model} — {device.serial} [{device.state}]", device)
            if not self.devices.count():
                self.log.appendPlainText("Kein Gerät gefunden. USB-Verbindung und USB-Debugging prüfen.")
            elif not any(self.devices.itemData(i).state == "device" for i in range(self.devices.count())):
                self.log.appendPlainText(
                    "Kein autorisiertes Gerät. Smartphone entsperren, USB-Debugging bestätigen und erneut suchen."
                )
            else:
                for index in range(self.devices.count()):
                    if self.devices.itemData(index).state == "device":
                        self.devices.setCurrentIndex(index)
                        break

        self.launch(action, received)

    def invalidate_inventory(self) -> None:
        self.inventory = None
        if self.thread is None:
            self.set_busy(False)
        self.update_preview()

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
        if self.thread is None:
            self.set_busy(False)
        if self.inventory is None:
            self.summary.setText("Gerät auswählen und Bestand einlesen. Gesperrte Bereiche werden gemeldet.")
            return
        summary = (
            f"{len(self.inventory.files):,} Dateien, {readable_size(self.inventory.total_bytes)}\n"
            f"Speicher: {', '.join(self.inventory.roots)}\n"
            f"Hinweise aus Bestandsaufnahme: {len(self.inventory.issues)}\n"
            "Ausgeschlossen: Android/data, Android/obb, Cache und App-Installationsdateien\n"
            f"{self.inventory.selection}\n"
            f"Zusätzlich erkannte ausgeschlossene Dateien: {self.inventory.excluded_files}"
        )
        if self.target.text().strip():
            path = Path(self.target.text().strip()).expanduser().absolute()
            while not path.exists() and path != path.parent:
                path = path.parent
            try:
                free = shutil.disk_usage(path).free
                summary += f"\nFreier Zielspeicher: {readable_size(free)}"
                if free < self.inventory.total_bytes + 16 * 1024 * 1024:
                    summary += "\nHinweis: Für eine vollständige neue Kopie möglicherweise zu wenig Platz."
            except OSError as exc:
                summary += f"\nZielspeicher nicht prüfbar: {exc}"
        self.summary.setText(summary)

    def scan_device(self) -> None:
        device = self.devices.currentData()
        if device is None or device.state != "device":
            self.show_error("Bitte ein autorisiertes Gerät auswählen.")
            return
        self.inventory = None
        serial = device.serial
        self.current.setText("Dateibestand wird erfasst …")

        def received(result: object) -> None:
            if not isinstance(result, Inventory):
                raise TypeError("Expected inventory")
            self.inventory = result
            self.log.appendPlainText(f"Bestand: {len(result.files)} Dateien, {readable_size(result.total_bytes)}")
            self.log.appendPlainText(
                f"{result.selection} "
                f"{result.excluded_files} zusätzlich erkannte App-Dateien ausgeschlossen."
            )
            for issue in result.issues:
                self.log.appendPlainText("HINWEIS: " + issue)
            self.current.setText("Vorschau bereit. Die Dateien werden erst beim Start übertragen.")
            self.update_preview()

        self.launch(
            lambda worker: Adb(serial, self.cancel_event).scan(worker.scan_progress.emit), received
        )

    def start_backup(self) -> None:
        inventory = self.inventory
        if inventory is None or not self.target.text().strip():
            self.show_error("Bitte Bestand einlesen und einen Backup-Zielordner auswählen.")
            return
        target = Path(self.target.text().strip()).expanduser().resolve()
        full_verification = self.full_verification.isChecked()
        mode = "Vollständige SHA-256-Prüfung" if full_verification else "Schnelllauf nach Größe/Änderungszeit"
        if target == Path(target.anchor) or target == Path.home():
            self.show_error("Bitte einen eigenen Backup-Unterordner statt Wurzel- oder Home-Verzeichnis wählen.")
            return
        answer = QMessageBox.question(
            self, "Sicherung starten",
            f"{len(inventory.files)} Dateien nach {target} sichern?\n"
            f"Prüfmodus: {mode}\n"
            "Vorhandene Inhalte bleiben erhalten. Gesperrte Bereiche sind nicht Bestandteil der Sicherung.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.save_target()
        self.log.appendPlainText(f"Sicherung gestartet: {target}")

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

        self.launch(action, received)

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
        self.current.setText(report.outcome)
        self.log.appendPlainText(
            f"\n{report.outcome}\n"
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
            self.log.appendPlainText(report.selection)
        if report.verification == "quick":
            self.log.appendPlainText(
                "Schnelllauf: Übersprungene Dateien wurden nicht erneut gehasht. "
                "Gleiche Größe und Änderungszeit garantieren keine Inhaltsgleichheit."
            )
        if report.errors:
            self.log.appendPlainText("\nFEHLER – keine vollständige Sicherung:")
            for error in report.errors:
                self.log.appendPlainText(error)
        if report.empty_files:
            self.log.appendPlainText("\nLEERE QUELLDATEIEN – kopiert, aber ohne Dateiinhalt:")
            for name in report.empty_files:
                self.log.appendPlainText(name)
        if report.metadata_warnings:
            self.log.appendPlainText("\nMETADATENWARNUNGEN – Dateien erfolgreich gesichert:")
            for name, warnings in report.metadata_warnings.items():
                self.log.appendPlainText(name + "\n  " + "\n  ".join(warnings))

    def cancel(self) -> None:
        self.cancel_event.set()
        self.abort.setEnabled(False)
        self.current.setText("Abbruch angefordert; laufender Transfer wird gestoppt …")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.thread is not None:
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
    app.setApplicationName("Android-Medienbackup")
    app.setDesktopFileName("android-media-backup")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("backup.svg"))))
    window = Window()
    window.show()
    sys.exit(app.exec())
