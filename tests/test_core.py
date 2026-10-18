import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from android_backup.core import (
    Adb, Backup, BackupError, Cancelled, Inventory, Metadata,
    SourceFile, Store, file_digest, publish, run_command,
    excluded_app_file,
    load_report,
)


class EmptySourceDeletionTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.adb = Adb("phone", self.cancel)
        self.roots = ["/storage/emulated/0"]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def execute_script(self, script, timeout):
        result = subprocess.run(
            ["sh", "-c", script.replace("/storage/emulated/0", str(self.root))],
            capture_output=True, check=False,
        )
        if result.returncode:
            raise BackupError(result.stderr.decode())
        return result.stdout

    def test_delete_only_empty_regular_file_with_special_name(self):
        name = "Bild ' # @ ; $(touch UNEXPECTED)\n.jpg"
        target = self.root / name
        target.touch()
        untouched = self.root / "other.jpg"
        untouched.touch()
        with patch.object(self.adb, "checked_shell", side_effect=self.execute_script):
            self.adb.delete_empty_file("/storage/emulated/0/" + name, self.roots)
        self.assertFalse(target.exists())
        self.assertTrue(untouched.exists())
        self.assertFalse((self.root / "UNEXPECTED").exists())

    def test_nonempty_missing_directory_and_symlink_are_refused(self):
        (self.root / "filled.jpg").write_bytes(b"redownloaded")
        (self.root / "empty.jpg").touch()
        (self.root / "folder").mkdir()
        (self.root / "link.jpg").symlink_to(self.root / "empty.jpg")
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        with patch.object(self.adb, "checked_shell", side_effect=self.execute_script):
            for name in ("filled.jpg", "missing.jpg", "folder", "link.jpg", "alias/empty.jpg"):
                with self.subTest(name=name), self.assertRaises(BackupError):
                    self.adb.delete_empty_file("/storage/emulated/0/" + name, self.roots)
        self.assertEqual((self.root / "filled.jpg").read_bytes(), b"redownloaded")
        self.assertTrue((self.root / "empty.jpg").exists())

    def test_invalid_paths_never_reach_device(self):
        with patch.object(self.adb, "checked_shell") as shell:
            for path, roots in (
                ("/storage/emulated/0", self.roots),
                ("/storage/emulated/0/../secret", self.roots),
                ("/storage/emulated/0//photo.jpg", self.roots),
                ("/storage/emulated/0/./photo.jpg", self.roots),
                ("/storage/emulated/0/photo\0.jpg", self.roots),
                ("/storage/emulated/01/photo.jpg", self.roots),
                ("/data/private/photo.jpg", ["/data/private"]),
                ("/storage/emulated/0/photo.jpg", []),
            ):
                with self.subTest(path=path), self.assertRaises(BackupError):
                    self.adb.delete_empty_file(path, roots)
        shell.assert_not_called()

    def test_device_failure_and_unconfirmed_output_are_not_success(self):
        for response in (BackupError("USB getrennt"), b"", b"unexpected"):
            with self.subTest(response=response):
                with patch.object(
                    self.adb, "checked_shell",
                    side_effect=response if isinstance(response, Exception) else None,
                    return_value=response,
                ), self.assertRaises(BackupError):
                    self.adb.delete_empty_file("/storage/emulated/0/empty.jpg", self.roots)

    def test_cancellation_prevents_adb_command_and_sd_root_is_supported(self):
        self.cancel.set()
        with patch("android_backup.core.subprocess.Popen") as popen, self.assertRaises(Cancelled):
            self.adb.delete_empty_file("/storage/emulated/0/empty.jpg", self.roots)
        popen.assert_not_called()
        self.cancel.clear()
        with patch.object(self.adb, "checked_shell", return_value=b"DELETED\n") as shell:
            self.adb.delete_empty_file("/storage/AB12-CD34/Download/empty.jpg", ["/storage/AB12-CD34"])
        self.assertIn("/storage/AB12-CD34/Download/empty.jpg", shell.call_args.args[0])

    def test_open_source_folder_encodes_document_uri_and_uses_selected_device(self):
        for root, volume in (("/storage/emulated/0", "primary"), ("/storage/AB12-CD34", "AB12-CD34")):
            with self.subTest(root=root):
                path = root + "/Download/Ayu Gram/#é ' $(name)/photo.jpg"
                result = subprocess.CompletedProcess([], 0, b"Status: ok\n", b"")
                with patch.object(self.adb, "shell", return_value=result) as shell:
                    self.adb.open_source_folder(path, [root])
                script = shell.call_args.args[0]
                uri = "content://com.android.externalstorage.documents/document/" + quote(
                    volume + ":Download/Ayu Gram/#é ' $(name)", safe=""
                )
                self.assertIn(shlex.quote(uri), script)
                self.assertIn("android.intent.action.VIEW", script)
                self.assertIn("vnd.android.document/directory", script)
                self.assertNotIn("rm ", script)

    def test_open_source_folder_rejects_invalid_path_and_android_failures(self):
        with patch.object(self.adb, "shell") as shell, self.assertRaises(BackupError):
            self.adb.open_source_folder("/data/private/photo.jpg", self.roots)
        shell.assert_not_called()
        for code, stdout, stderr in (
            (0, b"Starting: Intent\n", b"Error: Activity not found\n"),
            (0, b"Starting: Intent\n", b""),
            (1, b"", b"Permission denied"),
            (0, b"Status: ok\n", b"SecurityException: denied"),
        ):
            with self.subTest(stderr=stderr), patch.object(
                self.adb, "shell", return_value=subprocess.CompletedProcess([], code, stdout, stderr)
            ), self.assertRaises(BackupError):
                self.adb.open_source_folder("/storage/emulated/0/Download/empty.jpg", self.roots)

    def test_open_source_folder_handles_file_in_storage_root(self):
        with patch.object(
            self.adb, "shell", return_value=subprocess.CompletedProcess([], 0, b"Status: ok\n", b"")
        ) as shell:
            self.adb.open_source_folder("/storage/emulated/0/photo.jpg", self.roots)
        self.assertIn(
            "content://com.android.externalstorage.documents/document/primary%3A",
            shell.call_args.args[0],
        )
        self.assertNotIn("primary%3A.", shell.call_args.args[0])


class FakeAdb(Adb):
    def __init__(self, files, cancel):
        super().__init__("phone", cancel)
        self.files = files
        self.pulls = 0
        self.bad_pull = False
        self.change_hash = False
        self.cancel_pull = False

    def stat(self, path):
        return SourceFile(path, len(self.files[path]), 1700000000)

    def digest(self, path):
        if self.change_hash:
            return hashlib.sha256(str(self.pulls).encode()).hexdigest()
        return hashlib.sha256(self.files[path]).hexdigest()

    def pull(self, path, destination):
        self.pulls += 1
        destination.write_bytes(b"broken" if self.bad_pull else self.files[path])
        if self.cancel_pull:
            self.cancel.set()
            raise Cancelled("USB transfer cancelled")


class FakeMetadata(Metadata):
    def inspect(self, path, original_name, mtime):
        if original_name.endswith(".jpg"):
            return "Fotos", "2023", []
        if original_name.endswith(".mp4"):
            return "Videos", "2024", []
        return "Dokumente", None, []


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.target = Path(self.temp.name) / "backup"
        self.cancel = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def run_backup(self, files, configure=None, issues=None):
        adb = FakeAdb(files, self.cancel)
        if configure:
            configure(adb)
        inventory = Inventory("phone", ["/storage/emulated/0"], [adb.stat(p) for p in files], issues or [])
        report = Backup(adb, FakeMetadata(self.cancel), self.cancel, full_verification=True).run(inventory, self.target)
        self.assertEqual(json.loads(Path(report.report_path).read_text())["status"], report.status)
        self.assertFalse(list((self.target / ".backup").glob("staging-*")))
        return report, adb

    def test_layout_dedup_collision_and_repeat(self):
        files = {
            "/storage/emulated/0/DCIM/a.jpg": b"photo one",
            "/storage/emulated/0/Android/media/app/a.jpg": b"photo two",
            "/storage/emulated/0/Pictures/copy.jpg": b"photo one",
            "/storage/emulated/0/Download/movie.mp4": b"movie",
            "/storage/emulated/0/Download/file.pdf": b"pdf",
        }
        report, adb = self.run_backup(files)
        self.assertEqual((report.saved, report.existing, report.failed), (4, 1, 0))
        self.assertEqual(report.status, "complete")
        self.assertEqual(len(list((self.target / "Fotos" / "2023").iterdir())), 2)
        self.assertEqual((self.target / "Videos/2024/movie.mp4").read_bytes(), b"movie")
        self.assertEqual((self.target / "Dokumente/file.pdf").read_bytes(), b"pdf")
        with sqlite3.connect(self.target / ".backup/index.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 5)
        repeated, repeat_adb = self.run_backup(files)
        self.assertEqual((repeated.saved, repeated.existing), (0, 5))
        self.assertEqual(repeat_adb.pulls, 0)
        deleted, _ = self.run_backup({})
        self.assertEqual(deleted.saved, 0)
        self.assertTrue((self.target / "Fotos/2023/a.jpg").is_file())
        changed, _ = self.run_backup({"/storage/emulated/0/DCIM/a.jpg": b"new photo"})
        self.assertEqual(changed.saved, 1)
        self.assertEqual(len(list((self.target / "Fotos/2023").iterdir())), 3)

    def test_partial_transfer_never_published(self):
        report, adb = self.run_backup(
            {"/storage/emulated/0/a.jpg": b"photo"}, lambda adb: setattr(adb, "bad_pull", True)
        )
        self.assertEqual((report.status, report.failed), ("issues", 1))
        self.assertEqual(adb.pulls, 2)
        self.assertFalse((self.target / "Fotos").exists())

    def test_changing_source_is_not_success(self):
        report, _ = self.run_backup(
            {"/storage/emulated/0/a.jpg": b"photo"}, lambda adb: setattr(adb, "change_hash", True)
        )
        self.assertEqual(report.failed, 1)
        self.assertFalse((self.target / "Fotos").exists())

    def test_abort_preserves_completed_files(self):
        report, _ = self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
        report, _ = self.run_backup(
            {"/storage/emulated/0/b.jpg": b"other"}, lambda adb: setattr(adb, "cancel_pull", True)
        )
        self.assertEqual(report.status, "cancelled")
        self.assertEqual(report.saved, 0)
        self.assertTrue((self.target / "Fotos/2023/a.jpg").is_file())
        self.assertFalse((self.target / "Fotos/2023/b.jpg").exists())

    def test_full_disk_is_reported(self):
        with patch("android_backup.core.shutil.disk_usage", return_value=type("Usage", (), {"free": 0})()):
            report, adb = self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
        self.assertEqual(report.failed, 1)
        self.assertEqual(adb.pulls, 0)
        self.assertIn("Speicherplatz", report.issues[0])

    def test_scan_issues_are_not_complete(self):
        report, _ = self.run_backup({}, issues=["Android/data: Permission denied"])
        self.assertEqual(report.status, "issues")
        self.assertEqual(report.errors, report.issues)

    def test_successful_copy_with_metadata_warnings_is_not_transfer_failure(self):
        path = "/storage/emulated/0/photo.jpg"
        with patch.object(FakeMetadata, "inspect", return_value=(
            "Fotos", "2023", [path + ": Bad length ICC_Profile (length 632)"]
        )):
            report, _ = self.run_backup({path: b"photo"})
        self.assertEqual(report.status, "warnings")
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.metadata_warnings), 1)
        self.assertIn("Alle ausgewählten Dateien gesichert", report.outcome)
        loaded = load_report(Path(report.report_path))
        self.assertEqual(loaded.metadata_warnings, report.metadata_warnings)

    def test_empty_file_is_explicit_even_when_already_present(self):
        path = "/storage/emulated/0/empty.jpg"
        for _ in range(2):
            report, _ = self.run_backup({path: b""})
            self.assertEqual(report.empty_files, [path])
            self.assertEqual(report.status, "warnings")
            self.assertEqual(report.failed, 0)

    def test_legacy_report_groups_warnings_but_keeps_unknown_errors(self):
        path = Path(self.temp.name) / "old.json"
        path.write_text(json.dumps({
            "serial": "phone", "roots": [], "status": "issues", "total": 2,
            "saved": 1, "existing": 0, "failed": 1,
            "issues": [
                "/video.mp4: Ungültiges Aufnahmedatum CreateDate='0000:00:00 00:00:00'.",
                "/video.mp4: Ungültiges Aufnahmedatum TrackCreateDate='0000:00:00 00:00:00'.",
                "/empty.jpg: File is empty",
                "/missing.jpg: ADB: Permission denied",
            ],
        }))
        report = load_report(path)
        self.assertEqual(len(report.metadata_warnings), 1)
        self.assertEqual(len(report.metadata_warnings["/video.mp4"]), 2)
        self.assertEqual(report.empty_files, ["/empty.jpg"])
        self.assertEqual(report.errors, ["/missing.jpg: ADB: Permission denied"])
        self.assertIn("nicht vollständig", report.outcome)

    def test_invalid_report_is_explicit_error(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text('{"status": "complete"}')
        with self.assertRaises(BackupError):
            load_report(path)

    def test_quick_repeat_uses_one_stat_and_no_hash_or_pull_per_file(self):
        files = {f"/storage/emulated/0/{index}.jpg": str(index).encode() for index in range(105)}
        self.run_backup(files)
        adb = FakeAdb(files, self.cancel)
        inventory = Inventory("phone", ["/storage/emulated/0"], [adb.stat(path) for path in files])
        with patch.object(adb, "digest", side_effect=AssertionError("Unexpected source hash")), patch.object(
            adb, "pull", side_effect=AssertionError("Unexpected transfer")
        ), patch("android_backup.core.file_digest", side_effect=AssertionError("Unexpected local hash")), patch.object(
            adb, "stat", wraps=adb.stat
        ) as stat:
            report = Backup(adb, FakeMetadata(self.cancel), self.cancel).run(inventory, self.target)
        self.assertEqual(stat.call_count, 105)
        self.assertEqual((report.saved, report.existing, report.quick_skipped, report.failed), (0, 105, 105, 0))
        loaded = load_report(Path(report.report_path))
        self.assertEqual((loaded.verification, loaded.quick_skipped), ("quick", 105))

    def test_quick_mode_uses_live_stat_not_stale_inventory(self):
        path = "/storage/emulated/0/photo.jpg"
        self.run_backup({path: b"old"})
        adb = FakeAdb({path: b"longer new photo"}, self.cancel)
        stale = Inventory("phone", ["/storage/emulated/0"], [SourceFile(path, 3, 1700000000)])
        report = Backup(adb, FakeMetadata(self.cancel), self.cancel).run(stale, self.target)
        self.assertEqual((report.saved, report.quick_skipped, report.failed), (1, 0, 0))
        self.assertEqual(adb.pulls, 1)

    def test_quick_mode_detects_missing_and_wrong_sized_target(self):
        path = "/storage/emulated/0/photo.jpg"
        self.run_backup({path: b"photo"})
        target = self.target / "Fotos/2023/photo.jpg"
        for missing in (False, True):
            if missing:
                target.unlink()
            else:
                target.write_bytes(b"broken size")
            adb = FakeAdb({path: b"photo"}, self.cancel)
            inventory = Inventory("phone", ["/storage/emulated/0"], [adb.stat(path)])
            report = Backup(adb, FakeMetadata(self.cancel), self.cancel).run(inventory, self.target)
            self.assertEqual((report.failed, report.quick_skipped), (1, 0))

    def test_full_mode_detects_same_size_source_change(self):
        path = "/storage/emulated/0/photo.jpg"
        self.run_backup({path: b"old"})
        report, adb = self.run_backup({path: b"new"})
        self.assertEqual((report.saved, report.quick_skipped), (1, 0))
        self.assertEqual(report.verification, "full")
        self.assertEqual(adb.pulls, 1)

    def test_corrupted_existing_backup_is_not_overwritten(self):
        self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
        path = self.target / "Fotos/2023/a.jpg"
        path.write_bytes(b"xxxxx")
        report, adb = self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
        self.assertEqual(report.failed, 1)
        self.assertEqual(adb.pulls, 0)
        self.assertEqual(path.read_bytes(), b"xxxxx")

    def test_special_names_and_long_names(self):
        names = ["weird ' ü\n$(touch NEVER).jpg", "x" * 220 + ".jpg", ".hidden.jpg"]
        report, _ = self.run_backup({"/storage/emulated/0/" + name: str(i).encode() for i, name in enumerate(names)})
        self.assertEqual(report.failed, 0)
        self.assertEqual(len(list((self.target / "Fotos/2023").iterdir())), 3)

    def test_lock_rejects_concurrent_backup(self):
        with Store(self.target, self.cancel):
            with self.assertRaisesRegex(BackupError, "bereits"):
                with Store(self.target, self.cancel):
                    self.fail("Second store opened")

    def test_symlink_destination_is_rejected(self):
        self.target.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.target / "Fotos").symlink_to(outside, target_is_directory=True)
        report, _ = self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
        self.assertEqual(report.failed, 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_atomic_publication_does_not_replace(self):
        first, target = Path(self.temp.name) / "first", Path(self.temp.name) / "target"
        first.write_bytes(b"new")
        target.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            publish(first, target)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(first.read_bytes(), b"new")
        target.unlink()
        publish(first, target)
        self.assertFalse(first.exists())
        self.assertEqual(target.read_bytes(), b"new")

    def test_unindexed_file_is_reused_without_overwriting(self):
        directory = self.target / "Fotos/2023"
        directory.mkdir(parents=True)
        (directory / "a.jpg").write_bytes(b"photo")
        report, _ = self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
        self.assertEqual(report.failed, 0)
        self.assertEqual(list(directory.iterdir()), [directory / "a.jpg"])

    def test_database_failure_persists_failed_report(self):
        with patch.object(Store, "remember", side_effect=sqlite3.OperationalError("disk full")):
            report, _ = self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
        self.assertEqual(report.status, "failed")
        self.assertIn("disk full", report.issues[-1])

    def test_backup_on_project_drive_without_hardlink_requirement(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            self.target = Path(directory) / "backup"
            report, _ = self.run_backup({"/storage/emulated/0/a.jpg": b"photo"})
            self.assertEqual(report.status, "complete")
            self.assertEqual((self.target / "Fotos/2023/a.jpg").read_bytes(), b"photo")

    def test_stale_inventory_excludes_app_data_but_preserves_app_media(self):
        files = {
            "/storage/emulated/0/Android/data/app/files/file.pdf": b"app data",
            "/storage/emulated/0/Android/obb/app/main.obb": b"expansion",
            "/storage/emulated/0/App/Cache/file.jpg": b"cache",
            "/storage/emulated/0/Download/installer.APK": b"installer",
            "/storage/emulated/0/Android/media/app/image.jpg": b"photo",
            "/storage/emulated/0/Android/media/app/document.pdf": b"document",
        }
        report, adb = self.run_backup(files)
        self.assertEqual((report.saved, report.failed, report.excluded_files, report.total), (2, 0, 4, 2))
        self.assertEqual(adb.pulls, 2)
        self.assertEqual(report.status, "complete")
        with sqlite3.connect(self.target / ".backup/index.sqlite3") as db:
            paths = {row[0] for row in db.execute("SELECT source_path FROM sources")}
        self.assertEqual(paths, {path for path in files if "/Android/media/" in path})

    def test_category_selection_allows_app_audio(self):
        app_paths = [
            "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Voice Notes/a.opus",
            "/storage/emulated/0/WhatsApp/Media/WhatsApp Audio/b.MP3",
            "/storage/ABCD-1234/Telegram/Telegram Audio/c.ogg",
            "/storage/emulated/0/Signal/d.m4a",
            "/storage/emulated/0/com.example.recorder/recording.wav",
            "/storage/emulated/0/Android/media/example/archive.zip",
            "/storage/emulated/0/Android/media/example/unknown",
        ]
        personal_paths = [
            "/storage/emulated/0/Music/song.mp3",
            "/storage/emulated/0/Download/note.opus",
            "/storage/ABCD-1234/Recordings/recording.wav",
            "/storage/emulated/0/Android/media/com.whatsapp/image.jpg",
            "/storage/emulated/0/Telegram/video.mp4",
            "/storage/emulated/0/Signal/document.pdf",
        ]
        files = {path: str(index).encode() for index, path in enumerate(app_paths + personal_paths)}
        report, adb = self.run_backup(files)
        self.assertEqual((report.saved, report.excluded_files, report.failed), (13, 0, 0))
        self.assertEqual(adb.pulls, 13)
        with sqlite3.connect(self.target / ".backup/index.sqlite3") as db:
            self.assertEqual({row[0] for row in db.execute("SELECT source_path FROM sources")}, set(files))


class MetadataTests(unittest.TestCase):
    def inspect(self, data, name="x.jpg", mtime=1700000000, code=0):
        result = subprocess.CompletedProcess([], code, json.dumps([data]).encode(), b"")
        with patch("android_backup.core.run_command", return_value=result):
            return Metadata(threading.Event()).inspect(Path("/temporary/transfer"), name, mtime)

    def test_capture_date_beats_mtime(self):
        category, year, issues = self.inspect({"DateTimeOriginal": "2020:12:31 23:59:59+02:00"})
        self.assertEqual((category, year, issues), ("Fotos", "2020", []))

    def test_video_date_and_invalid_fallback(self):
        self.assertEqual(
            self.inspect({"MediaCreateDate": "2022:01:02 03:04:05"}, "movie.mp4")[:2],
            ("Videos", "2022"),
        )
        category, year, issues = self.inspect({"DateTimeOriginal": "2021:02:30 00:00:00"})
        self.assertEqual(year, str(datetime.fromtimestamp(1700000000).year))
        self.assertTrue(issues)
        self.assertEqual(self.inspect({"CreateDate": "0000:00:00 00:00:00"}, mtime=0)[1], "Unbekannt")

    def test_content_overrides_extension_and_nonmedia_have_no_year(self):
        self.assertEqual(self.inspect({"MIMEType": "video/mp4"}, "download.bin")[:2], ("Videos", "2023"))
        for name, expected in [("file.pdf", "Dokumente"), ("music.flac", "Audio"), ("x.zip", "Archive"), ("x.bin", "Sonstige")]:
            self.assertEqual(self.inspect({}, name)[:2], (expected, None))

    def test_unsupported_metadata_still_classifies_and_reports(self):
        category, year, issues = self.inspect({"Error": "Unknown file type"}, "thing.bin", code=1)
        self.assertEqual((category, year), ("Sonstige", None))
        self.assertTrue(issues)

    def test_malformed_exif_output_is_explicit_error(self):
        result = subprocess.CompletedProcess([], 0, b"not JSON", b"")
        with patch("android_backup.core.run_command", return_value=result):
            with self.assertRaises(BackupError):
                Metadata(threading.Event()).inspect(Path("/file"), "file.jpg", 0)


class AdbTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.adb = Adb("phone", self.cancel)

    def test_scan_preserves_newlines_quotes_unicode_and_deduplicates_roots(self):
        paths = ["/storage/emulated/0/strange ' ü\nfile.jpg", "/storage/ABCD-1234/a.mp4"]

        def shell(script, timeout=180):
            path = paths[0] if "/storage/emulated/0" in script else paths[1]
            output = b"123 1700000000\n" + path.encode() + b"\0"
            return subprocess.CompletedProcess([], 1, output, b"find: Android/data: Permission denied")

        with patch.object(self.adb, "checked_shell", side_effect=[
            b"/system/bin/find\n/system/bin/stat\n/system/bin/sha256sum\n",
            b"/storage/emulated/0\n/storage/ABCD-1234\n/storage/emulated/0\n",
        ]), patch.object(self.adb, "shell", side_effect=shell):
            inventory = self.adb._scan_downloads()
        self.assertEqual(len(inventory.roots), 2)
        self.assertEqual({f.path for f in inventory.files}, set(paths))
        self.assertEqual(inventory.total_bytes, 246)
        self.assertTrue(inventory.issues)

    def test_scan_rejects_bad_records_and_outside_paths(self):
        for record in (b"bad data\0", b"1 0\n/etc/passwd\0", b"1 0\n/storage/emulated/0/../oops\0"):
            with self.subTest(record=record):
                with patch.object(self.adb, "checked_shell", side_effect=[b"", b"/storage/emulated/0\n"]), patch.object(
                    self.adb, "shell", return_value=subprocess.CompletedProcess([], 0, record, b"")
                ):
                    with self.assertRaises(BackupError):
                        self.adb._scan_downloads()

    def test_scan_streams_partial_records_without_double_counting(self):
        updates = []
        raw = b"3 1700000000\n/storage/emulated/0/a.jpg\0"
        raw += b"4294967296 1700000000\n/storage/emulated/0/b.mp4\0"

        def shell(script, timeout=180, on_stdout=None):
            on_stdout(raw[:12])
            self.assertEqual(len(updates), 1)
            on_stdout(raw[:40])
            self.assertEqual(updates[-1][0], 1)
            on_stdout(raw)
            return subprocess.CompletedProcess([], 0, raw, b"")

        with patch.object(self.adb, "checked_shell", side_effect=[b"", b"/storage/emulated/0\n"]), patch.object(
            self.adb, "shell", side_effect=shell
        ):
            result = self.adb._scan_downloads(lambda *args: updates.append(args))
        self.assertEqual([item[0] for item in updates], [0, 1, 2])
        self.assertEqual(updates[-1][1], 4294967299)
        self.assertEqual(result.total_bytes, 4294967299)

    def test_exclusion_rules_keep_personal_media(self):
        for path in [
            "/storage/emulated/0/Android/data/app/photo.jpg",
            "/storage/ABCD-1234/ANDROID/OBB/app/main.obb",
            "/storage/emulated/0/App/.cache/photo.jpg",
            "/storage/emulated/0/App/code_cache/code",
            "/storage/emulated/0/DCIM/.thumbnails/photo.jpg",
            "/storage/emulated/0/Download/app.xapk",
        ]:
            self.assertTrue(excluded_app_file(path), path)
        for path in [
            "/storage/emulated/0/Android/media/app/photo.jpg",
            "/storage/emulated/0/Download/data.pdf",
            "/storage/emulated/0/Pictures/cache.jpg",
            "/storage/emulated/0/Android/database-not-data/photo.jpg",
        ]:
            self.assertFalse(excluded_app_file(path), path)

    def test_scan_prunes_technical_directories_and_counts_installers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            excluded = ["Download/Android/data/app/x.txt", "Download/Android/obb/app/x.obb",
                        "Download/App/CACHE/x.jpg", "Download/DCIM/.thumbnails/x.jpg"]
            included = ["Download/Android/media/app/x.jpg", "Download/document.pdf",
                        "Download/Android/media/app/voice.opus", "Download/WhatsApp/Media/note.mp3"]
            filtered = ["Download/app.apk"]
            for name in excluded + included + filtered:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"123")

            def shell(script, timeout=180):
                local = script.replace("/storage/emulated/0", directory)
                result = subprocess.run(["sh", "-c", local], capture_output=True, check=True)
                self.assertNotIn(b"/Android/data/", result.stdout)
                self.assertNotIn(b"/Android/obb/", result.stdout)
                self.assertNotIn(b"/CACHE/", result.stdout)
                result.stdout = result.stdout.replace(directory.encode(), b"/storage/emulated/0")
                return result

            with patch.object(self.adb, "checked_shell", side_effect=[b"", b"/storage/emulated/0\n"]), patch.object(
                self.adb, "shell", side_effect=shell
            ):
                inventory = self.adb._scan_downloads()
            self.assertEqual({f.path for f in inventory.files}, {"/storage/emulated/0/" + p for p in included})
            self.assertEqual(inventory.excluded_files, 1)
            self.assertEqual(inventory.total_bytes, 12)

    def test_app_audio_filters_removed(self):
        for directory in ["Android/media/app", "WhatsApp", "WHATSAPP BUSINESS", "Telegram",
                          "Signal", "com.example.app", "org.example", "de.example"]:
            for name in ["voice.opus", "sound.MP3", "archive.zip", "unknown"]:
                self.assertFalse(excluded_app_file(f"/storage/emulated/0/{directory}/{name}"))
            for name in ["photo.JPG", "video.mp4", "document.pdf"]:
                self.assertFalse(excluded_app_file(f"/storage/emulated/0/{directory}/{name}"))
        for directory in ["Music", "Download", "Recordings", "WhatsApp-export", "my.telegram"]:
            self.assertFalse(excluded_app_file(f"/storage/emulated/0/{directory}/voice.opus"))

    def test_remote_shell_quoting_and_hash_with_special_path(self):
        path = "/storage/emulated/0/a ' $(touch BAD)\n.jpg"
        digest = "a" * 64
        with patch("android_backup.core.run_command", return_value=subprocess.CompletedProcess([], 0, (digest + "  -\n").encode(), b"")) as run:
            self.assertEqual(self.adb.digest(path), digest)
        args = run.call_args.args[0]
        script = shlex.split(args[-1])
        self.assertEqual(script[:2], ["sh", "-c"])
        self.assertEqual(shlex.split(script[2]), ["sha256sum", "<", path])

    def test_scan_shell_protocol_runs_with_real_posix_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["normal.txt", "line\nbreak.txt", "quote' $(echo nope).txt"]
            (root / "Download").mkdir()
            for name in names:
                (root / "Download" / name).write_bytes(b"123")
            (root / "Android/media/app").mkdir(parents=True)
            (root / "Android/media/app/never-traverse.txt").write_bytes(b"app data")
            scripts = []

            def shell(script, timeout=180):
                scripts.append(script)
                local = script.replace("/storage/emulated/0", directory)
                result = subprocess.run(["sh", "-c", local], capture_output=True, check=True)
                result.stdout = result.stdout.replace(directory.encode(), b"/storage/emulated/0")
                return result

            with patch.object(self.adb, "checked_shell", side_effect=[b"", b"/storage/emulated/0\n"]), patch.object(self.adb, "shell", side_effect=shell):
                inventory = self.adb._scan_downloads()
            self.assertEqual({Path(f.path).name for f in inventory.files}, set(names))
            self.assertEqual(inventory.total_bytes, 9)

    def test_category_selection_merges_index_and_downloads(self):
        updates = []
        download = SourceFile("/storage/emulated/0/Download/extra.zip", 5, 123)

        def shell(script, timeout=180):
            if "images/media" in script:
                output = b"Row: 0 _data=/storage/emulated/0/Android/media/app/photo.jpg, _size=12, date_modified=123\n"
            elif "audio/media" in script:
                output = b"Row: 0 _data=/storage/emulated/0/WhatsApp/voice.opus, _size=9, date_modified=123\n"
            elif "/file " in script:
                self.assertIn("media_type=6", script)
                output = b"Row: 0 _data=/storage/emulated/0/Documents/a, b.pdf, _size=7, date_modified=123\n"
            elif "/downloads " in script:
                output = b"Row: 0 _data=/storage/emulated/0/Download/extra.zip, _size=5, date_modified=123\n"
            else:
                output = b"No result found.\n"
            return subprocess.CompletedProcess([], 0, output, b"")

        with patch.object(self.adb, "_scan_downloads", return_value=Inventory(
            "phone", ["/storage/emulated/0"], [download]
        )), patch.object(self.adb, "shell", side_effect=shell):
            inventory = self.adb.scan(lambda *args: updates.append(args))
        self.assertEqual(len(inventory.files), 4)
        self.assertEqual(inventory.total_bytes, 33)
        self.assertTrue(any(source.path.endswith("voice.opus") for source in inventory.files))
        self.assertIn("Files by Google", inventory.selection)
        self.assertEqual(len(updates), 10)

    def test_provider_errors_and_malformed_rows_are_not_silent(self):
        for stdout, stderr in [
            (b"", b"Error while accessing provider:media"),
            (b"Row: 0 _data=null, _size=null, date_modified=null\n", b""),
            (b"Row: 0 _data=/etc/passwd, _size=1, date_modified=123\n", b""),
            (b"Row: 0 _data=/storage/emulated/0/name\nbroken.jpg, _size=1, date_modified=123\n", b""),
        ]:
            with self.subTest(stdout=stdout, stderr=stderr), patch.object(
                self.adb, "_scan_downloads", return_value=Inventory("phone", ["/storage/emulated/0"], [])
            ), patch.object(self.adb, "shell", return_value=subprocess.CompletedProcess([], 0, stdout, stderr)):
                with self.assertRaises(BackupError):
                    self.adb.scan()

    def test_missing_index_size_is_read_from_actual_file(self):
        path = "/storage/emulated/0/Download/file.pdf"
        with patch.object(self.adb, "_scan_downloads", return_value=Inventory(
            "phone", ["/storage/emulated/0"], []
        )), patch.object(self.adb, "shell", return_value=subprocess.CompletedProcess(
            [], 0, f"Row: 0 _data={path}, _size=NULL, date_modified=123\n".encode(), b""
        )), patch.object(self.adb, "checked_shell", return_value=b"file"), patch.object(
            self.adb, "stat", return_value=SourceFile(path, 7, 123)
        ):
            inventory = self.adb.scan()
        self.assertEqual(inventory.files, [SourceFile(path, 7, 123)])
        self.assertEqual(inventory.issues, [])

    def test_download_index_directories_are_not_files(self):
        with patch.object(self.adb, "_scan_downloads", return_value=Inventory(
            "phone", ["/storage/emulated/0"], []
        )), patch.object(self.adb, "shell", return_value=subprocess.CompletedProcess(
            [], 0, b"Row: 0 _data=/storage/emulated/0/Download/Folder, _size=NULL, date_modified=123\n", b""
        )), patch.object(self.adb, "checked_shell", return_value=b"directory"):
            inventory = self.adb.scan()
        self.assertEqual(inventory.files, [])
        self.assertEqual(inventory.issues, [])


class CommandTests(unittest.TestCase):
    def test_stdout_feedback_arrives_before_command_finishes(self):
        event = threading.Event()
        updates = []

        def feedback(output):
            updates.append(output)
            event.set()

        with self.assertRaises(Cancelled):
            run_command(
                [sys.executable, "-u", "-c", "import time; print('alive'); time.sleep(60)"],
                event, on_stdout=feedback,
            )
        self.assertEqual(updates, [b"alive\n"])

    def test_cancel_and_timeout_stop_process(self):
        for cancel_first in (False, True):
            event = threading.Event()
            if cancel_first:
                event.set()
            with self.assertRaises(BackupError):
                run_command([sys.executable, "-c", "import time; time.sleep(60)"], event, timeout=0)

    def test_running_command_can_be_cancelled(self):
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                run_command([sys.executable, "-c", "import time; time.sleep(60)"], event)
        finally:
            timer.join()

    def test_file_hash_checks_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data"
            path.write_bytes(b"data")
            event = threading.Event()
            event.set()
            with self.assertRaises(Cancelled):
                file_digest(path, event)


if __name__ == "__main__":
    unittest.main()
