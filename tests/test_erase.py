# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Activation Lock detection and the factory-reset plan (no device is touched)."""

from __future__ import annotations

from pathlib import Path

import pytest

from devicedock.asyncutil import Job
from devicedock.devices.erase_engine import (
    EraseEngine,
    EraseRejected,
    build_erase_plan,
)
from devicedock.devices.libimobile import parse_activation_lock

UDID = "0000811000000000000000AA"


@pytest.mark.parametrize("value,expected", [
    ("WUVT", True),     # base64("YES"), what ideviceinfo prints
    ("Tk8=", False),    # base64("NO")
    ("YES", True),
    ("no", False),
    ("", None),
    ("garbage", None),
])
def test_parse_activation_lock(value: str, expected: bool | None) -> None:
    assert parse_activation_lock({"fm-activation-locked": value}) is expected


def test_parse_activation_lock_missing_key() -> None:
    assert parse_activation_lock({"ProductType": "iPhone14,8"}) is None


def test_erase_plan_rejects_activation_lock() -> None:
    with pytest.raises(EraseRejected) as excinfo:
        build_erase_plan(UDID, "Test iPhone", activation_locked=True)
    assert "Find My" in str(excinfo.value)


def test_erase_plan_accepts_an_unlocked_device() -> None:
    plan = build_erase_plan(UDID, "Test iPhone", "iPhone14,8", "27.2.1",
                            activation_locked=False)
    assert plan.udid == UDID
    assert plan.device_name == "Test iPhone"


def test_cancelled_erase_never_contacts_the_device(tmp_path: Path) -> None:
    plan = build_erase_plan(UDID, "Test iPhone")
    job = Job("erase")
    job.cancel()
    engine = EraseEngine(plan, job=job, log_dir=tmp_path / "logs",
                         erase_dir=tmp_path / "channel")
    result = engine.run()
    assert result.status == "cancelled"
    assert not (tmp_path / "channel").exists()


def test_erase_sends_the_device_erase_request(tmp_path: Path, monkeypatch) -> None:
    from devicedock.devices import libimobile

    calls: list[str] = []
    monkeypatch.setattr(libimobile, "erase_device_request", calls.append)

    plan = build_erase_plan(UDID, "Test iPhone")
    engine = EraseEngine(plan, log_dir=tmp_path / "logs", erase_dir=tmp_path / "channel")
    result = engine.run()

    assert result.status == "completed"
    assert calls == [UDID]
    assert (tmp_path / "channel").is_dir()
    assert engine.log_path is not None and engine.log_path.exists()
