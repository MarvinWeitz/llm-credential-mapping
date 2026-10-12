import hashlib
import json
import math
import os
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .errors import CredentialError
from .profiles import Profile, ProfileStore, atomic_write_text, gateway_origin
from .transport import REQUEST_TIMEOUT_SECONDS, NoRedirects

CUSTOMER_FILE = ".llm-customer"
OVERRIDE_VARIABLE = "LLM_CREDENTIALS_CUSTOMER"
RULES_CACHE_SECONDS = 3600
_MAX_FILE_BYTES = 4096
_MAX_RULES_BYTES = 1_048_576


def validate_customer(value: str) -> str:
    """Validate one exact customer name; reject empty, padded, non-ASCII or comma-containing values.

    The rules are strict enough for any gateway's request header, so no gateway needs its own. A leading
    ``!`` or ``&`` is refused because LiteLLM's tag routing reads it as an exclude/require directive.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
        or "," in value
        or value[0] in "!&"
        or any(not 32 <= ord(char) <= 126 for char in value)
    ):
        raise CredentialError(
            "Customer must be 1-256 printable ASCII characters, without commas, surrounding whitespace or a leading ! or &. "
            "Use the exact name the gateway expects; no prefix is added."
        )
    return value


@dataclass(frozen=True)
class Tag:
    """A customer offered by a catalogue, not an assertion that its caller may charge the customer."""

    name: str
    description: str | None = None


@dataclass(frozen=True)
class Attribution:
    """The customer chosen for a request and why; ``repository`` is the normalized Git identity, if any.

    ``source`` is one of explicit, local, file or rules. Gateway adapters turn an attribution into
    request headers; nothing else knows their names.
    """

    customer: str
    source: str
    detail: str
    repository: str | None = None

    def __post_init__(self) -> None:
        validate_customer(self.customer)

    def to_dict(self) -> dict:
        return {"customer": self.customer, "source": self.source, "detail": self.detail, "repository": self.repository}


# Committed per-repository or per-folder file


def find_customer_file(directory: Path, home: Path | None = None) -> Path | None:
    """Return the nearest .llm-customer in ``directory`` or a parent.

    Inside the home directory the search stops below it: the home directory itself and folders
    above it (often shared, like C:\\Users or /home) never assign a customer.
    """
    home = (home or Path.home()).resolve()
    for folder in (directory, *directory.parents):
        if folder == home:
            return None
        candidate = folder / CUSTOMER_FILE
        if candidate.is_file():
            return candidate
    return None


def read_customer_file(path: Path) -> str:
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise CredentialError(f"Cannot read {path}; it must be a small TOML file such as: customer = \"customer-acme\".") from None
    if set(data) != {"customer"} or not isinstance(data["customer"], str):
        raise CredentialError(f"{path} must contain exactly: customer = \"NAME\".")
    try:
        return validate_customer(data["customer"])
    except CredentialError as error:
        raise CredentialError(f"{path}: {error}") from None


def write_customer_file(directory: Path, customer: str) -> Path:
    path = directory / CUSTOMER_FILE
    content = (
        "# Customer for AI agent usage in this folder and below (llm-credentials).\n"
        f"customer = {json.dumps(validate_customer(customer))}\n"
    )
    try:
        atomic_write_text(path, content)
    except OSError:
        raise CredentialError(f"Cannot write {path}.") from None
    return path


# Central rules


def _glob(pattern: str) -> re.Pattern[str]:
    """Translate a repository glob: ** spans path segments, * and ? stay within one segment."""
    parts, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts))


@dataclass(frozen=True)
class Rules:
    """A central, ordered repository-to-customer rule list and an optional customer catalogue."""

    rules: tuple[tuple[str, str], ...]
    customers: tuple[Tag, ...]
    source: str

    @classmethod
    def parse(cls, data: object, source: str) -> "Rules":
        if (
            not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not set(data) <= {"version", "rules", "customers"}
            or not isinstance(data.get("rules", []), list) or not isinstance(data.get("customers", []), list)
        ):
            raise CredentialError(f"Unsupported or invalid customer rules document: {source}.")
        rules = []
        for rule in data.get("rules", []):
            if (
                not isinstance(rule, dict) or set(rule) != {"match", "customer"}
                or not isinstance(rule["match"], str) or not 0 < len(rule["match"]) <= 512
                or any(char.isspace() or ord(char) < 32 for char in rule["match"])
            ):
                raise CredentialError(f"Invalid rule in the customer rules document: {source}.")
            rules.append((rule["match"].lower(), validate_customer(rule["customer"])))
        customers = []
        for item in data.get("customers", []):
            if (
                not isinstance(item, dict) or not set(item) <= {"id", "name"} or "id" not in item
                or not isinstance(item.get("name", ""), str)
            ):
                raise CredentialError(f"Invalid customer in the customer rules document: {source}.")
            customers.append(Tag(validate_customer(item["id"]), item.get("name")))
        return cls(tuple(rules), tuple(customers), source)

    def match(self, identities: tuple[str, ...]) -> tuple[str, str, str] | None:
        """Return (customer, pattern, identity) for the first rule matching origin or upstream.

        Rule order decides: list specific rules before catch-alls. A fork therefore gets the rule for
        its upstream even when a later catch-all would match its own origin.
        """
        for pattern, customer in self.rules:
            for identity in identities:
                if _glob(pattern).fullmatch(identity):
                    return customer, pattern, identity
        return None


class RulesLoader:
    """Load a profile's rules: local files directly, URLs through an ETag cache that survives outages.

    ``token`` is only called for a URL on the gateway's own origin, so other hosts never see the credential.
    """

    def __init__(
        self, store: ProfileStore, profile: Profile, token: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.profile = profile
        self.token = token
        self.clock = clock

    def __call__(self) -> Rules | None:
        location = self.profile.rules
        if location is None:
            return None
        if "://" not in location:
            return Rules.parse(self._read_file(Path(location)), location)
        return Rules.parse(self._cached(location), location)

    @staticmethod
    def _read_file(path: Path) -> object:
        try:
            raw = path.read_bytes()
            if len(raw) > _MAX_RULES_BYTES:
                raise ValueError
            return json.loads(raw)
        except (OSError, UnicodeError, ValueError):
            raise CredentialError(f"Cannot read customer rules from {path}.") from None

    def _cache_path(self, url: str) -> Path:
        return self.store.home / "rules-cache" / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_cache(self, url: str) -> dict | None:
        try:
            cached = json.loads(self._cache_path(url).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            not isinstance(cached, dict) or set(cached) != {"url", "fetched_at", "etag", "document"}
            or cached["url"] != url or type(cached["fetched_at"]) not in (int, float)
            or not math.isfinite(cached["fetched_at"])
            or (cached["etag"] is not None and not isinstance(cached["etag"], str))
        ):
            return None
        try:
            Rules.parse(cached["document"], url)
        except CredentialError:
            return None
        return cached

    def _cached(self, url: str) -> object:
        cached = self._read_cache(url)
        if cached is not None and 0 <= self.clock() - cached["fetched_at"] < RULES_CACHE_SECONDS:
            return cached["document"]
        try:
            fetched = self._fetch(url, cached["etag"] if cached else None)
        except CredentialError:
            if cached is not None:
                return cached["document"]  # Stale rules beat no rules during an outage.
            raise
        document, etag = fetched if fetched is not None else (cached["document"], cached["etag"])
        Rules.parse(document, url)
        path = self._cache_path(url)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.store.locked("rules-cache"):
                atomic_write_text(path, json.dumps({"url": url, "fetched_at": self.clock(), "etag": etag, "document": document}))
        except (OSError, CredentialError):
            pass  # The rules are still usable for this call; the next call fetches again.
        return document

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
            raise CredentialError(f"Customer rules request returned HTTP {code}.") from None
        except (URLError, OSError, ValueError):
            raise CredentialError("Cannot fetch the customer rules; check connectivity.") from None
        if len(raw) > _MAX_RULES_BYTES:
            raise CredentialError("Customer rules exceed the size limit.")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CredentialError("Customer rules are not valid JSON.") from None
        return document, new_etag if isinstance(new_etag, str) and len(new_etag) <= 512 else None


# Resolution


def resolve_customer(
    store: ProfileStore, profile: Profile, directory: Path, checkout, *,
    explicit: str | None = None, rules: Callable[[], Rules | None] | None = None,
    environ: Mapping[str, str] | None = None, home: Path | None = None,
) -> Attribution | None:
    """Choose the customer for a directory; first match wins.

    1. ``explicit`` (--customer) or the LLM_CREDENTIALS_CUSTOMER environment variable;
    2. the developer's local override (``repo bind``) for the origin, then the upstream remote;
    3. the nearest committed .llm-customer file;
    4. the profile's central rules, matched against origin, then upstream.

    ``checkout`` is the ``repositories.Checkout`` for ``directory`` or None outside Git. Return None when
    nothing matches; callers decide whether to prompt or fail. Raise CredentialError for invalid input,
    an unreadable customer file or rules that cannot be loaded.
    """
    from .repositories import RepositoryStore  # repositories imports this module for validate_customer.

    identities = checkout.identities if checkout is not None else ()
    repository = identities[0] if identities else None
    environ = os.environ if environ is None else environ
    if explicit is not None:
        return Attribution(validate_customer(explicit), "explicit", "--customer", repository)
    if environ.get(OVERRIDE_VARIABLE):
        return Attribution(validate_customer(environ[OVERRIDE_VARIABLE]), "explicit", OVERRIDE_VARIABLE, repository)
    local = RepositoryStore(store)
    for identity in identities:
        customer = local.get(profile.gateway, identity)
        if customer is not None:
            return Attribution(customer, "local", f"repo bind for {identity}", repository)
    path = find_customer_file(directory, home)
    if path is not None:
        return Attribution(read_customer_file(path), "file", str(path), repository)
    loaded = (rules or RulesLoader(store, profile))()
    if loaded is not None:
        matched = loaded.match(identities)
        if matched is not None:
            customer, pattern, identity = matched
            return Attribution(customer, "rules", f"rule {pattern} matched {identity}", repository)
    return None


def unmapped_error(directory: Path, checkout) -> CredentialError:
    where = (
        f"repository {checkout.origin}" if checkout is not None and checkout.origin
        else f"folder {directory}"
    )
    return CredentialError(
        f"No customer is assigned to {where}. Run 'llm-credentials repo bind' in a terminal "
        "(only for you), 'llm-credentials repo bind --shared' (writes a .llm-customer file to commit for the team), "
        "or ask for a central rule; use --customer NAME for a single launch. Nothing was sent untagged."
    )
