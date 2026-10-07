import hashlib
import json
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
from .profiles import Profile, ProfileStore, atomic_write_json, gateway_origin
from .repositories import Checkout, RepositoryStore, find_checkout
from .tags import TAG_HEADER, Tag, validate_tag
from .transport import REQUEST_TIMEOUT_SECONDS, NoRedirects

CUSTOMER_FILE = ".llm-customer"
OVERRIDE_VARIABLE = "LLM_CREDENTIALS_CUSTOMER"
SPEND_METADATA_HEADER = "x-litellm-spend-logs-metadata"
RULES_CACHE_SECONDS = 3600
_MAX_FILE_BYTES = 4096
_MAX_RULES_BYTES = 1_048_576


@dataclass(frozen=True)
class Attribution:
    """The customer chosen for a directory and why; ``repository`` is the normalized Git identity, if any."""

    customer: str
    source: str
    detail: str
    repository: str | None

    def headers(self) -> dict[str, str]:
        """Request headers: the LiteLLM customer tag and nonsecret spend-log metadata for later re-attribution."""
        metadata = {"customer_source": self.source}
        if self.repository is not None:
            metadata["repository"] = self.repository
        return {
            TAG_HEADER: self.customer,
            SPEND_METADATA_HEADER: json.dumps(metadata, separators=(",", ":"), sort_keys=True),
        }

    def to_dict(self) -> dict:
        return {"customer": self.customer, "source": self.source, "detail": self.detail, "repository": self.repository}


# Committed per-repository or per-folder file


def find_customer_file(directory: Path, home: Path | None = None) -> Path | None:
    """Return the nearest .llm-customer in ``directory`` or a parent, ignoring the home directory itself."""
    home = (home or Path.home()).resolve()
    for folder in (directory, *directory.parents):
        if folder == home:
            continue
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
        raise CredentialError(f"{path} must contain exactly: customer = \"TAG\".")
    try:
        return validate_tag(data["customer"])
    except CredentialError as error:
        raise CredentialError(f"{path}: {error}") from None


def write_customer_file(directory: Path, customer: str) -> Path:
    path = directory / CUSTOMER_FILE
    content = f"# LiteLLM customer tag for AI agent usage in this folder (llm-credentials).\ncustomer = {json.dumps(validate_tag(customer))}\n"
    try:
        path.write_text(content, encoding="utf-8")
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
            raise CredentialError("Unsupported or invalid customer rules document.")
        rules = []
        for rule in data.get("rules", []):
            if (
                not isinstance(rule, dict) or set(rule) != {"match", "customer"}
                or not isinstance(rule["match"], str) or not 0 < len(rule["match"]) <= 512
                or any(char.isspace() or ord(char) < 32 for char in rule["match"])
            ):
                raise CredentialError("Invalid rule in the customer rules document.")
            rules.append((rule["match"].lower(), validate_tag(rule["customer"])))
        customers = []
        for item in data.get("customers", []):
            if (
                not isinstance(item, dict) or not set(item) <= {"tag", "name"} or "tag" not in item
                or not isinstance(item.get("name", ""), str)
            ):
                raise CredentialError("Invalid customer in the customer rules document.")
            customers.append(Tag(validate_tag(item["tag"]), item.get("name")))
        return cls(tuple(rules), tuple(customers), source)

    def match(self, identities: tuple[str, ...]) -> tuple[str, str, str] | None:
        """Return (customer, pattern, identity) for the first rule matching origin, then upstream."""
        for identity in identities:
            for pattern, customer in self.rules:
                if _glob(pattern).fullmatch(identity):
                    return customer, pattern, identity
        return None


def validate_rules_location(value: str, allow_http: bool = False) -> str:
    """Accept an HTTPS URL (HTTP only for opted-in loopback) or an absolute local file path."""
    if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise CredentialError("Customer rules must be an HTTPS URL or an absolute file path.")
    if "://" not in value:
        if not Path(value).is_absolute():
            raise CredentialError("Customer rules must be an HTTPS URL or an absolute file path.")
        return value
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.fragment:
        raise CredentialError("Customer rules URL must not contain credentials or a fragment.")
    gateway_origin(f"{parsed.scheme}://{parsed.netloc}", allow_http)
    return value


class RulesLoader:
    """Load a profile's rules: local files directly, URLs through an ETag cache that survives outages."""

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

    def _cached(self, url: str) -> object:
        path = self._cache_path(url)
        cached = None
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict) or set(cached) != {"url", "fetched_at", "etag", "document"} or cached["url"] != url:
                cached = None
        except (OSError, ValueError):
            cached = None
        if cached is not None and self.clock() - cached["fetched_at"] < RULES_CACHE_SECONDS:
            return cached["document"]
        try:
            fetched = self._fetch(url, cached["etag"] if cached else None)
        except CredentialError:
            if cached is not None:
                return cached["document"]
            raise
        document, etag = fetched if fetched is not None else (cached["document"], cached["etag"])
        Rules.parse(document, url)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.store.locked("rules-cache"):
                atomic_write_json(path, {"url": url, "fetched_at": self.clock(), "etag": etag, "document": document})
        except (OSError, CredentialError):
            pass  # The rules are still usable for this call; the next call fetches again.
        return document

    def _fetch(self, url: str, etag: str | None) -> tuple[object, str | None] | None:
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
            return json.loads(raw), new_etag if isinstance(new_etag, str) and len(new_etag) <= 512 else None
        except (ValueError, UnicodeError):
            raise CredentialError("Customer rules are not valid JSON.") from None


# Resolution


def resolve_customer(
    store: ProfileStore, profile: Profile, directory: Path | None = None, *,
    explicit: str | None = None, rules: Callable[[], Rules | None] | None = None,
    environ: Mapping[str, str] | None = None, checkout: Checkout | None = None,
) -> Attribution | None:
    """Choose the customer for a directory: explicit override, local binding, committed file, central rules.

    Return None when nothing matches; callers decide whether to prompt or fail. Raise CredentialError for
    invalid inputs, an unreadable customer file or rules that cannot be loaded.
    """
    directory = (directory or Path.cwd()).resolve()
    if checkout is None:
        checkout = find_checkout(directory)
    identities = checkout.identities if checkout is not None else ()
    repository = identities[0] if identities else None
    environ = os.environ if environ is None else environ
    if explicit is None and environ.get(OVERRIDE_VARIABLE):
        explicit = environ[OVERRIDE_VARIABLE]
        source_detail = f"environment variable {OVERRIDE_VARIABLE}"
    else:
        source_detail = "--customer"
    if explicit is not None:
        return Attribution(validate_tag(explicit), "explicit", source_detail, repository)
    local = RepositoryStore(store)
    for identity in identities:
        customer = local.get(profile.gateway, identity)
        if customer is not None:
            return Attribution(customer, "local", f"repo bind for {identity}", repository)
    path = find_customer_file(directory)
    if path is not None:
        return Attribution(read_customer_file(path), "file", str(path), repository)
    loaded = (rules or RulesLoader(store, profile))()
    if loaded is not None:
        matched = loaded.match(identities)
        if matched is not None:
            customer, pattern, identity = matched
            return Attribution(customer, "rules", f"rule {pattern} matched {identity}", repository)
    return None


def unmapped_error(directory: Path, checkout: Checkout | None) -> CredentialError:
    where = (
        f"repository {checkout.origin}" if checkout is not None and checkout.origin
        else f"folder {directory}"
    )
    return CredentialError(
        f"No customer is assigned to {where}. Run 'llm-credentials repo bind' in a terminal "
        "(only for you), 'llm-credentials repo bind --shared' (commits a .llm-customer file for the team), "
        "or ask for a central rule. Nothing was sent untagged."
    )
