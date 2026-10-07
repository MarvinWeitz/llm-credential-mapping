import time
from dataclasses import replace
from typing import Callable

from .errors import CredentialError
from .litellm import LiteLLMClient, Session
from .profiles import Profile, ProfileStore
from .storage import SecretStore
from .transport import GatewayError

# Two gateway round trips (refresh plus a possible revoke) at the transport timeout, plus vault I/O.
LOCK_WAIT_SECONDS = 60
# A token closer than this to expiry is never handed out, even when renewal is temporarily failing.
MINIMUM_VALIDITY_SECONDS = 60


def refresh_margin(session: Session) -> float:
    """Renew once less than max(10 min, 25 % of the lifetime) remains, but never in the first half of a short lifetime."""
    lifetime = session.lifetime
    if lifetime <= 0:
        return MINIMUM_VALIDITY_SECONDS
    return min(max(600.0, lifetime / 4), lifetime / 2)


class SessionManager:
    """Serialize profile grants and refresh rotation across processes.

    Renewal starts early so a failed attempt still leaves a usable token. Transport failures,
    throttling and server errors keep the refresh token for a later retry (LiteLLM claims a
    refresh token only after minting its replacement, and a replayed one is merely refused).
    Only a refused grant or an unexpected identity requires signing in again.
    """

    def __init__(
        self,
        profiles: ProfileStore,
        secrets: SecretStore,
        client_factory: Callable[[Profile], LiteLLMClient] = LiteLLMClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.profiles = profiles
        self.secrets = secrets
        self.client_factory = client_factory
        self.clock = clock

    def _locked(self, profile: Profile):
        return self.profiles.locked(profile.account, timeout=LOCK_WAIT_SECONDS)

    def _load(self, profile: Profile) -> Session | None:
        value = self.secrets.get(profile.account)
        if value is None:
            return None
        session = Session.from_dict(value)
        session.check_binding(profile)
        return session

    def login(self, profile: Profile, notify: Callable[[str], None], **options) -> Session:
        """Sign in through the browser without holding the profile lock, then replace and revoke any previous grant."""
        client = self.client_factory(profile)
        session = client.login(notify, **options)
        try:
            session.check_binding(profile)
            with self._locked(profile):
                try:
                    previous = self._load(profile)
                except CredentialError:
                    previous = None
                self.secrets.put(profile.account, session.to_dict())
        except CredentialError:
            try:
                client.revoke(session)
            except CredentialError:
                notify("Warning: the rejected grant could not be revoked; its access token expires naturally.")
            raise
        if previous is not None and previous.refresh_token != session.refresh_token:
            try:
                client.revoke(previous)
            except CredentialError:
                notify("Warning: previous refresh grant could not be revoked; contact the gateway administrator.")
        return session

    def token(self, profile: Profile, *, expected_user: str | None = None) -> str:
        """Return a usable access token, renewing it early; an expected employee must match before any refresh or release."""
        return self.session(profile, expected_user=expected_user).access_token

    def session(self, profile: Profile, *, expected_user: str | None = None) -> Session:
        """Return the current session, renewed when it is close to expiry; never opens a browser."""
        with self._locked(profile):
            session = self._load(profile)
            if session is None:
                raise CredentialError(f"No login for '{profile.name}'; run 'llm-credentials login --profile {profile.name}'.")
            if expected_user is not None and session.user_id != expected_user:
                raise CredentialError("The signed-in employee changed; start a new agent session. No credential returned.")
            if session.needs_login:
                raise CredentialError(
                    f"The sign-in for '{profile.name}' is no longer accepted; "
                    f"run 'llm-credentials login --profile {profile.name}'."
                )
            remaining = session.expires_at - self.clock()
            if remaining > refresh_margin(session):
                return session
            client = self.client_factory(profile)
            try:
                renewed = client.refresh(session)
            except GatewayError as error:
                if not error.retryable:
                    self._mark_needs_login(profile, session)
                    raise CredentialError(
                        f"{error} The gateway refused the sign-in; run 'llm-credentials login --profile {profile.name}'."
                    ) from None
                if remaining > MINIMUM_VALIDITY_SECONDS:
                    return session
                raise CredentialError(
                    f"{error} Renewal will be retried; the stored sign-in was kept."
                ) from None
            except CredentialError as error:
                self._mark_needs_login(profile, session)
                raise CredentialError(
                    f"{error} Run 'llm-credentials login --profile {profile.name}'."
                ) from None
            try:
                self.secrets.put(profile.account, renewed.to_dict())
            except CredentialError as error:
                # The previous refresh token is spent; keep no unsaved grant alive.
                try:
                    client.revoke(renewed)
                except CredentialError:
                    raise CredentialError(
                        f"{error} New refresh grant could not be revoked; contact the gateway administrator and log in again."
                    ) from None
                raise CredentialError(f"{error} Renewed grant was revoked; log in again.") from None
            return renewed

    def _mark_needs_login(self, profile: Profile, session: Session) -> None:
        try:
            self.secrets.put(profile.account, replace(session, needs_login=True).to_dict())
        except CredentialError:
            pass  # The next call retries the refused grant and reports the same error.

    def status(self, profile: Profile) -> dict:
        with self._locked(profile):
            session = self._load(profile)
            if session is None:
                return {"profile": profile.name, "gateway": profile.gateway, "team_id": profile.team_id, "state": "logged-out"}
            state = "re-login-required" if session.needs_login else (
                "expired" if session.expires_at <= self.clock() else "ready"
            )
            return {
                "profile": profile.name,
                "gateway": profile.gateway,
                "team_id": session.team_id,
                "user_id": session.user_id,
                "expires_at": session.expires_at,
                "state": state,
            }

    def logout(self, profile: Profile) -> None:
        with self._locked(profile):
            session = self._load(profile)
            if session is None:
                raise CredentialError("This profile is already logged out.")
            self.client_factory(profile).revoke(session)
            self.secrets.delete(profile.account)
