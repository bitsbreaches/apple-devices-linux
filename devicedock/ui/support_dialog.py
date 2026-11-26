# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Support Development dialog.

Both donation destinations are shown at once — QR code, address and actions side by side —
so nothing has to be scrolled or switched between. The ``qrcode`` module is a required
dependency (see ``requirements.txt``): the dialog is the only consumer, and a missing
module is reported as an installation fault rather than silently dropping the QR codes.
"""

from __future__ import annotations

import io
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..support import DonationOption, validated_donation_options

log = logging.getLogger(__name__)

QR_SIZE = 150


class SupportDialog(Adw.Window):
    """Modal dialog listing every donation option at the same time."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(transient_for=parent, modal=True, title="Support Development")
        self.set_default_size(760, 620)
        self.set_resizable(True)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(True)
        close_button = Gtk.Button(label="Close")
        close_button.add_css_class("flat")
        close_button.set_tooltip_text("Close this window (Escape)")
        close_button.connect("clicked", lambda _b: self.close())
        header.pack_start(close_button)
        toolbar.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_margin_top(12)
        body.set_margin_bottom(20)
        body.set_margin_start(24)
        body.set_margin_end(24)

        intro = Gtk.Label(
            label=("Donations support ongoing development, maintenance and new features. "
                   "They are entirely optional: every feature of this application works "
                   "without them."),
            justify=Gtk.Justification.CENTER,
            wrap=True,
            max_width_chars=68,
            halign=Gtk.Align.CENTER,
        )
        intro.add_css_class("dim-label")
        body.append(intro)

        try:
            options = validated_donation_options()
        except ValueError as exc:
            log.error("Donation configuration invalid: %s", exc)
            body.append(_error_row(str(exc)))
            options = ()

        if options:
            columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
            columns.set_homogeneous(True)
            columns.set_valign(Gtk.Align.CENTER)
            columns.set_vexpand(True)
            for option in options:
                columns.append(self._option_card(option))
            body.append(columns)

        self.status = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER,
                                halign=Gtk.Align.CENTER)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        body.append(self.status)

        toolbar.set_content(body)
        self.set_content(toolbar)

        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(lambda *_args: (self.close(), True)[1])))
        self.add_controller(shortcuts)

    # ------------------------------------------------------------------
    def _option_card(self, option: DonationOption) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        card.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(xalign=0.5, use_markup=True, justify=Gtk.Justification.CENTER,
                          label=f"<b>{GLib.markup_escape_text(option.name)}"
                                f" ({GLib.markup_escape_text(option.ticker)})</b>")
        card.append(title)

        subtitle = Gtk.Label(label=option.network, xalign=0.5)
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")
        card.append(subtitle)

        picture = self._qr_widget(option)
        if picture is not None:
            picture.set_halign(Gtk.Align.CENTER)
            card.append(picture)

        address = Gtk.Label(label=option.address, selectable=True, wrap=True,
                            justify=Gtk.Justification.CENTER, halign=Gtk.Align.CENTER,
                            max_width_chars=24)
        address.add_css_class("monospace")
        address.set_tooltip_text("Click twice to select the address, or use “Copy address”")
        card.append(address)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)
        copy_button = Gtk.Button(label="Copy address")
        copy_button.connect("clicked", self._copy_address, option.address)
        actions.append(copy_button)
        open_button = Gtk.Button(label="Open in wallet", icon_name="external-link-symbolic")
        open_button.set_tooltip_text(
            "Hand the payment URI to a wallet application registered on this system")
        open_button.connect("clicked", self._open_uri, option)
        actions.append(open_button)
        card.append(actions)
        return card

    def _qr_widget(self, option: DonationOption) -> Gtk.Widget | None:
        try:
            import qrcode
        except ImportError as exc:  # a required dependency: report, do not hide
            log.error("qrcode is not installed: %s", exc)
            return _error_row("The “qrcode” module is missing; reinstall the application.")
        try:
            image = qrcode.make(option.qr_payload)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(buffer.getvalue()))
        except Exception as exc:  # noqa: BLE001 - report the fault instead of hiding it
            log.warning("Could not render QR code for %s: %s", option.ticker, exc)
            return _error_row(str(exc))
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_can_shrink(False)
        picture.set_size_request(QR_SIZE, QR_SIZE)
        return picture

    def _copy_address(self, button: Gtk.Button, address: str) -> None:
        self.get_clipboard().set(address)
        self.status.set_text(f"Address copied to the clipboard: {address}")
        original = button.get_label()
        button.set_label("Copied")
        GLib.timeout_add(1500, self._restore_label, button, original)

    @staticmethod
    def _restore_label(button: Gtk.Button, original: str) -> bool:
        button.set_label(original)
        return GLib.SOURCE_REMOVE

    def _open_uri(self, _button: Gtk.Button, option: DonationOption) -> None:
        launcher = Gtk.UriLauncher(uri=option.payment_uri)
        try:
            launcher.launch(self, None, self._opened, option)
        except Exception as exc:  # noqa: BLE001
            self.status.set_text(f"Could not open a wallet application: {exc}")

    def _opened(self, launcher: Gtk.UriLauncher, result, option: DonationOption) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            self.status.set_text(
                f"No application is registered for {option.uri_scheme}: links "
                f"({exc.message}). Copy the address instead.")


def _error_row(message: str) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_halign(Gtk.Align.CENTER)
    icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
    row.append(icon)
    label = Gtk.Label(label=f"Donation details could not be displayed: {message}",
                      wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=48)
    row.append(label)
    return row


def show_support_dialog(parent: Gtk.Window) -> SupportDialog:
    dialog = SupportDialog(parent)
    dialog.present()
    return dialog
