from dataclasses import dataclass
from typing import Callable

from .errors import CredentialError
from .profiles import Profile, ProfileStore
from .repositories import RepositoryStore
from .sessions import SessionManager
from .storage import SecretStore


def validate_model(model: str) -> None:
    """Reject empty, whitespace-containing or oversized LiteLLM model aliases."""
    if (
        not isinstance(model, str) or not model.strip() or model != model.strip()
        or len(model) > 256 or any(char.isspace() or ord(char) < 32 for char in model)
    ):
        raise CredentialError("Provide a nonempty LiteLLM model alias without whitespace.")


@dataclass(frozen=True)
class RequestIdentity:
    """The customer and employee selected for one normalised repository and authentication profile."""

    customer: str
    user_id: str


def resolve_identity(
    store: ProfileStore, profile: Profile, repository: str,
    secrets_factory: Callable[[], SecretStore],
    manager_factory: Callable[[ProfileStore, SecretStore], SessionManager] = SessionManager,
) -> RequestIdentity:
    """Require a repository customer binding and usable employee grant, then return their immutable identity.

    Vault access and renewal happen only after the repository binding is validated.
    Raise CredentialError for missing bindings, logins or uncertain/unusable grants.
    """
    customer = RepositoryStore(store).get(profile.gateway, repository)
    if customer is None:
        raise CredentialError(
            "This repository has no customer binding for the selected gateway. "
            "Run 'llm-credentials repo bind --profile " + profile.name + "' in its terminal first."
        )
    manager = manager_factory(store, secrets_factory())
    status = manager.status(profile)
    if status["state"] == "logged-out":
        raise CredentialError(f"No login for '{profile.name}'; run login --profile {profile.name} in this host's terminal.")
    user_id = status["user_id"]
    manager.token(profile, expected_user=user_id)
    return RequestIdentity(customer, user_id)
