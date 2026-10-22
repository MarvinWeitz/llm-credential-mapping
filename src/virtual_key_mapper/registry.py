"""The available adapters. Adding a gateway, auth kind or tool means one module plus one entry here."""

from dataclasses import dataclass
from typing import Callable

from .adapters import Auth, Gateway, Integration
from .auth import forwarded, litellm_sso
from .errors import CredentialError
from .gateways.litellm import LiteLLMGateway
from .labels import Output
from .profiles import Profile, ProfileStore
from .storage import SecretStore


@dataclass(frozen=True)
class AuthKind:
    """``settings`` validates and normalizes a profile's settings; ``gateways`` limits usable gateway kinds (None: any).

    ``create`` receives a factory for the OS vault, so kinds that store nothing never open one.
    """

    create: Callable[[Profile, ProfileStore, Callable[[], SecretStore]], Auth]
    settings: Callable[[dict], dict]
    gateways: frozenset[str] | None = None


AUTH: dict[str, AuthKind] = {
    "litellm-sso": AuthKind(litellm_sso.LiteLLMSSO, litellm_sso.settings, frozenset({"litellm"})),
    "forwarded": AuthKind(forwarded.Forwarded, forwarded.settings),
}
# A gateway kind is its adapter class; ``outputs()`` is static, so profiles validate without building one.
GATEWAYS: dict[str, Callable[[Profile], Gateway]] = {
    "litellm": LiteLLMGateway,
}


def __getattr__(name: str) -> dict[str, Integration]:
    """``INTEGRATIONS``, loaded on first use: validating a profile does not import every tool integration."""
    if name != "INTEGRATIONS":
        raise AttributeError(name)
    from .integrations.claude import Claude
    from .integrations.codex import Codex
    from .integrations.opencode import OpenCode

    integrations = {"claude": Claude(), "codex": Codex(), "opencode": OpenCode()}
    globals()["INTEGRATIONS"] = integrations
    return integrations


def validate_profile(gateway_type: str, auth_type: str, settings: dict) -> dict:
    """Check that both kinds exist and fit together, and return the auth kind's normalized settings."""
    if gateway_type not in GATEWAYS:
        raise CredentialError(f"Unknown gateway type; choose one of: {', '.join(sorted(GATEWAYS))}.")
    if auth_type not in AUTH:
        raise CredentialError(f"Unknown authentication type; choose one of: {', '.join(sorted(AUTH))}.")
    kind = AUTH[auth_type]
    if kind.gateways is not None and gateway_type not in kind.gateways:
        raise CredentialError(f"Authentication '{auth_type}' does not work with gateway type '{gateway_type}'.")
    return kind.settings(settings)


def auth_for(profile: Profile, store: ProfileStore, secrets: Callable[[], SecretStore]) -> Auth:
    return AUTH[profile.auth_type].create(profile, store, secrets)


def gateway_for(profile: Profile) -> Gateway:
    return GATEWAYS[profile.gateway_type](profile)


def gateway_outputs(gateway_type: str) -> tuple[Output, ...]:
    """The outputs a gateway kind can fill (``Gateway.outputs``), without a profile."""
    if gateway_type not in GATEWAYS:
        raise CredentialError(f"Unknown gateway type; choose one of: {', '.join(sorted(GATEWAYS))}.")
    return tuple(GATEWAYS[gateway_type].outputs())
