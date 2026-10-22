"""The forwarded auth kind: credentials come from the sign-in on the user's local machine.

For dev containers and other remote VS Code windows. The Virtual Key Mapper extension running in the
remote window listens on a local socket and relays each request through VS Code to the companion
extension on the local machine, which answers with the local CLI (and its OS vault). Only short-lived
access tokens reach the remote side; the refresh grant never leaves the local vault.

Protocol: one JSON line per connection, ``{"version": 1, "operation": "token"|"profile", "profile": NAME}``,
answered by ``{"ok": true, "result": {...}}`` or ``{"ok": false, "error": "..."}``. A "token" result is
``{access_token, expires_at, profile, user}``; this module asks only for tokens. The extension asks for
"profile" to create the remote profile: ``{name, gateway, endpoints, rules, labels, outputs}``, where
``labels`` and ``outputs`` are the local profile's own definitions (``Profile.definitions()``), not the
merged ones; the remote CLI fetches the central label rules itself. Labels never travel with tokens: they
are resolved in the remote window by the remote CLI.
"""

import json
import os
import socket
import stat
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from ..adapters import Credential
from ..errors import CredentialError
from ..profiles import Profile, ProfileStore, validate_profile_name

AGENT_VARIABLE = "VKM_AGENT"
TIMEOUT_SECONDS = 120  # The local side may renew the credential first.
_MAX_ANSWER = 1_048_576


def settings(values: dict) -> dict:
    """Optional ``host_profile``: the local profile to use; the local default when absent."""
    if not set(values) <= {"host_profile"}:
        raise CredentialError("Forwarded sign-in takes only --host-profile.")
    if "host_profile" in values:
        validate_profile_name(values["host_profile"])
    return dict(values)


def agent_path(environ: Mapping[str, str] | None = None) -> str:
    """Where the VS Code extension listens; the same default is computed by the extension."""
    environ = os.environ if environ is None else environ
    if environ.get(AGENT_VARIABLE):
        return environ[AGENT_VARIABLE]
    if environ.get("XDG_RUNTIME_DIR"):
        return str(Path(environ["XDG_RUNTIME_DIR"]) / "vkm" / "agent.sock")
    user = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
    return str(Path(tempfile.gettempdir()) / f"vkm-{user}" / "agent.sock")


def _check_owner(path: str) -> None:
    """Refuse a socket another user could have placed: it must be ours, in a real directory only we (or root) can write."""
    directory, sock = os.lstat(os.path.dirname(path) or "."), os.lstat(path)
    if (
        not stat.S_ISDIR(directory.st_mode) or directory.st_uid not in (os.getuid(), 0) or directory.st_mode & 0o022
        or not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != os.getuid() or sock.st_mode & 0o022
    ):
        raise CredentialError(
            f"Refusing the sign-in socket {path}: it must belong to you, in a directory nobody else can write to."
        )


def _connect(path: str) -> socket.socket:
    if not hasattr(socket, "AF_UNIX"):
        raise CredentialError("Forwarded sign-in needs a Unix socket; it is meant for Linux dev containers.")
    if hasattr(os, "getuid"):
        _check_owner(path)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(TIMEOUT_SECONDS)
    connection.connect(path)
    return connection


def request(operation: str, host_profile: str | None, path: str,
            connect: Callable[[str], socket.socket] | None = None) -> dict:
    """Ask the local machine through the VS Code extension; raise CredentialError with its message on failure."""
    try:
        connection = (connect or _connect)(path)
    except (OSError, CredentialError) as error:
        if isinstance(error, CredentialError):
            raise
        raise CredentialError(
            "No connection to the sign-in on your local machine. Open this folder in VS Code with the "
            "'Virtual Key Mapper' extension in this container and 'Virtual Key Mapper: local sign-in' in your local VS Code."
        ) from None
    with connection:
        try:
            message = {"version": 1, "operation": operation, "profile": host_profile or ""}
            connection.sendall(json.dumps(message).encode() + b"\n")
            raw = b""
            while not raw.endswith(b"\n") and len(raw) <= _MAX_ANSWER:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                raw += chunk
        except OSError:
            raise CredentialError("The connection to your local sign-in failed; retry.") from None
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError):
        raise CredentialError("Your local sign-in sent an unreadable answer; update both VS Code extensions.") from None
    if not isinstance(answer, dict) or answer.get("ok") not in (True, False):
        raise CredentialError("Your local sign-in sent an unexpected answer; update both VS Code extensions.")
    if not answer["ok"]:
        error = answer.get("error")
        raise CredentialError(f"Local sign-in: {error if isinstance(error, str) else 'request failed'}")
    if not isinstance(answer.get("result"), dict):
        raise CredentialError("Your local sign-in sent an unexpected answer; update both VS Code extensions.")
    return answer["result"]


class Forwarded:
    """Credentials from the local machine's sign-in, relayed by VS Code; nothing is stored here."""

    def __init__(self, profile: Profile, store: ProfileStore, secrets: Callable[[], object] | None = None,
                 connect: Callable[[str], socket.socket] | None = None) -> None:
        # ``secrets`` (the vault factory) is never called: nothing is stored on this side.
        self.profile = profile
        self.connect = connect

    def _request(self, operation: str) -> dict:
        return request(operation, self.profile.auth.get("host_profile"), agent_path(), self.connect or _connect)

    def login(self, notify: Callable[[str], None], **options) -> Credential:
        raise CredentialError(
            f"Profile '{self.profile.name}' uses the sign-in of your local machine: run 'vkm login' "
            "there, in a local terminal."
        )

    def logout(self, notify: Callable[[str], None]) -> None:
        raise CredentialError(
            f"Profile '{self.profile.name}' uses the sign-in of your local machine: run 'vkm logout' there."
        )

    def status(self) -> dict:
        base = {"profile": self.profile.name, "gateway": self.profile.gateway, "auth": "forwarded", "agent": agent_path()}
        try:
            credential = self.credential()
        except CredentialError as error:
            return {**base, "state": "unavailable", "error": str(error)}
        return {**base, "state": "ready", "user_id": credential.user, "expires_at": credential.expires_at}

    def credential(self) -> Credential:
        result = self._request("token")
        token, expires_at, user = result.get("access_token"), result.get("expires_at"), result.get("user")
        if (
            not isinstance(token, str) or not token or any(ord(char) < 33 for char in token)
            or not (expires_at is None or type(expires_at) in (int, float))
            or not (user is None or isinstance(user, str))
        ):
            raise CredentialError("Your local sign-in returned an invalid token.")
        return Credential(token, expires_at, user)
