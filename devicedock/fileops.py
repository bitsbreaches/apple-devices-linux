# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Filesystem helpers shared by the export, catalog and transfer paths."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

#: How many collision variants are tried before giving up.
MAX_VARIANTS = 1000


def unique_destination(target: Path, taken: Collection[str] = (), *,
                       start: int = 2) -> Path:
    """Return ``target`` or the first free ``name (n).ext`` variant next to it.

    ``taken`` holds already-chosen destinations for the current operation (case-folded
    ``Path.as_posix()`` values) so names reserved earlier in the same batch are skipped even
    before they exist on disk. A bounded search keeps a pathological directory listing from
    looping forever.
    """
    reserved = {value.casefold() for value in taken}
    if _is_free(target, reserved):
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(start, start + MAX_VARIANTS):
        candidate = target.with_name(f"{stem} ({counter}){suffix}")
        if _is_free(candidate, reserved):
            return candidate
    raise OSError(f"cannot find a free filename for {target}")


def _is_free(path: Path, reserved: set[str]) -> bool:
    return path.as_posix().casefold() not in reserved and not path.exists()
