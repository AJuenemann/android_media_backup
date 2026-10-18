import os
import json
import sys
import tempfile
import time
import traceback
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QThread, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QScrollArea

from android_backup.core import BackupError, Cancelled, Device, Inventory, Report
from android_backup.gui import Window
from android_backup.widgets import Card, ConfirmationDialog, InfoDialog, ProblemsDialog, error_impact, metadata_impact
from tests.test_core import FakeAdb, FakeMetadata


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")

    def setUp(self):
        self.qt_errors = []
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        self.addCleanup(self.assert_no_qt_errors)

        def capture_qt_error(exc_type, value, tb):
            self.qt_errors.append("".join(traceback.format_exception(exc_type, value, tb)))

        sys.excepthook = capture_qt_error
        self.settings_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.settings_directory.cleanup)
        self.settings_path = str(Path(self.settings_directory.name) / "settings.ini")
        self.patches = [
            patch("android_backup.gui.QSettings", side_effect=lambda *args: QSettings(
                self.settings_path, QSettings.Format.IniFormat
            )),
            patch("android_backup.gui.shutil.which", return_value="/usr/bin/tool"),
            patch("android_backup.gui.checked_command", return_value=b"13.50\n"),
            patch("android_backup.gui.Adb.devices", return_value=[Device("phone", "device", "Test Phone")]),
            patch("android_backup.gui.Adb.scan", autospec=True, side_effect=lambda adb, progress: Inventory(
                adb.serial, ["/storage/emulated/0"], []
            )),
        ]
        for item in self.patches[:-1]:
            item.start()
            self.addCleanup(item.stop)
        self.scan_mock = self.patches[-1].start()
        self.addCleanup(self.patches[-1].stop)
        self.window = Window()
        self.addCleanup(self.cleanup_window)
        self.window.show()
        self.wait(lambda: self.window.devices.count() == 1 and self.window.worker is None)
        self.assertTrue(self.window.device_timer.isActive())
        self.window.device_timer.stop()

    def assert_no_qt_errors(self):
        self.assertFalse(self.qt_errors, "Unhandled Qt callback exceptions:\n" + "\n".join(self.qt_errors))

    def cleanup_window(self):
        if self.window.worker is not None:
            self.window.cancel()
            self.wait(lambda: self.window.worker is None)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while not predicate():
            self.app.processEvents()
            if time.monotonic() > deadline:
                self.fail("GUI operation did not finish")
            time.sleep(0.005)
        self.app.processEvents()

    def test_scan_backup_and_repeat_end_to_end(self):
        files = {
            "/storage/emulated/0/DCIM/a.jpg": b"photo",
            "/storage/emulated/0/Download/copy.jpg": b"photo",
            "/storage/emulated/0/Download/document.pdf": b"pdf",
        }
        fake = FakeAdb(files, self.window.cancel_event)
        inventory = Inventory("phone", ["/storage/emulated/0"], [fake.stat(p) for p in files])
        with tempfile.TemporaryDirectory() as folder, patch(
            "android_backup.gui.Adb", return_value=fake
        ), patch.object(fake, "scan", return_value=inventory), patch(
            "android_backup.gui.Metadata", return_value=FakeMetadata(self.window.cancel_event)
        ), patch("android_backup.gui.ConfirmationDialog.exec", return_value=QDialog.DialogCode.Accepted):
            self.window.target.setText(folder)
            self.window.scan_device()
            self.wait(lambda: self.window.worker is None)
            self.assertIn("3 Dateien", self.window.summary.text())
            self.assertTrue(self.window.start.isEnabled())
            self.window.start_backup()
            self.wait(lambda: self.window.worker is None)
            self.assertTrue((Path(folder) / "Fotos/2023/a.jpg").is_file())
            self.assertIn("Neu gesichert: 2; bereits vorhanden: 1", self.window.log.toPlainText())
            self.assertIn("Vollständig im erfassten", self.window.result_status.text())
            self.assertFalse(self.window.start.isEnabled())
            self.assertIsNone(self.window.inventory)
            self.window.scan.click()
            self.wait(lambda: self.window.worker is None)
            self.assertTrue(self.window.start.isEnabled())
            self.window.start_backup()
            self.wait(lambda: self.window.worker is None)
            self.assertIn("Neu gesichert: 0; bereits vorhanden: 3", self.window.log.toPlainText())

    def test_worker_runs_off_gui_thread_and_cancels(self):
        gui_thread = self.app.thread()
        worker_threads = []
        results = []

        def action(worker):
            worker_threads.append(QThread.currentThread())
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.window.launch(action, results.append)
        self.wait(lambda: bool(worker_threads))
        self.assertNotEqual(worker_threads[0], gui_thread)
        self.assertFalse(self.window.refresh.isEnabled())
        self.window.cancel()
        self.wait(lambda: self.window.worker is None)
        self.assertTrue(self.window.refresh.isEnabled())
        self.assertEqual(self.window.current.text(), "Abgebrochen")
        self.assertEqual(results, [])

    def test_result_handler_runs_on_gui_thread(self):
        threads = []
        self.window.launch(
            lambda worker: "done",
            lambda result: threads.append(QThread.currentThread()),
        )
        self.wait(lambda: self.window.worker is None)
        self.assertEqual(threads, [self.app.thread()])

    def test_scan_progress_and_stale_feedback_are_visible(self):
        def action(worker):
            worker.scan_progress.emit(123, 5 * 1024**3, "/storage/emulated/0/DCIM/image.jpg")
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.window.launch(action, lambda result: None)
        self.wait(lambda: "123 Dateien" in self.window.current.text())
        self.assertIn("5.0 GiB", self.window.current.text())
        self.assertEqual((self.window.progress.minimum(), self.window.progress.maximum()), (0, 0))
        self.assertTrue(self.window.activity_timer.isActive())
        self.window.last_feedback = time.monotonic() - 12
        self.window.started_at = time.monotonic() - 65
        self.window.update_activity()
        self.assertIn("Laufzeit 01:05", self.window.activity.text())
        self.assertIn("keine neue Rückmeldung", self.window.activity.text())
        self.window.cancel()
        self.window.update_activity()
        self.assertIn("Abbruch angefordert", self.window.activity.text())
        self.wait(lambda: self.window.worker is None)
        self.assertFalse(self.window.activity_timer.isActive())
        self.assertIn("Vorgang beendet", self.window.activity.text())

    def test_unauthorized_device_cannot_scan(self):
        with patch("android_backup.gui.Adb.devices", return_value=[Device("phone", "unauthorized", "Phone")]):
            self.window.refresh_devices()
            self.wait(lambda: self.window.worker is None)
        self.assertFalse(self.window.scan.isEnabled())
        self.assertFalse(self.window.start.isEnabled())
        self.assertIn("Kein autorisiertes Gerät", self.window.log.toPlainText())

    def test_missing_tools_are_visible(self):
        with patch("android_backup.gui.shutil.which", return_value=None):
            self.window.refresh_devices()
            self.wait(lambda: self.window.worker is None)
        self.assertIn("Fehlende Programme: adb, exiftool", self.window.log.toPlainText())
        self.assertFalse(self.window.scan.isEnabled())

    def test_device_switch_invalidates_scan(self):
        self.window.inventory = Inventory("phone", ["/storage/emulated/0"], [])
        self.window.target.setText("/tmp/android-backup")
        self.assertTrue(self.window.start.isEnabled())
        self.window.devices.addItem("Second", Device("second", "device", "Second"))
        self.window.devices.setCurrentIndex(1)
        self.assertIsNone(self.window.inventory)
        self.assertIsNotNone(self.window.worker)
        self.assertIn("Abbrechen", self.window.start.text())
        self.wait(lambda: self.window.worker is None)
        self.assertEqual(self.window.inventory.serial, "second")
        self.assertTrue(self.window.start.isEnabled())

    def test_backup_window_icon_can_be_rendered(self):
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertFalse(self.window.windowIcon().pixmap(64, 64).isNull())

    def test_report_distinguishes_warnings_empty_sources_and_errors(self):
        report = Report("phone", [], total=2, saved=2, status="warnings",
                        metadata_warnings={"/photo.jpg": ["Invalid EXIF text encoding for UserComment"]},
                        empty_files=["/empty.jpg"])
        self.window.show_report(report)
        self.assertIn("Alle ausgewählten Dateien gesichert", self.window.current.text())
        self.assertIn("fehlgeschlagen: 0", self.window.log.toPlainText())
        self.assertIn("1 Meldungen bei 1 Dateien", self.window.log.toPlainText())
        self.assertIn("bereits auf dem Smartphone 0 Byte", self.window.log.toPlainText())
        report.errors.append("Bestandsaufnahme unvollständig")
        self.window.show_report(report)
        self.assertIn("nicht vollständig", self.window.current.text())

    def test_quick_mode_default_and_report_discloses_skipped_hashes(self):
        self.assertFalse(self.window.full_verification.isChecked())
        self.window.show_report(Report(
            "phone", [], total=3, existing=3, status="complete",
            verification="quick", quick_skipped=3,
        ))
        self.assertIn("Ohne erneute Inhaltsprüfung übersprungen: 3", self.window.log.toPlainText())
        self.assertIn("keine Inhaltsgleichheit", self.window.log.toPlainText())

    def test_selected_backup_directory_persists_across_windows(self):
        folder = str(Path(self.settings_directory.name) / "Backup mit Leerzeichen")
        with patch("android_backup.gui.QFileDialog.getExistingDirectory", return_value=folder):
            self.window.choose_target()
        self.assertEqual(QSettings(self.settings_path, QSettings.Format.IniFormat).value(
            "backup_directory"
        ), folder)
        with patch("android_backup.gui.QTimer.singleShot"):
            reopened = Window()
            self.assertEqual(reopened.target.text(), folder)
            reopened.close()

    def test_manually_entered_directory_and_clear_are_saved(self):
        self.window.target.setText("~/Backup")
        self.window.target.editingFinished.emit()
        settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        settings.sync()
        self.assertEqual(settings.value("backup_directory"), str(Path.home() / "Backup"))
        self.window.target.clear()
        self.window.target.editingFinished.emit()
        settings.sync()
        self.assertEqual(settings.value("backup_directory"), "")

    def test_repeated_refresh_and_dropdown_selection(self):
        def detected(cancel):
            return [Device("first", "device", "First"), Device("second", "device", "Second")]

        with patch("android_backup.gui.Adb.devices", side_effect=detected):
            for _ in range(100):
                self.window.refresh.click()
                self.wait(lambda: self.window.worker is None)
                self.assertEqual(self.window.devices.count(), 2)
                self.window.devices.showPopup()
                self.app.processEvents()
                self.window.devices.setCurrentIndex(1)
                self.window.devices.hidePopup()
                self.assertEqual(self.window.devices.currentData().serial, "second")
                self.wait(lambda: self.window.worker is None)
                self.assertTrue(self.window.scan.isEnabled())

    def test_empty_device_list_has_visible_explanation(self):
        with patch("android_backup.gui.Adb.devices", return_value=[]):
            self.window.refresh.click()
            self.wait(lambda: self.window.worker is None)
        self.assertEqual(self.window.devices.count(), 0)
        self.assertIn("Kein Gerät erkannt", self.window.devices.placeholderText())
        self.assertFalse(self.window.scan.isEnabled())
        self.assertTrue(self.window.refresh.isEnabled())

    def test_startup_scans_automatically_without_starting_backup(self):
        self.assertIsNotNone(self.window.inventory)
        self.assertEqual(self.window.inventory.serial, "phone")
        self.assertFalse(self.window.result_card.isVisible())
        self.assertFalse(self.window.start.isEnabled())
        self.assertTrue(self.window.progress_card.isHidden())
        self.assertIn("Verbunden", self.window.device_status.text())

    def test_startup_with_remembered_target_enables_backup_after_scan(self):
        self.window.target.setText(self.settings_directory.name)
        self.window.save_target()
        self.cleanup_window()
        self.window = Window()
        self.window.show()
        self.wait(lambda: self.window.inventory is not None and self.window.worker is None)
        self.window.device_timer.stop()
        self.assertEqual(self.window.target.text(), self.settings_directory.name)
        self.assertTrue(self.window.start.isEnabled())
        self.assertEqual(self.window.start.text(), "Sicherung s&tarten")

    def test_refresh_scans_only_first_authorized_device_once(self):
        devices = [
            Device("locked", "unauthorized", "Locked"),
            Device("ready", "device", "Ready"),
            Device("other", "device", "Other"),
        ]
        self.scan_mock.reset_mock()
        with patch("android_backup.gui.Adb.devices", return_value=devices):
            self.window.refresh_devices()
            self.wait(lambda: self.window.worker is None)
        self.assertEqual(self.scan_mock.call_count, 1)
        self.assertEqual(self.window.inventory.serial, "ready")

    def test_failed_auto_scan_is_visible_and_does_not_retry_in_a_loop(self):
        with patch("android_backup.gui.Adb.scan", side_effect=BackupError("USB getrennt")) as scan:
            self.window.refresh_devices()
            self.wait(lambda: self.window.worker is None)
            self.app.processEvents()
        self.assertEqual(scan.call_count, 1)
        self.assertIsNone(self.window.inventory)
        self.assertTrue(self.window.banner.isVisible())
        self.assertIn("USB getrennt", self.window.banner.message.text())
        self.assertEqual(self.window.banner.message.property("tone"), "error")
        self.assertTrue(self.window.scan.isEnabled())

    def test_primary_button_cancels_scan_and_no_followup_is_started(self):
        def scanning(adb, progress):
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.scan_mock.reset_mock()
        self.scan_mock.side_effect = scanning
        self.window.scan.click()
        self.assertFalse(self.window.scan.isEnabled())
        self.assertEqual(self.window.scan.text(), "Bestand wird eingelesen …")
        self.assertTrue(self.window.progress_card.isVisible())
        self.assertEqual(self.window.start.property("variant"), "danger")
        self.window.start.click()
        self.assertFalse(self.window.start.isEnabled())
        self.wait(lambda: self.window.worker is None)
        self.app.processEvents()
        self.assertEqual(self.scan_mock.call_count, 1)
        self.assertTrue(self.window.scan.isEnabled())
        self.assertEqual(self.window.scan.text(), "Bestand &neu einlesen")
        self.assertIsNone(self.window.inventory)
        self.assertFalse(self.window.pending_scan)
        self.assertEqual(self.window.start.property("variant"), "primary")
        self.assertIn("Abgebrochen", self.window.banner.message.text())

    def test_structured_report_counts_severity_and_collapsed_details(self):
        report = Report(
            "phone", [], total=7, saved=2, existing=1, failed=1, status="issues",
            errors=["Datei konnte nicht gelesen werden"],
            metadata_warnings={"/photo.jpg": ["Invalid date", "Invalid EXIF"]},
            empty_files=["/empty.jpg"], verification="quick", quick_skipped=1,
        )
        self.window.show_report(report)
        self.assertTrue(self.window.result_card.isVisible())
        self.assertEqual(self.window.result_status.property("tone"), "error")
        self.assertEqual(
            {key: tile.value.text() for key, tile in self.window.result_stats.items()},
            {"saved": "2", "existing": "1", "failed": "1", "unprocessed": "3"},
        )
        self.assertTrue(self.window.report_errors.isVisible())
        self.assertFalse(self.window.report_errors.details.isVisible())
        self.window.report_errors.toggle.click()
        self.assertIn("Datei konnte", self.window.report_errors.details.toPlainText())
        self.assertTrue(self.window.report_errors.details.isVisible())
        self.assertIn("/photo.jpg\n  Invalid date\n  Invalid EXIF",
                      self.window.report_warnings.details.toPlainText())
        self.assertIn("2 Meldungen bei 1 Dateien", self.window.report_summary.text())
        self.assertIn("keine Inhaltsgleichheit", self.window.report_summary.text())
        self.assertTrue(self.window.report_problems.isEnabled())

    def test_report_outcomes_do_not_claim_success_on_warnings_or_cancellation(self):
        cases = [
            ({"status": "complete"}, "success"),
            ({"status": "warnings", "empty_files": ["/empty"]}, "warning"),
            ({"status": "warnings", "metadata_warnings": {"/photo": ["EXIF"]}}, "warning"),
            ({"status": "cancelled"}, "warning"),
            ({"status": "failed"}, "error"),
        ]
        for fields, tone in cases:
            with self.subTest(fields=fields):
                report = Report("phone", [], **fields)
                self.window.show_report(report)
                self.assertEqual(self.window.result_status.text(), report.outcome)
                self.assertEqual(self.window.result_status.property("tone"), tone)

    def test_banner_logs_and_displays_error_without_modal_dialog(self):
        with patch("PySide6.QtWidgets.QMessageBox.warning") as warning:
            self.window.show_error("<untrusted> Testfehler")
        warning.assert_not_called()
        self.assertTrue(self.window.banner.isVisible())
        self.assertIn("<untrusted> Testfehler", self.window.banner.message.text())
        self.assertEqual(self.window.banner.message.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("FEHLER: <untrusted> Testfehler", self.window.log.toPlainText())
        self.window.banner.dismiss.click()
        self.assertTrue(self.window.banner.isHidden())

    def test_system_theme_signal_updates_dashboard(self):
        for scheme, theme in ((Qt.ColorScheme.Dark, "dark"), (Qt.ColorScheme.Light, "light")):
            self.app.styleHints().colorSchemeChanged.emit(scheme)
            self.assertEqual(self.window.property("theme"), theme)
            self.assertEqual(self.window.palette().window().color().lightness() < 128, theme == "dark")
            self.assertFalse(self.window.windowIcon().pixmap(48, 48).isNull())

    def test_space_warning_and_scan_issues_are_explicit(self):
        self.window.inventory = Inventory("phone", ["/storage"], [], issues=["Gesperrter Bereich"])
        with patch("android_backup.gui.shutil.disk_usage") as usage:
            usage.return_value.free = 0
            self.window.target.setText(self.settings_directory.name)
            self.window.update_preview()
        self.assertIn("zu wenig Platz", self.window.space_hint.text())
        self.assertEqual(self.window.space_bar.value(), 100)
        self.assertEqual(self.window.space_bar.property("tone"), "warning")
        self.assertTrue(self.window.scan_issues.isVisible())
        self.assertIn("Gesperrter Bereich", self.window.scan_issues.details.toPlainText())

    def test_log_can_be_expanded_and_copied(self):
        self.assertFalse(self.window.log.isVisible())
        self.window.log_section.toggle.click()
        self.assertTrue(self.window.log.isVisible())
        self.window.copy_log()
        self.assertEqual(self.app.clipboard().text(), self.window.log.toPlainText())

    def test_problem_button_opens_report_without_requiring_report_file(self):
        report = Report(
            "phone", [], report_path="/tmp/backup/.backup/report.json",
            status="warnings", empty_files=["/empty.jpg"],
        )
        self.window.show_report(report)
        dialogs = []

        def execute(dialog):
            dialogs.append(dialog)
            return QDialog.DialogCode.Accepted

        with patch("android_backup.gui.ProblemsDialog.exec", new=execute):
            self.window.report_problems.click()
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        self.assertIsInstance(dialog, ProblemsDialog)
        self.assertIn("/empty.jpg", dialog.problems.toPlainText())
        self.assertIs(self.window.report, report)

    def test_problem_button_matches_report_details_and_resets_for_clean_reports(self):
        cases = [
            {"errors": ["Erfassung unvollständig"]},
            {"failed": 1, "total": 1},
            {"total": 1},
            {"empty_files": ["/empty.jpg"]},
            {"metadata_warnings": {"/photo.jpg": ["EXIF-Warnung"]}},
            {"status": "cancelled"},
            {"status": "failed"},
            {"status": "running"},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                self.window.show_report(Report("phone", [], **fields))
                self.assertTrue(self.window.report_problems.isEnabled())
                self.assertEqual(self.window.report_problems.text(), "&Probleme anzeigen")
                self.window.show_report(Report(
                    "phone", [], status="complete", total=2, saved=1, existing=1,
                    verification="quick", quick_skipped=1,
                ))
                self.assertFalse(self.window.report_problems.isEnabled())
                self.assertEqual(self.window.report_problems.text(), "Keine Probleme festgestellt")
                with patch("android_backup.gui.ProblemsDialog.exec") as execute:
                    self.window.report_problems.click()
                execute.assert_not_called()

    def test_problem_overview_lists_each_message_path_and_impact(self):
        report = Report(
            "phone", [], total=6, saved=2, failed=1, status="cancelled",
            errors=["/photo.jpg: Zu wenig freier Speicherplatz für die nächste Datei."],
            empty_files=["/empty.jpg"],
            metadata_warnings={
                "/photo <1>.jpg": ["Invalid EXIF text encoding for UserComment",
                                   "Bad length ICC_Profile", "Ungültiges Aufnahmedatum DateTimeOriginal='0'",
                                   "Kein verlässlich bestimmbares Jahr", "Unbekannte Warnung"],
            },
        )
        dialog = ProblemsDialog(self.window, report)
        text = dialog.problems.toPlainText()
        for expected in (
            "Sicherung abgebrochen", "nicht bestätigt", "Platz schaffen",
            "Nicht bearbeitete Dateien (3)", "/empty.jpg", "/photo <1>.jpg",
            "Kommentare", "Farben", "anderes gültiges Aufnahmedatum", "Unbekannt",
            "nicht sicher ableitbar",
        ):
            self.assertIn(expected, text)
        for warning in report.metadata_warnings["/photo <1>.jpg"]:
            self.assertIn(warning, text)
        self.assertEqual(text.count("Meldung:"), 7)
        self.assertEqual(text.count("Auswirkung:"), 7)
        dialog.deleteLater()

    def test_problem_overview_handles_clean_and_failed_reports_without_details(self):
        for status, failed in (("complete", 0), ("failed", 1)):
            with self.subTest(status=status):
                dialog = ProblemsDialog(self.window, Report("phone", [], status=status, total=failed, failed=failed))
                if not failed:
                    self.assertIn("Keine Probleme gemeldet", dialog.problems.toPlainText())
                else:
                    self.assertIn("Sicherung gestoppt", dialog.problems.toPlainText())
                    self.assertIn("Fehlgeschlagene Dateien: 1", dialog.problems.toPlainText())
                dialog.deleteLater()

    def test_log_toggle_scrolls_and_focuses_then_restores_previous_area(self):
        self.window.show_report(Report("phone", [], status="warnings", empty_files=["/empty.jpg"]))
        self.window.resize(760, 600)
        self.app.processEvents()
        self.window.dashboard_scroll.verticalScrollBar().setValue(0)
        original_scroll = self.window.dashboard_scroll.verticalScrollBar().value()
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.log)
        viewport = self.window.dashboard_scroll.viewport()
        top = self.window.log.mapTo(viewport, self.window.log.rect().topLeft()).y()
        bottom = self.window.log.mapTo(viewport, self.window.log.rect().bottomRight()).y()
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(bottom, viewport.height())
        self.assertGreater(self.window.dashboard_scroll.verticalScrollBar().value(), original_scroll)
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.report_problems)
        self.assertFalse(self.window.log.isVisible())
        self.assertEqual(self.window.dashboard_scroll.verticalScrollBar().value(), original_scroll)

    def test_log_collapse_without_report_returns_to_scan(self):
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.log)
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.scan)

    def test_log_collapse_with_clean_report_skips_disabled_problem_button(self):
        self.window.show_report(Report("phone", [], status="complete"))
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.log)
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.scan)

    def test_known_problem_effects_and_unknown_fallback_are_explicit(self):
        for message, expected in (
            ("Zu wenig freier Speicherplatz für die nächste Datei.", "Platz schaffen"),
            ("[Errno 28] No space left on device", "Platz schaffen"),
            ("Vorhandenes Backup fehlt oder ist beschädigt: /photo.jpg", "nicht überschrieben"),
            ("Bestandsaufnahme von /storage unvollständig", "übersehen"),
            ("Unvollständiger Android-Indexeintrag /photo.jpg", "übersehen"),
            ("Permission denied", "Zugriff verweigert"),
            ("Quelldatei wurde während der Sicherung verändert.", "Keine bestätigte Kopie"),
            ("Unbekannter Fehler", "keine erfolgreiche Sicherung bestätigt"),
        ):
            with self.subTest(message=message):
                self.assertIn(expected, error_impact(message))
        self.assertIn("nicht sicher ableitbar", metadata_impact("Unbekannte Warnung"))

    def test_problem_overview_keeps_long_paths_and_markup_as_readable_text(self):
        path = "/storage/" + "langer-pfad/" * 50 + "<b>Original</b>.jpg"
        message = "<script>keine Formatierung</script>"
        dialog = ProblemsDialog(self.window, Report(
            "phone", [], status="warnings", metadata_warnings={path: [message]},
        ))
        dialog.resize(760, 500)
        dialog.show()
        self.app.processEvents()
        self.assertIn(path, dialog.problems.toPlainText())
        self.assertIn(message, dialog.problems.toPlainText())
        self.assertEqual(dialog.problems.horizontalScrollBar().maximum(), 0)
        dialog.close()
        dialog.deleteLater()

    def test_backup_never_rescans_automatically_after_a_run(self):
        self.window.target.setText(self.settings_directory.name)
        for status in ("complete", "warnings", "issues", "cancelled", "failed"):
            with self.subTest(status=status):
                self.window.inventory = Inventory("phone", [], [])
                self.scan_mock.reset_mock()
                report = Report("phone", [], status=status)
                with patch("android_backup.gui.Backup") as backup, patch(
                    "android_backup.gui.ConfirmationDialog.exec",
                    return_value=QDialog.DialogCode.Accepted,
                ):
                    backup.return_value.run.return_value = report
                    self.window.start_backup()
                    self.wait(lambda: self.window.worker is None)
                self.scan_mock.assert_not_called()
                self.assertEqual(self.window.result_status.text(), report.outcome)
                self.assertIsNone(self.window.inventory)
                self.assertFalse(self.window.start.isEnabled())
                self.assertTrue(self.window.scan.isEnabled())
                self.assertFalse(self.window.pending_scan)
                with patch("android_backup.gui.Adb.devices", return_value=[
                    self.window.devices.currentData()
                ]):
                    self.window.poll_devices()
                    self.wait(lambda: self.window.device_monitor is None)
                self.scan_mock.assert_not_called()

    def test_small_window_uses_stacked_cards_without_horizontal_overflow(self):
        self.window.show_report(Report(
            "phone", [], status="warnings",
            empty_files=["/empty.jpg"], metadata_warnings={"/photo.jpg": ["Invalid EXIF"]},
        ))
        for width, height in ((760, 600), (1000, 860)):
            with self.subTest(width=width):
                self.window.resize(width, height)
                for _ in range(3):
                    self.app.processEvents()
                self.assertEqual(self.window.dashboard_scroll.horizontalScrollBar().maximum(), 0)
                self.assertEqual(self.window.size().width(), width)

    def test_full_current_path_is_available_without_expanding_layout(self):
        path = "/storage/emulated/0/" + "very-long-folder/" * 20 + "image.jpg"
        self.window.show_progress(2, 4, path)
        self.assertEqual(self.window.current.toolTip(), path)
        self.assertIn("2 / 4 Dateien", self.window.current.text())
        self.assertNotIn("very-long-folder", self.window.current.text())
        self.assertEqual(self.window.progress.value(), 2)
        self.assertEqual(self.window.progress.maximum(), 4)

    def test_closing_during_work_cancels_and_waits_before_closing(self):
        results = []

        def action(worker):
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.window.launch(action, results.append)
        with patch("android_backup.gui.ConfirmationDialog.exec", return_value=QDialog.DialogCode.Accepted):
            self.window.close()
        self.assertTrue(self.window.close_requested)
        self.wait(lambda: self.window.worker is None)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.pending_scan)
        self.assertEqual(results, [])

    def test_hotplug_timer_detects_connection_and_authorization(self):
        with patch("android_backup.gui.Adb.devices", return_value=[]):
            self.window.refresh_devices()
            self.wait(lambda: self.window.worker is None)
        self.scan_mock.reset_mock()
        self.window.device_timer.setInterval(10)
        with patch("android_backup.gui.Adb.devices", return_value=[
            Device("new", "unauthorized", "New phone")
        ]):
            self.wait(lambda: self.window.devices.count() == 1 and self.window.device_monitor is None)
            self.assertIn("Freigabe", self.window.device_status.text())
            self.assertIsNone(self.window.inventory)
        with patch("android_backup.gui.Adb.devices", return_value=[
            Device("new", "device", "New phone")
        ]):
            self.wait(lambda: self.window.inventory is not None and self.window.worker is None
                      and self.window.device_monitor is None)
        self.window.device_timer.stop()
        self.assertEqual(self.window.inventory.serial, "new")
        self.assertEqual(self.scan_mock.call_count, 1)
        self.assertFalse(self.window.result_card.isVisible())

    def test_poll_preserves_unchanged_inventory_and_selected_device(self):
        inventory = self.window.inventory
        devices = [self.window.devices.currentData(), Device("other", "device", "Other")]
        self.scan_mock.reset_mock()
        with patch("android_backup.gui.Adb.devices", return_value=devices):
            for _ in range(2):
                self.window.poll_devices()
                self.wait(lambda: self.window.device_monitor is None)
        self.assertIs(self.window.inventory, inventory)
        self.assertEqual(self.window.devices.currentData().serial, "phone")
        self.scan_mock.assert_not_called()

    def test_poll_disconnect_invalidates_inventory(self):
        self.window.target.setText(self.settings_directory.name)
        self.assertTrue(self.window.start.isEnabled())
        with patch("android_backup.gui.Adb.devices", return_value=[]):
            self.window.poll_devices()
            self.wait(lambda: self.window.device_monitor is None)
        self.assertIsNone(self.window.inventory)
        self.assertFalse(self.window.start.isEnabled())
        self.assertIn("Kein Gerät", self.window.device_status.text())

    def test_poll_does_not_interfere_with_running_operation(self):
        def action(worker):
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.window.launch(action, lambda result: None)
        with patch("android_backup.gui.Adb.devices") as devices:
            self.window.poll_devices()
        devices.assert_not_called()
        self.assertIsNone(self.window.device_monitor)
        self.window.cancel()
        self.wait(lambda: self.window.worker is None)

    def test_stale_monitor_result_does_not_replace_newer_manual_search(self):
        self.window.monitor_generation = self.window.operation_generation - 1
        inventory = self.window.inventory
        self.window.receive_device_update([])
        self.assertIs(self.window.inventory, inventory)
        self.assertEqual(self.window.devices.count(), 1)

    def test_monitor_failure_is_visible_and_stops_polling_until_retry(self):
        self.window.device_timer.start()
        with patch("android_backup.gui.Adb.devices", side_effect=BackupError("ADB offline")):
            self.window.poll_devices()
            self.wait(lambda: self.window.device_monitor is None)
        self.assertFalse(self.window.device_timer.isActive())
        self.assertIsNone(self.window.inventory)
        self.assertIn("Automatische Geräteerkennung fehlgeschlagen", self.window.banner.message.text())
        self.window.refresh_devices()
        self.wait(lambda: self.window.worker is None)
        self.assertTrue(self.window.device_timer.isActive())

    def test_close_cancels_and_joins_background_device_monitor(self):
        def detect(cancel):
            cancel.wait(5)
            raise Cancelled("cancel")

        with patch("android_backup.gui.Adb.devices", side_effect=detect):
            self.window.poll_devices()
            self.assertIsNotNone(self.window.device_monitor)
            self.window.close()
        self.assertIsNone(self.window.device_monitor)
        self.assertTrue(self.window.monitor_cancel.is_set())
        self.assertFalse(self.window.device_timer.isActive())

    def test_backup_confirmation_keeps_device_selection_stable(self):
        self.window.target.setText(self.settings_directory.name)
        self.window.monitor_generation = self.window.operation_generation
        inventory = self.window.inventory

        def confirm(*args):
            self.assertTrue(self.window.device_timer.signalsBlocked())
            self.window.receive_device_update([])
            self.assertIs(self.window.inventory, inventory)
            return QDialog.DialogCode.Rejected

        with patch("android_backup.gui.ConfirmationDialog.exec", side_effect=confirm):
            self.window.start_backup()
        self.assertFalse(self.window.device_timer.signalsBlocked())
        self.assertIsNone(self.window.worker)

    def test_scan_button_stays_disabled_during_progress_and_recovers_after_completion(self):
        import threading

        release = threading.Event()

        def scan(adb, progress):
            progress(12, 1024, "/storage/photo.jpg")
            release.wait(5)
            return Inventory(adb.serial, [], [])

        self.scan_mock.reset_mock()
        self.scan_mock.side_effect = scan
        self.window.scan.click()
        try:
            self.wait(lambda: "12 Dateien" in self.window.current.text())
            self.assertFalse(self.window.scan.isEnabled())
            self.window.scan.click()
            self.assertEqual(self.scan_mock.call_count, 1)
        finally:
            release.set()
            self.wait(lambda: self.window.worker is None)
        self.assertTrue(self.window.scan.isEnabled())
        self.assertEqual(self.window.scan.text(), "Bestand &neu einlesen")

    def test_start_dialog_uses_german_actions_and_enter_confirms_escape_cancels(self):
        for key, expected in (
            (Qt.Key.Key_Return, QDialog.DialogCode.Accepted),
            (Qt.Key.Key_Escape, QDialog.DialogCode.Rejected),
        ):
            with self.subTest(key=key):
                dialog = ConfirmationDialog(
                    self.window, "Sicherung starten", [("Zielordner", "/tmp/Backup <Fotos>")],
                    "Vorhandene Inhalte bleiben erhalten.", "Sicherung &starten",
                )
                dialog.show()
                self.app.processEvents()
                self.assertTrue(dialog.confirm_button.isDefault())
                self.assertEqual(dialog.confirm_button.text(), "Sicherung &starten")
                self.assertEqual(dialog.cancel_button.text(), "&Abbrechen")
                self.assertEqual(dialog.confirm_button.property("variant"), "primary")
                self.assertTrue(any(
                    item.text() == "/tmp/Backup <Fotos>" and item.textFormat() == Qt.TextFormat.PlainText
                    for item in dialog.findChildren(QLabel)
                ))
                QTest.keyClick(dialog, key)
                self.assertEqual(dialog.result(), expected)
                self.assertFalse(dialog.isVisible())
                dialog.deleteLater()

    def test_rejecting_real_confirmation_does_not_start_backup(self):
        self.window.target.setText(self.settings_directory.name)
        for action in ("escape", "cancel", "close"):
            with self.subTest(action=action):
                dialogs = []

                def reject_dialog():
                    dialog = self.window.findChild(ConfirmationDialog)
                    dialogs.append(dialog)
                    if action == "escape":
                        QTest.keyClick(dialog, Qt.Key.Key_Escape)
                    elif action == "cancel":
                        dialog.cancel_button.click()
                    else:
                        dialog.close()

                QTimer.singleShot(0, self.window, reject_dialog)
                with patch("android_backup.gui.Backup") as backup:
                    self.window.start_backup()
                self.assertEqual(len(dialogs), 1)
                self.assertFalse(dialogs[0].isVisible())
                backup.assert_not_called()
                self.assertIsNone(self.window.worker)
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_log_collapse_during_scan_focuses_enabled_action(self):
        def scan(adb, progress):
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.scan_mock.side_effect = scan
        self.window.scan.click()
        self.assertFalse(self.window.scan.isEnabled())
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.log)
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.start)
        self.assertTrue(self.window.start.isEnabled())
        self.assertTrue(self.window.start.isVisible())
        self.window.cancel()
        self.wait(lambda: self.window.worker is None)

    def test_log_collapse_without_device_focuses_toggle(self):
        with patch("android_backup.gui.Adb.devices", return_value=[]):
            self.window.refresh_devices()
            self.wait(lambda: self.window.worker is None)
        self.window.device_timer.stop()
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.log)
        self.window.log_section.toggle.click()
        self.wait(lambda: self.app.focusWidget() == self.window.log_section.toggle)

    def test_close_confirmation_defaults_to_cancel(self):
        dialog = ConfirmationDialog(
            self.window, "Vorgang läuft", [], "Sicherung nicht vollständig.",
            "Vorgang &abbrechen und schließen", default_accept=False,
        )
        self.assertTrue(dialog.cancel_button.isDefault())
        self.assertFalse(dialog.confirm_button.isDefault())
        dialog.deleteLater()

    def test_info_button_opens_themed_dialog(self):
        with patch("android_backup.gui.InfoDialog", autospec=True) as dialog:
            button = next(button for button in self.window.findChildren(QPushButton)
                          if button.text() == "&Info")
            button.click()
        dialog.assert_called_once_with(self.window)
        dialog.return_value.exec.assert_called_once()

    def test_info_dialog_preserves_explanations_and_closes_by_keyboard(self):
        for scheme, key in (
            (Qt.ColorScheme.Light, Qt.Key.Key_Return),
            (Qt.ColorScheme.Dark, Qt.Key.Key_Escape),
        ):
            with self.subTest(scheme=scheme):
                self.window.update_theme(scheme)
                dialog = InfoDialog(self.window)
                dialog.show()
                self.app.processEvents()
                text = "\n".join(item.text() for item in dialog.findChildren(QLabel))
                for expected in (
                    "Über die Sicherung", "löscht keine Quelldateien", "Files by Google",
                    "Gesperrte Bereiche", "Cache", "keine Inhaltsgleichheit",
                    "vollständige Integritätsprüfung",
                ):
                    self.assertIn(expected, text)
                self.assertEqual(len(dialog.findChildren(Card)), 3)
                self.assertTrue(dialog.close_button.isDefault())
                self.assertEqual(dialog.close_button.text(), "&Schließen")
                self.assertEqual(dialog.close_button.property("variant"), "primary")
                self.assertEqual(dialog.findChild(QScrollArea).horizontalScrollBar().maximum(), 0)
                QTest.keyClick(dialog, key)
                self.assertFalse(dialog.isVisible())
                dialog.deleteLater()

    def test_open_report_loads_selected_file_and_reveals_report_heading(self):
        for size, lines in (((1000, 860), 1), ((760, 600), 30)):
            with self.subTest(size=size):
                self.window.resize(*size)
                self.app.processEvents()
                report = Report(
                    "previous-phone", [], total=4, saved=2, existing=1, failed=1,
                    status="issues", errors=["/DCIM/photo.jpg: Zugriff verweigert"],
                    selection="\n".join(["Erfasster Bereich"] * lines),
                )
                path = Path(self.settings_directory.name) / f"report-{lines}.json"
                path.write_text(json.dumps(asdict(report)), encoding="utf-8")
                self.window.dashboard_scroll.verticalScrollBar().setValue(0)
                with patch("android_backup.gui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
                    self.window.open_report.click()
                self.wait(lambda: self.app.focusWidget() == self.window.result_card)
                self.assertEqual(self.window.report.serial, "previous-phone")
                self.assertIn(str(path), self.window.result_context.text())
                self.assertEqual(self.window.result_stats["saved"].value.text(), "2")
                self.assertEqual(self.window.result_stats["failed"].value.text(), "1")
                self.assertIn(report.errors[0], self.window.report_errors.details.toPlainText())
                viewport = self.window.dashboard_scroll.viewport()
                for widget in (self.window.result_status, self.window.result_context):
                    top = widget.mapTo(viewport, widget.rect().topLeft()).y()
                    bottom = widget.mapTo(viewport, widget.rect().bottomRight()).y()
                    self.assertGreaterEqual(top, 0)
                    self.assertLess(bottom, viewport.height())

    def test_open_report_cancel_or_invalid_file_preserves_previous_report(self):
        previous = Report("previous-phone", [], status="complete")
        self.window.show_report(previous)
        self.app.processEvents()
        with patch("android_backup.gui.QFileDialog.getOpenFileName", return_value=("", "")):
            self.window.open_report.click()
        self.assertIs(self.window.report, previous)
        path = Path(self.settings_directory.name) / "invalid.json"
        path.write_text("Kein JSON-Bericht", encoding="utf-8")
        with patch("android_backup.gui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.window.open_report.click()
        self.assertIs(self.window.report, previous)
        self.assertTrue(self.window.banner.isVisible())
        self.assertIn("Bericht konnte nicht gelesen werden", self.window.banner.message.text())

    def test_empty_source_actions_show_full_filename_and_explicit_folder_button(self):
        path = "/storage/emulated/0/Download/AyuGram/Saved Attachments/-123#1320x1995@photo.jpg"
        long_name = "<photo>&" + "1234567890" * 20 + ".jpg"
        dialog = ProblemsDialog(self.window, Report(
            "phone", [], empty_files=[path, "/storage/emulated/0/" + long_name], status="warnings"
        ))
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.source_name.toPlainText(), "-123#1320x1995@photo.jpg")
        dialog.empty_files.setCurrentIndex(1)
        self.assertEqual(dialog.source_name.toPlainText(), long_name)
        self.assertEqual(dialog.open_source.text(), "&Ordner auf Smartphone öffnen")
        self.assertFalse(any("kopieren" in button.text() for button in dialog.findChildren(QPushButton)))
        dialog.set_deletion_busy(True)
        self.assertFalse(dialog.delete_source.isEnabled())
        self.assertFalse(dialog.open_source.isEnabled())
        dialog.show()
        self.app.processEvents()
        self.assertEqual(dialog.source_name.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(dialog.width(), 900)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertTrue(dialog.isVisible())
        dialog.set_deletion_busy(False)
        dialog.close()

    def test_empty_source_deletion_requires_matching_device_and_explicit_confirmation(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        for serial, answer in (("other-phone", QDialog.DialogCode.Accepted),
                               ("phone", QDialog.DialogCode.Rejected)):
            with self.subTest(serial=serial):
                report = Report(serial, ["/storage/emulated/0"], empty_files=[path], status="warnings")
                self.window.report = report
                dialog = ProblemsDialog(self.window, report)
                self.addCleanup(dialog.deleteLater)
                with patch("android_backup.gui.ConfirmationDialog.exec", return_value=answer), patch(
                    "android_backup.gui.Adb.delete_empty_file"
                ) as delete:
                    self.window.delete_empty_source(dialog, path)
                delete.assert_not_called()
                self.assertIsNone(self.window.worker)
                if serial != "phone":
                    self.assertIn("Quellgerät", dialog.action_status.text())

    def test_empty_source_deletion_success_keeps_historical_report_and_invalidates_inventory(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        report = Report("phone", ["/storage/emulated/0"], empty_files=[path], status="warnings")
        self.window.show_report(report)
        dialog = ProblemsDialog(self.window, report)
        self.addCleanup(dialog.deleteLater)
        with patch("android_backup.gui.ConfirmationDialog.exec", return_value=QDialog.DialogCode.Accepted), patch(
            "android_backup.gui.Adb.delete_empty_file"
        ) as delete:
            self.window.delete_empty_source(dialog, path)
            self.assertTrue(dialog.deletion_busy)
            self.wait(lambda: self.window.worker is None and not dialog.deletion_busy)
        delete.assert_called_once_with(path, report.roots)
        self.assertIsNone(self.window.inventory)
        self.assertEqual(report.empty_files, [path])
        self.assertIn("gelöscht", dialog.action_status.text())
        self.assertFalse(dialog.delete_source.isEnabled())
        self.assertTrue(dialog.open_source.isEnabled())

    def test_empty_source_deletion_failure_is_visible_and_allows_retry(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        report = Report("phone", ["/storage/emulated/0"], empty_files=[path], status="warnings")
        self.window.report = report
        dialog = ProblemsDialog(self.window, report)
        self.addCleanup(dialog.deleteLater)
        with patch("android_backup.gui.ConfirmationDialog.exec", return_value=QDialog.DialogCode.Accepted), patch(
            "android_backup.gui.Adb.delete_empty_file", side_effect=BackupError("Datei ist nicht leer")
        ):
            self.window.delete_empty_source(dialog, path)
            self.wait(lambda: self.window.worker is None and not dialog.deletion_busy)
        self.assertIn("Datei ist nicht leer", dialog.action_status.text())
        self.assertTrue(dialog.delete_source.isEnabled())
        self.assertEqual(dialog.deleted_paths, set())

    def test_empty_source_deletion_rechecks_connected_device_after_confirmation(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        report = Report("phone", ["/storage/emulated/0"], empty_files=[path], status="warnings")
        self.window.report = report
        dialog = ProblemsDialog(self.window, report)
        self.addCleanup(dialog.deleteLater)
        with patch("android_backup.gui.ConfirmationDialog.exec", return_value=QDialog.DialogCode.Accepted), patch(
            "android_backup.gui.Adb.devices", return_value=[Device("other", "device", "Other")]
        ), patch("android_backup.gui.Adb.delete_empty_file") as delete:
            self.window.delete_empty_source(dialog, path)
            self.wait(lambda: self.window.worker is None and not dialog.deletion_busy)
        delete.assert_not_called()
        self.assertIn("Quellgerät", dialog.action_status.text())

    def test_empty_source_delete_button_uses_confirmation_with_safe_default(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        report = Report("phone", ["/storage/emulated/0"], empty_files=[path], status="warnings")
        self.window.report = report
        confirmations = []

        def reject_confirmation(dialog):
            confirmations.append(dialog)
            return QDialog.DialogCode.Rejected

        def click_delete(dialog):
            dialog.delete_source.click()
            return QDialog.DialogCode.Accepted

        with patch("android_backup.gui.ProblemsDialog.exec", new=click_delete), patch(
            "android_backup.gui.ConfirmationDialog.exec", new=reject_confirmation
        ), patch("android_backup.gui.Adb.delete_empty_file") as delete:
            self.window.show_problems()
        self.assertEqual(len(confirmations), 1)
        confirmation = confirmations[0]
        self.assertTrue(confirmation.cancel_button.isDefault())
        self.assertEqual(confirmation.confirm_button.property("variant"), "danger")
        text = "\n".join(item.text() for item in confirmation.findChildren(QLabel))
        for expected in (path, "phone", "Kein Papierkorb", "nicht atomar", "0 Byte"):
            self.assertIn(expected, text)
        delete.assert_not_called()

    def test_empty_source_deletion_rejects_unknown_path_and_running_operation(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        report = Report("phone", ["/storage/emulated/0"], empty_files=[path], status="warnings")
        self.window.report = report
        dialog = ProblemsDialog(self.window, report)
        self.addCleanup(dialog.deleteLater)
        with patch("android_backup.gui.Adb.delete_empty_file") as delete:
            self.window.delete_empty_source(dialog, "/storage/emulated/0/another.jpg")
            self.assertIn("nicht als leere Quelldatei", dialog.action_status.text())
            self.window.launch(lambda worker: self.window.cancel_event.wait(5), lambda result: None)
            self.window.delete_empty_source(dialog, path)
            self.assertIn("laufenden Vorgang", dialog.action_status.text())
            self.window.cancel()
            self.wait(lambda: self.window.worker is None)
        delete.assert_not_called()

    def test_show_source_folder_opens_matching_device_and_surfaces_failures(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        report = Report("phone", ["/storage/emulated/0"], empty_files=[path], status="warnings")
        self.window.report = report
        dialog = ProblemsDialog(self.window, report)
        self.addCleanup(dialog.deleteLater)
        dialog.open_requested.connect(lambda path: self.window.open_source_folder(dialog, path))
        for error in (None, BackupError("Kein Dateimanager")):
            with self.subTest(error=error), patch(
                "android_backup.gui.Adb.open_source_folder", side_effect=error
            ) as opened:
                inventory = self.window.inventory
                dialog.open_source.click()
                self.assertFalse(dialog.open_source.isEnabled())
                self.wait(lambda: self.window.worker is None and not dialog.deletion_busy)
                opened.assert_called_once_with(path, report.roots)
                self.assertIs(self.window.inventory, inventory)
                if error:
                    self.assertIn("Kein Dateimanager", dialog.action_status.text())
                    self.assertEqual(dialog.action_status.property("tone"), "error")
                else:
                    self.assertIn("nicht automatisch markiert", dialog.action_status.text())

    def test_show_source_folder_rejects_wrong_device_and_unlisted_path(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        report = Report("other-phone", ["/storage/emulated/0"], empty_files=[path], status="warnings")
        self.window.report = report
        dialog = ProblemsDialog(self.window, report)
        self.addCleanup(dialog.deleteLater)
        with patch("android_backup.gui.Adb.open_source_folder") as opened:
            self.window.open_source_folder(dialog, path)
            self.assertIn("Quellgerät", dialog.action_status.text())
            self.window.open_source_folder(dialog, "/not-in-report")
            self.assertIn("nicht im angezeigten Bericht", dialog.action_status.text())
        opened.assert_not_called()

    def test_source_folder_button_is_wired_and_rechecks_connected_device(self):
        path = "/storage/emulated/0/Download/empty.jpg"
        self.window.report = Report(
            "phone", ["/storage/emulated/0"], empty_files=[path], status="warnings"
        )
        statuses = []

        def click_open(dialog):
            dialog.open_source.click()
            self.wait(lambda: self.window.worker is None and not dialog.deletion_busy)
            statuses.append(dialog.action_status.text())
            return QDialog.DialogCode.Accepted

        with patch("android_backup.gui.ProblemsDialog.exec", new=click_open), patch(
            "android_backup.gui.Adb.devices", return_value=[]
        ), patch("android_backup.gui.Adb.open_source_folder") as opened, patch(
            "android_backup.gui.Adb.delete_empty_file"
        ) as deleted:
            self.window.show_problems()
        self.assertEqual(len(statuses), 1)
        self.assertIn("nicht verbunden oder nicht autorisiert", statuses[0])
        opened.assert_not_called()
        deleted.assert_not_called()


class GuiHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        GuiTests.setUpClass()

    def test_callback_exceptions_fail_the_test_and_restore_exception_hook(self):
        for error in (AssertionError, RuntimeError):
            with self.subTest(error=error):
                class CallbackFailure(GuiTests):
                    def runTest(self):
                        def received(result):
                            raise error("Callback failure probe")

                        self.window.launch(lambda worker: "done", received)
                        self.wait(lambda: self.window.worker is None)

                original_hook = sys.excepthook
                result = unittest.TestResult()
                CallbackFailure("runTest").run(result)
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(len(result.failures), 1)
                self.assertIn("Callback failure probe", result.failures[0][1])
                self.assertIs(sys.excepthook, original_hook)

    def test_failed_setup_cleans_window_worker_patches_and_settings(self):
        class SetupFailure(GuiTests):
            def setUp(self):
                super().setUp()
                self.window.launch(
                    lambda worker: self.window.cancel_event.wait(5), lambda result: None
                )
                self.fail("Setup failure probe")

            def runTest(self):
                raise AssertionError("Test body must not run")

        from android_backup.core import Adb

        original_scan = Adb.scan
        original_hook = sys.excepthook
        case = SetupFailure("runTest")
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("Setup failure probe", result.failures[0][1])
        self.assertIsNone(case.window.worker)
        self.assertIsNone(case.window.device_monitor)
        with self.assertRaises(RuntimeError):
            case.window.isVisible()
        self.assertFalse(Path(case.settings_directory.name).exists())
        self.assertIs(Adb.scan, original_scan)
        self.assertIs(sys.excepthook, original_hook)


if __name__ == "__main__":
    unittest.main()
