import json
from typing import Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .errors import CredentialError


class SecretStore(Protocol):
    """Store a project's complete session atomically, without plaintext fallback."""

    def get(self, account: str) -> dict | None: ...
    def put(self, account: str, value: dict) -> None: ...
    def delete(self, account: str) -> None: ...


class NativeKeyring:
    """Use an OS credential vault; refuse absent, third-party or plaintext backends."""

    SERVICE = "llm-credential-mapping"
    ALLOWED = {
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet5"),
    }

    def __init__(self) -> None:
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
        try:
            raw = self.backend.get_password(self._service(account), account)
        except (KeyringError, OSError):
            raise CredentialError("Cannot read the OS credential vault; unlock it and retry.") from None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            raise CredentialError(
                "Invalid stored credential; remove this profile's entry from the OS vault "
                "and log in again. Ask the gateway administrator to revoke the damaged grant."
            ) from None
        if not isinstance(value, dict):
            raise CredentialError("Invalid stored credential; remove this profile's entry from the OS vault.")
        return value

    def put(self, account: str, value: dict) -> None:
        try:
            self.backend.set_password(self._service(account), account, json.dumps(value))
        except (KeyringError, OSError, ValueError):
            raise CredentialError("Cannot save credentials in the OS vault; nothing was written to disk.") from None

    def delete(self, account: str) -> None:
        try:
            self.backend.delete_password(self._service(account), account)
        except PasswordDeleteError:
            raise CredentialError("Cannot delete the stored credential; retry logout.") from None
        except (KeyringError, OSError):
            raise CredentialError("Cannot access the OS credential vault; retry logout.") from None

    def _service(self, account: str) -> str:
        # Windows keyring writes otherwise share a non-atomic service-name slot.
        return f"{self.SERVICE}:{account}"
