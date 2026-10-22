import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from .errors import CredentialError
from .labels import validate_key, validate_value
from .profiles import ProfileStore, atomic_write_json, gateway_origin

_SSH_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
_AZURE_SSH_HOSTS = ("ssh.dev.azure.com", "vs-ssh.visualstudio.com")
# Cloud forges whose HTTPS paths never carry Bitbucket Server's /scm/ prefix.
_NO_SCM_PREFIX = ("github.com", "gitlab.com", "bitbucket.org", "dev.azure.com")
# SSH-over-port-443 endpoints, reached directly or through a 'HostName' in ~/.ssh/config.
_SSH_ENDPOINTS = {"ssh.github.com": "github.com", "altssh.gitlab.com": "gitlab.com", "altssh.bitbucket.org": "bitbucket.org"}


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

    Credentials, ports and a trailing .git are removed; Azure DevOps forms are collapsed, as are
    forge SSH-over-443 endpoints and the /scm/ prefix of Bitbucket Server HTTPS remotes. SSH host
    aliases are resolved with ``resolve_ssh_host`` when given. Local or ambiguous remotes are rejected.
    """
    if (
        not isinstance(remote, str) or not remote or remote != remote.strip()
        or any(char.isspace() or ord(char) < 32 for char in remote)
    ):
        raise CredentialError("Git origin is invalid; configure an HTTP/SSH origin or use --label.")
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
            raise CredentialError("Git origin is invalid; configure an HTTP/SSH origin or use --label.") from None
    else:
        match = re.fullmatch(r"(?:[^/@:\s]+@)?(\[[^\]]+\]|[^/:\s]+):(.+)", remote)
        if match is None or re.match(r"^[A-Za-z]:[\\/]|\A/", remote):
            raise CredentialError("Local Git origins are not supported for mapping; use --label.")
        host, path, ssh = match[1].strip("[]"), match[2], True
    if ssh and resolve_ssh_host is not None and _SSH_HOST.fullmatch(host):
        host = resolve_ssh_host(host) or host
    host = host.lower()
    if ssh:
        host = _SSH_ENDPOINTS.get(host, host)
    path = unquote(path).strip("/").lower()
    if path.endswith(".git"):
        path = path[:-4]
    if (
        not host or not path or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in host + path)
    ):
        raise CredentialError("Git origin has an ambiguous repository path; use --label.")
    host, path = _azure_path(host, path)
    if not ssh and host not in _NO_SCM_PREFIX and path.startswith("scm/") and path.count("/") == 2:
        path = path[4:]  # Bitbucket Server: https://host/scm/project/repo is ssh://host/project/repo.
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


class RepositoryStore:
    """Per-developer label bindings keyed by gateway and normalized repository identity (version 2)."""

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

    @staticmethod
    def _validate_labels(labels: object) -> dict[str, str]:
        if not isinstance(labels, Mapping) or not labels:
            raise CredentialError("A label binding needs at least one label.")
        return {validate_key(key): validate_value(value, key) for key, value in labels.items()}

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 2, "bindings": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):  # RecursionError: nesting too deep.
            raise CredentialError("Cannot read repositories.json; repair the nonsecret configuration.") from None
        if (
            not isinstance(data, dict) or set(data) != {"version", "bindings"}
            or type(data["version"]) is not int or data["version"] != 2 or not isinstance(data["bindings"], dict)
        ):
            raise CredentialError("Unsupported or invalid repositories.json.")
        for key, entry in data["bindings"].items():
            if not isinstance(entry, dict) or set(entry) != {"gateway", "repository", "labels"}:
                raise CredentialError("Invalid repository binding.")
            gateway, repository = entry["gateway"], entry["repository"]
            self._validate_identity(gateway, repository)
            if key != self._key(gateway, repository):
                raise CredentialError("Invalid repository binding identity.")
            self._validate_labels(entry["labels"])
        return data

    def get(self, gateway: str, repository: str) -> dict[str, str]:
        """The labels bound to one repository; empty when it has no binding."""
        with self.profiles.locked("repositories"):
            entry = self._read()["bindings"].get(self._key(gateway, repository))
            return dict(entry["labels"]) if entry is not None else {}

    def bind(self, gateway: str, repository: str, labels: Mapping[str, str], *, replace: bool = False) -> dict[str, str]:
        """Set the given keys and keep the others; a different value for a bound key needs ``replace``.

        Without ``replace`` a session that bound a key concurrently is never overwritten silently.
        Return the binding's labels after the change.
        """
        self._validate_identity(gateway, repository)
        labels = self._validate_labels(labels)
        with self.profiles.locked("repositories"):
            data = self._read()
            key = self._key(gateway, repository)
            current = dict(data["bindings"][key]["labels"]) if key in data["bindings"] else {}
            changed = [name for name, value in labels.items() if current.get(name, value) != value]
            if changed and not replace:
                raise CredentialError(
                    f"This repository already has a different value for: {', '.join(changed)}. "
                    "Use --label for one launch, or 'repo bind --replace --label KEY=VALUE' to change it."
                )
            current.update(labels)
            data["bindings"][key] = {"gateway": gateway, "repository": repository, "labels": current}
            atomic_write_json(self.path, data)
            return current

    def unbind(self, gateway: str, repository: str, keys: Iterable[str] | None = None) -> None:
        """Remove the binding, or only ``keys``; removing the last key removes the binding."""
        with self.profiles.locked("repositories"):
            data = self._read()
            key = self._key(gateway, repository)
            if key not in data["bindings"]:
                raise CredentialError("This repository has no personal label binding.")
            if keys is not None:
                keys = list(dict.fromkeys(keys))
                labels = data["bindings"][key]["labels"]
                unbound = [name for name in keys if name not in labels]
                if not keys or unbound:
                    raise CredentialError(f"This repository has no personal binding for: {', '.join(unbound) or 'no key given'}.")
                for name in keys:
                    del labels[name]
                if labels:
                    atomic_write_json(self.path, data)
                    return
            del data["bindings"][key]
            atomic_write_json(self.path, data)

    def list(self, gateway: str) -> list[dict]:
        """Bindings for one gateway: ``{"gateway", "repository", "labels"}`` sorted by repository."""
        with self.profiles.locked("repositories"):
            return sorted(
                (entry for entry in self._read()["bindings"].values() if entry["gateway"] == gateway),
                key=lambda entry: entry["repository"],
            )
