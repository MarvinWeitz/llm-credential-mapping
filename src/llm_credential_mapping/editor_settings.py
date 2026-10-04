import json
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Generic, Iterator, Self, TypeVar

from filelock import FileLock, Timeout

from .errors import CredentialError
from .jsonc import JsoncDocument, Value as _Value
from .profiles import atomic_write_text

_WRAPPER = "claudeCode.claudeProcessWrapper"
_LOGIN = "claudeCode.disableLoginPrompt"
_ENVIRONMENT = "claudeCode.environmentVariables"
_HOME = "LLM_CREDENTIAL_MAPPING_HOME"
_MAX_BYTES = 2_097_152


class _Document(JsoncDocument):
    """Edit bootstrap fields in JSONC using source offsets; the standard JSON decoder validates the masked document."""

    def __init__(self, source: str) -> None:
        super().__init__(source)
        members = self._members(self.root)
        for member in members:
            if member.name == _ENVIRONMENT:
                self._environment(member.value)

    def _environment(self, node: _Value) -> list[tuple[str, _Value]]:
        if not isinstance(node.value, list):
            raise CredentialError("VS Code claudeCode.environmentVariables must be an array; repair it before applying.")
        elements = self._elements(node)
        entries = []
        for item in elements:
            data = item.value
            if (
                not isinstance(data, dict) or not isinstance(data.get("name"), str)
                or not isinstance(data.get("value"), str)
            ):
                raise CredentialError("VS Code Claude environment entries must contain string-valued name and value fields.")
            entries.append((data["name"], item))
        return entries

    def merge(self, settings: dict) -> str:
        if (
            set(settings) != {_WRAPPER, _LOGIN, _ENVIRONMENT}
            or not isinstance(settings[_WRAPPER], str) or not settings[_WRAPPER]
            or type(settings[_LOGIN]) is not bool or not isinstance(settings[_ENVIRONMENT], list)
            or any(
                not isinstance(item, dict) or set(item) != {"name", "value"}
                or not isinstance(item["name"], str) or not item["name"] or not isinstance(item["value"], str)
                for item in settings[_ENVIRONMENT]
            )
            or len({item["name"] for item in settings[_ENVIRONMENT]}) != len(settings[_ENVIRONMENT])
        ):
            raise CredentialError("Invalid generated Claude bootstrap settings; nothing was applied.")
        members = self._members(self.root)
        edits = []
        additions = []
        for name in (_WRAPPER, _LOGIN):
            matching = [item for item in members if item.name == name]
            for member in matching:
                if type(member.value.value) is not type(settings[name]) or member.value.value != settings[name]:
                    edits.append((member.value.start, member.value.end, json.dumps(settings[name])))
            if not matching:
                additions.append((json.dumps(name) + ": ", settings[name]))
        environment = [member for member in members if member.name == _ENVIRONMENT]
        if not environment:
            additions.append((json.dumps(_ENVIRONMENT) + ": ", settings[_ENVIRONMENT]))
        for member in environment:
            elements = self._environment(member.value)
            extra = []
            for generated in settings[_ENVIRONMENT]:
                matching = [item for name, item in elements if name == generated["name"]]
                for element in matching:
                    for property in self._members(element):
                        if property.name == "value" and property.value.value != generated["value"]:
                            edits.append((property.value.start, property.value.end, json.dumps(generated["value"])))
                if not matching:
                    extra.append(("", generated))
            edits.extend(self._append(
                member.value, [item.start for _, item in elements], [item.end for _, item in elements], extra,
            ))
        edits.extend(self._append(
            self.root, [item.start for item in members], [item.value.end for item in members], additions,
        ))
        return self._edited(edits)

    def disable(self) -> str | None:
        members = self._members(self.root)
        wrappers = [member.value.value for member in members if member.name == _WRAPPER]

        def owned(value: object) -> bool:
            return isinstance(value, str) and PureWindowsPath(value).name.casefold() in (
                "llm-credentials-claude", "llm-credentials-claude.exe",
            )

        if any(value is not None and value != "" and not owned(value) for value in wrappers):
            raise CredentialError("A different Claude process wrapper is configured; no settings were removed.")
        environments = {
            index: self._environment(member.value)
            for index, member in enumerate(members) if member.name == _ENVIRONMENT
        }
        if not any(owned(value) for value in wrappers) and not any(
            name == _HOME for elements in environments.values() for name, _ in elements
        ):
            return None
        removed = {index for index, member in enumerate(members) if member.name in (_WRAPPER, _LOGIN)}
        edits = []
        for index, elements in environments.items():
            home_entries = {position for position, (name, _) in enumerate(elements) if name == _HOME}
            if len(home_entries) == len(elements):
                removed.add(index)
            elif home_entries:
                edits.extend(self._remove(
                    [(element.start, element.end) for _, element in elements], home_entries,
                ))
        edits.extend(self._remove([(member.start, member.value.end) for member in members], removed))
        return self._edited(edits)

def settings_file(explicit: Path | None = None) -> Path:
    """Resolve User/Remote settings; require an explicit path for ambiguous locations or desktop editor profiles."""
    try:
        if explicit is not None:
            path = explicit.expanduser()
        elif os.environ.get("VSCODE_AGENT_FOLDER"):
            path = Path(os.environ["VSCODE_AGENT_FOLDER"]).expanduser()
            if not path.is_absolute():
                raise CredentialError("VSCODE_AGENT_FOLDER must be absolute; use --settings-file.")
            path = path / "data" / "Machine" / "settings.json"
        elif os.environ.get("VSCODE_PORTABLE"):
            path = Path(os.environ["VSCODE_PORTABLE"]).expanduser()
            if not path.is_absolute():
                raise CredentialError("VSCODE_PORTABLE must be absolute; use --settings-file.")
            path = path / "user-data" / "User" / "settings.json"
        else:
            home = Path.home()
            if sys.platform == "win32":
                if not os.environ.get("APPDATA"):
                    raise CredentialError("Cannot locate VS Code User Settings without APPDATA; use --settings-file.")
                root = Path(os.environ["APPDATA"])
            elif sys.platform == "darwin":
                root = home / "Library" / "Application Support"
            else:
                root = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
            if not root.is_absolute():
                raise CredentialError("The VS Code configuration directory must be absolute; use --settings-file.")
            remote = [
                home / folder / "data" / "Machine" / "settings.json"
                for folder in (".vscode-server", ".vscode-server-insiders")
                if (home / folder).is_dir()
            ]
            local = [
                root / folder / "User" / "settings.json"
                for folder in ("Code", "Code - Insiders") if (root / folder / "User").is_dir()
            ]
            remote_host = any(os.environ.get(name) for name in ("REMOTE_CONTAINERS", "CODESPACES", "SSH_CONNECTION"))
            if remote_host and not remote:
                raise CredentialError("Cannot locate this remote host's VS Code settings; use --settings-file.")
            candidates = remote if remote_host else remote + local
            if len(candidates) > 1:
                raise CredentialError("Multiple VS Code settings locations found; use --settings-file for the intended editor.")
            path = candidates[0] if candidates else root / "Code" / "User" / "settings.json"
        # Remote Machine settings are shared across editor profiles.
        if explicit is None and path.parent.name == "User":
            profiles = path.parent / "profiles"
            if profiles.is_dir() and any(item.is_dir() for item in profiles.iterdir()):
                raise CredentialError("VS Code desktop profiles found; use --settings-file for the active editor profile.")
        if path.name != "settings.json" or path.parent.name.casefold() == ".vscode":
            raise CredentialError("Choose a VS Code User/Remote settings.json file, not Workspace Settings.")
        resolved = path.resolve()
        if resolved.parent.name.casefold() == ".vscode":
            raise CredentialError("Choose User/Remote Settings, not Workspace Settings.")
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise CredentialError("Cannot resolve VS Code settings; use --settings-file with an accessible User/Remote path.") from None


_DocumentType = TypeVar("_DocumentType", bound=JsoncDocument)


@dataclass
class EditorEdit(Generic[_DocumentType]):
    """A locked UTF-8 editor snapshot; bounded atomic writes reject external content or permission changes."""

    path: Path
    original: bytes | None
    permissions: int | None
    document: _DocumentType

    @classmethod
    def _read(cls, path: Path, document_factory: Callable[[str], _DocumentType]) -> Self:
        try:
            original = path.read_bytes() if path.exists() else None
            if original is not None and len(original) > _MAX_BYTES:
                raise CredentialError("VS Code settings exceed 2 MiB; no settings were applied.")
            if original is not None and not os.access(path, os.W_OK):
                raise CredentialError("VS Code settings are read-only; no settings were applied.")
            permissions = stat.S_IMODE(path.stat().st_mode) if original is not None else None
            return cls(path, original, permissions, document_factory(original.decode("utf-8") if original is not None else ""))
        except (OSError, UnicodeError):
            raise CredentialError("Cannot read VS Code settings as UTF-8; check permissions and repair the file.") from None

    def write(self, updated: str) -> bool:
        """Atomically save validated JSONC; return whether bytes changed or raise CredentialError without dumping contents."""
        return self._write(updated)

    def _write(self, updated: str | None) -> bool:
        encoded = updated.encode("utf-8") if updated is not None else None
        if encoded is not None and len(encoded) > _MAX_BYTES:
            raise CredentialError("Resulting VS Code settings exceed 2 MiB; no settings were applied.")
        try:
            current = self.path.read_bytes() if self.path.exists() else None
            if current != self.original:
                raise CredentialError("VS Code settings changed during setup; save the editor settings and retry.")
            if current is not None and stat.S_IMODE(self.path.stat().st_mode) != self.permissions:
                raise CredentialError("VS Code settings permissions changed during setup; retry without editing the file.")
            if updated is not None and current != encoded:
                atomic_write_text(self.path, updated, newline="", permissions=self.permissions)
                return True
        except OSError:
            raise CredentialError("Cannot atomically update VS Code settings; check write permissions and retry.") from None
        return False


class SettingsEdit(EditorEdit[_Document]):
    """An editor snapshot for Claude bootstrap changes; preserve unrelated JSONC and environment entries."""

    @classmethod
    def read(cls, path: Path) -> Self:
        return cls._read(path, _Document)

    def apply(self, settings: dict) -> None:
        """Atomically apply a merge; reject changed files and raise CredentialError on failure without dumping their contents."""
        self._write(self.document.merge(settings))

    def disable(self) -> bool:
        """Disconnect the recognised adapter and return whether the file changed; retain D-Bus and unrelated settings.

        Raise CredentialError for another wrapper, invalid settings, concurrent edits or write failures.
        """
        return self._write(self.document.disable())


@contextmanager
def _locked_target(path: Path) -> Iterator[None]:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = FileLock(str(path) + ".llm-credentials.lock", timeout=10)
        lock.acquire()
    except Timeout:
        raise CredentialError("Another editor setup is busy; try again.") from None
    except OSError:
        raise CredentialError("Cannot access the VS Code settings directory.") from None
    try:
        yield
    finally:
        try:
            lock.release()
        except OSError:
            raise CredentialError("Cannot release the editor settings lock; settings may already have been applied.") from None


@contextmanager
def editing(path: Path) -> Iterator[SettingsEdit]:
    """Lock one settings target across CLI processes; reject malformed input before setup changes its CLI configuration."""
    with _locked_target(path):
        yield SettingsEdit.read(path)


@contextmanager
def editing_document(
    path: Path, document_factory: Callable[[str], _DocumentType],
) -> Iterator[EditorEdit[_DocumentType]]:
    """Lock and validate one editor JSONC target with its schema; retain optimistic write and permission checks."""
    with _locked_target(path):
        yield EditorEdit[_DocumentType]._read(path, document_factory)
