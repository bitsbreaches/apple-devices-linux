#!/usr/bin/env bash
# Remove an installed copy of Device Dock.
#
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
#
# Usage: uninstall.sh [--purge] [--yes]
#   --purge  also delete user settings, logs and cached decrypted data
#   --yes    do not ask for confirmation (for scripted removal)
#
# Your backups are never deleted, not even with --purge.
set -euo pipefail

PURGE=0
ASSUME_YES="${ASSUME_YES:-0}"
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Resolve the installation from this script's own location when it is the installed copy,
# so an installation under a non-default prefix is removed correctly. Defaulting to
# $HOME/.local in that case would delete an unrelated installation (and, with --purge, an
# unrelated settings folder).
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SELF_DIR/devicedock/__init__.py" ]; then
  APPDIR="$SELF_DIR"
  PREFIX="$(cd "$SELF_DIR/../.." && pwd)"
else
  PREFIX="${PREFIX:-$HOME/.local}"
  APPDIR="$PREFIX/lib/device-dock"
fi
LEGACY_APPDIR="$PREFIX/share/devicedock"
BINDIR="$PREFIX/bin"
DATADIR="$PREFIX/share"
DATAHOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BACKUPDIR="$DATAHOME/devicedock/Backups"

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/devicedock"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/devicedock"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/devicedock"

# Never let an unset or root-ish value reach rm -rf.
for path in "$APPDIR" "$CONFIG" "$STATE" "$CACHE"; do
  case "$path" in
    ""|"/"|"$HOME") die "refusing to remove '$path'" ;;
  esac
done

say "Installation : $APPDIR"
if [ "$PURGE" -eq 1 ]; then
  say "User data    : $CONFIG $STATE $CACHE"
  say "Backups kept : $BACKUPDIR"
fi

if [ "$PURGE" -eq 1 ] && [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Delete the program and the user data listed above? [y/N] '
  read -r answer
  case "$answer" in
    [yY]*) ;;
    *) say "Cancelled; nothing was removed."; exit 1 ;;
  esac
fi

say "Removing $APPDIR"
rm -rf "$APPDIR"
# Nothing but the application payload lived here in older installations; never remove it
# if it holds backups.
if [ -d "$LEGACY_APPDIR" ]; then
  if [ -d "$LEGACY_APPDIR/Backups" ]; then
    say "Keeping $LEGACY_APPDIR (it contains backups)"
  else
    rm -rf "$LEGACY_APPDIR"
  fi
fi
rm -f "$BINDIR/device-dock"
rm -f "$BINDIR/devicedock" "$BINDIR/appledevices"  # earlier names
rm -f "$DATADIR/applications/io.github.bitsbreaches.DeviceDock.desktop"
rm -f "$DATADIR/icons/hicolor/scalable/apps/io.github.bitsbreaches.DeviceDock.svg"
for size in 48 64 128 256 512; do
  rm -f "$DATADIR/icons/hicolor/${size}x${size}/apps/io.github.bitsbreaches.DeviceDock.png"
done
rm -f "$DATADIR/metainfo/io.github.bitsbreaches.DeviceDock.metainfo.xml"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DATADIR/applications" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f "$DATADIR/icons/hicolor" 2>/dev/null || true
fi

if [ "$PURGE" -eq 1 ]; then
  say "Purging user data: $CONFIG $STATE $CACHE"
  rm -rf "$CONFIG" "$STATE" "$CACHE"
  say "Your backups in $BACKUPDIR were not touched."
  say "Stored backup passwords (if any) remain in the system keyring; remove them from"
  say "the keyring application (for example GNOME Passwords and Keys)."
else
  say "User settings, logs and cached data were kept. Re-run with --purge to remove them."
fi

say "Uninstalled."
