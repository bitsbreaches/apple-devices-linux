# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Feedback form details: what may be shared and how the prefilled link is built.

Only the four values below ever leave this machine, and only when the user asks for the
form to be opened with technical information. Device identifiers, serial numbers, device
names and anything personal are deliberately not part of :class:`FeedbackFacts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlencode

from . import __version__
from .models import DeviceInfo

BASE_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLScWVZpflIckbvxDuOyKWSz4Z6hQGk-QaZQU3lDPUjl5HZVHxQ/viewform"
)
VERSION_FIELD = "entry.832912332"
DISTRIBUTION_FIELD = "entry.1384178807"
MODEL_FIELD = "entry.830883738"
IOS_FIELD = "entry.569072799"

OS_RELEASE_PATH = Path("/etc/os-release")


@dataclass(frozen=True)
class FeedbackFacts:
    """The technical values that can be prefilled in the feedback form."""

    app_version: str = __version__
    distribution: str = ""
    device_model: str = ""
    ios_version: str = ""


def read_os_release(path: Path = OS_RELEASE_PATH) -> dict[str, str]:
    """Read ``/etc/os-release`` as key/value pairs; return empty when it is unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip("\"'")
    return values


def distribution_label(path: Path = OS_RELEASE_PATH) -> str:
    """Distribution name and version from os-release, or ``""`` when it cannot be read."""
    values = read_os_release(path)
    pretty = values.get("PRETTY_NAME", "").strip()
    if pretty:
        return pretty
    name = values.get("NAME", "").strip()
    version = values.get("VERSION", "").strip() or values.get("VERSION_ID", "").strip()
    if name and version:
        return f"{name} {version}"
    return name or version


def collect_facts(devices: Sequence[DeviceInfo], *,
                  os_release_path: Path = OS_RELEASE_PATH) -> FeedbackFacts:
    """Describe this installation and the first connected device, without identifiers."""
    device = next((item for item in devices if not item.error), None)
    model = ""
    ios_version = ""
    if device is not None:
        # The marketing name and iOS version are hardware/software facts; the device name,
        # UDID, serial number and other identifiers are intentionally never read here.
        model = device.model_name or device.product_type
        ios_version = device.product_version
    return FeedbackFacts(
        app_version=__version__,
        distribution=distribution_label(os_release_path),
        device_model=model,
        ios_version=ios_version,
    )


def build_feedback_url(facts: FeedbackFacts, include_technical: bool = True) -> str:
    """Build the form URL, prefilling only the values that are available."""
    if not include_technical:
        return BASE_URL
    params: list[tuple[str, str]] = [("usp", "pp_url")]
    for key, value in (
        (VERSION_FIELD, facts.app_version),
        (DISTRIBUTION_FIELD, facts.distribution),
        (MODEL_FIELD, facts.device_model),
        (IOS_FIELD, facts.ios_version),
    ):
        value = value.strip()
        if value:
            params.append((key, value))
    return f"{BASE_URL}?{urlencode(params)}"
