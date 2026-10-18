import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path

from android_backup.core import Metadata


@unittest.skipUnless(shutil.which("exiftool"), "ExifTool not installed")
class ExifToolIntegrationTests(unittest.TestCase):
    def test_real_quicktime_creation_year(self):
        def atom(kind, content):
            return struct.pack(">I", len(content) + 8) + kind + content

        seconds = int(
            (datetime(2021, 12, 31, 23, 59, tzinfo=timezone.utc)
             - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds()
        )
        header = struct.pack(">IIIII", 0, seconds, seconds, 1000, 1000)
        header += struct.pack(">IHHII", 65536, 256, 0, 0, 0)
        header += struct.pack(">9I", 65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)
        header += b"\0" * 24 + struct.pack(">I", 1)
        movie = atom(b"ftyp", b"isom\0\0\0\0isommp42") + atom(b"moov", atom(b"mvhd", header))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "movie.mp4"
            path.write_bytes(movie)
            category, year, issues = Metadata(threading.Event()).inspect(path, "movie.mp4", 1700000000)
            self.assertEqual((category, year), ("Videos", "2021"))
            self.assertEqual(issues, [])

    def test_real_image_metadata_and_extensionless_content(self):
        def chunk(kind, content):
            return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))

        image = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
            + chunk(b"IEND", b"")
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.png"
            path.write_bytes(image)
            subprocess.run(
                ["exiftool", "-overwrite_original", "-DateTimeOriginal=2020:12:31 23:59:59", str(path)],
                check=True, capture_output=True,
            )
            path = path.rename(Path(folder) / "transfer")
            category, year, issues = Metadata(threading.Event()).inspect(path, "download.bin", 1700000000)
            self.assertEqual((category, year), ("Fotos", "2020"))
            self.assertEqual(issues, [])

    def test_unknown_file_is_not_discarded_and_txt_remains_document(self):
        with tempfile.TemporaryDirectory() as folder:
            unknown = Path(folder) / "unknown.bin"
            unknown.write_bytes(b"\x00\x01\x02\x03\x04\x05")
            category, year, issues = Metadata(threading.Event()).inspect(unknown, "unknown.bin", 0)
            self.assertEqual((category, year), ("Sonstige", None))
            self.assertTrue(issues)
            text = Path(folder) / "note.txt"
            text.write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                Metadata(threading.Event()).inspect(text, "note.txt", 0)[:2], ("Dokumente", None)
            )


if __name__ == "__main__":
    unittest.main()
