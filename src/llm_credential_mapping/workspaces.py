import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import CredentialError
from .profiles import ProfileStore, atomic_write_json
from .repositories import current_repository


@dataclass(frozen=True)
class WorkspaceBinding:
    """A local launch directory's explicitly selected primary Git checkout and expected origin identity."""

    workspace: str
    directory: str
    repository: str

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in (self.workspace, self.directory, self.repository)
            )
            or not Path(self.workspace).is_absolute() or not Path(self.directory).is_absolute()
        ):
            raise CredentialError("Invalid workspace primary-repository binding.")


class WorkspaceStore:
    """Persist exact canonical workspace mappings without credentials or repository discovery.

    Invalid or inaccessible configuration raises CredentialError.
    """

    def __init__(self, profiles: ProfileStore) -> None:
        self.profiles = profiles
        self.path = profiles.home / "workspaces.json"

    @staticmethod
    def _key(path: Path) -> str:
        try:
            return os.path.normcase(str(path.expanduser().resolve()))
        except (OSError, ValueError, RuntimeError):
            raise CredentialError("Cannot resolve the workspace directory.") from None

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "bindings": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RuntimeError):
            raise CredentialError("Cannot read workspaces.json; repair the nonsecret configuration.") from None
        if (
            not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(data.get("bindings"), dict)
        ):
            raise CredentialError("Unsupported or invalid workspaces.json.")
        for key, item in data["bindings"].items():
            if not isinstance(item, dict) or set(item) != {"workspace", "directory", "repository"}:
                raise CredentialError("Invalid workspace primary-repository binding.")
            binding = WorkspaceBinding(**item)
            if key != self._key(Path(binding.workspace)):
                raise CredentialError("Invalid workspace primary-repository binding.")
        return data

    def bind(self, workspace: Path, directory: Path) -> WorkspaceBinding:
        """Explicitly replace a workspace's primary checkout after verifying its Git origin; never require a vault."""
        try:
            workspace, directory = workspace.expanduser().resolve(), directory.expanduser().resolve()
        except (OSError, ValueError):
            raise CredentialError("Cannot resolve the workspace or primary repository directory.") from None
        if not workspace.is_dir():
            raise CredentialError("The workspace directory does not exist.")
        repository = current_repository(directory)
        binding = WorkspaceBinding(str(workspace), str(directory), repository)
        with self.profiles.locked("workspaces"):
            data = self._read()
            data["bindings"][self._key(workspace)] = binding.__dict__
            atomic_write_json(self.path, data)
        return binding

    def get(self, workspace: Path) -> WorkspaceBinding | None:
        """Return an exact-path selection or None; do not inherit a parent mapping or validate the checkout."""
        with self.profiles.locked("workspaces"):
            item = self._read()["bindings"].get(self._key(workspace))
            return WorkspaceBinding(**item) if item is not None else None

    def unbind(self, workspace: Path) -> None:
        """Remove an exact-path selection; raise CredentialError if none exists. Running processes are unchanged."""
        with self.profiles.locked("workspaces"):
            data = self._read()
            key = self._key(workspace)
            if key not in data["bindings"]:
                raise CredentialError("This workspace has no primary repository mapping.")
            del data["bindings"][key]
            atomic_write_json(self.path, data)

    def list(self) -> list[WorkspaceBinding]:
        with self.profiles.locked("workspaces"):
            return [WorkspaceBinding(**item) for key, item in sorted(self._read()["bindings"].items())]
