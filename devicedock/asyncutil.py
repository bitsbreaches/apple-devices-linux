# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Threading helpers that keep the GTK main loop responsive.

Long operations (backup scanning, manifest queries, extraction, libimobiledevice calls)
run on worker threads and hand results back to the UI thread through ``GLib.idle_add``.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

log = logging.getLogger(__name__)


class Job:
    """Cancellation token plus progress reporting for one background operation."""

    def __init__(self, name: str = "job") -> None:
        self.name = name
        self._cancelled = threading.Event()
        self._progress: Callable[[float, str], None] | None = None

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise OperationCancelled(self.name)

    def set_progress_handler(self, handler: Callable[[float, str], None] | None) -> None:
        self._progress = handler

    def progress(self, fraction: float, text: str = "") -> None:
        """Report progress; safe to call from a worker thread.

        A negative or ``None`` fraction means "amount unknown": the UI should pulse rather
        than show a fabricated percentage.
        """
        handler = self._progress
        if handler is None:
            return
        value = -1.0 if fraction is None or fraction < 0 else min(1.0, fraction)
        GLib.idle_add(handler, value, text)


class OperationCancelled(Exception):
    """Raised inside a worker when its Job has been cancelled."""


def run_in_thread(
    work: Callable[[Job], Any],
    *,
    name: str = "job",
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> tuple[threading.Thread, Job]:
    """Run ``work(job)`` on a daemon thread and dispatch results on the GTK loop."""
    job = Job(name)

    def runner() -> None:
        try:
            result = work(job)
        except OperationCancelled:
            log.info("%s cancelled", name)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            log.error("%s failed: %s\n%s", name, exc, traceback.format_exc())
            if on_error is not None:
                GLib.idle_add(on_error, exc)
        else:
            if on_success is not None:
                GLib.idle_add(on_success, result)
        finally:
            if on_done is not None:
                GLib.idle_add(on_done)

    thread = threading.Thread(target=runner, name=f"devicedock-{name}", daemon=True)
    thread.start()
    return thread, job
