# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Feedback form link tests: prefilling, omission and the privacy boundary."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from devicedock import __version__, feedback
from devicedock.models import DeviceInfo


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_url_prefills_every_available_value() -> None:
    facts = feedback.FeedbackFacts(
        app_version="1.0.0",
        distribution="Ubuntu 24.04.5 LTS",
        device_model="iPhone 13",
        ios_version="17.4",
    )
    url = feedback.build_feedback_url(facts)
    assert url.startswith(feedback.BASE_URL + "?")
    assert _query(url) == {
        "usp": ["pp_url"],
        feedback.VERSION_FIELD: ["1.0.0"],
        feedback.DISTRIBUTION_FIELD: ["Ubuntu 24.04.5 LTS"],
        feedback.MODEL_FIELD: ["iPhone 13"],
        feedback.IOS_FIELD: ["17.4"],
    }


def test_url_omits_unavailable_values() -> None:
    facts = feedback.FeedbackFacts(app_version="2.0.0", distribution="")
    url = feedback.build_feedback_url(facts)
    query = _query(url)
    assert query[feedback.VERSION_FIELD] == ["2.0.0"]
    assert feedback.DISTRIBUTION_FIELD not in query
    assert feedback.MODEL_FIELD not in query
    assert feedback.IOS_FIELD not in query


def test_disabled_technical_information_opens_the_plain_form() -> None:
    facts = feedback.FeedbackFacts(
        app_version="1.0.0",
        distribution="Debian GNU/Linux 13 (trixie)",
        device_model="iPhone 15",
        ios_version="18.0",
    )
    assert feedback.build_feedback_url(facts, include_technical=False) == feedback.BASE_URL
    assert "?" not in feedback.build_feedback_url(facts, include_technical=False)


def test_link_never_contains_device_identifiers() -> None:
    device = DeviceInfo(
        udid="00008110-001A2C3D4E5F6G7H",
        name="Test iPhone",
        product_type="iPhone14,5",
        product_version="17.4",
        build_version="21E219",
        serial_number="F2LX12345678",
        imei="353061234567890",
        phone_number="+61 400 000 000",
    )
    facts = feedback.collect_facts([device])
    url = feedback.build_feedback_url(facts)
    for secret in (device.udid, device.name, device.serial_number, device.imei,
                   device.phone_number, device.build_version):
        assert secret not in url
    assert facts.device_model == "iPhone 13"
    assert facts.ios_version == "17.4"


def test_no_connected_device_omits_device_fields() -> None:
    facts = feedback.collect_facts([])
    assert facts.app_version == __version__
    assert facts.device_model == ""
    assert facts.ios_version == ""
    query = _query(feedback.build_feedback_url(facts))
    assert feedback.MODEL_FIELD not in query
    assert feedback.IOS_FIELD not in query


def test_device_error_does_not_produce_hardware_values() -> None:
    device = DeviceInfo(udid="abc", error="usbmuxd is not running")
    facts = feedback.collect_facts([device])
    assert facts.device_model == ""
    assert facts.ios_version == ""


def test_distribution_comes_from_os_release(tmp_path) -> None:
    path = tmp_path / "os-release"
    path.write_text(
        'NAME="Ubuntu"\nVERSION="24.04.5 LTS (Noble Numbat)"\n'
        'PRETTY_NAME="Ubuntu 24.04.5 LTS"\nID=ubuntu\n',
        encoding="utf-8",
    )
    assert feedback.distribution_label(path) == "Ubuntu 24.04.5 LTS"
    assert feedback.collect_facts([], os_release_path=path).distribution == \
        "Ubuntu 24.04.5 LTS"


def test_distribution_falls_back_to_name_and_version(tmp_path) -> None:
    path = tmp_path / "os-release"
    path.write_text('NAME=Arch\nVERSION_ID="rolling"\n', encoding="utf-8")
    assert feedback.distribution_label(path) == "Arch rolling"


def test_missing_os_release_is_omitted(tmp_path) -> None:
    path = tmp_path / "missing"
    assert feedback.distribution_label(path) == ""
    url = feedback.build_feedback_url(feedback.collect_facts([], os_release_path=path))
    assert feedback.DISTRIBUTION_FIELD not in _query(url)
