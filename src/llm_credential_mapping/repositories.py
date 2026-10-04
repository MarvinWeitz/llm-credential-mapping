import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import CredentialError
from .profiles import ProfileStore, atomic_write_json, gateway_origin
from .tags import validate_tag


def repository_identity(remote: str) -> str:
    """Normalize an HTTP/SSH/Git origin to host/path, removing credentials and the .git suffix; reject local or ambiguous remotes."""
    if (
        not isinstance(remote, str) or not remote or remote != remote.strip()
        or any(char.isspace() or ord(char) < 32 for char in remote)
    ):
        raise CredentialError("Git origin is invalid; configure an HTTP/SSH origin or use --customer.")
    port = None
    if "://" in remote:
        try:
            parsed = urlsplit(remote)
            if (
                parsed.scheme not in ("https", "http", "ssh", "git")
                or not parsed.hostname or parsed.query or parsed.fragment
            ):
                raise ValueError
            host, path, port = parsed.hostname.lower(), parsed.path, parsed.port
            if port == {"https": 443, "http": 80, "ssh": 22, "git": 9418}[parsed.scheme]:
                port = None
        except ValueError:
            raise CredentialError("Git origin is invalid; configure an HTTP/SSH origin or use --customer.") from None
    else:
        match = re.fullmatch(r"(?:[^/@:\s]+@)?(\[[^\]]+\]|[^/:\s]+):(.+)", remote)
        if match is None or re.match(r"^[A-Za-z]:[\\/]|\A/", remote):
            raise CredentialError("Local Git origins are not supported for mapping; use --customer.")
        host, path = match[1].lower().strip("[]"), match[2]
    path = unquote(path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if (
        not host or not path or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in host + path)
    ):
        raise CredentialError("Git origin has an ambiguous repository path; use --customer.")
    if host == "github.com":
        path = path.lower()
    host = f"[{host}]" if ":" in host else host
    return f"{host}{':' + str(port) if port is not None else ''}/{path}"


def current_repository(directory: Path | None = None) -> str:
    """Identify the current Git repository by origin, including from subdirectories; never contact the remote."""
    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=directory, capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            raise CredentialError("Cannot identify the repository; check Git and the directory, or use --customer.") from None
        if result.returncode != 0:
            if "detected dubious ownership" in result.stderr:
                raise CredentialError(
                    "Git refused this repository because of dubious ownership. "
                    "Verify its ownership/trust on the extension host; the CLI does not bypass Git's safety checks."
                )
            if args[0] == "rev-parse":
                raise CredentialError(
                    "Git cannot identify a working tree in the lookup directory. "
                    "Check 'git rev-parse --show-toplevel' there, or use --customer for a CLI launch."
                )
            raise CredentialError(
                "Git cannot read this repository's origin remote. "
                "Check 'git remote get-url origin' there, or use --customer for a CLI launch."
            )
        if len(result.stdout) > 8192:
            raise CredentialError("Git repository identity exceeded the size limit.")
        return result.stdout.strip()

    git("rev-parse", "--show-toplevel")
    return repository_identity(git("remote", "get-url", "origin"))


class RepositoryStore:
    """Store defaults for validated gateway roots and normalized repository identities; running sessions are unchanged."""

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
            validate_tag(entry["customer"])
        return data

    def get(self, gateway: str, repository: str) -> str | None:
        with self.profiles.locked("repositories"):
            entry = self._read()["bindings"].get(self._key(gateway, repository))
            return entry["customer"] if entry is not None else None

    def bind(self, gateway: str, repository: str, customer: str, *, replace: bool = False) -> None:
        self._validate_identity(gateway, repository)
        validate_tag(customer)
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
                raise CredentialError("This repository has no customer binding.")
            del data["bindings"][key]
            atomic_write_json(self.path, data)

    def list(self, gateway: str) -> list[dict[str, str]]:
        with self.profiles.locked("repositories"):
            return sorted(
                (entry for entry in self._read()["bindings"].values() if entry["gateway"] == gateway),
                key=lambda entry: entry["repository"],
            )
