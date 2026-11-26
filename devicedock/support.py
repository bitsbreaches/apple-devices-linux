# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Support Development: donation details and address validators.

The addresses below are the project's real donation destinations. They are checked before
display so a typo cannot send funds to an invalid address; nothing here invents a payment
provider, account or link.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

BITCOIN_ADDRESS = "bc1qe5mde00rmrx2jltpndcrzunqpn6s0n2z98fy86"
TRON_USDT_ADDRESS = "TQ9ofvqxWbXHHre3DMcRZXcgW1SR1Xn6z3"


@dataclass(frozen=True)
class DonationOption:
    ticker: str
    name: str
    network: str
    address: str
    uri_scheme: str

    @property
    def payment_uri(self) -> str:
        return f"{self.uri_scheme}:{self.address}"

    @property
    def qr_payload(self) -> str:
        # Wallet apps scan a BIP-21 style URI for Bitcoin; TRON wallets expect the address.
        return self.payment_uri if self.ticker == "BTC" else self.address


DONATION_OPTIONS = (
    DonationOption("BTC", "Bitcoin", "Bitcoin Mainnet", BITCOIN_ADDRESS, "bitcoin"),
    DonationOption("USDT", "Tether", "TRON Mainnet (TRC20)", TRON_USDT_ADDRESS, "tron"),
)


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def valid_bitcoin_mainnet_address(address: str) -> bool:
    if not address or (address.lower() != address and address.upper() != address):
        return False
    address = address.lower()
    if not address.startswith("bc1") or len(address) > 90:
        return False
    separator = address.rfind("1")
    if separator < 1 or separator + 7 > len(address):
        return False
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    try:
        data = [charset.index(character) for character in address[separator + 1:]]
    except ValueError:
        return False
    hrp = address[:separator]
    expanded_hrp = [ord(character) >> 5 for character in hrp]
    expanded_hrp.append(0)
    expanded_hrp.extend(ord(character) & 31 for character in hrp)
    if _bech32_polymod(expanded_hrp + data) != 1:
        return False
    payload = data[:-6]
    if not payload or payload[0] != 0:
        return False
    accumulator = bits = 0
    decoded: list[int] = []
    for value in payload[1:]:
        accumulator = (accumulator << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            decoded.append((accumulator >> bits) & 0xFF)
    if bits >= 5 or ((accumulator << (8 - bits)) & 0xFF):
        return False
    return len(decoded) in {20, 32}


def _base58_decode(value: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in value:
        number = number * 58 + alphabet.index(character)
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + decoded


def valid_tron_mainnet_address(address: str) -> bool:
    try:
        decoded = _base58_decode(address)
    except (ValueError, OverflowError):
        return False
    if len(decoded) != 25 or decoded[0] != 0x41:
        return False
    payload, checksum = decoded[:-4], decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return checksum == expected


_VALIDATORS = {"BTC": valid_bitcoin_mainnet_address, "USDT": valid_tron_mainnet_address}


def validated_donation_options() -> tuple[DonationOption, ...]:
    """Return the donation options, raising ``ValueError`` if any address is invalid."""
    for option in DONATION_OPTIONS:
        validator = _VALIDATORS.get(option.ticker)
        if validator is None or not validator(option.address):
            raise ValueError(
                f"The configured {option.ticker} donation address is invalid for "
                f"{option.network}."
            )
    return DONATION_OPTIONS
