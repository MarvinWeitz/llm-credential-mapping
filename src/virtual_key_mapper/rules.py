"""The central rules document (version "2.0"): label definitions, output overrides and repository rules."""

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .errors import CredentialError
from .labels import (
    KEY_PATTERN, LabelDefinition, OutputOverride, compile_formats, parse_definitions, parse_overrides, validate_key,
    validate_value,
)
from .profiles import Profile, ProfileStore, atomic_write_text, gateway_origin
from .transport import REQUEST_TIMEOUT_SECONDS, NoRedirects

RULES_CACHE_SECONDS = 3600
RULES_MAJOR, RULES_MINOR = 2, 0
_MAX_RULES = 1000
_MAX_GLOBSTARS = 8
_MAX_RULES_BYTES = 1_048_576
_FIELDS = {"version", "labels", "outputs", "rules"}


@dataclass(frozen=True)
class Glob:
    """A repository glob: ** spans path segments, * and ? stay within one segment.

    Matched by stepping the set of reachable pattern positions over the identity, so the time is at
    most pattern length times identity length; no backtracking.
    """

    tokens: tuple[str, ...]

    @classmethod
    def compile(cls, pattern: str) -> "Glob":
        tokens, index = [], 0
        while index < len(pattern):
            size = 2 if pattern.startswith("**", index) else 1
            tokens.append(pattern[index:index + size])
            index += size
        return cls(tuple(tokens))

    def _closure(self, states: set[int]) -> set[int]:
        """Add the positions after stars, which may match nothing."""
        pending = list(states)
        while pending:
            state = pending.pop()
            if state < len(self.tokens) and self.tokens[state] in ("*", "**") and state + 1 not in states:
                states.add(state + 1)
                pending.append(state + 1)
        return states

    def fullmatch(self, text: str) -> bool:
        states = self._closure({0})
        for char in text:
            following = set()
            for state in states:
                if state == len(self.tokens):
                    continue
                token = self.tokens[state]
                if token == "**" or (token == "*" and char != "/"):
                    following.add(state)
                elif token == char or (token == "?" and char != "/"):
                    following.add(state + 1)
            if not following:
                return False
            states = self._closure(following)
        return len(self.tokens) in states


@dataclass(frozen=True)
class Rule:
    """One rule: a lowercase repository glob and the values it sets (None: no value, stop searching)."""

    match: str
    labels: tuple[tuple[str, str | None], ...]
    pattern: Glob


@dataclass(frozen=True)
class Rules:
    """A parsed central document; ``outputs`` is grouped by gateway type, ``notices`` are remarks for the user."""

    labels: tuple[LabelDefinition, ...]
    outputs: dict[str, dict[str, OutputOverride]]
    rules: tuple[Rule, ...]
    source: str
    version: str = f"{RULES_MAJOR}.{RULES_MINOR}"
    notices: tuple[str, ...] = ()

    @classmethod
    def parse(cls, data: object, source: str) -> "Rules":
        where = f"The label rules document {source}"
        version = data.get("version") if isinstance(data, dict) else None
        parsed = re.fullmatch(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})", version) if isinstance(version, str) else None
        if parsed is None or int(parsed[1]) != RULES_MAJOR:
            raise CredentialError(f"{where} has an unsupported version; this vkm reads version \"{RULES_MAJOR}.x\".")
        known = int(parsed[2]) <= RULES_MINOR
        notices = () if known else (
            f"The label rules document is version {version}; fields this vkm does not know were ignored.",
        )
        if known and not set(data) <= _FIELDS:
            raise CredentialError(f"{where} has unknown field(s): {', '.join(sorted(map(str, set(data) - _FIELDS)))}.")
        labels = parse_definitions(data.get("labels", []), "rules", where, known_fields=known)
        definitions = {definition.key: definition for definition in labels}
        outputs = data.get("outputs", {})
        if not isinstance(outputs, dict) or not all(isinstance(kind, str) and KEY_PATTERN.fullmatch(kind) for kind in outputs):
            raise CredentialError(f"{where}: outputs must be grouped by gateway type.")
        overrides = {}
        for kind, section in outputs.items():
            overrides[kind] = parse_overrides(section, f"{where}, {kind} outputs", known_fields=known)
            for name, override in overrides[kind].items():
                if override.format is not None:
                    try:
                        compile_formats(override.format, definitions)  # Central formats use central labels only.
                    except CredentialError as error:
                        raise CredentialError(f"{where}, {kind} output '{name}': {error}") from None
        entries = data.get("rules", [])
        if not isinstance(entries, list) or len(entries) > _MAX_RULES:
            raise CredentialError(f"{where}: rules must be a list of at most {_MAX_RULES}.")
        return cls(labels, overrides, tuple(cls._rule(rule, definitions, where, known) for rule in entries), source,
                   version, notices)

    @staticmethod
    def _rule(rule: object, definitions: dict[str, LabelDefinition], where: str, known: bool) -> Rule:
        if (
            not isinstance(rule, dict) or not {"match", "labels"} <= set(rule) or (known and set(rule) != {"match", "labels"})
            or not isinstance(rule["match"], str) or not 0 < len(rule["match"]) <= 512
            # Spaces are allowed inside: identities are percent-decoded (Azure "My Project").
            or rule["match"] != rule["match"].strip()
            or any((char.isspace() and char != " ") or ord(char) < 32 for char in rule["match"])
            or rule["match"].count("**") > _MAX_GLOBSTARS
            or not isinstance(rule["labels"], dict) or not rule["labels"]
        ):
            raise CredentialError(
                f"{where}: each rule is {{\"match\": GLOB, \"labels\": {{...}}}} with a non-empty labels object "
                f"and at most {_MAX_GLOBSTARS} ** per pattern."
            )
        values = []
        for key, value in rule["labels"].items():
            validate_key(key)
            definition = definitions.get(key)
            if definition is None:
                raise CredentialError(f"{where}: the rule {json.dumps(rule['match'])} sets '{key}', which it does not define.")
            # A label whose sources exclude rules is still valid here; resolution ignores the value ("source not allowed").
            if value is not None:
                validate_value(value, key)
                if not definition.accepts(value):
                    raise CredentialError(f"{where}: {json.dumps(value)} is not one of the values of '{key}'.")
            values.append((key, value))
        match = rule["match"].lower()
        return Rule(match, tuple(values), Glob.compile(match))

    def match(self, identities: tuple[str, ...]) -> dict[str, tuple[str | None, str, str]]:
        """Return key -> (value, pattern, identity): each key from the first matching rule that sets it.

        Rules are tried in order, each against origin then upstream, so a fork gets the rule for its
        upstream even when a later catch-all would match its own origin, and a catch-all below can
        still supply keys a specific rule leaves out.
        """
        found: dict[str, tuple[str | None, str, str]] = {}
        for rule in self.rules:
            identity = next((identity for identity in identities if rule.pattern.fullmatch(identity)), None)
            if identity is None:
                continue
            for key, value in rule.labels:
                found.setdefault(key, (value, rule.match, identity))
        return found


class RulesLoader:
    """Load a profile's rules: local files directly, URLs through an ETag cache that survives outages.

    ``token`` is only called for a URL on the gateway's own origin, so other hosts never see the credential.
    ``validate`` checks a parsed document against the profile and gateway (``LabelConfig`` building); a
    published document that fails it is treated like an invalid one, so the last good cached copy is used.
    """

    def __init__(
        self, store: ProfileStore, profile: Profile, token: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time, validate: Callable[[Rules], object] | None = None,
    ) -> None:
        self.store = store
        self.profile = profile
        self.token = token
        self.clock = clock
        self.validate = validate

    def __call__(self) -> Rules | None:
        location = self.profile.rules
        if location is None:
            return None
        if "://" not in location:
            return self._parse(self._read_file(Path(location)), location)
        return self._cached(location)

    def _parse(self, document: object, source: str) -> Rules:
        rules = Rules.parse(document, source)
        if self.validate is not None:
            self.validate(rules)
        return rules

    @staticmethod
    def _read_file(path: Path) -> object:
        try:
            raw = path.read_bytes()
            if len(raw) > _MAX_RULES_BYTES:
                raise ValueError
            return json.loads(raw)
        except (OSError, UnicodeError, ValueError, RecursionError):  # RecursionError: nesting too deep.
            raise CredentialError(f"Cannot read label rules from {path}.") from None

    def _cache_path(self, url: str) -> Path:
        return self.store.home / "rules-cache" / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_cache(self, url: str) -> tuple[dict, Rules] | None:
        try:
            cached = json.loads(self._cache_path(url).read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        if (
            not isinstance(cached, dict) or set(cached) != {"url", "fetched_at", "etag", "document"}
            or cached["url"] != url or type(cached["fetched_at"]) not in (int, float)
            or not math.isfinite(cached["fetched_at"])
            or (cached["etag"] is not None and not isinstance(cached["etag"], str))
        ):
            return None
        try:
            return cached, self._parse(cached["document"], url)
        except CredentialError:
            return None

    @staticmethod
    def _stale(rules: Rules, error: CredentialError) -> Rules:
        return replace(rules, notices=(*rules.notices, f"Using the last good copy of the label rules: {error}"))

    def _cached(self, url: str) -> Rules:
        found = self._read_cache(url)
        cached, rules = found if found is not None else (None, None)
        if cached is not None and 0 <= self.clock() - cached["fetched_at"] < RULES_CACHE_SECONDS:
            return rules
        try:
            fetched = self._fetch(url, cached["etag"] if cached else None)
        except CredentialError as error:
            if rules is not None:
                return self._stale(rules, error)  # Stale rules beat no rules during an outage.
            raise
        document, etag = fetched if fetched is not None else (cached["document"], cached["etag"])
        try:
            parsed = self._parse(document, url)
        except CredentialError as error:
            if rules is not None:
                return self._stale(rules, error)  # A broken publication must not block those with valid rules.
            raise
        path = self._cache_path(url)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.store.locked("rules-cache"):
                atomic_write_text(path, json.dumps({"url": url, "fetched_at": self.clock(), "etag": etag, "document": document}))
        except (OSError, CredentialError):
            pass  # The rules are still usable for this call; the next call fetches again.
        return parsed

    def _fetch(self, url: str, etag: str | None) -> tuple[object, str | None] | None:
        """Return (document, etag), or None when the server confirms the cached ETag (HTTP 304)."""
        headers = {"Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        parsed = urlsplit(url)
        if self.token is not None and gateway_origin(f"{parsed.scheme}://{parsed.netloc}", True) == self.profile.gateway:
            headers["Authorization"] = "Bearer " + self.token()
        try:
            with build_opener(NoRedirects()).open(Request(url, headers=headers), timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(_MAX_RULES_BYTES + 1)
                new_etag = response.headers.get("ETag")
        except HTTPError as error:
            code = error.code
            error.close()
            if code == 304 and etag:
                return None
            raise CredentialError(f"Label rules request returned HTTP {code}.") from None
        except (URLError, OSError, ValueError):
            raise CredentialError("Cannot fetch the label rules; check connectivity.") from None
        if len(raw) > _MAX_RULES_BYTES:
            raise CredentialError("Label rules exceed the size limit.")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise CredentialError("Label rules are not valid JSON.") from None
        return document, new_etag if isinstance(new_etag, str) and len(new_etag) <= 512 else None
