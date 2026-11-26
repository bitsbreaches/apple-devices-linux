#!/usr/bin/env bash
# Install Device Dock into a prefix (default: ~/.local).
#
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"
# A private application directory lives in lib/, not share/: share/<app> is the user's
# XDG data directory, and the backup library must survive uninstall and re-install.
APPDIR="$PREFIX/lib/device-dock"
LEGACY_APPDIR="$PREFIX/share/devicedock"
OLD_APPDIR="$PREFIX/lib/devicedock"
BINDIR="$PREFIX/bin"
DATADIR="$PREFIX/share"
DATAHOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BACKUPDIR="$DATAHOME/devicedock/Backups"
OLD_BACKUPDIR="$DATAHOME/appledevices/Backups"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$SCRIPT_DIR/.." && pwd)"

say() { printf '%s\n' "$*"; }

if [ ! -f "$SRC/LICENSE" ] || [ ! -f "$SRC/THIRD_PARTY_NOTICES.md" ]; then
  say "error: LICENSE and THIRD_PARTY_NOTICES.md must exist in $SRC" >&2
  exit 1
fi

# Refuse to destroy an older layout that now holds user backups.
if [ -d "$LEGACY_APPDIR/Backups" ]; then
  say "error: $LEGACY_APPDIR/Backups contains backups from a previous installation." >&2
  say "       Move that folder somewhere safe, then run this script again." >&2
  exit 1
fi
if [ -d "$LEGACY_APPDIR" ]; then
  say "Removing the previous installation at $LEGACY_APPDIR"
  rm -rf "$LEGACY_APPDIR"
fi

# A copy installed by an older release lived in lib/devicedock. Only program files and a
# virtual environment are there, so it is safe to replace.
if [ -d "$OLD_APPDIR" ]; then
  say "Removing the previous program files at $OLD_APPDIR"
  rm -rf "$OLD_APPDIR"
fi

say "Installing Device Dock to $APPDIR (prefix: $PREFIX)"

mkdir -p "$APPDIR" "$BINDIR" "$DATADIR/applications"

# Backup library: created here so "Create backup" always has a destination that exists.
# Backups from an earlier installation are moved, never copied or deleted, so the library
# survives the rename without duplicating a potentially large directory.
if [ -d "$OLD_BACKUPDIR" ] && [ ! -e "$BACKUPDIR" ]; then
  mkdir -p "$(dirname "$BACKUPDIR")"
  say "Moving the existing backup library to $BACKUPDIR"
  mv "$OLD_BACKUPDIR" "$BACKUPDIR" 2>/dev/null || {
    say "warning: could not move $OLD_BACKUPDIR; it remains in use" >&2
  }
fi
mkdir -p "$BACKUPDIR"
say "Backup library: $BACKUPDIR"

# Application payload.
rm -rf "$APPDIR/devicedock"
cp -a "$SRC/devicedock" "$APPDIR/devicedock"
find "$APPDIR/devicedock" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp -a "$SRC/requirements.txt" "$APPDIR/"
cp -a "$SRC/LICENSE" "$SRC/THIRD_PARTY_NOTICES.md" "$SRC/README.md" "$APPDIR/"

# Version identification.
"$SRC/.venv/bin/python" - > "$APPDIR/VERSION" 2>/dev/null <<'PY' || python3 - > "$APPDIR/VERSION" <<'PY2'
import pathlib, re
text = pathlib.Path("devicedock/__init__.py").read_text()
print(re.search(r'__version__ = "([^"]+)"', text).group(1))
PY
import pathlib, re
text = pathlib.Path("devicedock/__init__.py").read_text()
print(re.search(r'__version__ = "([^"]+)"', text).group(1))
PY2

# Runtime dependencies in a private virtual environment.
if [ ! -x "$APPDIR/venv/bin/python" ]; then
  say "Creating virtual environment (system PyGObject is reused)…"
  python3 -m venv --system-site-packages "$APPDIR/venv"
fi
say "Installing Python dependencies…"
"$APPDIR/venv/bin/pip" install --quiet --upgrade pip
"$APPDIR/venv/bin/pip" install --quiet -r "$APPDIR/requirements.txt"

# Launcher.
cat > "$BINDIR/device-dock" <<LAUNCHER
#!/bin/sh
# Device Dock — launcher
# Copyright (C) 2026 Device Dock Contributors. GPL-3.0-only.
APPDIR="$APPDIR"
export DEVICE_DOCK_DOCDIR="\$APPDIR"
export PYTHONPATH="\$APPDIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec "\$APPDIR/venv/bin/python" -m devicedock "\$@"
LAUNCHER
chmod 0755 "$BINDIR/device-dock"

# Desktop entry, icon and AppStream metadata.
sed "s|@APPDIR@|$APPDIR|g" "$SRC/packaging/io.github.bitsbreaches.DeviceDock.desktop" \
  > "$DATADIR/applications/io.github.bitsbreaches.DeviceDock.desktop"
chmod 0644 "$DATADIR/applications/io.github.bitsbreaches.DeviceDock.desktop"

ICONDIR="$DATADIR/icons/hicolor/scalable/apps"
mkdir -p "$ICONDIR" "$DATADIR/metainfo"
cp -a "$SRC/packaging/io.github.bitsbreaches.DeviceDock.svg" \
      "$ICONDIR/io.github.bitsbreaches.DeviceDock.svg"
for size in 48 64 128 256 512; do
  RASTERDIR="$DATADIR/icons/hicolor/${size}x${size}/apps"
  mkdir -p "$RASTERDIR"
  cp -a "$SRC/packaging/icons/hicolor/${size}x${size}/apps/io.github.bitsbreaches.DeviceDock.png" \
        "$RASTERDIR/io.github.bitsbreaches.DeviceDock.png"
  chmod 0644 "$RASTERDIR/io.github.bitsbreaches.DeviceDock.png"
done
cp -a "$SRC/packaging/io.github.bitsbreaches.DeviceDock.metainfo.xml" \
      "$DATADIR/metainfo/io.github.bitsbreaches.DeviceDock.metainfo.xml"
chmod 0644 "$ICONDIR/io.github.bitsbreaches.DeviceDock.svg" \
           "$DATADIR/metainfo/io.github.bitsbreaches.DeviceDock.metainfo.xml"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DATADIR/applications" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f "$DATADIR/icons/hicolor" 2>/dev/null || true
fi

cp -a "$SCRIPT_DIR/uninstall.sh" "$APPDIR/uninstall.sh"
chmod 0755 "$APPDIR/uninstall.sh"

say ""
say "Installed version $("$BINDIR/device-dock" --version | head -1)"
say "Launch with: device-dock"
say "Uninstall with: $APPDIR/uninstall.sh [--purge]"
say "Mandatory system packages (device features, live browsing, playback):"
say "  sudo apt install libimobiledevice-utils usbmuxd ifuse fuse3 \\"
say "                   gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav"
say "Optional preview extras (PDF thumbnails, video thumbnails, HEIC, keyring):"
say "  sudo apt install poppler-utils ffmpeg heif-gdk-pixbuf gir1.2-secret-1"
say "System packages are not installed automatically; install them in a terminal, not from the GUI."
say ""
say "Licensed under the GNU General Public License v3.0 only; the licence text is"
say "included in $APPDIR (LICENSE)."
