# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Dialogs: password entry, confirmations, messages and a cancellable progress window."""

from __future__ import annotations

import time
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..asyncutil import Job, OperationCancelled, run_in_thread
from ..models import human_duration, human_size


def show_error(parent: Gtk.Window, heading: str, body: str) -> Adw.MessageDialog:
    dialog = Adw.MessageDialog(transient_for=parent, modal=True, heading=heading, body=body)
    dialog.add_response("close", "Close")
    dialog.set_default_response("close")
    dialog.present()
    return dialog


def show_info(parent: Gtk.Window, heading: str, body: str) -> None:
    dialog = Adw.MessageDialog(transient_for=parent, modal=True, heading=heading, body=body)
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.present()


def confirm(parent: Gtk.Window, heading: str, body: str, action_label: str,
            on_confirm: Callable[[], None], *, destructive: bool = False) -> None:
    dialog = Adw.MessageDialog(transient_for=parent, modal=True, heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", action_label)
    dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog, response: str) -> None:
        if response == "confirm":
            on_confirm()

    dialog.connect("response", on_response)
    dialog.present()
    return dialog


def ask_password(parent: Gtk.Window, title: str, subtitle: str,
                 on_submit: Callable[[str], None], *,
                 error: str | None = None) -> Adw.MessageDialog:
    """Ask for a password, reporting a failed attempt inside the same dialog.

    A rejected password must not leave a second "incorrect password" window behind: the
    caller re-opens this one dialog with ``error`` set, so at most one password window is
    ever on screen.
    """
    dialog = Adw.MessageDialog(transient_for=parent, modal=True, heading=title, body=subtitle)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    if error:
        message = Gtk.Label(label=error, xalign=0, wrap=True)
        message.add_css_class("error")
        box.append(message)
    entry = Gtk.PasswordEntry(show_peek_icon=True)
    # GTK 4 removed Gtk.Entry.set_activates_default(); Return is wired directly instead, so
    # typing the password and pressing Enter submits it.
    entry.set_margin_top(8)
    box.append(entry)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("unlock", "Unlock")
    dialog.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("unlock")
    dialog.set_close_response("cancel")

    def submit() -> None:
        text = entry.get_text()
        if text:
            dialog.close()
            on_submit(text)

    def on_response(_dialog, response: str) -> None:
        if response == "unlock":
            submit()

    entry.connect("activate", lambda _entry: submit())
    dialog.connect("response", on_response)
    dialog.present()
    entry.grab_focus()
    return dialog


class ProgressDialog(Adw.Window):
    """Modal progress window driving one cancellable background job.

    Two independent channels feed it: the generic ``Job`` fraction/message, and — for
    operations that can report it — structured metrics (throughput, ETA, elapsed) pushed by
    the worker thread. Cancelling closes the window immediately: the job is signalled and
    finishes in the background, so a UI thread never waits on a tool that is stalled on a
    locked device.
    """

    def __init__(self, parent: Gtk.Window, title: str, subtitle: str = "") -> None:
        # Not modal: the work runs in the background, so the application stays usable.
        super().__init__(transient_for=parent, modal=False, title=title)
        self.set_default_size(480, -1)
        self._job: Job | None = None
        self._on_success: Callable | None = None
        self._on_error: Callable | None = None
        self._on_done: Callable | None = None
        self._started = time.monotonic()
        self._finished = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self.heading = Gtk.Label(label=title, xalign=0, wrap=True)
        self.heading.add_css_class("title-4")
        self.label = Gtk.Label(label=subtitle or title, xalign=0, wrap=True,
                               selectable=True, max_width_chars=64)
        self.label.set_lines(3)
        self.label.set_ellipsize(3)
        self.label.add_css_class("dim-label")
        self.bar = Gtk.ProgressBar(show_text=False)
        self.metrics = Gtk.Label(label="Starting…", xalign=0, wrap=True)
        self.metrics.add_css_class("caption")
        self.metrics.add_css_class("dim-label")

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.minimise_button = Gtk.Button(label="Minimise")
        self.minimise_button.set_tooltip_text(
            "Keep working in the application; the status stays visible in the header")
        self.minimise_button.connect("clicked", lambda _b: self.minimise())
        buttons.append(self.minimise_button)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._cancel)
        buttons.append(self.cancel_button)

        for widget in (self.heading, self.label, self.bar, self.metrics, buttons):
            box.append(widget)
        toolbar.set_content(box)
        self.set_content(toolbar)
        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------ progress input
    def report_progress(self, progress) -> None:
        """Thread-safe: publish a ``BackupProgress``-like observation."""
        parts = []
        if progress.file_count:
            parts.append(f"{progress.file_count:,} files")
        if progress.bytes_on_disk:
            parts.append(human_size(progress.bytes_on_disk))
        if getattr(progress, "rate_bytes_per_sec", 0):
            parts.append(f"{human_size(int(progress.rate_bytes_per_sec))}/s")
        if getattr(progress, "eta_seconds", None):
            parts.append(f"~{human_duration(progress.eta_seconds)} left")
        parts.append(human_duration(progress.elapsed_seconds))
        self.set_metrics(" · ".join(parts))

    def set_metrics(self, text: str) -> None:
        GLib.idle_add(self._apply_metrics, text)

    def _apply_metrics(self, text: str) -> bool:
        if not self._finished:
            self.metrics.set_text(text)
            parent = self.get_transient_for()
            notify = getattr(parent, "update_job_progress", None)
            if callable(notify):
                notify(text)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------ job plumbing
    def minimise(self) -> None:
        """Hide the window without touching the job; Cancel stays available on the banner."""
        self.hide()

    def _on_close_request(self, *_args) -> bool:
        # Closing hides the window, like Minimise: cancelling is the explicit action, and the
        # running job must not be killed by accident.
        if self._job is not None and not self._job.cancelled and not self._finished:
            self.minimise()
            return True
        return False

    def start(self, work: Callable[[Job], object], *, on_success: Callable | None = None,
              on_error: Callable | None = None, on_done: Callable | None = None,
              name: str = "progress") -> None:
        self._on_success = on_success
        self._on_error = on_error
        self._on_done = on_done

        def wrapped(job: Job):
            job.set_progress_handler(self._update_progress)
            return work(job)

        _thread, self._job = run_in_thread(
            wrapped,
            name=name,
            on_success=self._finish_success,
            on_error=self._finish_error,
            on_done=self._finish_done,
        )
        parent = self.get_transient_for()
        track = getattr(parent, "track_job_window", None)
        if callable(track):
            track(self)
        self.present()

    def _update_progress(self, fraction: float, text: str) -> bool:
        if self._finished:
            return GLib.SOURCE_REMOVE
        if fraction is None or fraction < 0:
            self.bar.pulse()
        else:
            self.bar.set_fraction(fraction)
            self.bar.set_show_text(True)
        if text:
            self.label.set_text(text)
        return GLib.SOURCE_REMOVE

    def _cancel(self, _button: Gtk.Button) -> None:
        """Signal the job and close at once; the worker unwinds on its own thread."""
        if self._job is not None and not self._job.cancelled:
            self._job.cancel()
            parent = self.get_transient_for()
            if parent is not None and hasattr(parent, "show_toast"):
                parent.show_toast("Cancelling… the partial backup is kept and can resume")
        self._finished = True
        self.close()

    def _finish_success(self, result: object) -> None:
        self._finished = True
        if self._on_success is not None:
            self._on_success(result)

    def _finish_error(self, exc: Exception) -> None:
        if isinstance(exc, OperationCancelled):
            self._finished = True
            return
        self._finished = True
        if self._on_error is not None:
            self._on_error(exc)
        else:
            show_error(self.get_transient_for(), "Operation failed", str(exc))

    def _finish_done(self) -> None:
        self._finished = True
        parent = self.get_transient_for()
        clear = getattr(parent, "clear_job_window", None)
        if callable(clear):
            clear()
        if self._on_done is not None:
            self._on_done()
        self.close()


def _plain_text(markdown: str) -> str:
    """Reduce the licence markdown to readable plain text for display.

    The files on disk remain the authoritative documents; this only affects the dialog.
    """
    lines = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if set(stripped) == {"-"} and stripped:
            lines.append("")
            lines.append("—" * 40)
            continue
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip()
            lines.append("")
            lines.append(text.upper() if stripped.startswith("# ") else text)
            lines.append("")
            continue
        lines.append(line.replace("**", "").replace("`", ""))
    return "\n".join(lines).strip() + "\n"


def _document_view(text: str) -> Gtk.Widget:
    """Scrollable, read-only view of a distribution document."""
    view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                        wrap_mode=Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(12)
    view.set_right_margin(12)
    view.set_top_margin(12)
    view.set_bottom_margin(12)
    view.get_buffer().set_text(_plain_text(text))
    view.set_size_request(760, 480)
    scroller = Gtk.ScrolledWindow()
    scroller.set_child(view)
    scroller.set_min_content_height(420)
    scroller.set_min_content_width(700)
    scroller.set_has_frame(True)
    return scroller


def show_document(parent: Gtk.Window, heading: str, text: str) -> None:
    """Show a licence or third-party notices document in a scrollable dialog."""
    dialog = Adw.MessageDialog(transient_for=parent, modal=True, heading=heading,
                               body="")
    dialog.set_extra_child(_document_view(text))
    dialog.add_response("close", "Close")
    dialog.set_default_response("close")
    dialog.set_close_response("close")
    dialog.present()
