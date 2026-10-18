from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable


class BackupError(Exception):
    pass


class Cancelled(BackupError):
    pass


class SourceChanged(BackupError):
    pass


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("Vorgang abgebrochen.")


def run_command(
    args: list[str], cancel: threading.Event, timeout: int = 180,
    on_stdout: Callable[[bytes], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    check_cancel(cancel)
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise BackupError(f"{args[0]} konnte nicht gestartet werden: {exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise BackupError(f"Zeitlimit für {args[0]} überschritten.")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                if on_stdout:
                    on_stdout(stdout)
                return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired as exc:
                if on_stdout and exc.output:
                    on_stdout(exc.output)
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def checked_command(
    args: list[str], cancel: threading.Event, timeout: int = 180
) -> bytes:
    result = run_command(args, cancel, timeout)
    if result.returncode:
        message = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise BackupError(f"{args[0]} (Exit {result.returncode}): {message}")
    return result.stdout


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    mtime: int


EXCLUDED_DIRECTORIES = {"cache", ".cache", "code_cache", ".thumbnails"}
EXCLUDED_EXTENSIONS = {".apk", ".apks", ".xapk", ".aab", ".dex", ".odex", ".vdex"}


def excluded_app_file(path: str) -> bool:
    source = PurePosixPath(path)
    directories = [part.lower() for part in source.parts[:-1]]
    return (
        any(part in EXCLUDED_DIRECTORIES for part in directories)
        or any(
            parent == "android" and child in {"data", "obb"}
            for parent, child in zip(directories, directories[1:])
        )
        or source.suffix.lower() in EXCLUDED_EXTENSIONS
    )


@dataclass
class Inventory:
    serial: str
    roots: list[str]
    files: list[SourceFile]
    issues: list[str] = field(default_factory=list)
    excluded_files: int = 0
    selection: str = "Android-Medienindex plus Download-Ordner; kann von Files by Google abweichen."

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)


class Adb:
    def __init__(self, serial: str, cancel: threading.Event):
        self.serial = serial
        self.cancel = cancel

    @staticmethod
    def devices(cancel: threading.Event) -> list[Device]:
        output = checked_command(["adb", "devices", "-l"], cancel).decode("utf-8")
        devices = []
        for line in output.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                model = next(
                    (value.removeprefix("model:") for value in fields[2:] if value.startswith("model:")),
                    fields[0],
                )
                devices.append(Device(fields[0], fields[1], model.replace("_", " ")))
        return devices

    def shell(
        self, script: str, timeout: int = 180,
        on_stdout: Callable[[bytes], None] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return run_command(
            ["adb", "-s", self.serial, "shell", "-T", "sh -c " + shlex.quote(script)],
            self.cancel,
            timeout,
            on_stdout=on_stdout,
        )

    def checked_shell(self, script: str, timeout: int = 180) -> bytes:
        result = self.shell(script, timeout)
        if result.returncode:
            error = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise BackupError(f"ADB auf {self.serial}: {error}")
        return result.stdout

    def scan(
        self, progress: Callable[[int, int, str], None] | None = None,
    ) -> Inventory:
        inventory = self._scan_downloads(progress)
        files = {source.path: source for source in inventory.files}
        excluded = inventory.excluded_files
        collections = [
            ("Bilder", "images/media", ""),
            ("Videos", "video/media", ""),
            ("Audio", "audio/media", ""),
            ("Dokumente", "file", " AND media_type=6"),
            ("Downloads", "downloads", ""),
        ]
        for label, collection, extra in collections:
            check_cancel(self.cancel)
            if progress:
                progress(len(files), sum(item.size for item in files.values()), f"Android-Kategorie: {label}")
            query = (
                f"content query --uri content://media/external/{collection}"
                " --projection _data:_size:date_modified"
                f" --where {shlex.quote('is_pending=0 AND is_trashed=0' + extra)}"
            )
            result = self.shell(query, timeout=180)
            text = result.stdout.decode("utf-8")
            if result.returncode or result.stderr or "Error while accessing provider" in text:
                error = (result.stderr or result.stdout).decode("utf-8", errors="replace")
                raise BackupError(f"Android-Kategorie {label} konnte nicht gelesen werden: {error}")
            for line in text.splitlines():
                if not line.strip() or line.strip() == "No result found.":
                    continue
                match = re.fullmatch(
                    r"Row: \d+ _data=(.+), _size=(\d+|NULL|null), date_modified=(\d+|NULL|null)", line
                )
                if not match:
                    raise BackupError(
                        f"Ungültiger Datensatz in Android-Kategorie {label}; keine vollständige Auswahl möglich."
                    )
                path, size, mtime = match.groups()
                if (
                    ".." in PurePosixPath(path).parts
                    or not any(PurePosixPath(path).is_relative_to(root) for root in inventory.roots)
                ):
                    raise BackupError(f"Android-Kategorie {label} enthält einen nicht unterstützten Speicherpfad.")
                if excluded_app_file(path):
                    excluded += 1
                    continue
                if not size.isdigit() or not mtime.isdigit():
                    try:
                        kind = self.checked_shell(
                            f"if test -d {shlex.quote(path)}; then printf directory; "
                            f"elif test -f {shlex.quote(path)}; then printf file; else exit 1; fi"
                        )
                        if kind == b"directory":
                            continue
                        if kind != b"file":
                            raise BackupError("Dateityp des Indexeintrags konnte nicht festgestellt werden.")
                        source = self.stat(path)
                    except BackupError as exc:
                        inventory.issues.append(f"Unvollständiger Android-Indexeintrag {path}: {exc}")
                        continue
                else:
                    source = SourceFile(path, int(size), int(mtime))
                files[path] = source
            if progress:
                progress(len(files), sum(item.size for item in files.values()), f"Kategorie {label} erfasst")
        inventory.files = sorted(files.values(), key=lambda item: item.path)
        inventory.excluded_files = excluded
        return inventory

    def _scan_downloads(
        self, progress: Callable[[int, int, str], None] | None = None,
    ) -> Inventory:
        self.checked_shell("command -v find && command -v stat && command -v sha256sum")
        root_script = """
set -e
test -d /storage/emulated/0
for root in /storage/emulated/0 /storage/????-????; do
    test -d "$root" || continue
    readlink -f "$root"
done
"""
        raw_roots = self.checked_shell(root_script).decode("utf-8").splitlines()
        roots = sorted(set(raw_roots))
        if not roots or any(not root.startswith("/storage/") for root in roots):
            raise BackupError("Keine gültigen gemeinsamen Speicher gefunden.")
        files: dict[str, SourceFile] = {}
        issues = []
        total_bytes = 0
        excluded_paths: set[str] = set()
        for root in roots:
            consumed = 0

            def consume(output: bytes) -> None:
                nonlocal consumed, total_bytes
                last_path = ""
                while True:
                    end = output.find(b"\0", consumed)
                    if end < 0:
                        break
                    record = output[consumed:end]
                    consumed = end + 1
                    if not record:
                        continue
                    try:
                        header, raw_path = record.split(b"\n", 1)
                        size, mtime = map(int, header.split())
                        path = raw_path.decode("utf-8")
                    except (ValueError, UnicodeError) as exc:
                        raise BackupError("Ungültige ADB-Dateiliste; Sicherung nicht gestartet.") from exc
                    if (
                        size < 0 or ".." in PurePosixPath(path).parts
                        or not PurePosixPath(path).is_relative_to(root)
                    ):
                        raise BackupError(f"Ungültiger Quellpfad oder Dateigröße: {path!r}")
                    if excluded_app_file(path):
                        excluded_paths.add(path)
                        continue
                    previous = files.get(path)
                    total_bytes += size - (previous.size if previous else 0)
                    files[path] = SourceFile(path, size, mtime)
                    last_path = path
                if progress and last_path:
                    progress(len(files), total_bytes, last_path)

            if progress:
                progress(len(files), total_bytes, f"Download-Ordner durchsuchen: {root}")
            prune = " -o ".join(
                ["-ipath '*/Android/data'", "-ipath '*/Android/obb'"]
                + [f"-iname {shlex.quote(name)}" for name in sorted(EXCLUDED_DIRECTORIES)]
            )
            script = (
                f"for d in {shlex.quote(root + '/Download')} {shlex.quote(root + '/Downloads')}; do "
                f'test -d "$d" || continue; find "$d" -type d \\( {prune} \\) -prune -o -type f -exec sh -c '
                + shlex.quote(
                    'for p do h=$(stat -c "%s %Y" "$p") || continue; '
                    'printf "%s\\n%s\\000" "$h" "$p"; done'
                )
                + " sh {} +; done"
            )
            if progress:
                result = self.shell(script, timeout=1800, on_stdout=consume)
            else:
                result = self.shell(script, timeout=1800)
            consume(result.stdout)
            if consumed != len(result.stdout):
                raise BackupError("Unvollständiger Datensatz in der ADB-Dateiliste.")
            if result.stderr:
                issues.append(result.stderr.decode("utf-8", errors="replace").strip())
            if result.returncode:
                issues.append(f"Bestandsaufnahme von {root} unvollständig (Exit {result.returncode}).")
        return Inventory(
            self.serial, roots, sorted(files.values(), key=lambda item: item.path),
            issues, len(excluded_paths),
        )

    def stat(self, path: str) -> SourceFile:
        output = self.checked_shell(f"stat -c '%s %Y' {shlex.quote(path)}")
        try:
            size, mtime = map(int, output.split())
            if size < 0:
                raise ValueError("negative size")
        except ValueError as exc:
            raise BackupError(f"Ungültige Dateiinformationen für {path!r}") from exc
        return SourceFile(path, size, mtime)

    def digest(self, path: str) -> str:
        output = self.checked_shell(f"sha256sum < {shlex.quote(path)}", timeout=3600)
        digest = output[:64].decode("ascii", errors="replace")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise BackupError(f"Ungültige SHA-256-Prüfsumme für {path!r}")
        return digest

    def pull(self, path: str, destination: Path) -> None:
        checked_command(
            ["adb", "-s", self.serial, "pull", "-a", path, str(destination)],
            self.cancel,
            timeout=3600,
        )


def file_digest(path: Path, cancel: threading.Event) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancel(cancel)
            digest.update(chunk)
    return digest.hexdigest()


EXTENSIONS = {
    "Fotos": "jpg jpeg jpe png gif webp heic heif avif bmp tif tiff dng raw cr2 cr3 nef arw rw2 orf raf svg",
    "Videos": "mp4 m4v mov mkv avi webm 3gp 3g2 mpg mpeg mts m2ts ts vob wmv flv",
    "Audio": "mp3 m4a aac wav flac ogg opus wma aiff amr mid midi ape",
    "Dokumente": "pdf txt md csv json xml html htm doc docx odt rtf xls xlsx ods ppt pptx odp epub mobi",
    "Archive": "zip 7z rar tar gz bz2 xz zst tgz apk",
}
EXTENSION_TYPES = {
    extension: category for category, extensions in EXTENSIONS.items() for extension in extensions.split()
}


class Metadata:
    def __init__(self, cancel: threading.Event):
        self.cancel = cancel

    def inspect(self, path: Path, original_name: str, mtime: int) -> tuple[str, str | None, list[str]]:
        result = run_command(
            [
                "exiftool", "-json", "-n", "-charset", "filename=UTF8",
                "-MIMEType", "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
                "-TrackCreateDate", "-Error", "-Warning", str(path),
            ],
            self.cancel,
        )
        if result.returncode not in (0, 1):
            raise BackupError(f"ExifTool: {result.stderr.decode('utf-8', errors='replace')}")
        try:
            data = json.loads(result.stdout)[0]
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, IndexError, TypeError) as exc:
            raise BackupError(f"Ungültige ExifTool-Ausgabe für {original_name!r}") from exc
        issues = [f"{original_name}: {data[key]}" for key in ("Error", "Warning") if key in data]
        if result.stderr:
            issues.append(f"{original_name}: {result.stderr.decode('utf-8', errors='replace').strip()}")
        if result.returncode and not issues:
            raise BackupError(f"ExifTool fehlgeschlagen für {original_name!r} (Exit 1).")
        extension = PurePosixPath(original_name).suffix.lower().lstrip(".")
        category = EXTENSION_TYPES.get(extension, "Sonstige")
        mime = data.get("MIMEType", "")
        if mime.startswith("image/"):
            category = "Fotos"
        elif mime.startswith("video/"):
            category = "Videos"
        elif mime.startswith("audio/"):
            category = "Audio"
        elif mime == "application/pdf":
            category = "Dokumente"
        if category not in ("Fotos", "Videos"):
            return category, None, issues
        for key in ("DateTimeOriginal", "CreateDate", "MediaCreateDate", "TrackCreateDate"):
            value = data.get(key)
            if value is None:
                continue
            match = re.match(
                r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(value)
            )
            if match:
                try:
                    captured = datetime(*map(int, match.groups()))
                    if 1900 <= captured.year <= datetime.now().year + 1:
                        return category, str(captured.year), issues
                except ValueError:
                    pass
            issues.append(f"{original_name}: Ungültiges Aufnahmedatum {key}={value!r}.")
        if mtime > 0:
            try:
                modified = datetime.fromtimestamp(mtime)
                if 1900 <= modified.year <= datetime.now().year + 1:
                    return category, str(modified.year), issues
            except (ValueError, OverflowError, OSError):
                pass
        issues.append(f"{original_name}: Kein verlässlich bestimmbares Jahr; Ablage unter Unbekannt.")
        return category, "Unbekannt", issues


@dataclass
class Report:
    serial: str
    roots: list[str]
    started: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    finished: str = ""
    status: str = "running"
    total: int = 0
    saved: int = 0
    existing: int = 0
    excluded_files: int = 0
    failed: int = 0
    processed_bytes: int = 0
    issues: list[str] = field(default_factory=list)
    report_path: str = ""
    selection: str = ""
    errors: list[str] = field(default_factory=list)
    metadata_warnings: dict[str, list[str]] = field(default_factory=dict)
    empty_files: list[str] = field(default_factory=list)
    verification: str = "full"
    quick_skipped: int = 0

    @property
    def outcome(self) -> str:
        if self.status == "cancelled":
            return "Abgebrochen – Sicherung nicht vollständig"
        if self.status == "failed":
            return "Sicherung gestoppt – nicht vollständig"
        if self.failed or self.errors:
            return "Sicherung mit Fehlern – nicht vollständig"
        if self.metadata_warnings or self.empty_files:
            return "Alle ausgewählten Dateien gesichert – mit Warnungen"
        return "Vollständig im erfassten, zugänglichen Umfang"


def load_report(path: Path) -> Report:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Bericht ist kein JSON-Objekt.")
        for key in ("serial", "status"):
            if not isinstance(data.get(key), str):
                raise ValueError(f"Ungültiges Feld: {key}")
        if data["status"] not in {"complete", "warnings", "issues", "cancelled", "failed"}:
            raise ValueError("Ungültiger Abschlussstatus.")
        for key in ("total", "saved", "existing", "failed"):
            if type(data.get(key)) is not int or data[key] < 0:
                raise ValueError(f"Ungültiger Zähler: {key}")
        for key in ("roots", "issues"):
            if not isinstance(data.get(key), list) or not all(isinstance(item, str) for item in data[key]):
                raise ValueError(f"Ungültiges Feld: {key}")
        if data["saved"] + data["existing"] + data["failed"] > data["total"]:
            raise ValueError("Dateizähler sind widersprüchlich.")
        report = Report(
            data["serial"], data["roots"], status=data["status"], total=data["total"],
            saved=data["saved"], existing=data["existing"], failed=data["failed"],
            issues=data["issues"], report_path=str(path),
        )
        selection = data.get("selection", "")
        excluded = data.get("excluded_files", 0)
        if not isinstance(selection, str) or type(excluded) is not int or excluded < 0:
            raise ValueError("Ungültige Auswahl oder Ausschlussanzahl.")
        report.selection = selection
        report.excluded_files = excluded
        verification = data.get("verification", "full")
        quick_skipped = data.get("quick_skipped", 0)
        if verification not in ("full", "quick") or type(quick_skipped) is not int or not 0 <= quick_skipped <= report.existing:
            raise ValueError("Ungültiger Prüfmodus oder Schnelllauf-Zähler.")
        report.verification = verification
        report.quick_skipped = quick_skipped
        if "metadata_warnings" in data:
            warnings = data["metadata_warnings"]
            if not isinstance(warnings, dict) or not all(
                isinstance(name, str) and isinstance(messages, list)
                and all(isinstance(message, str) for message in messages)
                for name, messages in warnings.items()
            ):
                raise ValueError("Ungültige Metadatenwarnungen.")
            report.metadata_warnings = warnings
            for key in ("errors", "empty_files"):
                value = data.get(key)
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f"Ungültiges Feld: {key}")
                setattr(report, key, value)
        else:
            # Older reports stored every diagnostic in one list; unknown messages remain errors.
            markers = (
                "Ungültiges Aufnahmedatum", "Kein verlässlich bestimmbares Jahr",
                "Bad length ICC_Profile", "[Minor]", "[minor]", "Invalid EXIF text encoding",
            )
            for issue in report.issues:
                name, separator, message = issue.partition(": ")
                if separator and message == "File is empty":
                    report.empty_files.append(name)
                elif separator and message.startswith(markers):
                    report.metadata_warnings.setdefault(name, []).append(message)
                else:
                    report.errors.append(issue)
        return report
    except (ValueError, TypeError, OSError) as exc:
        raise BackupError(f"Bericht konnte nicht gelesen werden: {exc}") from exc


def publish(temporary: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise BackupError("Diese Linux-Laufzeit unterstützt renameat2 nicht.") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), str(destination))
        raise OSError(code, os.strerror(code), str(destination))
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Store:
    def __init__(self, target: Path, cancel: threading.Event):
        self.target = target.expanduser().resolve()
        self.cancel = cancel
        self.database: sqlite3.Connection | None = None
        self.lock_stream = None
        self.staging: Path | None = None
        self.quick_seen: list[tuple[str, str, str, str]] = []

    def __enter__(self) -> Store:
        import fcntl

        self.target.mkdir(parents=True, exist_ok=True)
        control = self.target / ".backup"
        if control.is_symlink():
            raise BackupError("Das Steuerverzeichnis .backup darf kein symbolischer Link sein.")
        control.mkdir(exist_ok=True)
        lock = control / "lock"
        if lock.is_symlink():
            raise BackupError("Die Sperrdatei darf kein symbolischer Link sein.")
        self.lock_stream = lock.open("a+b")
        try:
            fcntl.flock(self.lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_stream.close()
            raise BackupError("Dieses Backup-Ziel wird bereits von einer anderen Sicherung verwendet.") from exc
        except OSError as exc:
            self.lock_stream.close()
            raise BackupError(f"Backup-Ziel konnte nicht exklusiv gesperrt werden: {exc}") from exc
        try:
            database_path = control / "index.sqlite3"
            if database_path.is_symlink():
                raise BackupError("Die Backup-Datenbank darf kein symbolischer Link sein.")
            self.database = sqlite3.connect(database_path)
            self.database.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS contents (
                    digest TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    serial TEXT NOT NULL, source_path TEXT NOT NULL, digest TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime INTEGER NOT NULL, first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL, PRIMARY KEY (serial, source_path, digest),
                    FOREIGN KEY (digest) REFERENCES contents(digest)
                );
                PRAGMA foreign_keys=ON;
            """)
            self.staging = control / ("staging-" + uuid.uuid4().hex)
            self.staging.mkdir()
        except (OSError, sqlite3.Error, BackupError):
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.database:
                self.database.close()
            if self.staging:
                shutil.rmtree(self.staging)
        finally:
            if self.lock_stream:
                self.lock_stream.close()

    @property
    def db(self) -> sqlite3.Connection:
        if self.database is None:
            raise BackupError("Backup-Datenbank ist nicht geöffnet.")
        return self.database

    def existing(self, digest: str, verify_hash: bool = True) -> Path | None:
        row = self.db.execute("SELECT relative_path, size FROM contents WHERE digest=?", (digest,)).fetchone()
        if not row:
            return None
        relative = Path(row[0])
        if relative.is_absolute() or ".." in relative.parts:
            raise BackupError("Ungültiger Zielpfad im Backup-Verzeichnis.")
        path = self.target / relative
        if (
            not path.resolve().is_relative_to(self.target)
            or any(parent.is_symlink() for parent in (path, *path.parents) if parent != self.target)
        ):
            raise BackupError(f"Unsicherer Backup-Pfad: {path}")
        if (
            not path.is_file() or path.stat().st_size != row[1]
            or (verify_hash and file_digest(path, self.cancel) != digest)
        ):
            raise BackupError(f"Vorhandenes Backup fehlt oder ist beschädigt: {path}. Nicht überschrieben.")
        return path

    def quick_match(self, serial: str, source: SourceFile) -> bool:
        row = self.db.execute(
            """SELECT digest FROM sources
            WHERE serial=? AND source_path=? AND size=? AND mtime=?
            ORDER BY last_seen DESC LIMIT 1""",
            (serial, source.path, source.size, source.mtime),
        ).fetchone()
        if not row:
            return False
        if self.existing(row[0], verify_hash=False) is None:
            return False
        self.quick_seen.append(
            (datetime.now().astimezone().isoformat(), serial, source.path, row[0])
        )
        if len(self.quick_seen) >= 100:
            self.flush_quick_seen()
        return True

    def flush_quick_seen(self) -> None:
        if self.quick_seen:
            with self.db:
                self.db.executemany(
                    "UPDATE sources SET last_seen=? WHERE serial=? AND source_path=? AND digest=?",
                    self.quick_seen,
                )
            self.quick_seen.clear()

    def place(self, temporary: Path, source: SourceFile, digest: str, category: str, year: str | None) -> Path:
        directory = self.target / category
        if year:
            directory /= year
        current = self.target
        for part in directory.relative_to(self.target).parts:
            current /= part
            if current.is_symlink():
                raise BackupError(f"Zielordner darf kein symbolischer Link sein: {current}")
            current.mkdir(exist_ok=True)
        original = PurePosixPath(source.path).name
        # Reserve room for a digest suffix even on filesystems with 255-byte names.
        name = original
        if len(os.fsencode(name)) > 180:
            extension = PurePosixPath(name).suffix
            name = os.fsencode(PurePosixPath(name).stem)[:120].decode("utf-8", errors="ignore")
            name += "__" + digest[:16] + extension[:20]
        destination = directory / name
        number = 0
        while True:
            check_cancel(self.cancel)
            if destination.is_symlink():
                raise BackupError(f"Zieldatei darf kein symbolischer Link sein: {destination}")
            if destination.exists():
                if destination.is_file() and file_digest(destination, self.cancel) == digest:
                    break
            else:
                try:
                    # Linux RENAME_NOREPLACE publishes atomically, including on drives without hardlinks.
                    publish(temporary, destination)
                    break
                except FileExistsError:
                    continue
            number += 1
            suffix = "" if number == 1 else f"_{number}"
            base = Path(name)
            destination = directory / f"{base.stem}__{digest[:16]}{suffix}{base.suffix}"
        relative = str(destination.relative_to(self.target))
        with self.db:
            self.db.execute(
                "INSERT INTO contents(digest, relative_path, size) VALUES (?, ?, ?)",
                (digest, relative, source.size),
            )
        return destination

    def remember(self, serial: str, source: SourceFile, digest: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.db:
            self.db.execute(
                """INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(serial, source_path, digest)
                DO UPDATE SET size=excluded.size, mtime=excluded.mtime, last_seen=excluded.last_seen""",
                (serial, source.path, digest, source.size, source.mtime, now, now),
            )

    def save_report(self, report: Report) -> None:
        report.finished = datetime.now().astimezone().isoformat()
        destination = self.target / ".backup" / ("report-" + uuid.uuid4().hex + ".json")
        report.report_path = str(destination)
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(asdict(report), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())


class Backup:
    def __init__(
        self, adb: Adb, metadata: Metadata, cancel: threading.Event,
        progress: Callable[[int, int, str], None] = lambda done, total, path: None,
        full_verification: bool = False,
    ):
        self.adb = adb
        self.metadata = metadata
        self.cancel = cancel
        self.progress = progress
        self.full_verification = full_verification

    def one(self, store: Store, snapshot: SourceFile) -> tuple[bool, SourceFile, list[str]]:
        if store.staging is None:
            raise BackupError("Temporärer Sicherungsbereich fehlt.")
        extension = PurePosixPath(snapshot.path).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,20}", extension):
            extension = ""
        temporary = store.staging / ("transfer" + extension)
        for attempt in range(2):
            check_cancel(self.cancel)
            try:
                before = self.adb.stat(snapshot.path)
                digest = self.adb.digest(snapshot.path)
                existing = store.existing(digest)
                if existing is None:
                    if shutil.disk_usage(store.target).free < before.size + 16 * 1024 * 1024:
                        raise BackupError("Zu wenig freier Speicherplatz für die nächste Datei.")
                    self.adb.pull(snapshot.path, temporary)
                    if temporary.stat().st_size != before.size or file_digest(temporary, self.cancel) != digest:
                        raise SourceChanged("Übertragung oder Quelldatei hat sich verändert.")
                after_digest = self.adb.digest(snapshot.path)
                after = self.adb.stat(snapshot.path)
                if before != after or digest != after_digest:
                    raise SourceChanged("Quelldatei wurde während der Sicherung verändert.")
                issues: list[str] = []
                if existing is None:
                    category, year, issues = self.metadata.inspect(temporary, snapshot.path, before.mtime)
                    os.utime(temporary, (before.mtime, before.mtime))
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    store.place(temporary, before, digest, category, year)
                store.remember(self.adb.serial, before, digest)
                return existing is None, before, issues
            except SourceChanged:
                if attempt == 1:
                    raise
            finally:
                temporary.unlink(missing_ok=True)
        raise BackupError("Datei konnte nicht gesichert werden.")

    def run(self, inventory: Inventory, target: Path) -> Report:
        if inventory.serial != self.adb.serial:
            raise BackupError("Bestandsaufnahme gehört zu einem anderen Gerät.")
        files = [source for source in inventory.files if not excluded_app_file(source.path)]
        report = Report(
            self.adb.serial, inventory.roots, total=len(files), issues=list(inventory.issues),
            excluded_files=inventory.excluded_files + len(inventory.files) - len(files),
            selection=inventory.selection,
            errors=list(inventory.issues),
            verification="full" if self.full_verification else "quick",
        )
        with Store(target, self.cancel) as store:
            try:
                for index, source in enumerate(files):
                    check_cancel(self.cancel)
                    self.progress(index, report.total, source.path)
                    try:
                        actual = self.adb.stat(source.path) if not self.full_verification else source
                        if not self.full_verification and store.quick_match(self.adb.serial, actual):
                            saved, issues = False, []
                            report.quick_skipped += 1
                        else:
                            saved, actual, issues = self.one(store, source)
                        report.saved += int(saved)
                        report.existing += int(not saved)
                        report.processed_bytes += actual.size
                        report.issues.extend(issues)
                        if actual.size == 0:
                            report.empty_files.append(actual.path)
                        warnings = [
                            issue.removeprefix(actual.path + ": ")
                            for issue in issues
                            if issue.removeprefix(actual.path + ": ") != "File is empty"
                        ]
                        if warnings:
                            report.metadata_warnings[actual.path] = warnings
                    except Cancelled:
                        raise
                    except (BackupError, OSError) as exc:
                        report.failed += 1
                        report.issues.append(f"{source.path}: {exc}")
                        report.errors.append(f"{source.path}: {exc}")
                    self.progress(index + 1, report.total, source.path)
                report.status = (
                    "issues" if report.errors or report.failed
                    else "warnings" if report.metadata_warnings or report.empty_files
                    else "complete"
                )
            except Cancelled:
                report.status = "cancelled"
                report.issues.append("Vom Benutzer abgebrochen; verbleibende Dateien wurden nicht gesichert.")
            except (OSError, sqlite3.Error, BackupError) as exc:
                report.status = "failed"
                report.issues.append(f"Sicherung gestoppt: {exc}")
                report.errors.append(f"Sicherung gestoppt: {exc}")
            try:
                store.flush_quick_seen()
            except sqlite3.Error as exc:
                report.status = "failed"
                message = f"Sichtungszeitpunkte konnten nicht gespeichert werden: {exc}"
                report.errors.append(message)
                report.issues.append(message)
            store.save_report(report)
        return report
