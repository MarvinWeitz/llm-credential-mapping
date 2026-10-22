"""Labels: key/value attribution for requests (docs/labels.md).

This module is the vendor-neutral model: label definitions and their merge, resolved values, the
format engine and the outputs a gateway adapter offers. It imports nothing else of this package
but errors, so profiles, rules and repositories can all build on it.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Collection, Iterable, Mapping, Sequence

from .errors import CredentialError

KEY_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ENV_PREFIX = "VKM_LABEL_"
# The --label values of one launch, for hooks and plugins that ask vkm again (never a resolution source).
LAUNCH_VARIABLE = "VKM_LAUNCH_LABELS"
SOURCES = ("explicit", "env", "local", "file", "rules")
MISSING_PHRASE = "Required labels have no value:"
NO_LABELS_NOTICE = "No labels are defined for this profile; requests carry only the repository."

MAX_LABELS = 16
MAX_VALUES = 200
MAX_TEXT = 200
MAX_VALUE = 256
MAX_ENTRY = 512
MAX_ENTRIES = 32
MAX_HEADER_BYTES = 4096

LIST = "list"
JSON = "json"


def validate_key(key: object) -> str:
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise CredentialError(
            f"Invalid label key {json.dumps(key) if isinstance(key, str) else ''}: "
            "use 1-32 lowercase letters, digits, _ or -, starting with a letter."
        )
    return key


def _wire_text(value: object, limit: int) -> bool:
    """Printable ASCII without commas, surrounding whitespace or a leading ! or & (LiteLLM routing directives)."""
    return (
        isinstance(value, str) and 0 < len(value) <= limit and value == value.strip() and "," not in value
        and value[0] not in "!&" and all(32 <= ord(char) <= 126 for char in value)
    )


def validate_value(value: object, key: str | None = None) -> str:
    """Validate one exact label value; the rules are strict enough for any gateway's request header."""
    if not _wire_text(value, MAX_VALUE):
        label = f" for '{key}'" if key else ""
        raise CredentialError(
            f"Label value{label} must be 1-{MAX_VALUE} printable ASCII characters, without commas, "
            "surrounding whitespace or a leading ! or &."
        )
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_TEXT or not value.isprintable():
        raise CredentialError(f"{what} must be 1-{MAX_TEXT} printable characters.")
    return value


def env_name(key: str) -> str:
    """The environment variable for a key: VKM_LABEL_ plus the key in uppercase, with - as _."""
    return ENV_PREFIX + key.upper().replace("-", "_")


# Definitions


@dataclass(frozen=True)
class Choice:
    """A value offered for a label: from its definition (``name``) or a gateway catalogue (``description``)."""

    id: str
    name: str | None = None
    description: str | None = None


_DEFINITION_FIELDS = {"key", "required", "description", "values", "strict", "sources"}


@dataclass(frozen=True)
class LabelDefinition:
    """One label: its key, whether it is required, its offered values and the sources that may set it.

    ``origin`` is "rules" for the central document's labels and "profile" for personal ones.
    """

    key: str
    required: bool = False
    description: str | None = None
    values: tuple[Choice, ...] | None = None
    strict: bool = False
    sources: tuple[str, ...] = SOURCES
    origin: str = "profile"

    def __post_init__(self) -> None:
        validate_key(self.key)
        where = f"Label '{self.key}'"
        if type(self.required) is not bool or type(self.strict) is not bool:
            raise CredentialError(f"{where}: required and strict must be true or false.")
        if self.description is not None:
            _text(self.description, f"{where}: the description")
        if self.values is not None:
            if not isinstance(self.values, tuple) or not 0 < len(self.values) <= MAX_VALUES:
                raise CredentialError(f"{where}: values must list 1-{MAX_VALUES} choices.")
            ids = set()
            for choice in self.values:
                if not isinstance(choice, Choice) or choice.description is not None:
                    raise CredentialError(f"{where}: invalid value.")
                validate_value(choice.id, self.key)
                if choice.name is not None:
                    _text(choice.name, f"{where}: the name of {json.dumps(choice.id)}")
                if choice.id in ids:
                    raise CredentialError(f"{where}: the value {json.dumps(choice.id)} is listed twice.")
                ids.add(choice.id)
        if self.strict and self.values is None:
            raise CredentialError(f"{where}: strict needs values.")
        if (
            not isinstance(self.sources, tuple) or not self.sources
            or not all(isinstance(source, str) for source in self.sources) or not set(self.sources) <= set(SOURCES)
            or len(set(self.sources)) != len(self.sources)
        ):
            raise CredentialError(f"{where}: sources must name some of: {', '.join(SOURCES)}, each once.")
        object.__setattr__(self, "sources", tuple(source for source in SOURCES if source in self.sources))
        if self.origin not in ("rules", "profile"):
            raise CredentialError(f"{where}: invalid origin.")

    @classmethod
    def parse(cls, data: object, origin: str, *, known_fields: bool = True) -> "LabelDefinition":
        """Read the JSON form; ``known_fields`` refuses unknown fields (a newer minor version ignores them)."""
        if not isinstance(data, dict) or "key" not in data:
            raise CredentialError("A label definition must be an object with a key.")
        key = validate_key(data["key"])
        if known_fields and not set(data) <= _DEFINITION_FIELDS:
            unknown = ", ".join(sorted(map(str, set(data) - _DEFINITION_FIELDS)))
            raise CredentialError(f"Label '{key}': unknown field(s) {unknown}.")
        values = data.get("values")
        if values is not None:
            if not isinstance(values, list):
                raise CredentialError(f"Label '{key}': values must be a list.")
            choices = []
            for item in values:
                if (
                    not isinstance(item, dict) or "id" not in item
                    or (known_fields and not set(item) <= {"id", "name"})
                ):
                    raise CredentialError(f"Label '{key}': each value must be {{\"id\", \"name\"?}}.")
                choices.append(Choice(item["id"], item.get("name")))
            values = tuple(choices)
        sources = data.get("sources", list(SOURCES))
        if not isinstance(sources, list):
            raise CredentialError(f"Label '{key}': sources must be a list.")
        return cls(
            key, data.get("required", False), data.get("description"), values, data.get("strict", False),
            tuple(sources), origin,
        )

    def to_dict(self) -> dict:
        """The JSON form, without defaults and without ``origin``."""
        value: dict = {"key": self.key}
        if self.required:
            value["required"] = True
        if self.description is not None:
            value["description"] = self.description
        if self.values is not None:
            value["values"] = [
                {"id": choice.id, **({"name": choice.name} if choice.name is not None else {})} for choice in self.values
            ]
        if self.strict:
            value["strict"] = True
        if self.sources != SOURCES:
            value["sources"] = list(self.sources)
        return value

    def choice(self, value: str) -> Choice | None:
        return next((choice for choice in self.values or () if choice.id == value), None)

    def accepts(self, value: str) -> bool:
        """Whether a valid value is allowed: any value, or with ``strict`` only the listed ids."""
        return not self.strict or self.choice(value) is not None


def check_definitions(definitions: Sequence[LabelDefinition], where: str) -> tuple[LabelDefinition, ...]:
    """Refuse duplicate keys, keys that share a VKM_LABEL_ variable, and more than MAX_LABELS labels."""
    seen: dict[str, str] = {}
    for definition in definitions:
        name = env_name(definition.key)
        if name in seen:
            raise CredentialError(
                f"{where}: labels '{seen[name]}' and '{definition.key}' conflict (both use {name})."
            )
        seen[name] = definition.key
    if len(definitions) > MAX_LABELS:
        raise CredentialError(f"{where}: at most {MAX_LABELS} labels can be defined.")
    return tuple(definitions)


def parse_definitions(data: object, origin: str, where: str, *, known_fields: bool = True) -> tuple[LabelDefinition, ...]:
    """Read a ``labels`` list in its written order."""
    if not isinstance(data, list):
        raise CredentialError(f"{where}: labels must be a list.")
    try:
        definitions = [LabelDefinition.parse(item, origin, known_fields=known_fields) for item in data]
    except CredentialError as error:
        raise CredentialError(f"{where}: {error}") from None
    return check_definitions(definitions, where)


# Outputs and formats


@dataclass(frozen=True)
class Placeholder:
    key: str


@dataclass(frozen=True)
class Format:
    """One compiled format entry: literal text and placeholders, substituted in one left-to-right pass."""

    text: str
    parts: tuple[str | Placeholder, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(part.key for part in self.parts if isinstance(part, Placeholder)))

    @property
    def lead(self) -> str | None:
        """The key of the placeholder this entry starts with, if it starts with one."""
        return self.parts[0].key if isinstance(self.parts[0], Placeholder) else None

    def render(self, values: Mapping[str, str]) -> str | None:
        """The entry with values substituted, or None when a label it uses has no value (the entry is dropped)."""
        if any(key not in values for key in self.keys):
            return None
        return "".join(values[part.key] if isinstance(part, Placeholder) else part for part in self.parts)

    def inversion(self, key: str) -> tuple[str, str] | None:
        """(PREFIX, SUFFIX) when this entry is PREFIX${key}SUFFIX with one placeholder, for gateway catalogues."""
        holders = [index for index, part in enumerate(self.parts) if isinstance(part, Placeholder)]
        if len(holders) != 1 or self.parts[holders[0]] != Placeholder(key):
            return None
        index = holders[0]
        return "".join(self.parts[:index]), "".join(self.parts[index + 1:])


def compile_format(text: object, keys: Collection[str] | None = None) -> Format:
    """Compile ``entry := (literal | "$$" | "${" KEY "}")*``; ``keys`` None accepts any valid key (offline checks)."""
    if not isinstance(text, str) or not 0 < len(text) <= MAX_ENTRY:
        raise CredentialError(f"A format must be a string of 1-{MAX_ENTRY} characters.")
    where = f"Format {json.dumps(text)}"
    parts: list[str | Placeholder] = []
    literal, index = [], 0
    while index < len(text):
        char = text[index]
        if char != "$":
            literal.append(char)
            index += 1
        elif text.startswith("$$", index):
            literal.append("$")
            index += 2
        elif text.startswith("${", index):
            end = text.find("}", index + 2)
            key = text[index + 2:end] if end >= 0 else None
            if key is None or not KEY_PATTERN.fullmatch(key):
                raise CredentialError(f"{where}: write placeholders as ${{key}} with a lowercase label key, and $ as $$.")
            if keys is not None and key not in keys:
                raise CredentialError(f"{where}: no label '{key}' is defined.")
            if literal:
                parts.append("".join(literal))
                literal = []
            elif parts:
                raise CredentialError(f"{where}: placeholders must be separated by literal text.")
            parts.append(Placeholder(key))
            index = end + 1
        else:
            raise CredentialError(f"{where}: write a literal $ as $$.")
    if literal:
        parts.append("".join(literal))
    for part in parts:
        if isinstance(part, str) and (
            "," in part or any(not 32 <= ord(char) <= 126 for char in part)
        ):
            raise CredentialError(f"{where}: literal text must be printable ASCII without commas.")
    first, last = parts[0], parts[-1]
    if (isinstance(first, str) and first[0] in "!& ") or (isinstance(last, str) and last[-1] == " "):
        raise CredentialError(f"{where}: an entry may not start with !, & or a space, or end with a space.")
    return Format(text, tuple(parts))


def compile_formats(texts: Sequence[str], keys: Collection[str] | None = None) -> tuple[Format, ...]:
    """Compile a list output's entries and refuse look-alikes, where one label's value could read as another's.

    If an entry starts with a placeholder, all must start with the same one. Of entries that start
    with literal text, two whose text before the first placeholder is equal, or one a prefix of the
    other, must have the same first placeholder.
    """
    formats = tuple(compile_format(text, keys) for text in texts)

    def refuse(entries: Iterable[Format], problem: str) -> CredentialError:
        return CredentialError(
            "Formats " + ", ".join(json.dumps(entry.text) for entry in entries) + f" {problem}; "
            "one label's value could then read as another's."
        )

    leads = {entry.lead for entry in formats}
    if len(leads) > 1 and leads != {None}:
        raise refuse(formats, "do not all start with the same placeholder")
    for index, entry in enumerate(formats):
        if entry.lead is not None:
            continue
        for other in formats[index + 1:]:
            first, second = entry.parts[0], other.parts[0]
            if (first.startswith(second) or second.startswith(first)) and _first_key(entry) != _first_key(other):
                raise refuse((entry, other), "start with the same text but different placeholders")
    return formats


def _first_key(entry: Format) -> str | None:
    return entry.keys[0] if entry.keys else None


@dataclass(frozen=True)
class Output:
    """Something a gateway adapter can fill from labels; the adapter fixes the header name.

    ``kind`` is "list" (formatted entries) or "json" (the metadata document, on or off). ``default``
    gives a list output's entries for the effective label keys. ``reserved`` names results the
    gateway gives another meaning to, which must never be sent.
    """

    name: str
    kind: str
    header: str
    default: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None
    reserved: Callable[[str], bool] = field(default=lambda result: False, compare=False)


@dataclass(frozen=True)
class OutputOverride:
    """``{"format": str | [str, ...]}`` or ``{"enabled": false}``."""

    format: tuple[str, ...] | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or (self.format is None) == self.enabled:
            raise CredentialError('An output override is either {"format": ...} or {"enabled": false}.')
        if self.format is not None:
            if not isinstance(self.format, tuple) or not all(isinstance(text, str) for text in self.format):
                raise CredentialError("An output format is a string or a list of strings.")
            if not 0 < len(self.format) <= MAX_ENTRIES or len(set(self.format)) != len(self.format):
                raise CredentialError(f"An output format lists 1-{MAX_ENTRIES} different entries.")
            for text in self.format:
                compile_format(text)

    @classmethod
    def parse(cls, data: object, where: str, *, known_fields: bool = True) -> "OutputOverride":
        if not isinstance(data, dict):
            raise CredentialError(f"{where}: an output override must be an object.")
        if known_fields and not set(data) <= {"format", "enabled"}:
            raise CredentialError(f"{where}: an output override has only format or enabled.")
        if "format" in data and "enabled" in data:
            raise CredentialError(f"{where}: an output override has format or enabled, not both.")
        try:
            if "format" in data:
                value = data["format"]
                return cls(tuple(value) if isinstance(value, list) else (value,))
            if data.get("enabled", True) is not False:
                raise CredentialError('An output override is either {"format": ...} or {"enabled": false}.')
            return cls(None, False)
        except CredentialError as error:
            raise CredentialError(f"{where}: {error}") from None

    def to_dict(self) -> dict:
        return {"format": list(self.format)} if self.format is not None else {"enabled": False}


def parse_overrides(data: object, where: str, *, known_fields: bool = True) -> dict[str, OutputOverride]:
    if not isinstance(data, dict) or not all(isinstance(name, str) and KEY_PATTERN.fullmatch(name) for name in data):
        raise CredentialError(f"{where}: outputs must map output names to overrides.")
    return {
        name: OutputOverride.parse(value, f"{where}, output '{name}'", known_fields=known_fields)
        for name, value in data.items()
    }


def check_overrides(outputs: Sequence[Output], overrides: Mapping[str, OutputOverride], where: str) -> None:
    """Refuse overrides of outputs the gateway does not have, formats for json outputs and look-alike entries."""
    known = {output.name: output for output in outputs}
    problems = []
    for name, override in overrides.items():
        if name not in known:
            problems.append(f"{where}: this gateway has no output '{name}'; it has: {', '.join(known) or 'none'}.")
        elif override.format is not None and known[name].kind != LIST:
            problems.append(f"{where}: output '{name}' takes no format; it can only be switched off.")
        elif override.format is not None:
            try:
                compile_formats(override.format)
            except CredentialError as error:
                problems.append(f"{where}, output '{name}': {error}")
    if problems:
        raise CredentialError(" ".join(problems))


def definitions_from_dict(data: object, where: str = "Definitions") -> tuple[tuple[LabelDefinition, ...], dict[str, OutputOverride]]:
    """Read ``{"labels": [...], "outputs": {...}}`` (``profile add --definitions`` and ``profile show --json``)."""
    if not isinstance(data, dict) or not set(data) <= {"labels", "outputs"}:
        raise CredentialError(f'{where} must be {{"labels": [...], "outputs": {{...}}}}.')
    return (
        parse_definitions(data.get("labels", []), "profile", where),
        parse_overrides(data.get("outputs", {}), where),
    )


# Values


@dataclass(frozen=True)
class Label:
    """One defined label for a directory: its value (None: none) and where it came from.

    ``source`` and ``detail`` are set for a value and for an explicit "no value" (a stop); both are
    None when no allowed source had an entry.
    """

    key: str
    value: str | None
    source: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        validate_key(self.key)
        if self.value is not None:
            validate_value(self.value, self.key)
        if self.source is not None and self.source not in SOURCES:
            raise CredentialError("Invalid label source.")


@dataclass(frozen=True)
class Ignored:
    """A value that is not used: ``reason`` is "unknown label" or "source not allowed"."""

    key: str
    value: str | None
    source: str
    detail: str
    reason: str

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "source": self.source, "detail": self.detail, "reason": self.reason}


@dataclass(frozen=True)
class Attribution:
    """The labels chosen for one directory; ``repository`` is the origin's normalized identity, if any.

    ``labels`` holds every defined label in definition order, ``missing`` the required keys without
    a value. Gateway outputs turn an attribution into request headers (``LabelConfig.headers``).
    """

    labels: tuple[Label, ...] = ()
    missing: tuple[str, ...] = ()
    ignored: tuple[Ignored, ...] = ()
    repository: str | None = None

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def values(self) -> dict[str, str]:
        return {label.key: label.value for label in self.labels if label.value is not None}

    @property
    def sources(self) -> dict[str, str]:
        return {label.key: label.source for label in self.labels if label.value is not None}

    def get(self, key: str) -> Label | None:
        return next((label for label in self.labels if label.key == key), None)


class MissingLabelsError(CredentialError):
    """Required labels without a value; the message starts with MISSING_PHRASE, which tools match on."""

    def __init__(self, keys: Sequence[str], where: str | None = None) -> None:
        self.keys = tuple(keys)
        place = f" for {where}" if where else ""
        super().__init__(
            f"{MISSING_PHRASE} {', '.join(self.keys)}{place}. Set them with 'vkm repo bind --label KEY=VALUE' "
            "(only for you), 'vkm repo bind --shared --label KEY=VALUE' (a .vkm file to commit for the team) "
            "or a central rule; use 'vkm run --label KEY=VALUE' for one launch. Nothing was sent."
        )


def launch_marker(explicit: Mapping[str, str | None]) -> str:
    """VKM_LAUNCH_LABELS for a launch: compact JSON of the --label values ("" as null)."""
    return json.dumps({key: value or None for key, value in explicit.items()}, separators=(",", ":"), sort_keys=True)


def parse_launch_marker(text: str | None) -> dict[str, str | None]:
    """The --label values a launcher recorded; a malformed marker counts as absent."""
    if not text or len(text) > 65536:
        return {}
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        for key, value in data.items():
            validate_key(key)
            if value is not None:
                validate_value(value, key)
    except (ValueError, RecursionError, CredentialError):
        return {}
    return data


# The effective configuration


@dataclass(frozen=True)
class EffectiveOutput:
    """An adapter output as configured: enabled or off, its compiled entries, and who set it."""

    output: Output
    enabled: bool
    formats: tuple[Format, ...]
    origin: str  # "default", "rules" or "profile"


def metadata(attribution: Attribution) -> dict:
    """The json output's document: values and sources of the labels that have one, and the repository."""
    value: dict = {"labels": attribution.values, "sources": attribution.sources}
    if attribution.repository is not None:
        value["repository"] = attribution.repository
    return value


def _merge(
    central: Sequence[LabelDefinition], profile: Sequence[LabelDefinition],
) -> tuple[tuple[LabelDefinition, ...], list[str]]:
    """Central labels in document order, then profile labels; a profile label that collides is dropped."""
    names = {env_name(definition.key): definition.key for definition in central}
    merged, notices = list(central), []
    for definition in profile:
        if env_name(definition.key) in names:
            notices.append(
                f"The profile label '{definition.key}' is ignored: the central rules define "
                f"'{names[env_name(definition.key)]}'."
            )
            continue
        merged.append(definition)
    return check_definitions(merged, "Labels of the central rules and the profile"), notices


def _format_keys(override: OutputOverride) -> tuple[str, ...]:
    """The keys an override's entries use; entries that do not compile are reported by ``check_overrides``."""
    keys = []
    for text in override.format or ():
        try:
            keys.extend(compile_format(text).keys)
        except CredentialError:
            pass
    return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class LabelConfig:
    """The labels and outputs in effect: central document, profile and adapter defaults combined.

    ``rules`` is the central rules document (``rules.Rules``) when there is one; the resolver uses
    its rules. ``notices`` lists dropped profile pieces and document remarks for the user.
    """

    definitions: tuple[LabelDefinition, ...]
    outputs: tuple[EffectiveOutput, ...]
    notices: tuple[str, ...] = ()
    rules: object | None = None

    @classmethod
    def build(
        cls, outputs: Sequence[Output], *, central_labels: Sequence[LabelDefinition] = (),
        central_outputs: Mapping[str, OutputOverride] | None = None,
        profile_labels: Sequence[LabelDefinition] = (), profile_outputs: Mapping[str, OutputOverride] | None = None,
        rules: object | None = None, notices: Sequence[str] = (), unread_central: bool = False,
    ) -> "LabelConfig":
        """Combine and check everything; any problem that could misattribute a request raises one error.

        ``unread_central`` checks a profile whose central document cannot be read: a placeholder for a
        key nothing here defines may be one of its labels, so it is accepted and counts as required.
        """
        central_outputs, profile_outputs = central_outputs or {}, profile_outputs or {}
        definitions, merged_notices = _merge(central_labels, profile_labels)
        notices = [*notices, *merged_notices]
        keys = tuple(definition.key for definition in definitions)
        profile_outputs = dict(profile_outputs)
        for name, override in list(profile_outputs.items()):
            # A profile format for a profile label dropped above would block every request: drop it too.
            lost = [
                key for key in _format_keys(override) if key not in keys and any(item.key == key for item in profile_labels)
            ]
            if lost and name not in central_outputs:
                del profile_outputs[name]
                notices.append(f"The profile's '{name}' output is ignored: its profile label '{lost[0]}' is ignored.")
        central_keys = {definition.key for definition in central_labels}
        problems = []
        for where, overrides in (("The central rules", central_outputs), ("The profile", profile_outputs)):
            try:
                check_overrides(outputs, overrides, where)
            except CredentialError as error:
                problems.append(str(error))
        effective = []
        for output in outputs:
            override, origin, allowed = None, "default", None if unread_central else keys
            if output.name in central_outputs:
                override, origin, allowed = central_outputs[output.name], "rules", central_keys
                if output.name in profile_outputs:
                    notices.append(f"The profile's '{output.name}' output is ignored: the central rules set it.")
            elif output.name in profile_outputs:
                override, origin = profile_outputs[output.name], "profile"
            enabled = override.enabled if override is not None else True
            texts: Sequence[str] = ()
            if output.kind == LIST and enabled:
                texts = override.format if override is not None else output.default(keys) if output.default else ()
            try:
                formats = compile_formats(texts, allowed)
            except CredentialError as error:
                problems.append(f"Output '{output.name}': {error}")
                formats = ()
            effective.append(EffectiveOutput(output, enabled, formats, origin))
        lists = [item for item in effective if item.enabled and item.output.kind == LIST]
        sends_json = any(item.enabled and item.output.kind == JSON for item in effective)
        required = {definition.key for definition in definitions if definition.required}
        for key in (definition.key for definition in definitions if definition.required):
            if lists:
                reached = any(
                    key in entry.keys and all(other in required or (unread_central and other not in keys) for other in entry.keys)
                    for item in lists for entry in item.formats
                )
            else:
                reached = sends_json
            if not reached:
                problems.append(
                    f"The required label '{key}' reaches no output: use it in an enabled list format "
                    "whose other placeholders are required labels."
                )
        for item in lists:
            for entry in item.formats:
                if not entry.keys and item.output.reserved(entry.render({})):
                    problems.append(f"Output '{item.output.name}': {json.dumps(entry.text)} is reserved by the gateway.")
        if problems:
            raise CredentialError("Invalid label configuration: " + " ".join(problems))
        return cls(definitions, tuple(effective), tuple(notices), rules)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(definition.key for definition in self.definitions)

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(definition.key for definition in self.definitions if definition.required)

    def get(self, key: str) -> LabelDefinition | None:
        return next((definition for definition in self.definitions if definition.key == key), None)

    def output(self, name: str) -> EffectiveOutput | None:
        return next((item for item in self.outputs if item.output.name == name), None)

    def header_names(self) -> tuple[str, ...]:
        """Every header the adapter can send, enabled or not, for tools configured ahead of time."""
        return tuple(item.output.header for item in self.outputs)

    def inversions(self, output: str, key: str) -> tuple[tuple[str, str], ...]:
        """(PREFIX, SUFFIX) of each enabled ``output`` entry that is PREFIX${key}SUFFIX, for catalogues."""
        item = self.output(output)
        if item is None or not item.enabled:
            return ()
        return tuple(dict.fromkeys(pair for entry in item.formats if (pair := entry.inversion(key)) is not None))

    def headers(self, attribution: Attribution) -> dict[str, str]:
        """The request headers for an attribution; MissingLabelsError while required labels have no value.

        A list output whose entries are all dropped is omitted; the json output is sent even without values.
        """
        if attribution.missing:
            raise MissingLabelsError(attribution.missing)
        values = attribution.values
        headers = {}
        for item in self.outputs:
            if not item.enabled:
                continue
            if item.output.kind == JSON:
                value = json.dumps(metadata(attribution), separators=(",", ":"), sort_keys=True, ensure_ascii=True)
            else:
                results = []
                for entry in item.formats:
                    result = entry.render(values)
                    if result is None:
                        continue
                    if not _wire_text(result, MAX_ENTRY) or item.output.reserved(result):
                        raise CredentialError(
                            f"Output '{item.output.name}': {json.dumps(entry.text)} gives {json.dumps(result[:80])}, "
                            f"which is not 1-{MAX_ENTRY} printable ASCII characters without commas or is reserved "
                            "by the gateway. Nothing was sent."
                        )
                    results.append(result)
                if not results:
                    continue
                value = ",".join(dict.fromkeys(results))
            if len(value.encode()) > MAX_HEADER_BYTES:
                raise CredentialError(
                    f"The {item.output.name} output exceeds {MAX_HEADER_BYTES} bytes; use shorter label values or formats. "
                    "Nothing was sent."
                )
            headers[item.output.header] = value
        return headers

    def describe(self, attribution: Attribution) -> list[dict]:
        """``repo show --json`` labels: each defined label with its value, catalogue name, source and definition."""
        result = []
        for definition in self.definitions:
            label = attribution.get(definition.key) or Label(definition.key, None)
            choice = definition.choice(label.value) if label.value is not None else None
            result.append({
                "key": definition.key, "value": label.value, "name": choice.name if choice else None,
                "source": label.source, "detail": label.detail, "required": definition.required,
                "description": definition.description,
            })
        return result


def header_names(outputs: Iterable[Output]) -> tuple[str, ...]:
    return tuple(output.header for output in outputs)
