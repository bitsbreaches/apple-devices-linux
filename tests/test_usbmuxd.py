# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Startup diagnostics for the usbmuxd daemon socket."""

from __future__ import annotations

from devicedock.devices import libimobile


def _set_environment(monkeypatch, *, socket_present: bool, installed: bool,
                     systemd: bool, unit_state: str = "") -> None:
    monkeypatch.setattr(libimobile, "usbmuxd_socket_present", lambda: socket_present)
    monkeypatch.setattr(libimobile.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if installed or name == "systemctl"
                        else None)
    monkeypatch.setattr(libimobile, "_systemd_running", lambda: systemd)
    monkeypatch.setattr(libimobile, "_usbmuxd_unit_state", lambda: unit_state)


def test_a_present_socket_needs_no_explanation(monkeypatch) -> None:
    _set_environment(monkeypatch, socket_present=True, installed=True, systemd=True)
    assert libimobile.usbmuxd_unavailable_message() is None


def test_a_missing_daemon_says_it_is_not_installed(monkeypatch) -> None:
    _set_environment(monkeypatch, socket_present=False, installed=False, systemd=True)
    message = libimobile.usbmuxd_unavailable_message()
    assert "not installed" in message
    assert "apt install usbmuxd" in message


def test_a_stopped_service_is_started_through_systemd(monkeypatch) -> None:
    _set_environment(monkeypatch, socket_present=False, installed=True, systemd=True,
                     unit_state="enabled")
    message = libimobile.usbmuxd_unavailable_message()
    assert "systemctl start usbmuxd" in message
    assert "does not run systemd" not in message


def test_a_masked_unit_is_reported_as_administrator_policy(monkeypatch) -> None:
    _set_environment(monkeypatch, socket_present=False, installed=True, systemd=True,
                     unit_state="masked")
    message = libimobile.usbmuxd_unavailable_message()
    assert "masked" in message
    assert "systemctl unmask" in message


def test_a_system_without_systemd_gets_init_system_advice(monkeypatch) -> None:
    _set_environment(monkeypatch, socket_present=False, installed=True, systemd=False)
    message = libimobile.usbmuxd_unavailable_message()
    assert "does not run systemd" in message
    assert "systemctl start usbmuxd" not in message
