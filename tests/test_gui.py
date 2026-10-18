import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from android_backup.core import Cancelled, Device, Inventory, Report
from android_backup.gui import Window
from tests.test_core import FakeAdb, FakeMetadata


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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
            patch("android_backup.gui.QMessageBox.warning"),
        ]
        for item in self.patches:
            item.start()
        self.window = Window()
        self.window.show()
        self.wait(lambda: self.window.devices.count() == 1 and self.window.thread is None)

    def tearDown(self):
        if self.window.thread is not None:
            self.window.cancel()
            self.wait(lambda: self.window.thread is None)
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
            self.wait(lambda: self.window.thread is None)
            self.assertIn("3 Dateien", self.window.summary.text())
            self.assertTrue(self.window.start.isEnabled())
            self.window.start_backup()
            self.wait(lambda: self.window.thread is None)
            self.assertTrue((Path(folder) / "Fotos/2023/a.jpg").is_file())
            self.assertIn("Neu gesichert: 2; bereits vorhanden: 1", self.window.log.toPlainText())
            self.assertIn("Vollständig im erfassten", self.window.current.text())
            self.assertFalse(self.window.start.isEnabled())
            self.window.scan_device()
            self.wait(lambda: self.window.thread is None)
            self.window.start_backup()
            self.wait(lambda: self.window.thread is None)
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
        self.wait(lambda: self.window.thread is None)
        self.assertTrue(self.window.refresh.isEnabled())
        self.assertEqual(self.window.current.text(), "Abgebrochen")

    def test_result_handler_runs_on_gui_thread(self):
        threads = []
        self.window.launch(
            lambda worker: "done",
            lambda result: threads.append(QThread.currentThread()),
        )
        self.wait(lambda: self.window.thread is None)
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
        self.wait(lambda: self.window.thread is None)
        self.assertFalse(self.window.activity_timer.isActive())
        self.assertIn("Vorgang beendet", self.window.activity.text())

    def test_unauthorized_device_cannot_scan(self):
        with patch("android_backup.gui.Adb.devices", return_value=[Device("phone", "unauthorized", "Phone")]):
            self.window.refresh_devices()
            self.wait(lambda: self.window.thread is None)
        self.assertFalse(self.window.scan.isEnabled())
        self.assertFalse(self.window.start.isEnabled())
        self.assertIn("Kein autorisiertes Gerät", self.window.log.toPlainText())

    def test_missing_tools_are_visible(self):
        with patch("android_backup.gui.shutil.which", return_value=None):
            self.window.refresh_devices()
            self.wait(lambda: self.window.thread is None)
        self.assertIn("Fehlende Programme: adb, exiftool", self.window.log.toPlainText())
        self.assertFalse(self.window.scan.isEnabled())

    def test_device_switch_invalidates_scan(self):
        self.window.inventory = Inventory("phone", ["/storage/emulated/0"], [])
        self.window.target.setText("/tmp/android-backup")
        self.assertTrue(self.window.start.isEnabled())
        self.window.devices.addItem("Second", Device("second", "device", "Second"))
        self.window.devices.setCurrentIndex(1)
        self.assertIsNone(self.window.inventory)
        self.assertFalse(self.window.start.isEnabled())

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
                self.wait(lambda: self.window.thread is None)
                self.assertEqual(self.window.devices.count(), 2)
                self.window.devices.showPopup()
                self.app.processEvents()
                self.window.devices.setCurrentIndex(1)
                self.window.devices.hidePopup()
                self.assertEqual(self.window.devices.currentData().serial, "second")
                self.assertTrue(self.window.scan.isEnabled())

    def test_empty_device_list_has_visible_explanation(self):
        with patch("android_backup.gui.Adb.devices", return_value=[]):
            self.window.refresh.click()
            self.wait(lambda: self.window.thread is None)
        self.assertEqual(self.window.devices.count(), 0)
        self.assertIn("Kein Gerät erkannt", self.window.devices.placeholderText())
        self.assertFalse(self.window.scan.isEnabled())
        self.assertTrue(self.window.refresh.isEnabled())


if __name__ == "__main__":
    unittest.main()
