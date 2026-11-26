# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Runtime compatibility for Debian's Cryptodome naming."""

from __future__ import annotations

import importlib
import sys
import types

import devicedock


def test_debian_cryptodome_is_aliased_for_upstream_crypto_imports(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "Crypto", raising=False)
    monkeypatch.delitem(sys.modules, "Cryptodome", raising=False)
    fake = types.ModuleType("Cryptodome")

    imported: list[str] = []

    def fake_import(name: str):
        imported.append(name)
        if name == "Crypto":
            raise ImportError("Debian exposes Cryptodome")
        if name == "Cryptodome" or name.startswith("Crypto."):
            return fake
        raise AssertionError(f"unexpected import {name}")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    devicedock._add_pycryptodome_compatibility()
    assert sys.modules["Crypto"] is fake
    # Crypto.Hash.SHA1/SHA256 and KDF must be eager, because Cryptodome's KDF imports
    # those modules under the Cryptodome name and would leave Crypto.Hash incomplete.
    assert "Crypto.Hash.SHA1" in imported
    assert "Crypto.Hash.SHA256" in imported
    assert "Crypto.Protocol.KDF" in imported


def test_existing_crypto_module_is_never_replaced(monkeypatch) -> None:
    existing = types.ModuleType("Crypto")
    monkeypatch.setitem(sys.modules, "Crypto", existing)
    calls: list[str] = []
    monkeypatch.setattr(importlib, "import_module",
                        lambda name: calls.append(name) or existing)
    devicedock._add_pycryptodome_compatibility()
    assert sys.modules["Crypto"] is existing
    assert calls == []
