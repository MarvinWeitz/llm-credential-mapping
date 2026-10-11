"""Interfaces between the vendor-neutral core and its adapters.

Auth adapters obtain credentials, gateway adapters know URLs and attribution headers, and
integration adapters configure and launch tools. Implementations are listed in ``registry``.
"""

import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from .customers import Attribution, Tag
from .profiles import Profile


class Api(StrEnum):
    """A wire protocol a tool speaks; a gateway maps each one it supports to a base URL."""

    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_CHAT = "openai-chat"
    OPENAI_RESPONSES = "openai-responses"


@dataclass(frozen=True)
class Credential:
    """A bearer secret for the gateway; ``notice`` is a nonsecret message for the user, e.g. sign in again soon."""

    secret: str = field(repr=False)
    expires_at: float | None = None
    user: str | None = None
    notice: str | None = None


class Auth(Protocol):
    """Obtains credentials for one profile; never opens a browser outside ``login``."""

    def login(self, notify: Callable[[str], None], **options) -> Credential: ...
    def logout(self) -> None: ...
    def status(self) -> dict: ...
    def credential(self) -> Credential: ...


class Gateway(Protocol):
    """Knows a gateway's endpoints and how it expects a customer to be attributed."""

    def endpoint(self, api: Api) -> str:
        """Return the base URL for ``api``; raise CredentialError if the gateway does not offer it."""

    def attribution_headers(self, attribution: Attribution) -> dict[str, str]: ...

    def attribution_header_names(self) -> tuple[str, ...]:
        """All header names ``attribution_headers`` may return, for tools configured ahead of time."""

    def visible_customers(self, credential: Credential) -> list[Tag] | None:
        """Customers the gateway lists for this credential, or None when it has no catalogue."""


@dataclass(frozen=True)
class IntegrationContext:
    """What an integration may use: the profile, its gateway, a credential and how to call this CLI.

    ``cli`` is the argv prefix that runs this CLI with the same configuration folder; tools that renew
    credentials themselves call ``cli + ("token", "--profile", NAME)`` instead of receiving ``token``.
    ``home`` holds the integration's own nonsecret state (what setup changed, for a clean removal).
    """

    profile: Profile
    gateway: Gateway
    token: str = field(repr=False)
    home: Path | None = None
    cli: tuple[str, ...] = (sys.executable, "-I", "-m", "llm_credential_mapping")

    def cli_command(self, *args: str) -> tuple[str, ...]:
        """This CLI with the integration's configuration folder, e.g. ``cli_command("token", "--profile", "a")``."""
        return (*self.cli, *(("--home", str(self.home)) if self.home is not None else ()), *args)


class Integration(Protocol):
    """Configures and launches one tool; it only uses its context and the attribution."""

    def setup(self, context: IntegrationContext, model: str | None = None, replace: bool = False) -> list[str]:
        """Write the tool's user-level configuration; return messages for the user."""

    def remove(self, context: IntegrationContext) -> list[str]:
        """Undo ``setup``, keeping unrelated configuration."""

    def command(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> tuple[list[str], dict[str, str]]: ...

    def launch(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> int: ...
