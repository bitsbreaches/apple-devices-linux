# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Destructive-operation safety: pairing, Activation Lock and activation are distinct."""

from __future__ import annotations

import pytest

from devicedock.devices import safety
from devicedock.devices.erase_engine import EraseRejected, build_erase_plan
from devicedock.devices.libimobile import PairingCheck, PairingState
from devicedock.devices.restore_engine import RestoreRejected, build_restore_plan

UDID = "0000811000000000000000AA"
TRUSTED = PairingCheck(PairingState.TRUSTED)


def _state(**kwargs) -> safety.DeviceSafetyState:
    values = {"udid": UDID, "pairing": TRUSTED}
    values.update(kwargs)
    return safety.DeviceSafetyState(**values)


def test_a_trusted_activated_unlocked_device_may_proceed() -> None:
    assert safety.refusal(_state(activation_locked=False, activation_state="Activated"),
                          action="a backup restore") is None


def test_activation_lock_refuses_every_destructive_action() -> None:
    for action in ("an erase", "a backup restore"):
        reason = safety.refusal(_state(activation_locked=True, activation_state="Activated"),
                                action=action)
        assert reason is not None
        assert "Find My iPhone" in reason
        assert "Activation Lock" in reason, "the two names must be tied together"
        assert action in reason
        assert "Settings → [your name] → Find My" in reason


def test_an_unpaired_device_is_refused_with_trust_guidance() -> None:
    state = _state(pairing=PairingCheck(PairingState.PAIRING_REQUIRED, "not paired"))
    reason = safety.refusal(state, action="an erase")
    assert reason is not None
    assert "Trust This Computer" in reason
    assert "Find My" not in reason, "a pairing failure must not be blamed on Find My iPhone"


def test_a_locked_device_is_refused_without_mentioning_trust() -> None:
    state = _state(pairing=PairingCheck(PairingState.LOCKED, "passcode is set"))
    reason = safety.refusal(state, action="an erase")
    assert reason is not None
    assert "passcode" in reason
    assert "Trust This Computer" not in reason


@pytest.mark.parametrize("state_name", [PairingState.UNAVAILABLE, PairingState.ERROR])
def test_unreadable_pairing_state_fails_closed(state_name) -> None:
    state = _state(pairing=PairingCheck(state_name, "socket closed"))
    reason = safety.refusal(state, action="a factory reset")
    assert reason is not None
    assert "could not be read" in reason
    assert "nothing was sent" in reason


def test_unknown_activation_lock_fails_closed() -> None:
    """A silent device must not be treated as “not locked”."""
    reason = safety.refusal(_state(activation_locked=None), action="an erase")
    assert reason is not None
    assert "did not report whether Find My iPhone is on" in reason


def test_an_unactivated_device_is_refused_with_its_own_explanation() -> None:
    reason = safety.refusal(_state(activation_locked=False, activation_state="Unactivated"),
                            action="a backup restore")
    assert reason is not None
    assert "setup" in reason
    assert "Find My" not in reason


def test_engines_refuse_an_unactivated_device_on_their_own(tmp_path) -> None:
    with pytest.raises(EraseRejected) as erase:
        build_erase_plan(UDID, "iPhone", activation_locked=False,
                         activation_state="Unactivated")
    assert "setup" in str(erase.value)

    import plistlib

    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "Info.plist").write_bytes(plistlib.dumps({
        "Target Identifier": UDID, "Product Type": "iPhone14,8", "Product Version": "27.2.1"}))
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    (backup / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": "finished"}))
    with pytest.raises(RestoreRejected) as restore:
        build_restore_plan(backup, UDID, activation_locked=False,
                           activation_state="Unactivated")
    assert "setup" in str(restore.value)


def test_preflight_short_circuits_when_the_device_is_not_trusted(monkeypatch) -> None:
    """Without trust, Lockdown is not queried — the pairing state is the answer."""
    monkeypatch.setattr(safety.libimobile, "validate_pairing",
                        lambda udid, timeout=25.0: PairingCheck(
                            PairingState.PAIRING_REQUIRED, "not paired"))
    queried: list[str] = []
    monkeypatch.setattr(safety.libimobile, "read_device_state",
                        lambda udid, timeout=25.0: queried.append(udid))

    state = safety.destructive_preflight(UDID)

    assert queried == []
    assert state.pairing.state is PairingState.PAIRING_REQUIRED


def test_preflight_reads_lock_and_activation_state_from_a_trusted_device(monkeypatch) -> None:
    from devicedock.models import DeviceInfo

    monkeypatch.setattr(safety.libimobile, "validate_pairing",
                        lambda udid, timeout=25.0: TRUSTED)
    monkeypatch.setattr(
        safety.libimobile, "read_device_state",
        lambda udid, timeout=25.0: DeviceInfo(udid=udid, activation_locked=False,
                                              activation_state="Activated"))

    state = safety.destructive_preflight(UDID)

    assert state.activation_locked is False
    assert state.activation_state == "Activated"
    assert safety.refusal(state, action="an erase") is None


def test_preflight_propagates_an_unreachable_device(monkeypatch) -> None:
    monkeypatch.setattr(safety.libimobile, "validate_pairing",
                        lambda udid, timeout=25.0: TRUSTED)

    def unreachable(udid, timeout=25.0):
        raise RuntimeError("Unable to retrieve device list!")

    monkeypatch.setattr(safety.libimobile, "read_device_state", unreachable)

    with pytest.raises(RuntimeError):
        safety.destructive_preflight(UDID)
