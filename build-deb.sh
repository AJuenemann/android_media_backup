#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
    printf 'Fehler: %s\n' "$*" >&2
    exit 1
}

for command in dpkg-deb python3 desktop-file-validate; do
    command -v "$command" >/dev/null 2>&1 || fail "Benötigtes Systemprogramm fehlt: $command"
done

python3 -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Python 3.11 oder neuer wird zum Paketbau benötigt."
[[ "$(dpkg --print-architecture)" == amd64 ]] \
    || fail "Der Paketbau wird nur auf amd64 unterstützt."

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"
version="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
stage="$work_dir/package"
mkdir -p \
    "$stage/DEBIAN" \
    "$stage/usr/bin" \
    "$stage/usr/lib/android-media-backup" \
    "$stage/usr/share/android-media-backup" \
    "$stage/usr/share/applications" \
    "$stage/usr/share/icons/hicolor/scalable/apps"
python3 - <<'PY' > "$stage/usr/share/android-media-backup/requirements.txt"
import tomllib

dependencies = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
if not dependencies:
    raise SystemExit("pyproject.toml enthält keine Python-Abhängigkeiten.")
print("\n".join(dependencies))
PY
cp -a "$project_dir/android_backup" "$stage/usr/lib/android-media-backup/"
find "$stage/usr/lib/android-media-backup/android_backup" -type d -name __pycache__ -prune -exec rm -rf -- {} +
cat > "$stage/DEBIAN/control" <<CONTROL
Package: android-media-backup
Version: $version
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Android Media Backup <android-media-backup@localhost>
Depends: python3 (>= 3.11), python3-venv, adb, desktop-file-utils, libimage-exiftool-perl, libdbus-1-3, libegl1, libfontconfig1, libfreetype6, libgl1, libglib2.0-0t64, libice6, libsm6, libwayland-client0, libwayland-cursor0, libx11-6, libx11-xcb1, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-shape0, libxcb-shm0, libxcb-sync1, libxcb-util1, libxcb-xfixes0, libxcb-xinerama0, libxcb-xkb1, libxkbcommon0, libxkbcommon-x11-0, libzstd1
Description: Local, verified and additive Android media backup
 Graphical Android media backup over USB using ADB.
CONTROL
cat > "$stage/DEBIAN/postinst" <<POSTINST
#!/bin/sh
set -e
case "\${1:-}" in
    configure)
        runtime_dir=/opt/android-media-backup/venv
        /usr/bin/python3 -m venv "\$runtime_dir"
        "\$runtime_dir/bin/python" -m pip install --disable-pip-version-check --no-input --only-binary=:all: --no-cache-dir -r /usr/share/android-media-backup/requirements.txt
        ;;
esac
exit 0
POSTINST
cat > "$stage/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
case "${1:-}" in
    remove|purge|disappear) rm -rf -- /opt/android-media-backup/venv ;;
esac
exit 0
POSTRM
chmod 755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/postrm"
sh -n "$stage/DEBIAN/postinst"
sh -n "$stage/DEBIAN/postrm"
cat > "$stage/usr/bin/android-backup" <<'LAUNCHER'
#!/bin/sh
PYTHONPATH="/usr/lib/android-media-backup${PYTHONPATH:+:$PYTHONPATH}" \
    exec /opt/android-media-backup/venv/bin/python -m android_backup "$@"
LAUNCHER
chmod 755 "$stage/usr/bin/android-backup"
install -Dm644 "$project_dir/android-media-backup.desktop" \
    "$stage/usr/share/applications/android-media-backup.desktop"
install -Dm644 "$project_dir/android_backup/backup.svg" \
    "$stage/usr/share/icons/hicolor/scalable/apps/android-media-backup.svg"
desktop-file-validate "$stage/usr/share/applications/android-media-backup.desktop"

output_dir="$project_dir/dist"
mkdir -p "$output_dir"
package="$output_dir/android-media-backup_${version}_amd64.deb"
dpkg-deb --root-owner-group --build "$stage" "$package"
[[ "$(dpkg-deb --field "$package" Package)" == android-media-backup \
    && "$(dpkg-deb --field "$package" Version)" == "$version" \
    && "$(dpkg-deb --field "$package" Architecture)" == amd64 ]] \
    || fail "Paketmetadaten stimmen nicht mit Projektname, Version oder Architektur überein."
contents="$(dpkg-deb --contents "$package")"
for path in \
    ./usr/bin/android-backup \
    ./usr/lib/android-media-backup/android_backup/__main__.py \
    ./usr/share/android-media-backup/requirements.txt \
    ./usr/share/applications/android-media-backup.desktop \
    ./usr/share/icons/hicolor/scalable/apps/android-media-backup.svg; do
    grep -Fq "$path" <<<"$contents" || fail "Datei fehlt im Paket: $path"
done
[[ -x "$stage/DEBIAN/postinst" && -x "$stage/DEBIAN/postrm" ]] \
    || fail "Post-Installations- oder Entfernungs-Skript fehlt."
printf 'Paket erstellt: %s\n' "$package"
