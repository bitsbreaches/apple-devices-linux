#!/usr/bin/env bash
# Install a built package in a clean Ubuntu 24.04 container and check that it works
# without any host Python.
#
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
#
#   ./packaging/verify-package.sh [path/to/device-dock_<version>_amd64.deb]
#
# Environment:
#   DEVICE_DOCK_VERIFY_IMAGE  Image without Python (default: ubuntu:24.04).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${DEVICE_DOCK_VERIFY_IMAGE:-ubuntu:24.04}"

if [ $# -ge 1 ]; then
    DEB="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
else
    DEB="$(ls -t "$SRC"/dist/device-dock_*_amd64.deb 2>/dev/null | head -n 1 || true)"
fi
if [ -z "${DEB:-}" ] || [ ! -f "$DEB" ]; then
    echo "error: no package found; run ./packaging/build-deb.sh first" >&2
    exit 1
fi

echo "Verifying $(basename "$DEB") in $IMAGE"

# Build an encrypted-backup fixture with a known password so the installed binary can prove
# that unlocking works with its own runtime. It needs a Python with cryptography+pytest,
# which is only a build-host requirement; the check is skipped when none is available.
FIXTURE_ROOT=""
FIXTURE_DIR=""
FIXTURE_PASSWORD=""
for candidate in "$SRC/.venv/bin/python" python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import cryptography, pytest' >/dev/null 2>&1; then
        FIXTURE_ROOT="$(mktemp -d)"
        read -r FIXTURE_UDID FIXTURE_PASSWORD < <("$candidate" - "$SRC" "$FIXTURE_ROOT" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
import conftest

conftest.build_encrypted_backup(Path(sys.argv[2]) / conftest.UDID)
print(conftest.UDID, conftest.ENCRYPTED_PASSWORD)
PY
)
        FIXTURE_DIR="/fixture/$FIXTURE_UDID"
        break
    fi
done
if [ -n "$FIXTURE_ROOT" ]; then
    trap 'rm -rf "$FIXTURE_ROOT"' EXIT
else
    echo "NOTE: encrypted-backup check skipped (no python3 with cryptography and pytest)"
fi

EXTRA=()
if [ -n "$FIXTURE_ROOT" ]; then
    EXTRA+=(--volume "$FIXTURE_ROOT:/fixture:ro" --env "FIXTURE_DIR=$FIXTURE_DIR"
            --env "FIXTURE_PASSWORD=$FIXTURE_PASSWORD")
fi

docker run --rm -i "${EXTRA[@]}" --volume "$(dirname "$DEB"):/dist:ro" "$IMAGE" bash -s -- "$DEB" <<'INNER'
set -euo pipefail
deb="/dist/$(basename "$1")"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends "$deb" >/dev/null
apt-get install -y -qq --no-install-recommends xvfb x11-utils dbus >/dev/null

# The package must not have installed a Python interpreter of its own.
if command -v python3 >/dev/null 2>&1; then
    echo "FAIL: python3 is installed" >&2
    exit 1
fi
echo "python3: absent (the application uses its bundled runtime)"

device-dock --version
device-dock --licence | head -n 3
device-dock --third-party-notices | head -n 3

# Encrypted backups must work from the bundled runtime with no system Python at all.
device-dock --self-check
device-dock --self-check | grep -q "Decryption engine: iphone_backup_decrypt" \
    || { echo "FAIL: the bundled decryption engine is unavailable" >&2; exit 1; }
device-dock --self-check | grep -q "Crypto backend:" \
    || { echo "FAIL: the bundled Crypto backend is unavailable" >&2; exit 1; }
dpkg -s usbmuxd >/dev/null || { echo "FAIL: usbmuxd is not installed as a dependency" >&2; exit 1; }

if [ -n "${FIXTURE_DIR:-}" ]; then
    DEVICE_DOCK_SELF_CHECK_PASSWORD="$FIXTURE_PASSWORD" \
        device-dock --self-check --backup "$FIXTURE_DIR" >/tmp/unlock.log \
        || { echo "FAIL: the bundled engine could not unlock the encrypted fixture" >&2; cat /tmp/unlock.log >&2; exit 1; }
    grep -q "Unlocked:" /tmp/unlock.log \
        || { echo "FAIL: no files were readable after unlocking" >&2; cat /tmp/unlock.log >&2; exit 1; }
    echo "encrypted backup: unlocked with the correct password"
    if DEVICE_DOCK_SELF_CHECK_PASSWORD="definitely-wrong-password" \
        device-dock --self-check --backup "$FIXTURE_DIR" >/tmp/wrong.log; then
        echo "FAIL: an incorrect backup password was accepted" >&2
        exit 1
    fi
    grep -q "REJECTED" /tmp/wrong.log \
        || { echo "FAIL: wrong-password handling did not report rejection" >&2; cat /tmp/wrong.log >&2; exit 1; }
    echo "encrypted backup: incorrect password rejected"
fi

( cd /dist && sha256sum -c "$(basename "$deb").sha256" )

cat > /tmp/gui-check.sh <<'GUI'
set -u
device-dock >/tmp/gui.log 2>&1 &
pid=$!
for _ in $(seq 1 30); do
    if xwininfo -root -tree 2>/dev/null | grep -q "Device Dock"; then
        echo "GUI: window shown"
        kill "$pid" 2>/dev/null || true
        exit 0
    fi
    sleep 1
done
echo "FAIL: no window appeared" >&2
cat /tmp/gui.log >&2
kill "$pid" 2>/dev/null || true
exit 1
GUI
dbus-run-session -- xvfb-run -a bash /tmp/gui-check.sh

apt-get remove -y -qq device-dock >/dev/null
for path in /usr/bin/device-dock /usr/lib/device-dock/app/device-dock; do
    if [ -e "$path" ]; then
        echo "FAIL: $path survived removal" >&2
        exit 1
    fi
done
# The shared device daemon belongs to the distribution and must survive removal.
dpkg -s usbmuxd >/dev/null || { echo "FAIL: removing device-dock removed usbmuxd" >&2; exit 1; }
echo "OK: package installs, runs without host Python, unlocks encrypted backups, and uninstalls"
INNER
