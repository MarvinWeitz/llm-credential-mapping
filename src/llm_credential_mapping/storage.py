import hashlib
import json
import logging
import os
import re
import uuid
from typing import Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .errors import CredentialError


class SecretStore(Protocol):
    """Store a profile's complete session atomically, without plaintext fallback."""

    def get(self, account: str) -> dict | None: ...
    def put(self, account: str, value: dict) -> None: ...
    def delete(self, account: str) -> None: ...


class NativeKeyring:
    """Store complete sessions in an OS vault; callers must serialize same-account access."""

    SERVICE = "llm-credential-mapping"
    ENTRY_CHARS = 1280
    MAX_CHARS = 1_048_576
    MANIFEST = "_llm_credential_mapping_vault"
    ALLOWED = {
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet5"),
    }

    def __init__(self) -> None:
        self._backend_errors = (KeyringError, OSError, ValueError)
        if os.name == "nt":
            from keyring.backends.Windows import pywintypes

            self._backend_errors += (pywintypes.error,)
        try:
            backend = keyring.get_keyring()
            candidates = getattr(backend, "backends", [backend])
            self.backend = next(
                item
                for item in candidates
                if (type(item).__module__, type(item).__name__) in self.ALLOWED
            )
        except (KeyringError, StopIteration, RuntimeError, OSError):
            raise CredentialError(
                "No supported OS keyring. Enable Windows Credential Manager, macOS "
                "Keychain, Secret Service or KWallet. Plaintext fallback is disabled."
            ) from None

    def get(self, account: str) -> dict | None:
        raw = self._read(self._service(account), account)
        if raw is None:
            return None
        value = self._object(raw)
        manifest = self._manifest(value)
        if manifest is None:
            return value
        parts = []
        for service in self._fragment_services(account, manifest):
            part = self._read(service, account)
            if (
                not isinstance(part, str)
                or not 0 < len(part) <= self.ENTRY_CHARS
                or not part.isascii()
            ):
                raise self._invalid()
            parts.append(part)
        raw = "".join(parts)
        if (
            len(raw) > self.MAX_CHARS
            or hashlib.sha256(raw.encode()).hexdigest() != manifest["sha256"]
        ):
            raise self._invalid()
        value = self._object(raw)
        if self.MANIFEST in value:
            raise self._invalid()
        return value

    def put(self, account: str, value: dict) -> None:
        if self.MANIFEST in value:
            raise CredentialError("Stored session contains a reserved vault-format field.")
        raw = json.dumps(value)
        if len(raw) > self.MAX_CHARS:
            raise CredentialError("Session is too large for the supported OS-vault format.")
        root = self._service(account)
        previous = self._read(root, account)
        old_services = self._stored_fragments(account, previous)
        if len(raw) <= self.ENTRY_CHARS:
            try:
                self._write(root, account, raw)
            except (CredentialError, KeyboardInterrupt):
                self._restore(root, account, previous)
                raise
        else:
            parts = [raw[start:start + self.ENTRY_CHARS] for start in range(0, len(raw), self.ENTRY_CHARS)]
            manifest = {
                "version": 1,
                "generation": uuid.uuid4().hex,
                "parts": len(parts),
                "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            }
            pointer = json.dumps({self.MANIFEST: manifest})
            services = self._fragment_services(account, manifest)
            try:
                for service, part in zip(services, parts):
                    self._write(service, account, part)
                # Publish only a complete generation; the previous session stays readable.
                self._write(root, account, pointer)
            except (CredentialError, KeyboardInterrupt):
                try:
                    published = self._read(root, account) == pointer
                except CredentialError:
                    logging.getLogger(__name__).warning(
                        "Cannot confirm the vault write; unused fragments may remain. Log in again."
                    )
                else:
                    if not published:
                        self._cleanup(services, account)
                        self._restore(root, account, previous)
                raise
        self._cleanup(old_services, account)

    def delete(self, account: str) -> None:
        root = self._service(account)
        services = self._stored_fragments(account, self._read(root, account))
        self._remove(root, account)
        self._cleanup(services, account)

    def _read(self, service: str, account: str) -> str | None:
        try:
            return self.backend.get_password(service, account)
        except self._backend_errors:
            raise CredentialError("Cannot read the OS credential vault; unlock it and retry.") from None

    def _write(self, service: str, account: str, raw: str) -> None:
        try:
            self.backend.set_password(service, account, raw)
        except self._backend_errors:
            raise CredentialError("Cannot save the complete session in the OS vault; no plaintext fallback was used.") from None

    def _remove(self, service: str, account: str) -> None:
        try:
            self.backend.delete_password(service, account)
        except PasswordDeleteError:
            raise CredentialError("Cannot delete the stored credential; retry logout.") from None
        except self._backend_errors:
            raise CredentialError("Cannot access the OS credential vault; retry logout.") from None

    def _cleanup(self, services: list[str], account: str) -> None:
        for service in services:
            try:
                if self._read(service, account) is not None:
                    self._remove(service, account)
            except CredentialError:
                logging.getLogger(__name__).warning(
                    "Could not remove an unused OS-vault fragment. Remove obsolete entries "
                    "under llm-credential-mapping from the vault manually."
                )

    def _restore(self, root: str, account: str, previous: str | None) -> None:
        """Put back a root entry lost by a failed overwrite: macOS Keychain overwrites by deleting, then adding."""
        try:
            if previous is not None and self._read(root, account) is None:
                self._write(root, account, previous)
        except CredentialError:
            logging.getLogger(__name__).warning("Cannot restore the previous sign-in in the OS vault; log in again.")

    def _stored_fragments(self, account: str, raw: str | None) -> list[str]:
        """Fragments named by a root entry about to be replaced or removed; a damaged root names none we trust."""
        if raw is None:
            return []
        try:
            manifest = self._manifest(self._object(raw))
        except CredentialError:
            logging.getLogger(__name__).warning(
                "Found a damaged OS-vault entry and overwrote or removed it; obsolete entries under llm-credential-mapping may remain in the vault."
            )
            return []
        return self._fragment_services(account, manifest) if manifest else []

    def _object(self, raw: str) -> dict:
        try:
            value = json.loads(raw)
        except ValueError:
            raise self._invalid() from None
        if not isinstance(value, dict):
            raise self._invalid()
        return value

    def _manifest(self, value: dict) -> dict | None:
        if self.MANIFEST not in value:
            return None
        manifest = value[self.MANIFEST]
        if (
            set(value) != {self.MANIFEST}
            or not isinstance(manifest, dict)
            or set(manifest) != {"version", "generation", "parts", "sha256"}
            or type(manifest["version"]) is not int
            or manifest["version"] != 1
            or not isinstance(manifest["generation"], str)
            or re.fullmatch(r"[0-9a-f]{32}", manifest["generation"]) is None
            or type(manifest["parts"]) is not int
            or not 1 <= manifest["parts"] <= (self.MAX_CHARS + self.ENTRY_CHARS - 1) // self.ENTRY_CHARS
            or not isinstance(manifest["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) is None
        ):
            raise self._invalid()
        return manifest

    def _fragment_services(self, account: str, manifest: dict) -> list[str]:
        return [
            f"{self._service(account)}:{manifest['generation']}:{index}"
            for index in range(manifest["parts"])
        ]

    @staticmethod
    def _invalid() -> CredentialError:
        return CredentialError(
            "Invalid or incomplete stored credential; remove this profile's entries "
            "from the OS vault and log in again. Ask the gateway administrator to revoke the damaged grant."
        )

    def _service(self, account: str) -> str:
        # Windows keyring writes otherwise share a non-atomic service-name slot.
        return f"{self.SERVICE}:{account}"
