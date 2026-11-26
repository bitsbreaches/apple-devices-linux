# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Logging configuration.

Secrets never reach the log: passwords, keybag material and decrypted file bytes are
never passed to a logger, and log records are additionally scrubbed for password fields.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import APP_STATE_DIR

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(passphrase|password|passwd|secret)\s*[=:]\s*\S+"), r"\1=<redacted>"),
]

LOG_PATH = APP_STATE_DIR / "devicedock.log"


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - stdlib API
        text = super().format(record)
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def setup_logging(level: str = "INFO") -> Path:
    """Configure root logging to stderr and a rotating file; return the log path."""
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = _RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("devicedock").info("Logging to %s", LOG_PATH)
    return LOG_PATH
