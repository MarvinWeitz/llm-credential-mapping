import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from .errors import CredentialError
from .profiles import ProfileStore, atomic_write_json, gateway_origin
from .customers import validate_customer

_SSH_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
_AZURE_SSH_HOSTS = ("ssh.dev.azure.com", "vs-ssh.visualstudio.com")


def _azure_path(host: str, path: str) -> tuple[str, str]:
    """Collapse Azure DevOps HTTPS, SSH and legacy visualstudio.com remotes to dev.azure.com/org/project/repo."""
    parts = path.split("/")
    if host in _AZURE_SSH_HOSTS and len(parts) == 4 and parts[0] == "v3":
        return "dev.azure.com", "/".join(parts[1:])
    if host == "dev.azure.com" and len(parts) == 4 and parts[2] == "_git":
        return host, "/".join((parts[0], parts[1], parts[3]))
    if host.endswith(".visualstudio.com") and host not in _AZURE_SSH_HOSTS:
        organization = host.removesuffix(".visualstudio.com")
        if parts and parts[0] == "defaultcollection":
            parts = parts[1:]
        if len(parts) == 3 and parts[1] == "_git":
            return "dev.azure.com", "/".join((organization, parts[0], parts[2]))
    return host, path


def repository_identity(remote: str, resolve_ssh_host: Callable[[str], str | None] | None = None) -> str:
    """Normalize a Git remote to a lowercase host/path identity shared by its HTTPS and SSH forms.

    Credentials, ports and a trailing .git are removed; Azure DevOps forms are collapsed. SSH host
    aliases are resolved with ``resolve_ssh_host`` when given. Local or ambiguous remotes are rejected.
    """
    if (
        not isinstance(remote, str) or not remote or remote != remote.strip()
        or any(char.isspace() or ord(char) < 32 for char in remote)
    ):
        raise CredentialError("Git origin is invalid; configure an HTTP/SSH origin or use --customer.")
    ssh = False
    if "://" in remote:
        try:
            parsed = urlsplit(remote)
            if (
                parsed.scheme not in ("https", "http", "ssh", "git")
                or not parsed.hostname or parsed.query or parsed.fragment
            ):
                raise ValueError
            parsed.port  # Reject invalid ports even though ports are not part of the identity.
            host, path, ssh = parsed.hostname, parsed.path, parsed.scheme == "ssh"
        except ValueError:
            raise CredentialError("Git origin is invalid; configure an HTTP/SSH origin or use --customer.") from None
    else:
        match = re.fullmatch(r"(?:[^/@:\s]+@)?(\[[^\]]+\]|[^/:\s]+):(.+)", remote)
        if match is None or re.match(r"^[A-Za-z]:[\\/]|\A/", remote):
            raise CredentialError("Local Git origins are not supported for mapping; use --customer.")
        host, path, ssh = match[1].strip("[]"), match[2], True
    if ssh and resolve_ssh_host is not None and _SSH_HOST.fullmatch(host):
        host = resolve_ssh_host(host) or host
    host = host.lower()
    path = unquote(path).strip("/").lower()
    if path.endswith(".git"):
        path = path[:-4]
    if (
        not host or not path or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in host + path)
    ):
        raise CredentialError("Git origin has an ambiguous repository path; use --customer.")
    host, path = _azure_path(host, path)
    host = f"[{host}]" if ":" in host else host
    return f"{host}/{path}"


def ssh_hostname(alias: str) -> str | None:
    """Return the HostName that OpenSSH configuration assigns to an alias, without connecting."""
    if not _SSH_HOST.fullmatch(alias) or shutil.which("ssh") is None:
        return None
    try:
        result = subprocess.run(
            ["ssh", "-G", alias], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "hostname" and _SSH_HOST.fullmatch(value.strip()):
            return value.strip()
    return None


@dataclass(frozen=True)
class Checkout:
    """A Git working tree and the normalized identities of its origin and upstream remotes, if usable."""

    root: Path
    origin: str | None
    upstream: str | None

    @property
    def identities(self) -> tuple[str, ...]:
        return tuple(identity for identity in (self.origin, self.upstream) if identity is not None)


def _git(directory: Path | None, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=directory, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    if result.returncode != 0 and "detected dubious ownership" in result.stderr:
        raise CredentialError(
            "Git refused this repository because of dubious ownership. "
            "Verify its ownership/trust; the CLI does not bypass Git's safety checks."
        )
    if len(result.stdout) > 8192:
        raise CredentialError("Git repository identity exceeded the size limit.")
    return result


def find_checkout(
    directory: Path | None = None, resolve_ssh_host: Callable[[str], str | None] | None = ssh_hostname,
) -> Checkout | None:
    """Return the Git checkout containing ``directory`` without contacting any remote, or None outside Git."""
    toplevel = _git(directory, "rev-parse", "--show-toplevel")
    if toplevel is None or toplevel.returncode != 0 or not toplevel.stdout.strip():
        return None
    identities = []
    for remote in ("origin", "upstream"):
        result = _git(directory, "remote", "get-url", remote)
        try:
            identities.append(
                repository_identity(result.stdout.strip(), resolve_ssh_host)
                if result is not None and result.returncode == 0 else None
            )
        except CredentialError:
            identities.append(None)
    return Checkout(Path(toplevel.stdout.strip()), *identities)


def current_repository(directory: Path | None = None) -> str:
    # Used by the legacy editor adapters (deleted in plan steps 4 and 7); new code uses find_checkout.
    """Identify the current Git repository by origin, including from subdirectories; never contact the remote."""
    checkout = find_checkout(directory)
    if checkout is None:
        raise CredentialError(
            "Git cannot identify a working tree in the lookup directory. "
            "Check 'git rev-parse --show-toplevel' there, or use --customer."
        )
    if checkout.origin is None:
        raise CredentialError(
            "This repository has no usable HTTP/SSH origin remote. "
            "Check 'git remote get-url origin', or use --customer or a .llm-customer file."
        )
    return checkout.origin


class RepositoryStore:
    """Per-developer customer overrides keyed by gateway and normalized repository identity."""

    def __init__(self, profiles: ProfileStore) -> None:
        self.profiles = profiles
        self.path = profiles.home / "repositories.json"

    @staticmethod
    def _key(gateway: str, repository: str) -> str:
        return hashlib.sha256(json.dumps([gateway, repository]).encode()).hexdigest()

    @staticmethod
    def _validate_identity(gateway: str, repository: str) -> None:
        if (
            not isinstance(gateway, str) or not isinstance(repository, str)
            or not repository or any(ord(char) < 32 or ord(char) == 127 for char in repository)
            or gateway_origin(gateway, True) != gateway
        ):
            raise CredentialError("Invalid repository binding identity.")

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "bindings": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CredentialError("Cannot read repositories.json; repair the nonsecret configuration.") from None
        if (
            not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(data.get("bindings"), dict)
        ):
            raise CredentialError("Unsupported or invalid repositories.json.")
        for key, entry in data["bindings"].items():
            if not isinstance(entry, dict) or set(entry) != {"gateway", "repository", "customer"}:
                raise CredentialError("Invalid repository binding.")
            gateway, repository = entry["gateway"], entry["repository"]
            self._validate_identity(gateway, repository)
            if key != self._key(gateway, repository):
                raise CredentialError("Invalid repository binding identity.")
            validate_customer(entry["customer"])
        return data

    def get(self, gateway: str, repository: str) -> str | None:
        with self.profiles.locked("repositories"):
            entry = self._read()["bindings"].get(self._key(gateway, repository))
            return entry["customer"] if entry is not None else None

    def bind(self, gateway: str, repository: str, customer: str, *, replace: bool = False) -> None:
        self._validate_identity(gateway, repository)
        validate_customer(customer)
        with self.profiles.locked("repositories"):
            data = self._read()
            key = self._key(gateway, repository)
            previous = data["bindings"].get(key)
            if previous and previous["customer"] != customer and not replace:
                raise CredentialError(
                    "Another session already bound this repository. "
                    "Use --customer for one launch, or 'repo bind --customer TAG' to change its default."
                )
            data["bindings"][key] = {"gateway": gateway, "repository": repository, "customer": customer}
            atomic_write_json(self.path, data)

    def unbind(self, gateway: str, repository: str) -> None:
        with self.profiles.locked("repositories"):
            data = self._read()
            key = self._key(gateway, repository)
            if key not in data["bindings"]:
                raise CredentialError("This repository has no local customer binding.")
            del data["bindings"][key]
            atomic_write_json(self.path, data)

    def list(self, gateway: str) -> list[dict[str, str]]:
        with self.profiles.locked("repositories"):
            return sorted(
                (entry for entry in self._read()["bindings"].values() if entry["gateway"] == gateway),
                key=lambda entry: entry["repository"],
            )
