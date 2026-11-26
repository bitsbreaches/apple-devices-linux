# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Support Development configuration tests.

The donation addresses are the owner's real destinations, copied from the reference project.
These tests guarantee that a corrupted address is refused rather than displayed.
"""

from __future__ import annotations

import pytest

from devicedock import support


def test_addresses_match_the_reference_project() -> None:
    assert support.BITCOIN_ADDRESS == "bc1qe5mde00rmrx2jltpndcrzunqpn6s0n2z98fy86"
    assert support.TRON_USDT_ADDRESS == "TQ9ofvqxWbXHHre3DMcRZXcgW1SR1Xn6z3"


def test_options_are_validated_and_labelled() -> None:
    options = support.validated_donation_options()
    assert [o.ticker for o in options] == ["BTC", "USDT"]
    btc, usdt = options
    assert btc.name == "Bitcoin" and btc.network == "Bitcoin Mainnet"
    assert usdt.name == "Tether" and usdt.network == "TRON Mainnet (TRC20)"
    # Bitcoin wallets expect a BIP-21 URI; TRON wallets expect the bare address.
    assert btc.qr_payload == f"bitcoin:{support.BITCOIN_ADDRESS}"
    assert usdt.qr_payload == support.TRON_USDT_ADDRESS


def test_bitcoin_validator_rejects_mutations() -> None:
    address = support.BITCOIN_ADDRESS
    assert support.valid_bitcoin_mainnet_address(address)
    mutated = address[:-1] + ("7" if address[-1] != "7" else "8")
    assert not support.valid_bitcoin_mainnet_address(mutated)
    assert not support.valid_bitcoin_mainnet_address("")
    assert not support.valid_bitcoin_mainnet_address("1BoatSLRHtKNngkdXEeobR76b53LETtpyT")


def test_tron_validator_rejects_mutations() -> None:
    address = support.TRON_USDT_ADDRESS
    assert support.valid_tron_mainnet_address(address)
    mutated = address[:-1] + ("4" if address[-1] != "4" else "5")
    assert not support.valid_tron_mainnet_address(mutated)
    assert not support.valid_tron_mainnet_address("TQ9ofvqxWbXHHre3DMcRZXcgW1SR1Xn6z")


def test_invalid_address_raises_instead_of_displaying(monkeypatch) -> None:
    broken = (
        support.DonationOption("BTC", "Bitcoin", "Bitcoin Mainnet", "bc1qnotvalid", "bitcoin"),
    )
    monkeypatch.setattr(support, "DONATION_OPTIONS", broken)
    with pytest.raises(ValueError):
        support.validated_donation_options()


def test_no_payment_provider_or_account_is_invented() -> None:
    text = (support.__file__ and open(support.__file__, encoding="utf-8").read()) or ""
    for forbidden in ("paypal", "stripe", "patreon", "buymeacoffee", "gumroad", "ko-fi"):
        assert forbidden not in text.lower()
