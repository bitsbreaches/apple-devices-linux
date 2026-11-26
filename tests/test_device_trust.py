from __future__ import annotations

from types import SimpleNamespace

from devicedock.devices import libimobile
from devicedock.models import DeviceInfo


def _result(code: int, output: str = ""):
    return SimpleNamespace(returncode=code, stdout=output, stderr="")


def test_pairing_validation_recognizes_existing_pair(monkeypatch):
    calls = []
    monkeypatch.setattr(libimobile.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(libimobile, "_run", lambda args, **_kwargs: calls.append(args) or _result(0))
    check = libimobile.validate_pairing("device-udid")
    assert check.state is libimobile.PairingState.TRUSTED
    assert calls == [["/usr/bin/idevicepair", "-u", "device-udid", "validate"]]


def test_pairing_validation_distinguishes_locked_and_unavailable(monkeypatch):
    monkeypatch.setattr(libimobile.shutil, "which", lambda name: f"/usr/bin/{name}")
    responses = iter((_result(1, "Passcode is set"), _result(1, "Could not connect to lockdownd")))
    monkeypatch.setattr(libimobile, "_run", lambda *_args, **_kwargs: next(responses))
    assert libimobile.validate_pairing("phone").state is libimobile.PairingState.LOCKED
    assert libimobile.validate_pairing("phone").state is libimobile.PairingState.UNAVAILABLE


def test_pairing_validation_does_not_guess_from_generic_lockdown_error(monkeypatch):
    monkeypatch.setattr(libimobile.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(libimobile, "_run", lambda *_args, **_kwargs:
                        _result(1, "Unexpected lockdown response"))
    assert libimobile.validate_pairing("phone").state is libimobile.PairingState.ERROR


def test_discovery_reuses_details_for_already_connected_device(monkeypatch):
    info_calls = []
    monkeypatch.setattr(libimobile, "list_device_ids", lambda: ["phone"])
    monkeypatch.setattr(libimobile, "device_info", lambda udid: info_calls.append(udid)
                        or DeviceInfo(udid=udid, name="Test iPhone"))
    first = libimobile.discover_devices()
    second = libimobile.discover_devices(first)
    assert first == second
    assert info_calls == ["phone"]
    third = libimobile.discover_devices(second, refresh_details=True)
    assert third == second
    assert info_calls == ["phone", "phone"]


def test_one_hundred_concurrent_device_refreshes_share_one_operation(monkeypatch):
    from devicedock.config import Settings
    from devicedock.ui import state as state_module

    state = state_module.AppState(Settings())
    monkeypatch.setattr(state_module.libimobile, "missing_required_tools", lambda: [])
    monkeypatch.setattr(state_module.libimobile, "usbmuxd_socket_present", lambda: True)
    calls = []

    def fake_run(_work, **callbacks):
        calls.append(callbacks)

    monkeypatch.setattr(state_module, "run_in_thread", fake_run)
    completed = []
    for _ in range(100):
        state.refresh_devices(lambda: completed.append(True))
    assert len(calls) == 1
    calls[0]["on_success"]([DeviceInfo(udid="phone")])
    assert len(completed) == 100
    assert state.devices[0].udid == "phone"


def test_one_hundred_unchanged_polls_do_not_repeat_lockdown_initialization(monkeypatch):
    from devicedock.config import Settings
    from devicedock.ui import state as state_module

    state = state_module.AppState(Settings())
    monkeypatch.setattr(state_module.libimobile, "missing_required_tools", lambda: [])
    monkeypatch.setattr(state_module.libimobile, "usbmuxd_socket_present", lambda: True)
    monkeypatch.setattr(state_module.libimobile, "list_device_ids", lambda: ["phone"])
    device_calls = []
    monkeypatch.setattr(state_module.libimobile, "device_info", lambda udid:
                        device_calls.append(udid) or DeviceInfo(udid=udid, name="Phone"))

    def synchronous_run(work, *, on_success, **_kwargs):
        on_success(work(None))

    monkeypatch.setattr(state_module, "run_in_thread", synchronous_run)
    events = []
    state.subscribe("devices", lambda *_args: events.append("changed"))
    for _ in range(101):
        state.refresh_devices()
    assert device_calls == ["phone"]
    assert events == ["changed"]


def test_explicit_detail_refresh_waits_for_an_active_poll(monkeypatch):
    from devicedock.config import Settings
    from devicedock.ui import state as state_module

    state = state_module.AppState(Settings())
    monkeypatch.setattr(state_module.libimobile, "missing_required_tools", lambda: [])
    monkeypatch.setattr(state_module.libimobile, "usbmuxd_socket_present", lambda: True)
    calls = []
    details = []

    def discover(_previous, *, refresh_details):
        details.append(refresh_details)
        name = "Fresh name" if refresh_details else "Cached name"
        return [DeviceInfo(udid="phone", name=name)]

    monkeypatch.setattr(state_module.libimobile, "discover_devices", discover)

    def fake_run(work, **callbacks):
        calls.append((work, callbacks))

    monkeypatch.setattr(state_module, "run_in_thread", fake_run)
    state.refresh_devices()
    state.refresh_devices(refresh_details=True)
    assert len(calls) == 1
    calls[0][1]["on_success"](calls[0][0](None))
    assert len(calls) == 2
    calls[1][1]["on_success"](calls[1][0](None))
    assert state.devices[0].name == "Fresh name"
    assert details == [False, True]


def test_device_state_raises_when_the_device_is_unreachable(monkeypatch):
    """A failed query must never be reported as “not locked” before a destructive action."""
    monkeypatch.setattr(libimobile, "device_info",
                        lambda udid, timeout=25.0: DeviceInfo(udid=udid, error="not connected"))

    try:
        libimobile.read_device_state("phone")
    except RuntimeError as exc:
        assert "not connected" in str(exc)
    else:  # pragma: no cover - the assertion below is the failure
        raise AssertionError("read_device_state must raise for an unreachable device")


def test_device_state_reports_the_activation_flag_and_state(monkeypatch):
    monkeypatch.setattr(libimobile, "device_info",
                        lambda udid, timeout=25.0: DeviceInfo(
                            udid=udid, activation_locked=True, activation_state="Activated"))
    info = libimobile.read_device_state("phone")
    assert info.activation_locked is True
    assert info.activation_state == "Activated"


# --------------------------------------------------------------- repeated monitoring
def test_a_trusted_device_is_validated_without_ever_pairing(monkeypatch):
    """100 monitoring cycles must never run `idevicepair pair` (no trust dialog)."""
    commands: list[list[str]] = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        return _result(0, "SUCCESS: Validated pairing with device phone")

    monkeypatch.setattr(libimobile.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(libimobile, "_run", fake_run)

    for _ in range(100):
        assert libimobile.validate_pairing("phone").state is libimobile.PairingState.TRUSTED

    assert len(commands) == 100
    subcommands = [args[3] if len(args) > 3 else "" for args in commands]
    assert set(subcommands) == {"validate"}, subcommands
    assert not any("pair" in args[3:] for args in commands), "pairing must never be created"


def test_a_preflight_that_already_proved_trust_skips_validation(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(libimobile.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(libimobile, "_run", lambda args, **_kw: calls.append(args) or _result(0))
    monkeypatch.setattr(libimobile, "usbmuxd_socket_present", lambda: True)
    monkeypatch.setattr(libimobile, "list_device_ids", lambda: ["phone"])

    from devicedock.devices import media_files

    for _ in range(100):
        assert media_files.file_browser_preflight("phone", pairing_verified=True) is None
    assert calls == [], f"a device proven trusted must not be queried again: {calls}"

    assert media_files.file_browser_preflight("phone") is None
    assert calls and calls[0][3] == "validate"


def test_communication_failures_are_not_reported_as_trust_failures(monkeypatch):
    monkeypatch.setattr(libimobile.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(libimobile, "usbmuxd_socket_present", lambda: True)
    monkeypatch.setattr(libimobile, "list_device_ids", lambda: ["phone"])

    from devicedock.devices import media_files

    for detail in ("Could not connect to lockdownd", "device is locked", "Unexpected lockdown response"):
        monkeypatch.setattr(libimobile, "_run",
                            lambda args, _d=detail, **_kw: _result(1, _d))
        problem = media_files.file_browser_preflight("phone")
        assert problem is not None
        assert "Trust This Computer" not in problem, (detail, problem)


def test_monitoring_reuses_device_details_across_polling_cycles(monkeypatch):
    """One hundred polls must not re-query Lockdown, and must not leak state."""
    from devicedock.config import Settings
    from devicedock.ui import state as state_module

    fresh_reads: list[str] = []
    monkeypatch.setattr(state_module.libimobile, "list_device_ids", lambda: ["phone"])
    monkeypatch.setattr(state_module.libimobile, "missing_required_tools", lambda: [])
    monkeypatch.setattr(state_module.libimobile, "usbmuxd_socket_present", lambda: True)

    def fake_device_info(udid, timeout: float = 25.0):
        fresh_reads.append(udid)
        return DeviceInfo(udid=udid, name="Phone", activation_locked=False)

    monkeypatch.setattr(state_module.libimobile, "device_info", fake_device_info)

    # Exercise the application's own caching implementation, keeping a reference to it
    # before it is wrapped.
    original_discover = libimobile.discover_devices
    monkeypatch.setattr(
        state_module.libimobile, "discover_devices",
        lambda previous, refresh_details=False:
        original_discover(previous, refresh_details=refresh_details),
    )

    app_state = state_module.AppState(Settings())
    emitted: list[str] = []
    app_state.subscribe("devices", lambda *_: emitted.append("devices"))

    def synchronous_run(work, *, name="job", on_success=None, on_error=None, on_done=None):
        try:
            result = work(None)
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
        else:
            if on_success:
                on_success(result)
        if on_done:
            on_done()
        return None, None

    monkeypatch.setattr(state_module, "run_in_thread", synchronous_run)

    for _ in range(100):
        app_state.refresh_devices()
        assert app_state._devices_refreshing is False
        assert app_state._device_refresh_waiters == []

    assert len(fresh_reads) == 1, f"Lockdown was queried {len(fresh_reads)} times in 100 polls"
    # The device list only changed once, so listeners were told once.
    assert emitted == ["devices"], emitted
