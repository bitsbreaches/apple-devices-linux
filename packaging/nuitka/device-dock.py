# Entry point for the Nuitka standalone build; the Debian package runs the compiled binary.
#
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
import sys

from devicedock.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
