#!/usr/bin/env bash
# Build a source distribution tarball containing the application plus its licence
# documents, and verify that the required notices are present.
#
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SRC"

VERSION="$(python3 - <<'PY'
import pathlib, re
print(re.search(r'__version__ = "([^"]+)"', pathlib.Path("devicedock/__init__.py").read_text()).group(1))
PY
)"
NAME="device-dock-$VERSION"
STAGE="$(mktemp -d)"
OUT="$SRC/dist"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME" "$OUT"
# The complete corresponding source: application, tests, scripts, packaging, assets and
# documentation, minus caches and build output.
cp -a devicedock tests scripts packaging docs "$STAGE/$NAME/"
find "$STAGE/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/$NAME" -name '*.pyc' -delete 2>/dev/null || true
cp -a requirements.txt run.sh .gitignore "$STAGE/$NAME/"
cp -a LICENSE THIRD_PARTY_NOTICES.md README.md RELEASE_NOTES.md \
      CONTRIBUTING.md SECURITY.md "$STAGE/$NAME/"
mkdir -p "$STAGE/$NAME/.github"
cp -a .github/workflows "$STAGE/$NAME/.github/"
printf '%s\n' "$VERSION" > "$STAGE/$NAME/VERSION"

for required in LICENSE THIRD_PARTY_NOTICES.md devicedock/__init__.py; do
  if [ ! -e "$STAGE/$NAME/$required" ]; then
    echo "error: expected $required in the package" >&2
    exit 1
  fi
done

TARBALL="$OUT/$NAME-linux.tar.gz"
tar -C "$STAGE" -czf "$TARBALL" "$NAME"
echo "Built $TARBALL"
echo "Contents (licence documents must be present):"
tar -tzf "$TARBALL" | grep -E "(THIRD_PARTY_NOTICES\.md|LICENSE|VERSION)$" | sed 's/^/  /'
sha256sum "$TARBALL"
