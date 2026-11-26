# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Safety rules shared by the two operations that destroy device data.

Restore (``idevicebackup2 restore``) and Restore Device (the device's own *Erase All Content
and Settings* request) both need the same facts before anything is sent:

* **pairing/trust** — whether this computer holds a valid pair record for the device,
* **Find My iPhone / Activation Lock** — ``fm-activation-locked`` from Lockdown, which iOS
  enforces against both a restore and an erase,
* **activation state** — whether the device has been through setup at all; an unactivated
  device (the “Hello” screen) has nothing to restore onto and nothing to erase.

Pairing, Activation Lock and activation are *different* states and are reported separately:
a trusted device can still be locked, and an activated device may be unpaired. Where a fact
cannot be read, the refusal says so instead of guessing — these operations fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import libimobile
from .libimobile import PairingCheck, PairingState

#: The single Find My iPhone / Activation Lock explanation, named for the action attempted.
_FIND_MY_GUIDANCE = (
    "On the iPhone:\n"
    "1. Unlock it and open Settings → [your name] → Find My → Find My iPhone.\n"
    "2. Switch Find My iPhone off and enter the Apple ID password.\n"
    "3. Keep the iPhone connected and unlocked, then try again."
)

#: What the user may be told about a device that has not been set up.
UNACTIVATED_REASON = (
    "This iPhone has not been set up yet: it is still at the “Hello” setup screen. "
    "Complete setup first (or set it up from a backup on another device), then try again."
)


def activation_lock_reason(action: str) -> str:
    """Explain why iOS refuses ``action`` while Find My iPhone is on.

    Find My iPhone and Activation Lock are the same protection: the switch is Find My
    iPhone, and the lock it creates is Activation Lock.
    """
    return (
        f"This iPhone still has Find My iPhone turned on, and iOS will not accept {action} "
        "until it is off (Activation Lock is the same protection that stops a stolen phone "
        f"from being set up again).\n\n{_FIND_MY_GUIDANCE}"
    )


@dataclass(frozen=True)
class DeviceSafetyState:
    """Everything a destructive operation must know before it starts."""

    udid: str
    pairing: PairingCheck
    #: ``True``/``False`` from ``fm-activation-locked``; ``None`` when the device is silent.
    activation_locked: bool | None = None
    #: ``"Activated"``, ``"Unactivated"`` or ``""`` when the device does not report it.
    activation_state: str = ""


def refusal(state: DeviceSafetyState, *, action: str) -> str | None:
    """Return the reason ``action`` must not run, or ``None`` when it may proceed."""
    if state.pairing.state is PairingState.PAIRING_REQUIRED:
        return (
            "This iPhone has no valid pairing with this computer, so it cannot be "
            "written to. Unlock it, approve “Trust This Computer”, then try again.\n\n"
            f"Detail: {state.pairing.detail or 'not paired with this host'}"
        )
    if state.pairing.state is PairingState.LOCKED:
        return ("The iPhone is locked. Unlock it with its passcode, keep it connected, then "
                "try again.")
    if state.pairing.state in (PairingState.UNAVAILABLE, PairingState.ERROR):
        return (
            "The iPhone's state could not be read, so nothing was sent to it. Reconnect and "
            "unlock the phone, then try again.\n\n"
            f"Detail: {state.pairing.detail or state.pairing.state.value}"
        )
    if state.activation_locked:
        return activation_lock_reason(action)
    if state.activation_state.casefold() == "unactivated":
        return UNACTIVATED_REASON
    if state.activation_locked is None:
        return (
            "This iPhone did not report whether Find My iPhone is on, so nothing was sent "
            "to it. Unlock the phone, reconnect it, and try again: iOS will not accept "
            f"{action} while Activation Lock is enabled."
        )
    return None


def destructive_preflight(udid: str, timeout: float = 25.0) -> DeviceSafetyState:
    """Read the pairing, Activation Lock and activation state for a destructive action.

    Raises when the device cannot be read at all; callers must treat that as a refusal.
    """
    pairing = libimobile.validate_pairing(udid, timeout=timeout)
    if pairing.state is not PairingState.TRUSTED:
        # Without trust there is nothing else worth asking: Lockdown would refuse too.
        return DeviceSafetyState(udid=udid, pairing=pairing)
    info = libimobile.read_device_state(udid, timeout=timeout)
    return DeviceSafetyState(
        udid=udid,
        pairing=pairing,
        activation_locked=info.activation_locked,
        activation_state=info.activation_state,
    )
