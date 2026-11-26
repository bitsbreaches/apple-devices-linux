# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Progress plumbing tests: unknown amounts must pulse, never fake a percentage."""

from __future__ import annotations

from devicedock.asyncutil import Job


def _capture(job: Job) -> list[tuple[float, str]]:
    seen: list[tuple[float, str]] = []
    job.set_progress_handler(lambda fraction, text: seen.append((fraction, text)) or False)
    return seen


def _pump() -> None:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def test_negative_fraction_is_preserved_as_unknown() -> None:
    job = Job("test")
    seen = _capture(job)
    job.progress(-1.0, "working")
    _pump()
    assert seen == [(-1.0, "working")]


def test_fractions_are_clamped_to_one() -> None:
    job = Job("test")
    seen = _capture(job)
    job.progress(1.7, "done")
    _pump()
    assert seen[0][0] == 1.0
