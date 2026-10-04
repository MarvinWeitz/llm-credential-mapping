import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout

from .errors import CredentialError
from .profiles import atomic_write_text

_WRAPPER = "claudeCode.claudeProcessWrapper"
_LOGIN = "claudeCode.disableLoginPrompt"
_ENVIRONMENT = "claudeCode.environmentVariables"
_MAX_BYTES = 2_097_152
_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/', re.DOTALL)
_TRAILING_COMMAS = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])', re.DOTALL)


def _reject_constant(value: str) -> None:
    raise ValueError("JSON constants must use standard JSON syntax.")


def _mask_comments(source: str) -> str:
    def mask(match: re.Match[str]) -> str:
        token = match.group()
        return token if token.startswith('"') else "".join(char if char in "\r\n" else " " for char in token)

    masked = _TOKENS.sub(mask, source)
    return " " + masked[1:] if masked.startswith("\ufeff") else masked


@dataclass(frozen=True)
class _Value:
    start: int
    end: int
    value: object


@dataclass(frozen=True)
class _Member:
    name: str
    start: int
    value: _Value


class _Document:
    """Edit bootstrap fields in JSONC using source offsets; the standard JSON decoder validates the masked document."""

    def __init__(self, source: str) -> None:
        self.newline = "\r\n" if "\r\n" in source else "\n"
        comments = _mask_comments(source)
        if not comments.strip():
            source += (self.newline if source and not source.endswith(("\r", "\n")) else "") + "{}" + self.newline
            comments = _mask_comments(source)

        def mask_comma(match: re.Match[str]) -> str:
            if match.group().startswith('"'):
                return match.group()
            preceding = comments[:match.start()].rstrip()
            return " " if preceding and preceding[-1] not in "{[,:" else ","

        self.source = source
        self.comments = comments
        self.clean = _TRAILING_COMMAS.sub(mask_comma, comments)
        self.decoder = json.JSONDecoder(parse_constant=_reject_constant)
        try:
            value = json.loads(self.clean, parse_constant=_reject_constant)
        except json.JSONDecodeError as error:
            raise CredentialError(
                f"Invalid VS Code settings JSONC at line {error.lineno}, column {error.colno}; repair it before applying."
            ) from None
        except (ValueError, RecursionError):
            raise CredentialError("Invalid VS Code settings JSONC; repair it before applying.") from None
        if not isinstance(value, dict):
            raise CredentialError("VS Code User/Remote settings must contain a JSON object.")
        self.root = self._value(self._skip(0))
        members = self._members(self.root)
        self.indent = next((self._line_indent(item.start) for item in members if self._line_indent(item.start)), "  ")
        for member in members:
            if member.name == _ENVIRONMENT:
                self._environment(member.value)

    def _skip(self, index: int, source: str | None = None) -> int:
        source = self.clean if source is None else source
        while index < len(source) and source[index].isspace():
            index += 1
        return index

    def _value(self, start: int) -> _Value:
        value, end = self.decoder.raw_decode(self.clean, start)
        return _Value(start, end, value)

    def _members(self, node: _Value) -> list[_Member]:
        result = []
        index = self._skip(node.start + 1)
        while self.clean[index] != "}":
            name, end = self.decoder.raw_decode(self.clean, index)
            value = self._value(self._skip(self._skip(end) + 1))
            result.append(_Member(name, index, value))
            index = self._skip(value.end)
            if self.clean[index] == ",":
                index = self._skip(index + 1)
        return result

    def _elements(self, node: _Value) -> list[_Value]:
        result = []
        index = self._skip(node.start + 1)
        while self.clean[index] != "]":
            value = self._value(index)
            result.append(value)
            index = self._skip(value.end)
            if self.clean[index] == ",":
                index = self._skip(index + 1)
        return result

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

    def _line_indent(self, index: int) -> str:
        line = self.source[self.source.rfind("\n", 0, index) + 1:index]
        return line[:len(line) - len(line.lstrip(" \t"))]

    def _append(
        self, node: _Value, starts: list[int], ends: list[int], entries: list[tuple[str, object]],
    ) -> list[tuple[int, int, str]]:
        if not entries:
            return []
        closing = node.end - 1
        multiline = not starts or "\n" in self.source[node.start:node.end]
        trailing = bool(ends and self.comments[self._skip(ends[-1], self.comments)] == ",")
        edits = [(ends[-1], ends[-1], ",")] if ends and not trailing else []
        rendered = [prefix + json.dumps(value, indent=self.indent if multiline else None) for prefix, value in entries]
        if not multiline:
            edits.append((closing, closing, " " + ", ".join(rendered) + ("," if trailing else "")))
            return edits
        base = self._line_indent(node.start)
        prefix = self.source[self.source.rfind("\n", 0, starts[0]) + 1:starts[0]] if starts else ""
        indent = prefix if prefix and not prefix.strip() else base + self.indent
        block = ("," + self.newline).join(
            self.newline.join(indent + line for line in item.splitlines()) for item in rendered
        ) + ("," if trailing else "")
        line_start = self.source.rfind("\n", 0, closing) + 1
        if not self.source[line_start:closing].strip():
            edits.append((line_start, line_start, block + self.newline))
        else:
            edits.append((closing, closing, self.newline + block + self.newline + base))
        return edits

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
        source = self.source
        insertions: dict[int, list[str]] = {}
        replacements = []
        for start, end, text in edits:
            if start == end:
                insertions.setdefault(start, []).append(text)
            else:
                replacements.append((start, end, text))
        replacements.extend((index, index, "".join(parts)) for index, parts in insertions.items())
        for start, end, text in sorted(replacements, reverse=True):
            source = source[:start] + text + source[end:]
        _Document(source)
        return source


def settings_file(explicit: Path | None = None) -> Path:
    """Resolve a standard VS Code User/Remote settings file; require an explicit path for ambiguous locations/profiles."""
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
        if explicit is None:
            if any(
                folder.is_dir() and any(item.is_dir() for item in folder.iterdir())
                for folder in (path.parent / "profiles", path.parent.parent / "User" / "profiles")
            ):
                raise CredentialError("VS Code profiles found; use --settings-file for the active profile.")
        if path.name != "settings.json" or path.parent.name.casefold() == ".vscode":
            raise CredentialError("Choose a VS Code User/Remote settings.json file, not Workspace Settings.")
        resolved = path.resolve()
        if resolved.parent.name.casefold() == ".vscode":
            raise CredentialError("Choose User/Remote Settings, not Workspace Settings.")
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise CredentialError("Cannot resolve VS Code settings; use --settings-file with an accessible User/Remote path.") from None


@dataclass
class SettingsEdit:
    """A locked editor-settings snapshot; apply only generated bootstrap fields, preserving unrelated JSONC and environment entries."""

    path: Path
    original: bytes | None
    permissions: int | None
    document: _Document

    @classmethod
    def read(cls, path: Path) -> "SettingsEdit":
        try:
            original = path.read_bytes() if path.exists() else None
            if original is not None and len(original) > _MAX_BYTES:
                raise CredentialError("VS Code settings exceed 2 MiB; no settings were applied.")
            if original is not None and not os.access(path, os.W_OK):
                raise CredentialError("VS Code settings are read-only; no settings were applied.")
            permissions = stat.S_IMODE(path.stat().st_mode) if original is not None else None
            return cls(path, original, permissions, _Document(original.decode("utf-8") if original is not None else ""))
        except (OSError, UnicodeError):
            raise CredentialError("Cannot read VS Code settings as UTF-8; check permissions and repair the file.") from None

    def apply(self, settings: dict) -> None:
        """Atomically apply a merge; reject changed files and raise CredentialError on failure without dumping their contents."""
        updated = self.document.merge(settings)
        encoded = updated.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise CredentialError("Resulting VS Code settings exceed 2 MiB; no settings were applied.")
        try:
            current = self.path.read_bytes() if self.path.exists() else None
            if current != self.original:
                raise CredentialError("VS Code settings changed during setup; save the editor settings and retry.")
            if current is not None and stat.S_IMODE(self.path.stat().st_mode) != self.permissions:
                raise CredentialError("VS Code settings permissions changed during setup; retry without editing the file.")
            if current != encoded:
                atomic_write_text(self.path, updated, newline="", permissions=self.permissions)
        except OSError:
            raise CredentialError("Cannot atomically update VS Code settings; check write permissions and retry.") from None


@contextmanager
def editing(path: Path) -> Iterator[SettingsEdit]:
    """Lock one settings target across CLI processes; reject malformed input before setup changes its CLI configuration."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = FileLock(str(path) + ".llm-credentials.lock", timeout=10)
        lock.acquire()
    except Timeout:
        raise CredentialError("Another editor setup is busy; try again.") from None
    except OSError:
        raise CredentialError("Cannot access the VS Code settings directory.") from None
    try:
        yield SettingsEdit.read(path)
    finally:
        try:
            lock.release()
        except OSError:
            raise CredentialError("Cannot release the editor settings lock; settings may already have been applied.") from None
