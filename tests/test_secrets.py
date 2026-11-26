# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Keyring storage and migration from the schema used before the rename."""

from __future__ import annotations

import pytest

from devicedock import secrets


class _FakeSchema:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSecret:
    COLLECTION_DEFAULT = "default"

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def password_lookup_sync(self, schema, attributes, _cancellable):
        return self.entries.get((schema.name, attributes["backup"]))

    def password_store_sync(self, schema, attributes, _collection, _label, password,
                            _cancellable):
        self.entries[(schema.name, attributes["backup"])] = password
        return True

    def password_clear_sync(self, schema, attributes, _cancellable):
        return self.entries.pop((schema.name, attributes["backup"]), None) is not None


@pytest.fixture
def keyring(monkeypatch):
    fake = _FakeSecret()
    monkeypatch.setattr(secrets, "_Secret", fake)
    monkeypatch.setattr(secrets, "_SCHEMA", _FakeSchema(secrets._SCHEMA_NAME))
    monkeypatch.setattr(secrets, "_LEGACY_SCHEMA", _FakeSchema(secrets._LEGACY_SCHEMA_NAME))
    monkeypatch.setattr(secrets, "_load", lambda: True)
    return fake


def test_password_stored_under_the_current_schema_is_returned(keyring):
    assert secrets.store_password("00008110-000A", "secret")
    assert secrets.lookup_password("00008110-000A") == "secret"
    assert list(keyring.entries) == [(secrets._SCHEMA_NAME, "00008110-000A")]


def test_password_from_the_former_schema_is_migrated_on_read(keyring):
    keyring.entries[(secrets._LEGACY_SCHEMA_NAME, "00008110-000A")] = "old-secret"

    assert secrets.lookup_password("00008110-000A") == "old-secret"
    assert keyring.entries == {
        (secrets._SCHEMA_NAME, "00008110-000A"): "old-secret"}


def test_missing_password_is_reported_as_none(keyring):
    assert secrets.lookup_password("00008110-9999") is None
