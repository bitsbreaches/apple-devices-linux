#!/usr/bin/env bash
# Launch Device Dock, creating the virtual environment on first use.
set -euo pipefail
cd "$(dirname "$0")"

# Live browsing is optional for the rest of the application. Explain the missing system
# utility here, then still start the UI so backups and other features remain available.
if ! command -v ifuse >/dev/null 2>&1; then
  cat >&2 <<'NOTICE'
Device Dock: live iPhone file browsing needs ifuse and FUSE.
On Ubuntu/Debian, install the mandatory system packages with:
  sudo apt install libimobiledevice-utils usbmuxd ifuse fuse3 \
                   gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav
The application will start; On My iPhone will show setup guidance and a retry action.
NOTICE
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment (PyGObject comes from the system)…"
  python3 -m venv --system-site-packages .venv
  .venv/bin/pip install --upgrade pip >/dev/null
fi

if ! .venv/bin/python -c "import iphone_backup_decrypt" >/dev/null 2>&1; then
  echo "Installing Python dependencies…"
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m devicedock "$@"
