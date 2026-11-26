# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Device communication through libimobiledevice command-line tools.

Everything here is a thin, testable wrapper around the official user-space tools
(``idevice_id``, ``ideviceinfo``, ``idevicebackup2``). No private frameworks are used.
Pairing/trust is handled by usbmuxd + libimobiledevice; explicit user transfers and restore
operations write to the selected device.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..asyncutil import Job
from ..models import DeviceInfo

log = logging.getLogger(__name__)

REQUIRED_TOOLS = ("idevice_id", "ideviceinfo")
OPTIONAL_TOOLS = ("idevicebackup2", "idevicepair", "idevicediagnostics", "usbmuxd",
                  "ifuse", "fusermount3", "fusermount")

_INFO_KEYS = {
    "DeviceName": "name",
    "ProductType": "product_type",
    "ProductVersion": "product_version",
    "BuildVersion": "build_version",
    "SerialNumber": "serial_number",
    "IMEI": "imei",
    "PhoneNumber": "phone_number",
    "ConnectionType": "connection_type",
    "ActivationState": "activation_state",
}


class LibiMobileUnavailable(RuntimeError):
    """Raised when libimobiledevice binaries are not installed."""


class PairingState(str, Enum):
    TRUSTED = "trusted"
    PAIRING_REQUIRED = "pairing-required"
    LOCKED = "locked"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class PairingCheck:
    state: PairingState
    detail: str = ""


def validate_pairing(udid: str, timeout: float = 12.0) -> PairingCheck:
    """Validate an existing pair record without creating or deleting one.

    ``idevicepair validate`` is specifically a validation operation. If it is unavailable,
    a successful ``ideviceinfo`` session is accepted as proof of trust, while failures are
    classified conservatively; generic Lockdown/socket errors never imply missing trust.
    """
    pairing_tool = shutil.which("idevicepair")
    if pairing_tool:
        args = [pairing_tool, "-u", udid, "validate"]
    else:
        info_tool = shutil.which("ideviceinfo")
        if not info_tool:
            return PairingCheck(PairingState.ERROR, "idevicepair and ideviceinfo are not installed")
        args = [info_tool, "-u", udid]
    try:
        result = _run(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return PairingCheck(PairingState.UNAVAILABLE, "The Lockdown service did not respond in time.")
    except OSError as exc:
        return PairingCheck(PairingState.ERROR, str(exc))
    detail = (result.stderr or result.stdout).strip()
    if result.returncode == 0:
        return PairingCheck(PairingState.TRUSTED)
    folded = detail.casefold()
    if any(token in folded for token in (
        "not paired with this host", "not paired with this computer", "invalid host id",
        "invalid hostid", "pairing dialog response pending", "accept the trust dialog",
        "user denied the trust dialog",
    )):
        return PairingCheck(PairingState.PAIRING_REQUIRED, detail)
    if any(token in folded for token in ("passcode is set", "password protected", "device is locked")):
        return PairingCheck(PairingState.LOCKED, detail)
    if any(token in folded for token in (
        "no device", "device not found", "not connected", "disconnected", "could not connect",
        "connection refused", "socket", "permission denied", "operation not permitted",
        "timed out", "timeout",
    )):
        return PairingCheck(PairingState.UNAVAILABLE, detail)
    return PairingCheck(PairingState.ERROR, detail or f"Pair validation exited with status {result.returncode}.")


def tool_status() -> dict[str, bool]:
    return {name: shutil.which(name) is not None for name in REQUIRED_TOOLS + OPTIONAL_TOOLS}


def missing_required_tools() -> list[str]:
    return [name for name in REQUIRED_TOOLS if shutil.which(name) is None]


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    log.debug("Running %s", " ".join(args))
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def usbmuxd_socket_present() -> bool:
    """True when the usbmuxd daemon socket exists (device enumeration needs it)."""
    return Path("/var/run/usbmuxd").exists() or Path("/run/usbmuxd").exists()


def _systemd_running() -> bool:
    """True when systemd is the running service manager (not just installed)."""
    return Path("/run/systemd/system").is_dir()


def _usbmuxd_unit_state() -> str:
    """systemd's view of the usbmuxd unit: enabled, disabled, static, masked…

    An empty string means systemctl is unavailable or the unit is not known.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return ""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [systemctl, "is-enabled", "usbmuxd.service"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or result.stderr).strip().lower()


def usbmuxd_unavailable_message() -> str | None:
    """Explain how to make usbmuxd available, or return None when it is reachable.

    The Debian package starts usbmuxd during installation where systemd allows it. This
    message therefore distinguishes an absent daemon, an administrator policy that blocks
    the service, and an ordinary "not started" state so the advice is always correct.
    """
    if usbmuxd_socket_present():
        return None
    if shutil.which("usbmuxd") is None:
        return ("usbmuxd is not installed, so no device can be detected. Install it with "
                "“sudo apt install usbmuxd”, then reconnect the device.")
    if _systemd_running():
        state = _usbmuxd_unit_state()
        if state.startswith("masked"):
            return ("usbmuxd is installed but its service is masked by the administrator, "
                    "so it cannot be started. Ask the administrator to run "
                    "“sudo systemctl unmask usbmuxd && sudo systemctl enable --now "
                    "usbmuxd”.")
        return ("usbmuxd is installed but not running, so no device can be detected. Start "
                "it with “sudo systemctl start usbmuxd”, then reconnect the device. "
                "Installing the device-dock package starts it automatically.")
    return ("usbmuxd is installed but not running, so no device can be detected. This "
            "system does not run systemd, so start usbmuxd with your service manager (for "
            "example “sudo usbmuxd” or “sudo service usbmuxd start”), then reconnect the "
            "device.")


def list_device_ids() -> list[str]:
    """Return the UDIDs currently visible to usbmuxd."""
    if shutil.which("idevice_id") is None:
        raise LibiMobileUnavailable("idevice_id is not installed")
    proc = _run(["idevice_id", "-l"], timeout=10)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if not usbmuxd_socket_present():
            raise RuntimeError(
                usbmuxd_unavailable_message()
                or "usbmuxd is not running, so no device can be detected."
            )
        raise RuntimeError(detail or "idevice_id could not list devices")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def parse_info_output(text: str) -> dict[str, str]:
    """Parse ``ideviceinfo`` key/value output into a dict."""
    info: dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            info[key.strip()] = value.strip()
    return info


def parse_activation_lock(values: dict[str, str]) -> bool | None:
    """Decode the lockdown ``fm-activation-locked`` flag.

    ``ideviceinfo`` prints the flag base64-encoded because the device stores it as a data
    value (``WUVT`` → ``YES``); a plain ``YES``/``NO`` is accepted too. ``None`` means the
    device did not report the flag.
    """
    raw = (values.get("fm-activation-locked") or "").strip()
    if not raw:
        return None
    text = raw
    try:
        decoded = base64.b64decode(raw, validate=True).decode("ascii", "ignore").strip()
    except ValueError:
        decoded = ""
    if decoded:
        text = decoded
    folded = text.casefold()
    if folded in {"yes", "true", "1"}:
        return True
    if folded in {"no", "false", "0"}:
        return False
    return None


def device_info(udid: str, timeout: float = 25.0) -> DeviceInfo:
    """Read one device's identity and firmware information."""
    if shutil.which("ideviceinfo") is None:
        raise LibiMobileUnavailable("ideviceinfo is not installed")
    proc = _run(["ideviceinfo", "-u", udid], timeout=timeout)
    if proc.returncode != 0:
        message = proc.stderr.strip() or "ideviceinfo failed"
        return DeviceInfo(udid=udid, error=message)
    raw = parse_info_output(proc.stdout)
    info = DeviceInfo(udid=udid, raw=raw)
    info.activation_locked = parse_activation_lock(raw)
    for key, attr in _INFO_KEYS.items():
        if key in raw:
            setattr(info, attr, raw[key])
    return info


def discover_devices(previous: list[DeviceInfo] | None = None, *,
                     refresh_details: bool = False) -> list[DeviceInfo]:
    """Enumerate devices, reusing details for connected devices between polling cycles."""
    cached = {device.udid: device for device in (previous or [])}
    devices: list[DeviceInfo] = []
    for udid in list_device_ids():
        if not refresh_details and udid in cached:
            devices.append(cached[udid])
        else:
            devices.append(device_info(udid))
    return devices


def read_device_state(udid: str, timeout: float = 25.0) -> DeviceInfo:
    """Re-read one device's Lockdown state before a destructive operation.

    A cached record can be stale: the user may have just turned Find My iPhone off. An
    unreachable device raises instead of reporting defaults, so a destructive action is
    never offered on the strength of a failed query.
    """
    info = device_info(udid, timeout=timeout)
    if info.error:
        raise RuntimeError(info.error)
    return info


def idevicebackup2_version() -> str:
    if shutil.which("idevicebackup2") is None:
        return "not installed"
    try:
        proc = _run(["idevicebackup2", "--version"], timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    text = (proc.stdout or proc.stderr).strip().splitlines()
    return text[0] if text else "unknown"


def create_backup(
    udid: str,
    destination: Path,
    job: Job,
    *,
    full: bool = False,
    password: str | None = None,
    on_progress=None,
):
    """Create a backup with the ``idevicebackup2`` engine.

    The tool does the transfer itself and writes straight into ``<destination>/<UDID>/``; this
    process only observes it, so no backup data passes through Python.

    ``password`` is accepted for call-site compatibility and is deliberately **not** used:
    backup encryption is a device setting, applied beforehand by
    :func:`set_backup_encryption`, and ``idevicebackup2 backup`` never takes a password. A
    password supplied here has no effect on whether the resulting backup is encrypted.
    """
    from .backup_engine import create_backup as _cli_backup

    if password:
        log.debug("create_backup: password ignored; encryption is configured on the device "
                  "by set_backup_encryption()")
    return _cli_backup(udid, destination, job, full=full, on_progress=on_progress)


def backup_encryption_state(udid: str) -> bool | None:
    """Return whether the device encrypts backups (``None`` when it cannot be read)."""
    result = _run(["ideviceinfo", "-u", udid, "-q", "com.apple.mobile.backup",
                       "-k", "WillEncrypt"], timeout=20)
    if result.returncode != 0:
        return None
    value = result.stdout.strip().casefold()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    return None


def erase_device_request(udid: str) -> None:
    """Send MobileBackup2's EraseDevice request through LGPL libimobiledevice."""
    library = ctypes.util.find_library("imobiledevice-1.0")
    plist_library = ctypes.util.find_library("plist-2.0")
    if not library or not plist_library:
        raise LibiMobileUnavailable("libimobiledevice and libplist shared libraries are required")
    mobile = ctypes.CDLL(library)
    plist = ctypes.CDLL(plist_library)
    pointer = ctypes.c_void_p
    device = pointer()
    client = pointer()
    options = pointer()
    try:
        mobile.idevice_new.argtypes = [ctypes.POINTER(pointer), ctypes.c_char_p]
        mobile.idevice_new.restype = ctypes.c_int
        mobile.idevice_free.argtypes = [pointer]
        mobile.idevice_free.restype = ctypes.c_int
        mobile.mobilebackup2_client_start_service.argtypes = [
            pointer, ctypes.POINTER(pointer), ctypes.c_char_p]
        mobile.mobilebackup2_client_start_service.restype = ctypes.c_int
        mobile.mobilebackup2_send_message.argtypes = [pointer, ctypes.c_char_p, pointer]
        mobile.mobilebackup2_send_message.restype = ctypes.c_int
        mobile.mobilebackup2_client_free.argtypes = [pointer]
        mobile.mobilebackup2_client_free.restype = ctypes.c_int

        plist.plist_new_dict.restype = pointer
        plist.plist_new_string.argtypes = [ctypes.c_char_p]
        plist.plist_new_string.restype = pointer
        plist.plist_dict_set_item.argtypes = [pointer, ctypes.c_char_p, pointer]
        plist.plist_dict_set_item.restype = None
        plist.plist_free.argtypes = [pointer]
        plist.plist_free.restype = None

        result = mobile.idevice_new(ctypes.byref(device), udid.encode())
        if result != 0:
            raise RuntimeError(f"Could not connect to device {udid} (libimobiledevice error {result})")
        result = mobile.mobilebackup2_client_start_service(device, ctypes.byref(client),
                                                           b"DeviceDock")
        if result != 0:
            raise RuntimeError(f"Could not start the erase service (libimobiledevice error {result})")
        options = plist.plist_new_dict()
        value = plist.plist_new_string(udid.encode())
        if not options or not value:
            raise RuntimeError("Could not prepare the device erase request")
        plist.plist_dict_set_item(options, b"TargetIdentifier", value)
        result = mobile.mobilebackup2_send_message(client, b"EraseDevice", options)
        if result != 0:
            raise RuntimeError(f"The device rejected the erase request (libimobiledevice error {result})")
    finally:
        if options:
            plist.plist_free(options)
        if client:
            mobile.mobilebackup2_client_free(client)
        if device:
            mobile.idevice_free(device)


def set_backup_encryption(udid: str, enable: bool, password: str | None = None) -> tuple[bool, str]:
    """Turn backup encryption on (setting ``password``) or off.

    Returns ``(succeeded, message)`` with the tool's own words, so a rejected password or a
    locked device is reported exactly as the device explained it.
    """
    args = ["idevicebackup2", "-u", udid, "encryption", "on" if enable else "off"]
    if password:
        args.append(password)
    result = _run(args, timeout=180)
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if result.returncode == 0 and "error" not in output.casefold():
        return True, ("Backup encryption enabled." if enable
                      else "Backup encryption disabled.")
    return False, output or f"idevicebackup2 exited with status {result.returncode}"


def _normalise_identifier(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum()).upper()


def usb_link(identifier: str | None = None) -> tuple[int | None, str | None]:
    """Return ``(megabits_per_second, usb_version)`` of the device's USB link.

    Read from sysfs (``/sys/bus/usb/devices/*/serial``), which is the only way to see the
    negotiated link speed. iOS devices publish the ECID form of their UDID there
    (``00008110000E24C22EBA401E``), so the identifier is compared with separators removed.
    ``None`` means the device is not on a USB bus we can identify.
    """
    wanted = _normalise_identifier(identifier or "")
    if not wanted:
        return None, None
    base = "/sys/bus/usb/devices"
    try:
        entries = os.listdir(base)
    except OSError:
        return None, None
    for entry in entries:
        directory = os.path.join(base, entry)
        try:
            with open(os.path.join(directory, "serial"), encoding="utf-8") as handle:
                if _normalise_identifier(handle.read().strip()) != wanted:
                    continue
            with open(os.path.join(directory, "speed"), encoding="utf-8") as handle:
                # sysfs reports Mbps already (480, 5000, 10000).
                speed = int(float(handle.read().strip()))
            version = None
            try:
                with open(os.path.join(directory, "version"), encoding="utf-8") as handle:
                    version = handle.read().strip()
            except OSError:
                pass
            return speed, version
        except (OSError, ValueError):
            continue
    return None, None


def usb_link_note(identifier: str | None = None) -> str | None:
    """Explain a slow USB link, because it dominates backup throughput."""
    speed, version = usb_link(identifier)
    if speed is None:
        return None
    label = f"USB {version or '?'} · {speed} Mbps"
    if speed <= 480:
        return (f"{label}. This link caps the transfer at roughly 35–40 MB/s; it is a "
                "hardware limit on Lightning iPhones, while USB-C models need a USB 3 cable "
                "and port to go faster.")
    return f"{label}."
