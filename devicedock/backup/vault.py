# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Unified access to one backup: encrypted or plaintext, browsing and decryption.

Cryptography is delegated to the established, tested ``iphone_backup_decrypt``
implementation (keybag parsing, PBKDF2 derivation, RFC 3394 key unwrap and manifest
decryption). This module only adds: a private temporary workspace, precise per-file
decryption by manifest record, and explicit error types for the UI.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from ..asyncutil import Job
from ..config import APP_CACHE_DIR
from ..models import BackupInfo, FileRecord
from .manifest import ManifestError, ManifestQuery

log = logging.getLogger(__name__)


class BackupLockedError(RuntimeError):
    """The backup is encrypted and has not been unlocked."""


class BadPasswordError(RuntimeError):
    """The backup password does not unlock the keybag."""


class DecryptionUnavailableError(RuntimeError):
    """The bundled decryption engine could not be loaded (incomplete installation)."""


class PayloadMissingError(FileNotFoundError):
    """The manifest lists this file, but its payload is not in the backup.

    iOS prunes payloads it considers expendable and a failed transfer can leave entries
    behind, so a manifest row is not proof that the bytes exist. Callers must treat this as
    "file unavailable", never as an empty file: previews, exports and integrity checks all
    report it separately from a genuinely zero-byte file.
    """


#: Values returned by :meth:`BackupVault.payload_state`.
PAYLOAD_PRESENT = "present"
PAYLOAD_EMPTY = "empty"
PAYLOAD_MISSING = "missing"
PAYLOAD_NOT_A_FILE = "not-a-file"
PAYLOAD_UNREADABLE = "unreadable"


class BackupVault:
    """One opened backup directory.

    Usage::

        vault = BackupVault(info)
        vault.unlock(password)          # no-op for unencrypted backups
        manifest = vault.manifest()
        data = vault.read_file(record)
        vault.close()                   # removes decrypted temporary data
    """

    def __init__(self, info: BackupInfo) -> None:
        self.info = info
        self._manifest: ManifestQuery | None = None
        self._engine = None
        self._tempdir: Path | None = None

    # --- lifecycle ---------------------------------------------------
    @property
    def unlocked(self) -> bool:
        return self._manifest is not None

    def unlock(self, password: str | None = None) -> None:
        """Open the manifest, decrypting it first when the backup is encrypted."""
        if self._manifest is not None:
            return
        if not self.info.is_encrypted:
            self._manifest = ManifestQuery(self.info.path / "Manifest.db")
            return
        if password is None:
            raise BackupLockedError("This backup is encrypted; a password is required.")
        try:
            from iphone_backup_decrypt import EncryptedBackup, IncorrectPassphraseError
        except ImportError as exc:
            # The package ships this engine inside its own compiled runtime, so an import
            # failure means the installation is incomplete rather than that the user has to
            # install a Python package. Record the real cause for the diagnostics page.
            log.error("Bundled backup decryption engine is unavailable: %s", exc,
                      exc_info=True)
            raise DecryptionUnavailableError(
                "The backup decryption engine included with Device Dock could not be "
                f"loaded ({exc}). Reinstall the device-dock package; no Python packages "
                "need to be installed manually."
            ) from exc

        engine = EncryptedBackup(backup_directory=str(self.info.path), passphrase=password)
        try:
            engine.test_decryption()
        except IncorrectPassphraseError as exc:
            raise BadPasswordError("Incorrect backup password.") from exc
        except ValueError as exc:  # not encrypted / unsafe manifest
            raise ManifestError(str(exc)) from exc

        tempdir = Path(tempfile.mkdtemp(prefix="unlocked-", dir=_ensure_cache_dir()))
        tempdir.chmod(0o700)
        manifest_copy = tempdir / "Manifest.db"
        try:
            engine.save_manifest_file(str(manifest_copy))
            self._manifest = ManifestQuery(manifest_copy)
        except Exception:
            shutil.rmtree(tempdir, ignore_errors=True)
            raise
        self._engine = engine
        self._tempdir = tempdir
        log.info("Unlocked encrypted backup %s", self.info.path.name)

    def manifest(self) -> ManifestQuery:
        if self._manifest is None:
            raise BackupLockedError("Unlock the backup before reading its manifest.")
        return self._manifest

    def close(self) -> None:
        if self._manifest is not None:
            self._manifest.close()
            self._manifest = None
        self._engine = None
        if self._tempdir is not None:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None

    def __enter__(self) -> "BackupVault":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # --- file access -------------------------------------------------
    def _disk_path(self, file_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(file_id)):
            raise ValueError("The backup manifest contains an invalid payload identifier.")
        return self.info.path / file_id[:2] / file_id

    def payload_state(self, record: FileRecord) -> str:
        """Classify the payload behind one manifest record, without decrypting it.

        Presence can be checked before unlocking, which is what lets the browser and the
        integrity check say "missing" instead of showing an empty file.
        """
        if not record.is_file:
            return PAYLOAD_NOT_A_FILE
        try:
            disk_path = self._disk_path(record.file_id)
        except ValueError:
            return PAYLOAD_UNREADABLE
        try:
            status = disk_path.stat()
        except FileNotFoundError:
            return PAYLOAD_MISSING
        except OSError:
            return PAYLOAD_UNREADABLE
        if not status.st_size:
            return PAYLOAD_EMPTY
        return PAYLOAD_PRESENT

    def read_file(self, record: FileRecord) -> bytes:
        """Return the plaintext bytes of one manifest record.

        Raises :class:`PayloadMissingError` when the payload is absent (pruned or never
        transferred) so it can never be mistaken for a zero-byte file; a payload that
        genuinely holds no bytes reads as ``b""``.
        """
        if self._manifest is None:
            raise BackupLockedError("Unlock the backup before reading files.")
        if not record.is_file:
            raise IsADirectoryError(f"{record.domain}/{record.relative_path} is not a regular file")
        disk_path = self._disk_path(record.file_id)
        try:
            data = disk_path.read_bytes()
        except FileNotFoundError as exc:
            raise PayloadMissingError(
                f"The payload for {record.domain}/{record.relative_path} is not present in "
                "this backup (iOS prunes some files)."
            ) from exc
        if not self.info.is_encrypted:
            return data
        if not data:
            return b""
        if self._engine is None or self._engine.keybag is None:
            raise BackupLockedError("The keybag is not unlocked.")
        if record.encryption_key is None:
            # Unencrypted payload inside an encrypted backup (rare, but legal).
            return data
        protection_class, wrapped_key = split_file_key(
            record.encryption_key, record.protection_class
        )
        inner_key = self._engine.keybag.unwrap_key_for_class(protection_class, wrapped_key)
        return _aes_cbc_decrypt(inner_key, data)

    def copy_file(self, record: FileRecord, destination: Path, job=None) -> Path:
        """Stream one plaintext payload to ``destination`` without buffering the file."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self._disk_path(record.file_id)
        if not source.is_file():
            raise PayloadMissingError(
                f"The payload for {record.domain}/{record.relative_path} is not present in "
                "this backup (iOS prunes some files)."
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
        )
        temporary = Path(temporary_name)
        actual_size = 0
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                if not self.info.is_encrypted:
                    while chunk := input_file.read(1024 * 1024):
                        if job is not None:
                            job.check_cancelled()
                        output.write(chunk)
                else:
                    if self._engine is None or self._engine.keybag is None:
                        raise BackupLockedError("The keybag is not unlocked.")
                    if record.encryption_key is None:
                        while chunk := input_file.read(1024 * 1024):
                            if job is not None:
                                job.check_cancelled()
                            output.write(chunk)
                    else:
                        protection_class, wrapped_key = split_file_key(
                            record.encryption_key, record.protection_class
                        )
                        key = self._engine.keybag.unwrap_key_for_class(protection_class, wrapped_key)
                        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

                        decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
                        pending = b""
                        while chunk := input_file.read(1024 * 1024):
                            if job is not None:
                                job.check_cancelled()
                            if len(chunk) % 16:
                                raise ValueError("Encrypted payload is not a multiple of the AES block size")
                            plain = decryptor.update(chunk)
                            combined = pending + plain
                            if len(combined) > 16:
                                output.write(combined[:-16])
                                actual_size += len(combined) - 16
                                pending = combined[-16:]
                            else:
                                pending = combined
                        tail = pending + decryptor.finalize()
                        padding = tail[-1] if tail else 0
                        if 1 <= padding <= 16 and tail.endswith(bytes([padding]) * padding):
                            tail = tail[:-padding]
                        output.write(tail)
                        actual_size += len(tail)
                if not self.info.is_encrypted or record.encryption_key is None:
                    actual_size = output.tell()
                output.flush()
                os.fsync(output.fileno())
            if record.size is not None and actual_size != record.size:
                raise OSError(
                    f"Payload size mismatch for {record.relative_path}: "
                    f"manifest says {record.size:,} bytes, read {actual_size:,} bytes."
                )
            temporary.replace(destination)
            if record.mtime:
                try:
                    os.utime(destination, (record.mtime, record.mtime))
                except OSError:
                    pass
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    # --- verification ------------------------------------------------
    def verify(self, job: Job, *, sample_size: int = 20) -> dict:
        """Run inexpensive integrity checks against manifest and payload files."""
        manifest = self.manifest()
        result: dict = {
            "file_count": manifest.total_files(),
            "sampled": 0,
            "missing": 0,
            "empty": 0,
            "digest_mismatch": 0,
            "digest_checked": 0,
            "errors": [],
        }
        rows = manifest.search("", limit=sample_size * 50)
        candidates = [r for r in rows if r.is_file and r.size and r.size < 4 * 1024 * 1024]
        for index, record in enumerate(candidates[: sample_size * 5]):
            job.check_cancelled()
            job.progress(index / max(1, sample_size * 5), "Verifying payload files")
            if result["sampled"] >= sample_size:
                break
            state = self.payload_state(record)
            if state == PAYLOAD_MISSING:
                result["missing"] += 1
                continue
            if state == PAYLOAD_EMPTY:
                # A manifest size of zero is legitimate; nothing to hash.
                result["empty"] += 1
                result["sampled"] += 1
                continue
            if state == PAYLOAD_UNREADABLE:
                result["errors"].append(
                    f"{record.domain}/{record.relative_path}: payload cannot be read")
                continue
            result["sampled"] += 1
            if not record.digest:
                continue
            try:
                import hashlib

                data = self.read_file(record)
                actual = hashlib.sha1(data).digest()
                result["digest_checked"] += 1
                if actual != record.digest:
                    result["digest_mismatch"] += 1
            except Exception as exc:  # noqa: BLE001 - reported per-file
                result["errors"].append(f"{record.domain}/{record.relative_path}: {exc}")
        job.progress(1.0, "Verification finished")
        return result


def split_file_key(key: bytes, fallback_class: int | None) -> tuple[int, bytes]:
    """Split an iOS ``EncryptionKey`` blob into its protection class and wrapped key.

    iOS prefixes the RFC 3394 wrapped key with its protection class as a little-endian
    uint32, so the blob is 44 bytes. ``iphone_backup_decrypt`` accepts only the 40-byte
    wrapped part and rejects anything else, which is why the prefix must be removed here.
    """
    if len(key) >= 44:
        return int.from_bytes(key[:4], "little"), key[4:]
    if fallback_class is None:
        raise BackupLockedError(
            "This backup does not record the protection class needed to decrypt the file."
        )
    if len(key) != 40:
        raise BackupLockedError(
            f"Unsupported per-file key length ({len(key)} bytes) in this backup."
        )
    return int(fallback_class), key


def _ensure_cache_dir() -> Path:
    APP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    APP_CACHE_DIR.chmod(0o700)
    return APP_CACHE_DIR


def _aes_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-256-CBC decryption of an iOS backup payload, padding removed."""
    if len(data) % 16 != 0:
        raise ValueError("Encrypted payload is not a multiple of the AES block size")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    if not plain:
        return plain
    padding = plain[-1]
    if 1 <= padding <= 16 and plain.endswith(bytes([padding]) * padding):
        return plain[:-padding]
    return plain
