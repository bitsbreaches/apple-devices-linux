# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Diagnostics page: tool availability, environment facts, self-check and log tail."""

from __future__ import annotations

import importlib.util
import logging
import platform
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from ... import __version__, secrets
from ...asyncutil import run_in_thread
from ...previews import PREVIEW_TOOLS, preview_tool_status
from ...config import APP_STATE_DIR
from ...devices import libimobile
from ...logging_setup import LOG_PATH
from ..widgets import (
    PageScaffold,
    Space,
    caption,
    icon_text_button,
    key_value_grid,
    panel,
)
from .base import Page

log = logging.getLogger(__name__)


class DiagnosticsPage(Page):
    title = "Diagnostics"
    icon_name = "utilities-system-monitor-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        check_button = icon_text_button("Run self-check", "emblem-ok-symbolic")
        check_button.connect("clicked", lambda _b: self._self_check())
        copy_button = icon_text_button("Copy report", "edit-copy-symbolic")
        copy_button.connect("clicked", lambda _b: self._copy_report())

        self.scaffold = PageScaffold(
            "Diagnostics",
            "What this application can and cannot do on this machine.",
            actions=[check_button, copy_button],
            max_width=940,
        )
        self.append(self.scaffold)

        self.state.subscribe("devices", lambda *_: self.refresh())
        self.state.subscribe("vault", lambda *_: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._clear(self.scaffold.body)
        self.scaffold.add_section("Environment", self._environment_card())
        self.scaffold.add_section("Device tools", self._tools_card(),
                                  "Device communication runs through these system tools.")
        self.scaffold.add_section("Active backup", self._backup_card())
        self.scaffold.add_section("Connected device", self._device_card(),
                                  "Raw values as reported by libimobiledevice.")
        self.scaffold.add_section("Application log", self._log_card())

    def _environment_card(self) -> Gtk.Widget:
        try:
            import iphone_backup_decrypt

            engine = f"iphone_backup_decrypt {getattr(iphone_backup_decrypt, '__version__', 'installed')}"
        except ImportError:
            engine = "not installed — encrypted backups cannot be opened"
        try:
            import cryptography

            crypto = f"cryptography {cryptography.__version__}"
        except ImportError:
            crypto = "not installed"
        pairs = [
            ("Application", f"Device Dock {__version__}"),
            ("Python", platform.python_version()),
            ("Desktop", f"{platform.system()} · {platform.release()}"),
            ("GTK", f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"),
            ("libadwaita", f"{Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}.{Adw.MICRO_VERSION}"),
            ("Decryption engine", engine),
            ("Cryptography", crypto),
            ("Secret Service", "available" if secrets.available() else "unavailable"),
            ("State directory", str(APP_STATE_DIR)),
        ]
        return panel(key_value_grid(pairs))

    def _tools_card(self) -> Gtk.Widget:
        pairs = []
        for name, ok in self.state.tool_status.items():
            pairs.append((name, "available" if ok else "missing"))
        pairs.append(("idevicebackup2 version", libimobile.idevicebackup2_version()))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        body.append(key_value_grid(pairs))
        preview_pairs = [(f"{name} (optional)", "available" if ok else "not installed — "
                          f"{PREVIEW_TOOLS[name]} disabled")
                         for name, ok in preview_tool_status().items()]
        body.append(caption("Optional preview helpers (the application reports them in the "
                            "preview instead of failing):"))
        body.append(key_value_grid(preview_pairs))
        if libimobile.missing_required_tools():
            label = Gtk.Label(
                label="Device features are disabled until libimobiledevice-utils is installed.",
                xalign=0, wrap=True,
            )
            label.add_css_class("error")
            body.append(label)
        return panel(body)

    def _backup_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        info = self.state.active_backup
        vault = self.state.vault
        if info is None:
            body.append(caption("No backup selected."))
            return panel(body)
        pairs = [
            ("Backup", str(info.path)),
            ("Encrypted", "Yes" if info.is_encrypted else "No"),
            ("Unlocked", "Yes" if (vault is not None and vault.unlocked) else "No"),
        ]
        if vault is not None and vault.unlocked:
            manifest = vault.manifest()
            pairs.append(("Manifest entries", str(manifest.total_files())))
            pairs.append(("Domains", str(len(manifest.domains_with_sizes()))))
        else:
            pairs.append(("Manifest entries", "requires unlock"))
        body.append(key_value_grid(pairs))
        return panel(body)

    def _device_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        if self.state.device_error:
            body.append(Gtk.Label(label=self.state.device_error, xalign=0, wrap=True))
        elif not self.state.devices:
            body.append(caption("No device connected."))
        else:
            for device in self.state.devices:
                pairs = [
                    ("UDID", device.udid),
                    ("ProductType", device.product_type or "—"),
                    ("ProductVersion", device.product_version or "—"),
                    ("BuildVersion", device.build_version or "—"),
                    ("ConnectionType", device.connection_type or "—"),
                ]
                body.append(key_value_grid(pairs))
                body.append(Gtk.Separator())
        return panel(body)

    def _log_card(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        body.append(caption(str(LOG_PATH)))
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        self._log_view = view
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(220)
        scroller.set_child(view)
        body.append(scroller)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reload_button = icon_text_button("Reload log", "view-refresh-symbolic")
        reload_button.connect("clicked", lambda _b: self._load_log())
        open_button = icon_text_button("Open log file", "document-open-symbolic")
        open_button.connect("clicked", lambda _b: self._open_log())
        buttons.append(reload_button)
        buttons.append(open_button)
        body.append(buttons)
        self._load_log()
        return panel(body)

    # ------------------------------------------------------------------
    def _load_log(self) -> None:
        try:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
            text = "\n".join(lines) if lines else "(log is empty)"
        except FileNotFoundError:
            text = f"(no log yet at {LOG_PATH})"
        except OSError as exc:
            text = f"(cannot read log: {exc})"
        self._log_view.get_buffer().set_text(text)

    def _open_log(self) -> None:
        try:
            Gtk.show_uri(self.window, LOG_PATH.parent.as_uri(), Gdk.CURRENT_TIME)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot open log folder: %s", exc)

    def _copy_report(self) -> None:
        clipboard = self.window.get_clipboard()
        report = self._build_report()
        clipboard.set(report)
        self.window.show_toast("Diagnostics copied to clipboard")

    def _build_report(self) -> str:
        lines = [
            f"Device Dock {__version__}",
            f"Python {platform.python_version()} on {platform.system()} {platform.release()}",
            f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
            "",
            "Tools:",
        ]
        for name, ok in self.state.tool_status.items():
            lines.append(f"  {name}: {'ok' if ok else 'missing'}")
        lines.append("")
        lines.append(f"Backups discovered: {len(self.state.backups)}")
        for info in self.state.backups:
            lines.append(
                f"  {info.path.name}: {info.display_name} · {info.firmware or 'unknown firmware'}"
                f" · {'encrypted' if info.is_encrypted else 'plain'} · {info.last_backup_text}"
            )
        lines.append("")
        lines.append(f"Devices: {len(self.state.devices)}")
        for device in self.state.devices:
            lines.append(f"  {device.udid}: {device.product_type} {device.product_version}")
        if self.state.device_error:
            lines.append(f"  device error: {self.state.device_error}")
        if self.state.active_backup is not None:
            lines.append("")
            lines.append(f"Active backup: {self.state.active_backup.path}")
            lines.append(f"  unlocked: {self.state.vault is not None}")
        return "\n".join(lines)

    def _self_check(self) -> None:
        def work(_job):
            results = []
            missing = libimobile.missing_required_tools()
            results.append(("libimobiledevice tools", not missing,
                            "ok" if not missing else f"missing: {', '.join(missing)}"))
            engine = importlib.util.find_spec("iphone_backup_decrypt")
            results.append(("decryption engine", engine is not None,
                            "importable" if engine else "iphone_backup_decrypt is not installed"))
            for directory in self.state.settings.backup_directories:
                exists = Path(directory).expanduser().is_dir()
                results.append((f"backup dir {directory}", exists,
                                "exists" if exists else "missing"))
            if self.state.vault is not None and self.state.vault.unlocked:
                try:
                    total = self.state.vault.manifest().total_files()
                    results.append(("manifest readable", True, f"{total} entries"))
                except Exception as exc:  # noqa: BLE001
                    results.append(("manifest readable", False, str(exc)))
            else:
                results.append(("manifest readable", None, "no unlocked backup"))
            results.append(("secret service", secrets.available(),
                            "available" if secrets.available() else "unavailable"))
            return results

        def done(results) -> None:
            lines = []
            for name, ok, detail in results:
                marker = "PASS" if ok is True else "FAIL" if ok is False else "SKIP"
                lines.append(f"[{marker}] {name}: {detail}")
            self.window.show_report("Self-check", "\n".join(lines))

        run_in_thread(work, name="self-check", on_success=done,
                      on_error=lambda exc: self.window.show_report("Self-check failed", str(exc)))

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
