import time
from dataclasses import replace
from typing import Callable

from .errors import CredentialError
from .litellm import LiteLLMClient, Session
from .profiles import Profile, ProfileStore
from .storage import SecretStore


class SessionManager:
    """Serialize project grants and single-use refreshes; ambiguous renewal requires re-login."""

    def __init__(
        self,
        profiles: ProfileStore,
        secrets: SecretStore,
        client_factory: Callable[[Profile], LiteLLMClient] = LiteLLMClient,
    ) -> None:
        self.profiles = profiles
        self.secrets = secrets
        self.client_factory = client_factory

    def _load(self, profile: Profile) -> Session | None:
        value = self.secrets.get(profile.account)
        if value is None:
            return None
        session = Session.from_dict(value)
        session.check_binding(profile)
        return session

    def login(self, profile: Profile, notify: Callable[[str], None], **options) -> Session:
        with self.profiles.locked(profile.account):
            previous = self._load(profile)
            client = self.client_factory(profile)
            session = client.login(notify, **options)
            try:
                session.check_binding(profile)
                self.secrets.put(profile.account, session.to_dict())
            except CredentialError:
                try:
                    client.revoke(session)
                except CredentialError:
                    notify("Warning: the rejected grant could not be revoked; its access token expires naturally.")
                raise
            if previous is not None:
                try:
                    client.revoke(previous)
                except CredentialError:
                    notify("Warning: previous refresh grant could not be revoked; contact the gateway administrator.")
            return session

    def token(self, profile: Profile) -> str:
        with self.profiles.locked(profile.account):
            session = self._load(profile)
            if session is None:
                raise CredentialError(f"No login for '{profile.name}'; run login --project {profile.name}.")
            if session.refresh_pending:
                raise CredentialError("Previous renewal was interrupted or uncertain; log in again. No token replayed.")
            if session.expires_at > time.time() + 60:
                return session.access_token
            client = self.client_factory(profile)
            # A consumed refresh token must never be retried after an uncertain response.
            pending = replace(session, refresh_pending=True)
            self.secrets.put(profile.account, pending.to_dict())
            try:
                renewed = client.refresh(session)
            except CredentialError as error:
                raise CredentialError(f"{error} Renewal stopped; log in again before using this project.") from None
            try:
                self.secrets.put(profile.account, renewed.to_dict())
            except CredentialError as error:
                try:
                    client.revoke(renewed)
                except CredentialError:
                    raise CredentialError(
                        f"{error} New refresh grant could not be revoked; contact the gateway administrator and log in again."
                    ) from None
                raise CredentialError(f"{error} Renewed grant was revoked; log in again.") from None
            return renewed.access_token

    def status(self, profile: Profile) -> dict:
        with self.profiles.locked(profile.account):
            session = self._load(profile)
            if session is None:
                return {"project": profile.name, "gateway": profile.gateway, "team_id": profile.team_id, "state": "logged-out"}
            state = "re-login-required" if session.refresh_pending else (
                "expired" if session.expires_at <= time.time() else "ready"
            )
            return {
                "project": profile.name,
                "gateway": profile.gateway,
                "team_id": session.team_id,
                "user_id": session.user_id,
                "expires_at": session.expires_at,
                "state": state,
            }

    def logout(self, profile: Profile) -> None:
        with self.profiles.locked(profile.account):
            session = self._load(profile)
            if session is None:
                raise CredentialError("This project is already logged out.")
            self.client_factory(profile).revoke(session)
            self.secrets.delete(profile.account)
