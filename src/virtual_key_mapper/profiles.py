import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from .errors import CredentialError
from .labels import LabelDefinition, OutputOverride, check_definitions, check_overrides, definitions_from_dict


def gateway_origin(value: str, allow_http: bool = False) -> str:
    """Validate a gateway root URL; HTTP is allowed only for opted-in loopback hosts."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise CredentialError("Invalid gateway URL.") from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise CredentialError("Gateway must be a root URL without credentials, query or path.")
    if parsed.path not in ("", "/"):
        # LiteLLM mounted under SERVER_ROOT_PATH is not supported: every route is built on the origin.
        raise CredentialError(
            f"Gateway must be served at the host root; a path prefix such as '{parsed.path}' is not supported."
        )
    host = parsed.hostname.lower()
    if parsed.scheme != "https":
        if not (
            parsed.scheme == "http"
            and allow_http
            and host in ("localhost", "127.0.0.1", "::1")
        ):
            raise CredentialError("Use HTTPS; HTTP requires explicit loopback-only opt-in.")
    host = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise CredentialError("Profile name must be 1-64 letters, digits, dots, _ or -.")
    return name


def validate_rules_location(value: str, allow_http: bool = False) -> str:
    """Accept an HTTPS URL (HTTP only for opted-in loopback hosts) or an absolute local file path."""
    if (
        not isinstance(value, str) or not value or len(value) > 2048
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise CredentialError("Label rules must be an HTTPS URL or an absolute file path.")
    if "://" not in value:
        if not Path(value).is_absolute():
            raise CredentialError("Label rules must be an HTTPS URL or an absolute file path.")
        return value
    try:
        parsed = urlsplit(value)
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError:
        raise CredentialError("Label rules URL is invalid.") from None
    if has_credentials or parsed.fragment:
        raise CredentialError("Label rules URL must not contain credentials or a fragment.")
    gateway_origin(f"{parsed.scheme}://{parsed.netloc}", allow_http)
    return value


@dataclass(frozen=True)
class Profile:
    """A named profile: which gateway to use (kind and root URL) and how to authenticate (kind and settings).

    Kind-specific settings, such as a LiteLLM team ID, live in ``auth`` and are validated by that auth kind.
    ``rules`` optionally locates the central label rules; ``labels`` and ``outputs`` are the profile's own
    label definitions and output overrides (docs/labels.md). None of the three is part of the identity.
    """

    name: str
    gateway: str
    auth: dict
    gateway_type: str = "litellm"
    auth_type: str = "litellm-sso"
    allow_http: bool = False
    rules: str | None = None
    labels: tuple[LabelDefinition, ...] = ()
    outputs: dict[str, OutputOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .registry import gateway_outputs, validate_profile  # The registry's adapters import this module.

        validate_profile_name(self.name)
        if type(self.allow_http) is not bool:
            raise CredentialError("Invalid loopback HTTP setting.")
        if not isinstance(self.auth, dict) or not all(isinstance(key, str) for key in self.auth):
            raise CredentialError("Invalid authentication settings.")
        object.__setattr__(self, "gateway", gateway_origin(self.gateway, self.allow_http))
        object.__setattr__(self, "auth", validate_profile(self.gateway_type, self.auth_type, dict(self.auth)))
        if self.rules is not None:
            validate_rules_location(self.rules, self.allow_http)
        if (
            not isinstance(self.labels, tuple)
            or not all(isinstance(item, LabelDefinition) and item.origin == "profile" for item in self.labels)
            or not isinstance(self.outputs, dict)
            or not all(isinstance(name, str) and isinstance(item, OutputOverride) for name, item in self.outputs.items())
        ):
            raise CredentialError("Invalid label definitions or output overrides in the profile.")
        check_definitions(self.labels, "The profile")
        check_overrides(gateway_outputs(self.gateway_type), self.outputs, "The profile")

    @property
    def account(self) -> str:
        """A stable identity for this exact configuration; any change of gateway or auth gives a new one."""
        value = json.dumps(
            [self.name, self.gateway_type, self.gateway, self.auth_type, self.auth], sort_keys=True,
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def to_dict(self) -> dict:
        value = {
            "gateway": {"type": self.gateway_type, "url": self.gateway, "allow_http": self.allow_http},
            "auth": {"type": self.auth_type, **self.auth},
        }
        if self.rules is not None:
            value["rules"] = self.rules
        value.update(self.definitions())
        return value

    def definitions(self) -> dict:
        """The profile's own ``labels`` and ``outputs`` in their JSON form, each only when not empty."""
        value: dict = {}
        if self.labels:
            value["labels"] = [definition.to_dict() for definition in self.labels]
        if self.outputs:
            value["outputs"] = {name: override.to_dict() for name, override in self.outputs.items()}
        return value


def with_label(profile: Profile, definition: LabelDefinition) -> Profile:
    """Add a profile label, or replace the one with the same key in its place."""
    labels = list(profile.labels)
    index = next((index for index, item in enumerate(labels) if item.key == definition.key), None)
    if index is None:
        labels.append(definition)
    else:
        labels[index] = definition
    return replace(profile, labels=tuple(labels))


def without_label(profile: Profile, key: str) -> Profile:
    if all(item.key != key for item in profile.labels):
        raise CredentialError(f"The profile defines no label '{key}'.")
    return replace(profile, labels=tuple(item for item in profile.labels if item.key != key))


def with_output(profile: Profile, name: str, override: OutputOverride | None) -> Profile:
    """Set an output override; None returns the output to the adapter's default."""
    outputs = dict(profile.outputs)
    if override is None:
        outputs.pop(name, None)
    else:
        outputs[name] = override
    return replace(profile, outputs=outputs)


def default_home() -> Path:
    if override := os.environ.get("VKM_HOME"):
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "virtual-key-mapper"


def atomic_write_json(path: Path, value: dict) -> None:
    """Replace a JSON configuration atomically; the caller must hold its configuration lock."""
    atomic_write_text(path, json.dumps(value, indent=2) + "\n")


def atomic_write_text(
    path: Path, value: str, *, newline: str | None = None, permissions: int | None = None,
    follow_symlinks: bool = False,
) -> None:
    """Replace text atomically under the caller's lock; optionally preserve line endings and file permissions.

    ``follow_symlinks`` replaces a symlink's target instead of the link (dotfile managers). Only for files
    the user owns, such as tool configuration; never for files inside a repository, which may link anywhere.
    """
    if follow_symlinks:
        path = Path(os.path.realpath(path))
    filename = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline=newline, dir=path.parent, delete=False
        ) as output:
            filename = output.name
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        if permissions is not None:
            os.chmod(filename, permissions)
        for attempt in range(10):
            try:
                os.replace(filename, path)
                break
            except PermissionError:
                # Windows: virus scanners and the indexer briefly hold files open; give them a moment.
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if filename and os.path.exists(filename):
            os.unlink(filename)


class ProfileStore:
    """Persist nonsecret profile settings atomically in the user's configuration directory."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / "profiles.json"

    @contextmanager
    def locked(self, name: str = "profiles", *, timeout: float = 10) -> Iterator[None]:
        try:
            self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
            with FileLock(str(self.home / f"{name}.lock"), timeout=timeout):
                yield
        except Timeout:
            raise CredentialError("Another credential operation is busy; try again.") from None
        except OSError:
            raise CredentialError("Cannot access the credential configuration directory.") from None

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "active": None, "profiles": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):  # RecursionError: nesting too deep.
            raise CredentialError("Cannot read profiles.json; repair the nonsecret configuration.") from None
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
            or not isinstance(value.get("profiles"), dict)
            or (
                value.get("active") is not None
                and (
                    not isinstance(value["active"], str)
                    or value["active"] not in value["profiles"]
                )
            )
        ):
            raise CredentialError("Unsupported or invalid profiles.json.")
        for name, item in value["profiles"].items():
            self._profile(name, item)
        return value

    @staticmethod
    def _profile(name: str, item: object) -> Profile:
        if (
            not isinstance(item, dict) or set(item) - {"rules", "labels", "outputs"} != {"gateway", "auth"}
            or not isinstance(item["gateway"], dict) or set(item["gateway"]) != {"type", "url", "allow_http"}
            or not isinstance(item["auth"], dict) or "type" not in item["auth"]
        ):
            raise CredentialError("Invalid profile configuration.")
        gateway, auth = item["gateway"], dict(item["auth"])
        if not isinstance(gateway["url"], str) or not isinstance(gateway["type"], str) or not isinstance(auth["type"], str):
            raise CredentialError("Invalid gateway or authentication type in profile configuration.")
        auth_type = auth.pop("type")
        labels, outputs = definitions_from_dict(
            {key: item[key] for key in ("labels", "outputs") if key in item}, f"Profile '{name}'",
        )
        return Profile(
            name, gateway["url"], auth, gateway["type"], auth_type, gateway["allow_http"], item.get("rules"), labels, outputs,
        )

    def _write(self, value: dict) -> None:
        atomic_write_json(self.path, value)

    def add(self, profile: Profile) -> None:
        with self.locked():
            data = self._read()
            if profile.name in data["profiles"]:
                raise CredentialError("Profile already exists; choose a different name.")
            data["profiles"][profile.name] = profile.to_dict()
            if data["active"] is None:
                data["active"] = profile.name
            self._write(data)

    def update(self, name: str, change: Callable[[Profile], Profile]) -> Profile:
        """Apply ``change`` to a stored profile under the lock; it may not touch the gateway or the sign-in."""
        with self.locked():
            data = self._read()
            if name not in data["profiles"]:
                raise CredentialError("Unknown profile; use 'profile list'.")
            before = self._profile(name, data["profiles"][name])
            profile = change(before)
            if profile.name != name or profile.account != before.account:
                raise CredentialError("Updating a profile cannot change its gateway or sign-in; add a new profile.")
            data["profiles"][name] = profile.to_dict()
            self._write(data)
            return profile

    def select(self, name: str) -> None:
        with self.locked():
            data = self._read()
            if name not in data["profiles"]:
                raise CredentialError("Unknown profile; use 'profile list'.")
            data["active"] = name
            self._write(data)

    def get(self, name: str | None = None) -> Profile:
        with self.locked():
            data = self._read()
            name = name or data["active"]
            if name not in data["profiles"]:
                raise CredentialError("Select a configured profile with --profile or 'profile use'.")
            return self._profile(name, data["profiles"][name])

    def list(self) -> tuple[str | None, list[Profile]]:
        with self.locked():
            data = self._read()
            return data["active"], [
                self._profile(name, item) for name, item in sorted(data["profiles"].items())
            ]
