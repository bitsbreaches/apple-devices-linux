# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Collision-free destination naming used by every export and transfer path."""

from __future__ import annotations

from pathlib import Path

import pytest

from devicedock.fileops import unique_destination


def test_a_free_path_is_returned_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "report.pdf"
    assert unique_destination(target) == target


def test_an_existing_file_gets_a_numbered_variant(tmp_path: Path) -> None:
    target = tmp_path / "report.pdf"
    target.write_text("first", encoding="utf-8")
    (tmp_path / "report (2).pdf").write_text("second", encoding="utf-8")

    assert unique_destination(target) == tmp_path / "report (3).pdf"


def test_the_start_number_is_honoured(tmp_path: Path) -> None:
    """The manifest exporter has always numbered its first variant (1)."""
    target = tmp_path / "note.txt"
    target.write_text("", encoding="utf-8")

    assert unique_destination(target, start=1) == tmp_path / "note (1).txt"


def test_names_reserved_earlier_in_the_same_batch_are_skipped(tmp_path: Path) -> None:
    target = tmp_path / "photo.jpg"
    target.write_text("", encoding="utf-8")
    taken = {(tmp_path / "photo (2).jpg").as_posix().casefold()}

    assert unique_destination(target, taken) == tmp_path / "photo (3).jpg"


def test_reserved_names_ignore_case(tmp_path: Path) -> None:
    target = tmp_path / "Photo.JPG"
    target.write_text("", encoding="utf-8")
    taken = {(tmp_path / "photo (2).jpg").as_posix().casefold()}

    assert unique_destination(target, taken) == tmp_path / "Photo (3).JPG"


def test_an_exhausted_search_raises_instead_of_looping(tmp_path: Path) -> None:
    from devicedock import fileops

    target = tmp_path / "stuck.bin"
    target.write_text("", encoding="utf-8")
    for counter in range(2, 2 + fileops.MAX_VARIANTS):
        (tmp_path / f"stuck ({counter}).bin").write_text("", encoding="utf-8")

    with pytest.raises(OSError):
        unique_destination(target)
