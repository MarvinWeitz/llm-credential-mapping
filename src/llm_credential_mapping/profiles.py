import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from .errors import CredentialError


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
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise CredentialError("Gateway must be a root URL without credentials, query or path.")
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


@dataclass(frozen=True)
class Profile:
    """A named authentication profile bound to a gateway and business-unit team ID."""

    name: str
    gateway: str
    team_id: str
    allow_http: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.name):
            raise CredentialError("Project name must be 1-64 letters, digits, dots, _ or -.")
        if (
            not isinstance(self.team_id, str)
            or not self.team_id.strip()
            or self.team_id != self.team_id.strip()
            or len(self.team_id) > 256
            or any(ord(char) < 32 for char in self.team_id)
        ):
            raise CredentialError("Provide the exact, nonempty LiteLLM team ID.")
        if type(self.allow_http) is not bool:
            raise CredentialError("Invalid loopback HTTP setting.")
        object.__setattr__(self, "gateway", gateway_origin(self.gateway, self.allow_http))

    @property
    def account(self) -> str:
        value = json.dumps([self.name, self.gateway, self.team_id])
        return hashlib.sha256(value.encode()).hexdigest()


def default_home() -> Path:
    if override := os.environ.get("LLM_CREDENTIAL_MAPPING_HOME"):
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "llm-credential-mapping"


def atomic_write_json(path: Path, value: dict) -> None:
    """Replace a JSON configuration atomically; the caller must hold its configuration lock."""
    atomic_write_text(path, json.dumps(value, indent=2) + "\n")


def atomic_write_text(
    path: Path, value: str, *, newline: str | None = None, permissions: int | None = None,
) -> None:
    """Replace text atomically under the caller's lock; optionally preserve line endings and file permissions."""
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
        os.replace(filename, path)
    finally:
        if filename and os.path.exists(filename):
            os.unlink(filename)


class ProfileStore:
    """Persist nonsecret project settings atomically in the user's configuration directory."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / "projects.json"

    @contextmanager
    def locked(self, name: str = "profiles") -> Iterator[None]:
        try:
            self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
            with FileLock(str(self.home / f"{name}.lock"), timeout=10):
                yield
        except Timeout:
            raise CredentialError("Another credential operation is busy; try again.") from None
        except OSError:
            raise CredentialError("Cannot access the credential configuration directory.") from None

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "active": None, "projects": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CredentialError("Cannot read projects.json; repair the nonsecret configuration.") from None
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
            or not isinstance(value.get("projects"), dict)
            or (
                value.get("active") is not None
                and (
                    not isinstance(value["active"], str)
                    or value["active"] not in value["projects"]
                )
            )
        ):
            raise CredentialError("Unsupported or invalid projects.json.")
        for name, item in value["projects"].items():
            self._profile(name, item)
        return value

    @staticmethod
    def _profile(name: str, item: object) -> Profile:
        if not isinstance(item, dict) or set(item) != {"gateway", "team_id", "allow_http"}:
            raise CredentialError("Invalid project configuration.")
        if not isinstance(item["gateway"], str):
            raise CredentialError("Invalid gateway in project configuration.")
        return Profile(name, item["gateway"], item["team_id"], item["allow_http"])

    def _write(self, value: dict) -> None:
        atomic_write_json(self.path, value)

    def add(self, profile: Profile) -> None:
        with self.locked():
            data = self._read()
            if profile.name in data["projects"]:
                raise CredentialError("Project already exists; choose a different name.")
            data["projects"][profile.name] = {
                "gateway": profile.gateway,
                "team_id": profile.team_id,
                "allow_http": profile.allow_http,
            }
            if data["active"] is None:
                data["active"] = profile.name
            self._write(data)

    def select(self, name: str) -> None:
        with self.locked():
            data = self._read()
            if name not in data["projects"]:
                raise CredentialError("Unknown project; use 'project list'.")
            data["active"] = name
            self._write(data)

    def get(self, name: str | None = None) -> Profile:
        with self.locked():
            data = self._read()
            name = name or data["active"]
            if name not in data["projects"]:
                raise CredentialError("Select a configured project with --project or 'project use'.")
            return self._profile(name, data["projects"][name])

    def list(self) -> tuple[str | None, list[Profile]]:
        with self.locked():
            data = self._read()
            return data["active"], [
                self._profile(name, item) for name, item in sorted(data["projects"].items())
            ]
