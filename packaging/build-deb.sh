#!/usr/bin/env bash
# Build dist/device-dock_<version>_amd64.deb for Device Dock.
#
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
#
# The application is compiled with Nuitka in standalone mode: the package carries its own
# CPython runtime and Python dependencies, so it never uses (or depends on) the host's
# Python. Building happens in the pinned Ubuntu 24.04 image from packaging/docker/Dockerfile
# so that the native code targets the oldest supported glibc.
#
#   ./packaging/build-deb.sh
#
# Environment:
#   DEVICE_DOCK_NATIVE_BUILD=1  Build on this host instead of the pinned container.
#   DEVICE_DOCK_BUILD_IMAGE     Build image tag (default: device-dock-build:24.04).
#   DEVICE_DOCK_BUILD_PYTHON    Interpreter with Nuitka and requirements.txt installed
#                                 (native builds).
#   DEVICE_DOCK_JOBS            Nuitka parallel jobs (default: all CPUs).
#   DEVICE_DOCK_KEEP_BUILD=1    Keep the intermediate build directory.
#   DEVICE_DOCK_REUSE_DIST      Repackage an existing Nuitka .dist directory (development).
#   SOURCE_DATE_EPOCH             Reproducible build timestamp (optional).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${DEVICE_DOCK_BUILD_IMAGE:-device-dock-build:24.04}"

if [ "${DEVICE_DOCK_IN_CONTAINER:-0}" != "1" ] && [ "${DEVICE_DOCK_NATIVE_BUILD:-0}" != "1" ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "error: docker is required for a reproducible build;" >&2
        echo "       set DEVICE_DOCK_NATIVE_BUILD=1 to build on this host." >&2
        exit 1
    fi
    EXTRA=()
    if [ -n "${DEVICE_DOCK_REUSE_DIST:-}" ]; then
        EXTRA+=(--volume "$DEVICE_DOCK_REUSE_DIST:$DEVICE_DOCK_REUSE_DIST:ro")
    fi
    docker build --quiet --tag "$IMAGE" "$SRC/packaging/docker" >/dev/null
    docker run --rm "${EXTRA[@]}" \
        --env DEVICE_DOCK_IN_CONTAINER=1 \
        --env "HOST_UID=$(id -u)" \
        --env "HOST_GID=$(id -g)" \
        --env "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-}" \
        --env "DEVICE_DOCK_JOBS=${DEVICE_DOCK_JOBS:-}" \
        --env "DEVICE_DOCK_KEEP_BUILD=${DEVICE_DOCK_KEEP_BUILD:-}" \
        --env "DEVICE_DOCK_REUSE_DIST=${DEVICE_DOCK_REUSE_DIST:-}" \
        --volume "$SRC:/src" \
        "$IMAGE" /src/packaging/build-deb.sh "$@"
    exit 0
fi

cd "$SRC"

# An empty SOURCE_DATE_EPOCH is passed through Docker when the host does not set one, and
# dpkg refuses to parse it. Unset it so the build uses the current time.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    unset SOURCE_DATE_EPOCH
fi

PACKAGE="device-dock"
APP_ID="io.github.bitsbreaches.DeviceDock"
MAINTAINER="${MAINTAINER:-Device Dock Contributors <bitsbreaches@users.noreply.github.com>}"
DECRYPT_PACKAGE="iphone_backup_decrypt"
PYTHON="${DEVICE_DOCK_BUILD_PYTHON:-python3}"

VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' devicedock/__init__.py)"
if [ -z "$VERSION" ]; then
    echo "error: cannot read __version__ from devicedock/__init__.py" >&2
    exit 1
fi

BUILD="$SRC/build"
OUT="$SRC/dist"
STAGE="$BUILD/stage"
if [ "$(id -u)" = "0" ] && [ -n "${HOST_UID:-}" ]; then
    # A previous failed run inside the container leaves root-owned intermediates behind.
    chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$BUILD" 2>/dev/null || true
fi
rm -rf "$BUILD"
mkdir -p "$STAGE" "$OUT"

# --- compiled standalone application ------------------------------------------------
RUNTIME_SRC="${DEVICE_DOCK_REUSE_DIST:-$BUILD/nuitka/device-dock.dist}"
if [ -z "${DEVICE_DOCK_REUSE_DIST:-}" ]; then
    echo "Compiling the standalone application with Nuitka…"
    # The decryption library and its Crypto backend are *compiled into* the bundle. Nuitka
    # only follows imports from compiled modules, so a library shipped as loose source would
    # leave both it and the standard-library modules it needs out of the build (that is why
    # encrypted backups previously failed on an installed package).
    (cd "$SRC" && "$PYTHON" -m nuitka \
        --standalone \
        --assume-yes-for-downloads \
        --jobs="${DEVICE_DOCK_JOBS:-$(nproc)}" \
        --output-dir="$BUILD/nuitka" \
        --output-filename=device-dock \
        --include-package=devicedock \
        --include-package="$DECRYPT_PACKAGE" \
        --include-package=Crypto \
        --nofollow-import-to=pytest \
        --nofollow-import-to=pyflakes \
        packaging/nuitka/device-dock.py)
fi

if [ ! -x "$RUNTIME_SRC/device-dock" ]; then
    echo "error: Nuitka did not produce a standalone build" >&2
    exit 1
fi

# Fail the build rather than shipping a package whose encrypted-backup support is broken:
# the compiled binary must be able to import the engine and its Crypto backend with no
# system Python packages available.
if ! "$RUNTIME_SRC/device-dock" --self-check; then
    echo "error: the standalone build cannot load its bundled decryption engine" >&2
    exit 1
fi

PKGROOT="$STAGE/${PACKAGE}_${VERSION}_amd64"
APPDIR="$PKGROOT/usr/lib/device-dock"
RUNTIME="$APPDIR/app"
DOCDIR="$PKGROOT/usr/share/doc/$PACKAGE"

install -d "$PKGROOT/DEBIAN" "$APPDIR/documents" "$RUNTIME" "$DOCDIR" \
    "$PKGROOT/usr/bin" \
    "$PKGROOT/usr/share/applications" \
    "$PKGROOT/usr/share/metainfo" \
    "$PKGROOT/usr/share/icons/hicolor/scalable/apps"

cp -a "$RUNTIME_SRC/." "$RUNTIME/"
# Typelibs come from the installed gir1.2-* dependencies: bundling the build host's copies
# would override the host's GTK/libadwaita typelibs and could contradict the declared floors.
rm -rf "$RUNTIME/girepository"
ln -s ../documents "$RUNTIME/documents"

install -m 0755 packaging/device-dock "$PKGROOT/usr/bin/device-dock"
install -m 0644 "packaging/$APP_ID.svg" \
    "$PKGROOT/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
for size in 48 64 128 256 512; do
    install -d "$PKGROOT/usr/share/icons/hicolor/${size}x${size}/apps"
    install -m 0644 "packaging/icons/hicolor/${size}x${size}/apps/$APP_ID.png" \
        "$PKGROOT/usr/share/icons/hicolor/${size}x${size}/apps/$APP_ID.png"
done
install -m 0644 "packaging/$APP_ID.metainfo.xml" \
    "$PKGROOT/usr/share/metainfo/$APP_ID.metainfo.xml"
sed -e "s|@APPDIR@/run.sh|device-dock|g" -e "s|^Icon=.*|Icon=$APP_ID|" \
    "packaging/$APP_ID.desktop" \
    > "$PKGROOT/usr/share/applications/$APP_ID.desktop"
chmod 0644 "$PKGROOT/usr/share/applications/$APP_ID.desktop"

# --- documentation and licence documents -------------------------------------------
# The application reads the licence text and third-party notices at run time (menu,
# --licence), so they are also installed inside the payload: dpkg path-excludes strip
# /usr/share/doc/* except copyright on Ubuntu cloud/minimal images, which would otherwise
# leave the documents unavailable.
install -m 0644 LICENSE THIRD_PARTY_NOTICES.md README.md RELEASE_NOTES.md \
    "$APPDIR/documents/"
install -m 0644 LICENSE THIRD_PARTY_NOTICES.md README.md RELEASE_NOTES.md "$DOCDIR/"
install -m 0644 packaging/deb/copyright "$DOCDIR/copyright"

# Licence texts for the runtime and libraries inside the bundle. The bundled CPython and
# PyGObject come from the build image, whose Debian copyright files carry their notices;
# the remaining Python libraries come from wheels, whose licences ship in their dist-info.
install -d "$DOCDIR/bundled-licenses"
for package in python3.12 python3-gi; do
    if [ -r "/usr/share/doc/$package/copyright" ]; then
        install -m 0644 "/usr/share/doc/$package/copyright" \
            "$DOCDIR/bundled-licenses/$package.txt"
    fi
done
SITE="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for distribution in "$DECRYPT_PACKAGE" cryptography pycryptodome qrcode pillow; do
    info="$(find "$SITE" -maxdepth 1 -type d -iname "${distribution}-*.dist-info" \
        | head -n 1)"
    if [ -z "$info" ]; then
        echo "error: no $distribution metadata found in $SITE" >&2
        exit 1
    fi
    {
        printf '%s %s\n\n' "$distribution" \
            "$(sed -n 's/^Version: //p' "$info/METADATA" | head -n 1)"
        find "$info" -maxdepth 2 -type f \
            \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) \
            -exec cat {} +
    } > "$DOCDIR/bundled-licenses/$distribution.txt"
done

BUILD_DATE="$(date -u -R)"
if [ -n "${SOURCE_DATE_EPOCH:-}" ]; then
    BUILD_DATE="$(date -u -R -d "@$SOURCE_DATE_EPOCH")"
fi
cat > "$STAGE/changelog" <<CHANGELOG
${PACKAGE} (${VERSION}) unstable; urgency=medium

  * Rename the application to Device Dock (package device-dock, command device-dock).
    Existing settings, logs, cached data and the backup library are moved from the
    former appledevices directories on first run; no backup is copied or deleted.
  * This package replaces apple-devices and removes its launcher and desktop entry.
  * Standalone build: the application is compiled with Nuitka and carries its own
    CPython runtime, so it does not depend on the host Python version.
  * The application is licensed under the GNU General Public License v3.0 only.

 -- ${MAINTAINER}  ${BUILD_DATE}
CHANGELOG
gzip -9n -c "$STAGE/changelog" > "$DOCDIR/changelog.Debian.gz"
chmod 0644 "$DOCDIR/changelog.Debian.gz"

# --- Debian control data -----------------------------------------------------------
INSTALLED_SIZE="$(du -sk --apparent-size "$PKGROOT" | cut -f1)"
sed -e "s|@VERSION@|$VERSION|g" \
    -e "s|@INSTALLED_SIZE@|$INSTALLED_SIZE|g" \
    -e "s|@MAINTAINER@|$MAINTAINER|g" \
    packaging/deb/control > "$PKGROOT/DEBIAN/control"
chmod 0644 "$PKGROOT/DEBIAN/control"

for script in postinst postrm; do
    install -m 0755 "packaging/deb/$script" "$PKGROOT/DEBIAN/$script"
done

( cd "$PKGROOT" && find . -path ./DEBIAN -prune -o -type f -print |
    sed 's|^\./||' | sort | xargs md5sum ) > "$PKGROOT/DEBIAN/md5sums"
chmod 0644 "$PKGROOT/DEBIAN/md5sums"

# Distribution packages use 0755 for directories and 0644 for files, whatever the umask
# and repository permissions happened to be.
find "$PKGROOT" -type d -exec chmod 0755 {} +
find "$PKGROOT" -type f -exec chmod 0644 {} +
chmod 0755 "$PKGROOT/usr/bin/device-dock" "$RUNTIME/device-dock" \
           "$PKGROOT/DEBIAN/postinst" "$PKGROOT/DEBIAN/postrm"

for required in \
    usr/bin/device-dock \
    usr/lib/device-dock/app/device-dock \
    usr/lib/device-dock/documents/LICENSE \
    usr/lib/device-dock/documents/THIRD_PARTY_NOTICES.md \
    usr/lib/device-dock/documents/RELEASE_NOTES.md \
    usr/share/applications/$APP_ID.desktop \
    usr/share/icons/hicolor/128x128/apps/$APP_ID.png \
    usr/share/doc/$PACKAGE/copyright \
    usr/share/doc/$PACKAGE/bundled-licenses/$DECRYPT_PACKAGE.txt \
    usr/share/doc/$PACKAGE/bundled-licenses/python3.12.txt \
    usr/share/doc/$PACKAGE/LICENSE; do
    if [ ! -e "$PKGROOT/$required" ]; then
        echo "error: expected $required in the package" >&2
        exit 1
    fi
done

DEB="$OUT/${PACKAGE}_${VERSION}_amd64.deb"
rm -f "$DEB"
dpkg-deb --build --root-owner-group "$PKGROOT" "$DEB" >/dev/null
( cd "$OUT" && sha256sum "$(basename "$DEB")" > "$(basename "$DEB").sha256" )

if [ "${DEVICE_DOCK_KEEP_BUILD:-0}" != "1" ]; then
    rm -rf "$BUILD"
fi
if [ -n "${HOST_UID:-}" ] && [ "$(id -u)" = "0" ]; then
    chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$OUT" 2>/dev/null || true
fi

echo "Built $DEB"
du -h "$DEB"
dpkg-deb --info "$DEB" | sed 's/^/  /'
cat "$DEB.sha256"
