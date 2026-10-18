import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QThread, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from android_backup.core import BackupError, Cancelled, Device, Inventory, Report
from android_backup.gui import Window
from tests.test_core import FakeAdb, FakeMetadata


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")

    def setUp(self):
        self.settings_directory = tempfile.TemporaryDirectory()
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
        self.scan_mock = self.patches[-1].start()
        self.window = Window()
        self.window.show()
        self.wait(lambda: self.window.devices.count() == 1 and self.window.worker is None)

    def tearDown(self):
        if self.window.worker is not None:
            self.window.cancel()
            self.wait(lambda: self.window.worker is None)
        self.window.close()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.settings_directory.cleanup()

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
        ), patch("android_backup.gui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
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
            self.assertTrue(self.window.start.isEnabled())
            self.assertIsNotNone(self.window.inventory)
            self.window.start_backup()
            self.wait(lambda: self.window.worker is None)
            self.assertIn("Neu gesichert: 0; bereits vorhanden: 3", self.window.log.toPlainText())

    def test_worker_runs_off_gui_thread_and_cancels(self):
        gui_thread = self.app.thread()
        worker_threads = []

        def action(worker):
            worker_threads.append(QThread.currentThread())
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.window.launch(action, lambda result: self.fail("Cancellation returned success"))
        self.wait(lambda: bool(worker_threads))
        self.assertNotEqual(worker_threads[0], gui_thread)
        self.assertFalse(self.window.refresh.isEnabled())
        self.window.cancel()
        self.wait(lambda: self.window.worker is None)
        self.assertTrue(self.window.refresh.isEnabled())
        self.assertEqual(self.window.current.text(), "Abgebrochen")

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
        self.assertTrue(self.window.progress_card.isVisible())
        self.assertEqual(self.window.start.property("variant"), "danger")
        self.window.start.click()
        self.assertFalse(self.window.start.isEnabled())
        self.wait(lambda: self.window.worker is None)
        self.app.processEvents()
        self.assertEqual(self.scan_mock.call_count, 1)
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
        self.assertFalse(self.window.report_folder.isEnabled())

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
        with patch("android_backup.gui.QMessageBox.warning") as warning:
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

    def test_report_folder_open_failure_is_visible(self):
        report = Report("phone", [], report_path="/tmp/backup/.backup/report.json", status="complete")
        self.window.show_report(report)
        with patch("android_backup.gui.QDesktopServices.openUrl", return_value=False) as opened:
            self.window.report_folder.click()
        self.assertEqual(opened.call_args.args[0].toLocalFile(), "/tmp/backup/.backup")
        self.assertIn("Berichtsordner konnte nicht geöffnet", self.window.banner.message.text())

    def test_backup_rescans_only_after_a_completed_run(self):
        self.window.target.setText(self.settings_directory.name)
        for status, expected_scans in (("complete", 1), ("cancelled", 0), ("failed", 0)):
            with self.subTest(status=status):
                self.window.inventory = Inventory("phone", [], [])
                self.scan_mock.reset_mock()
                report = Report("phone", [], status=status)
                with patch("android_backup.gui.Backup") as backup, patch(
                    "android_backup.gui.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    backup.return_value.run.return_value = report
                    self.window.start_backup()
                    self.wait(lambda: self.window.worker is None)
                self.assertEqual(self.scan_mock.call_count, expected_scans)
                self.assertEqual(self.window.result_status.text(), report.outcome)
                self.assertEqual(self.window.inventory is not None, bool(expected_scans))

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
        def action(worker):
            self.window.cancel_event.wait(5)
            raise Cancelled("cancel")

        self.window.launch(action, lambda result: self.fail("Unexpected success"))
        with patch("android_backup.gui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window.close()
        self.assertTrue(self.window.close_requested)
        self.wait(lambda: self.window.worker is None)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.pending_scan)


if __name__ == "__main__":
    unittest.main()
