import json
import re
from dataclasses import dataclass

from .errors import CredentialError

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
class Value:
    start: int
    end: int
    value: object


@dataclass(frozen=True)
class Member:
    name: str
    start: int
    value: Value


class JsoncDocument:
    """Validate object/array JSONC and edit source spans without rewriting unrelated text.

    Comments, trailing commas, BOM and line endings are retained. Invalid input
    raises CredentialError with a location, never the document's contents.
    """

    def __init__(
        self, source: str, *, root_type: type = dict, label: str = "VS Code settings",
        root_error: str = "VS Code User/Remote settings must contain a JSON object.",
    ) -> None:
        self.newline = "\r\n" if "\r\n" in source else "\n"
        comments = _mask_comments(source)
        if not comments.strip():
            source += (
                (self.newline if source and not source.endswith(("\r", "\n")) else "")
                + ("[]" if root_type is list else "{}") + self.newline
            )
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
                f"Invalid {label} JSONC at line {error.lineno}, column {error.colno}; repair it before applying."
            ) from None
        except (ValueError, RecursionError):
            raise CredentialError(f"Invalid {label} JSONC; repair it before applying.") from None
        if not isinstance(value, root_type):
            raise CredentialError(root_error)
        self.root = self._value(self._skip(0))
        items = self._members(self.root) if root_type is dict else self._elements(self.root)
        self.indent = next((self._line_indent(item.start) for item in items if self._line_indent(item.start)), "  ")

    def _skip(self, index: int, source: str | None = None) -> int:
        source = self.clean if source is None else source
        while index < len(source) and source[index].isspace():
            index += 1
        return index

    def _value(self, start: int) -> Value:
        value, end = self.decoder.raw_decode(self.clean, start)
        return Value(start, end, value)

    def _members(self, node: Value) -> list[Member]:
        result = []
        index = self._skip(node.start + 1)
        while self.clean[index] != "}":
            name, end = self.decoder.raw_decode(self.clean, index)
            value = self._value(self._skip(self._skip(end) + 1))
            result.append(Member(name, index, value))
            index = self._skip(value.end)
            if self.clean[index] == ",":
                index = self._skip(index + 1)
        return result

    def _elements(self, node: Value) -> list[Value]:
        result = []
        index = self._skip(node.start + 1)
        while self.clean[index] != "]":
            value = self._value(index)
            result.append(value)
            index = self._skip(value.end)
            if self.clean[index] == ",":
                index = self._skip(index + 1)
        return result

    def _line_indent(self, index: int) -> str:
        line = self.source[self.source.rfind("\n", 0, index) + 1:index]
        return line[:len(line) - len(line.lstrip(" \t"))]

    def _append(
        self, node: Value, starts: list[int], ends: list[int], entries: list[tuple[str, object]],
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

    def _remove(self, spans: list[tuple[int, int]], removed: set[int]) -> list[tuple[int, int, str]]:
        edits = []
        commas = set()
        for index in removed:
            start, end = spans[index]
            edits.append((start, end, ""))
            comma = self._skip(end, self.comments)
            if self.comments[comma] == ",":
                commas.add(comma)
        remaining = [index for index in range(len(spans)) if index not in removed]
        if remaining and len(spans) - 1 in removed:
            trailing = self.comments[self._skip(spans[-1][1], self.comments)] == ","
            if not trailing:
                commas.add(self._skip(spans[remaining[-1]][1], self.comments))
        edits.extend((comma, comma + 1, "") for comma in commas)
        return edits

    def _edited(self, edits: list[tuple[int, int, str]]) -> str:
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
        type(self)(source)
        return source
