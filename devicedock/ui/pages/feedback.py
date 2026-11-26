# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Feedback page: disclose and prefill the technical information sent with the form."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ...feedback import build_feedback_url, collect_facts
from ..widgets import (
    PageScaffold,
    Space,
    banner,
    caption,
    icon_text_button,
    set_banner,
)
from .base import Page

log = logging.getLogger(__name__)

DESCRIPTION = ("Report bugs, request features or share your experience to help improve "
               "future releases.")
PRIVACY_NOTE = ("Never included: device identifiers (UDID), serial numbers, device names, "
                "Apple IDs, passwords, personal files or backup contents.")


class FeedbackPage(Page):
    title = "Feedback"
    icon_name = "mail-send-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        self.include_technical = True
        self._facts = collect_facts(())
        self._link = build_feedback_url(self._facts, True)

        self.scaffold = PageScaffold("Help improve Device Dock", DESCRIPTION, max_width=940)
        self.append(self.scaffold)

        self.launch_banner = banner()
        self.rows: dict[str, Adw.ActionRow] = {}
        self.scaffold.add_section(
            "Technical information",
            self._technical_card(),
            "Choose what the form is prefilled with. Nothing is sent until you submit it.",
        )
        self.scaffold.add(self.launch_banner)
        self.scaffold.add(self._actions())

        self.state.subscribe("devices", lambda *_: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------
    def _technical_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)

        self.switch = Adw.SwitchRow(
            title="Include technical information",
            subtitle="Application version, Linux distribution, iPhone model and iOS version.",
        )
        self.switch.set_active(self.include_technical)
        self.switch.connect("notify::active", self._on_include_toggled)

        self.fact_list = Gtk.ListBox()
        self.fact_list.add_css_class("boxed-list")
        self.fact_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.fact_list.append(self.switch)
        for key, title in (
            ("app_version", "Application version"),
            ("distribution", "Linux distribution"),
            ("device_model", "iPhone model"),
            ("ios_version", "iOS version"),
        ):
            row = Adw.ActionRow(title=title)
            self.fact_list.append(row)
            self.rows[key] = row
        body.append(self.fact_list)

        self.share_note = caption("")
        body.append(self.share_note)
        body.append(caption(PRIVACY_NOTE))
        return body

    def _actions(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        box.set_margin_top(Space.S)

        # The copy action is always available: it is both a convenience and the fallback
        # when no browser is registered for http links.
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        submit = icon_text_button("Submit Feedback", "mail-send-symbolic")
        submit.add_css_class("suggested-action")
        submit.connect("clicked", self._submit)
        copy = icon_text_button("Copy Feedback Link", "edit-copy-symbolic")
        copy.connect("clicked", self._copy_link)
        buttons.append(submit)
        buttons.append(copy)
        box.append(buttons)
        box.append(caption("The form opens in your default browser. Sharing is optional and "
                           "nothing is collected automatically."))
        return box

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._facts = collect_facts(self.state.devices)
        self._render_facts()
        self._update_link()

    def _render_facts(self) -> None:
        values = {
            "app_version": self._facts.app_version,
            "distribution": self._facts.distribution,
            "device_model": self._facts.device_model,
            "ios_version": self._facts.ios_version,
        }
        for key, row in self.rows.items():
            value = values[key]
            if not value and key in ("device_model", "ios_version"):
                value = "No device connected" if not self.state.devices else "Not reported"
            row.set_subtitle(value or "Not available")
            row.set_sensitive(self.include_technical)
        self._update_share_note()

    def _update_share_note(self) -> None:
        if self.include_technical:
            self.share_note.set_text(
                "These values are added to the form link. The form is prefilled, not submitted."
            )
        else:
            self.share_note.set_text(
                "Technical information is turned off: the form opens without prefilled values."
            )

    def _update_link(self) -> None:
        self._link = build_feedback_url(self._facts, self.include_technical)

    def _on_include_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        self.include_technical = row.get_active()
        for fact_row in self.rows.values():
            fact_row.set_sensitive(self.include_technical)
        self._update_share_note()
        self._update_link()

    # ------------------------------------------------------------------
    def _current_link(self) -> str:
        self.refresh()
        return self._link

    def _submit(self, _button: Gtk.Button) -> None:
        set_banner(self.launch_banner, "")
        launcher = Gtk.UriLauncher(uri=self._current_link())
        try:
            launcher.launch(self.window, None, self._launched)
        except Exception as exc:  # noqa: BLE001 - a missing URL handler must not crash the page
            self._launch_failed(str(exc))

    def _launched(self, launcher: Gtk.UriLauncher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            self._launch_failed(exc.message)

    def _launch_failed(self, detail: str) -> None:
        log.warning("Could not open the feedback form: %s", detail)
        set_banner(
            self.launch_banner,
            "Could not open a web browser. Use “Copy Feedback Link” and paste it into "
            "your browser.",
            "error",
        )

    def _copy_link(self, _button: Gtk.Button) -> None:
        self.get_clipboard().set(self._current_link())
        self.window.show_toast("Feedback link copied to the clipboard")
