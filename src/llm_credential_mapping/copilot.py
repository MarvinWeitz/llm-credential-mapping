import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass, replace
from pathlib import Path

# Legacy Copilot bridge, deleted in plan step 7; it may use the LiteLLM modules directly.
from .auth.litellm_oauth import required_text
from .auth.litellm_sso import SessionManager
from .customers import validate_customer
from .errors import CredentialError
from .profiles import Profile, ProfileStore, atomic_write_json, validate_profile_name
from .repositories import current_repository, repository_identity
from .request_context import resolve_identity, validate_model
from .storage import NativeKeyring, SecretStore


def _validate_capabilities(port: int, max_input_tokens: int, max_output_tokens: int, vision: bool) -> None:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise CredentialError("Copilot loopback port must be an integer between 1024 and 65535.")
    if any(type(value) is not int or not 1 <= value <= 2_000_000 for value in (
        max_input_tokens, max_output_tokens,
    )):
        raise CredentialError("Copilot token limits must be positive integers no greater than 2000000.")
    if type(vision) is not bool:
        raise CredentialError("Invalid Copilot vision capability.")


@dataclass(frozen=True)
class CopilotConnection:
    """A nonsecret, explicit checkout/profile/employee/customer/model snapshot for one loopback bridge.

    Ports and advertised token limits are fixed at setup; enabled is the only mutable state.
    The customer is never inferred from the active editor or from a later repository rebind.
    """

    profile: str
    account: str
    user_id: str
    repository: str
    customer: str
    model: str
    directory: str
    port: int
    max_input_tokens: int
    max_output_tokens: int
    vision: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("profile", "account", "user_id", "repository", "customer", "model", "directory"):
            required_text(asdict(self), name)
        validate_profile_name(self.profile)
        validate_model(self.model)
        validate_customer(self.customer)
        if not re.fullmatch(r"[0-9a-f]{64}", self.account):
            raise CredentialError("Invalid Copilot profile account identity.")
        if not Path(self.directory).is_absolute():
            raise CredentialError("Copilot needs an absolute checkout and normalised repository identity.")
        repository_identity("https://" + self.repository)
        _validate_capabilities(self.port, self.max_input_tokens, self.max_output_tokens, self.vision)
        if type(self.enabled) is not bool:
            raise CredentialError("Invalid Copilot enabled flag.")

    @property
    def id(self) -> str:
        values = asdict(self)
        del values["enabled"]
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    @property
    def base_path(self) -> str:
        return f"/llm-credentials/{self.id}/v1"

    @property
    def group_name(self) -> str:
        return f"LiteLLM | {self.repository} | {self.customer} [{self.id[:12]}]"

    @property
    def key_account(self) -> str:
        return "copilot-bridge-" + self.id

    def model_settings(self) -> dict:
        """Return fields to merge into a Custom Endpoint group, retaining VS Code's existing apiKey secret reference."""
        return {
            "apiType": "chat-completions",
            "models": [{
                "id": self.model,
                "name": f"{self.model} | {self.customer} [{self.id[:12]}]",
                "url": f"http://127.0.0.1:{self.port}{self.base_path}/chat/completions",
                "toolCalling": True,
                "vision": self.vision,
                "maxInputTokens": self.max_input_tokens,
                "maxOutputTokens": self.max_output_tokens,
            }],
        }


class CopilotStore:
    """Persist nonsecret bridge connections; serialize setup and disabling without changing other CLI state."""

    def __init__(self, profiles: ProfileStore) -> None:
        self.profiles = profiles
        self.path = profiles.home / "copilot.json"

    def _read(self) -> dict[str, CopilotConnection]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CredentialError("Cannot read copilot.json; repair it before changing connections.") from None
        if (
            not isinstance(value, dict) or set(value) != {"version", "connections"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["connections"], dict)
        ):
            raise CredentialError("Invalid Copilot configuration; no connections were changed.")
        result = {}
        for identifier, fields in value["connections"].items():
            if not isinstance(fields, dict) or set(fields) != set(CopilotConnection.__dataclass_fields__):
                raise CredentialError("Invalid saved Copilot connection.")
            connection = CopilotConnection(**fields)
            if identifier != connection.id:
                raise CredentialError("Saved Copilot connection identity does not match its context.")
            result[identifier] = connection
        return result

    def list(self) -> list[CopilotConnection]:
        with self.profiles.locked("copilot"):
            return list(self._read().values())

    def select(self, identifier: str | None = None) -> CopilotConnection:
        with self.profiles.locked("copilot"):
            rows = self._read()
        if identifier is not None:
            if identifier not in rows:
                raise CredentialError("No such Copilot connection; run 'copilot list'.")
            return rows[identifier]
        if not rows:
            raise CredentialError("No Copilot connection; run 'copilot setup' first.")
        if len(rows) != 1:
            raise CredentialError("Multiple Copilot connections; use --connection ID from 'copilot list'.")
        return next(iter(rows.values()))

    def put(self, connection: CopilotConnection, vault: SecretStore) -> None:
        with self.profiles.locked("copilot"):
            rows = self._read()
            if any(
                other.enabled and other.id != connection.id and other.port == connection.port
                for other in rows.values()
            ):
                raise CredentialError("Another connection already uses this port; choose --port or disable that connection.")
            key = vault.get(connection.key_account)
            if key is None:
                vault.put(connection.key_account, {"version": 1, "key": secrets.token_urlsafe(32)})
            else:
                _validate_key(key)
            rows[connection.id] = connection
            self._write(rows)

    def disable(self, identifier: str | None = None) -> CopilotConnection:
        connection = self.select(identifier)
        with self.profiles.locked("copilot"):
            rows = self._read()
            if connection.id not in rows:
                raise CredentialError("Copilot configuration changed; retry disabling the connection.")
            rows[connection.id] = replace(connection, enabled=False)
            self._write(rows)
        return rows[connection.id]

    def _write(self, rows: dict[str, CopilotConnection]) -> None:
        atomic_write_json(self.path, {
            "version": 1, "connections": {identifier: asdict(row) for identifier, row in rows.items()},
        })


def _validate_key(value: dict) -> str:
    if (
        not isinstance(value, dict) or set(value) != {"version", "key"}
        or type(value["version"]) is not int or value["version"] != 1
        or not isinstance(value["key"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value["key"])
    ):
        raise CredentialError("Invalid local Copilot key in the vault; no key was returned or replaced.")
    return value["key"]


def connection_key(connection: CopilotConnection, vault: SecretStore) -> str:
    """Return only the local bridge credential for entry in VS Code secret storage; never return a gateway token."""
    value = vault.get(connection.key_account)
    if value is None:
        raise CredentialError("The local Copilot key is missing; rerun setup and update the key in VS Code.")
    return _validate_key(value)


def validate_connection(
    store: ProfileStore, connection: CopilotConnection, vault: SecretStore | None = None,
) -> tuple[Profile, SessionManager]:
    """Require an enabled, unchanged checkout/profile and the pinned employee's renewable grant."""
    if not connection.enabled:
        raise CredentialError("This Copilot connection is disabled; rerun setup to enable it.")
    if current_repository(Path(connection.directory)) != connection.repository:
        raise CredentialError("The Copilot checkout's Git origin changed; rerun setup before using the bridge.")
    profile = store.get(connection.profile)
    if profile.account != connection.account:
        raise CredentialError("The pinned Copilot profile changed; rerun setup.")
    manager = SessionManager(store, vault if vault is not None else NativeKeyring())
    manager.token(profile, expected_user=connection.user_id)
    return profile, manager


def setup(
    store: ProfileStore, profile_name: str | None, model: str, directory: Path | None = None, *,
    port: int = 8743, max_input_tokens: int = 32000, max_output_tokens: int = 4096, vision: bool = False,
) -> CopilotConnection:
    """Validate a bound checkout and employee grant, save its bridge context and create/reuse an OS-vault local key.

    No editor files or gateway credentials are written to stdout. Raise CredentialError on invalid setup or storage failure.
    """
    validate_model(model)
    _validate_capabilities(port, max_input_tokens, max_output_tokens, vision)
    profile = store.get(profile_name)
    try:
        checkout = (directory or Path.cwd()).expanduser().resolve()
    except (OSError, RuntimeError):
        raise CredentialError("Cannot resolve the Copilot checkout directory.") from None
    repository = current_repository(checkout)
    identity = resolve_identity(store, profile, repository, NativeKeyring, SessionManager)
    connection = CopilotConnection(
        profile.name, profile.account, identity.user_id, repository, identity.customer, model, str(checkout),
        port, max_input_tokens, max_output_tokens, vision,
    )
    CopilotStore(store).put(connection, NativeKeyring())
    return connection
