import time
from dataclasses import replace
from typing import Callable

from .errors import CredentialError
from .litellm import IdentityChanged, LiteLLMClient, Session
from .profiles import Profile, ProfileStore
from .storage import SecretStore
from .transport import REQUEST_TIMEOUT_SECONDS, GatewayError

# Two gateway round trips (refresh plus a possible revoke) at the transport timeout, plus vault I/O.
LOCK_WAIT_SECONDS = 60
# A token closer than this to expiry is never handed out, even when renewal is temporarily failing.
MINIMUM_VALIDITY_SECONDS = 60
# Waits after consecutive temporary renewal failures: 30 s, doubling up to 15 min.
FIRST_RETRY_DELAY_SECONDS = 30
MAXIMUM_RETRY_DELAY_SECONDS = 900


def refresh_margin(session: Session) -> float:
    """Renew once less than max(10 min, 25 % of the lifetime) remains, but never in the first half of a short lifetime."""
    lifetime = session.lifetime
    if lifetime <= 0:
        return MINIMUM_VALIDITY_SECONDS
    return min(max(600.0, lifetime / 4), lifetime / 2)


class SessionManager:
    """Serialize profile grants and refresh rotation across processes.

    Renewal starts early so a failed attempt still leaves a usable token. Transport failures,
    throttling, server errors, redirects and unusable responses keep the refresh token and are
    retried after a growing delay (LiteLLM claims a refresh token only after minting its
    replacement, and a replayed one is merely refused). A refused grant stops renewal, but the
    current access token is still released until it nearly expires: it cannot be revoked anyway.
    Only an unexpected identity stops releasing it at once.
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
        saved = False
        unreadable = None
        try:
            session.check_binding(profile)
            notify("Saving the sign-in (waiting for any other credential operation)...")
            with self._locked(profile):
                try:
                    previous = self._load(profile)
                except CredentialError as error:
                    previous, unreadable = None, error
                self.secrets.put(profile.account, session.to_dict())
                saved = True
        except BaseException:
            # Includes Ctrl+C while waiting for the lock: keep no unsaved grant alive.
            if not saved:
                try:
                    client.revoke(session)
                except CredentialError:
                    notify("Warning: the unsaved grant could not be revoked; its access token expires naturally.")
            raise
        if unreadable is not None:
            notify(
                f"Warning: the previous sign-in for '{profile.name}' could not be read ({unreadable}) "
                "and was replaced without being revoked. Ask the gateway administrator to revoke it."
            )
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
        """Return the current session, renewed when it is close to expiry; never opens a browser.

        A returned session with ``needs_login`` set still works but can no longer be renewed;
        callers should tell the user to sign in again.
        """
        with self._locked(profile):
            session = self._load(profile)
            if session is None:
                raise CredentialError(f"No login for '{profile.name}'; run 'llm-credentials login --profile {profile.name}'.")
            if expected_user is not None and session.user_id != expected_user:
                raise CredentialError("The signed-in employee changed; start a new agent session. No credential returned.")
            command = f"'llm-credentials login --profile {profile.name}'"
            relogin = f"run {command}"
            if session.blocked:
                raise CredentialError(f"The sign-in for '{profile.name}' must not be used any more; {relogin}.")
            if session.needs_login:
                if self._usable(session):
                    return session
                raise CredentialError(f"The sign-in for '{profile.name}' is no longer accepted; {relogin}.")
            if session.expires_at - self.clock() > refresh_margin(session):
                return session
            if session.retry_after is not None and self.clock() < session.retry_after and self._usable(session):
                return session
            client = self.client_factory(profile)
            try:
                renewed = client.refresh(session)
            except IdentityChanged as error:
                self._save_state(profile, replace(session, blocked=True))
                raise CredentialError(f"{error} Run {command}.") from None
            except GatewayError as error:
                if error.status is not None and 300 <= error.status < 400:
                    # A redirect never spends the refresh token; a moved endpoint is rediscovered by login.
                    detail = f"The stored sign-in was kept; if the gateway moved its endpoints, {relogin}."
                elif error.retryable:
                    detail = "Renewal will be retried; the stored sign-in was kept."
                else:
                    refused = self._save_state(profile, replace(session, needs_login=True))
                    if self._usable(refused):
                        return refused
                    raise CredentialError(f"{error} The gateway refused the sign-in; {relogin}.") from None
                return self._retry_later(profile, session, f"{error} {detail}")
            except CredentialError as error:
                # An unusable response, e.g. from an intermediary; the refresh token may still be valid.
                return self._retry_later(profile, session, f"{error} Renewal will be retried; the stored sign-in was kept.")
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

    def _usable(self, session: Session) -> bool:
        """Measured now, after any slow network attempt, so a nearly expired token is never released."""
        return session.expires_at - self.clock() > MINIMUM_VALIDITY_SECONDS

    def _retry_later(self, profile: Profile, session: Session, message: str) -> Session:
        """Record a temporary failure and return the session while its token is still usable."""
        failures = session.refresh_failures + 1
        delay = min(FIRST_RETRY_DELAY_SECONDS * 2 ** min(failures - 1, 16), MAXIMUM_RETRY_DELAY_SECONDS)
        # Always leave room for one more attempt before the token becomes unusable.
        last_chance = session.expires_at - MINIMUM_VALIDITY_SECONDS - REQUEST_TIMEOUT_SECONDS
        waiting = self._save_state(profile, replace(
            session, retry_after=min(self.clock() + delay, last_chance), refresh_failures=failures,
        ))
        if self._usable(waiting):
            return waiting
        raise CredentialError(message)

    def _save_state(self, profile: Profile, session: Session) -> Session:
        try:
            self.secrets.put(profile.account, session.to_dict())
        except CredentialError:
            pass  # The next call repeats the attempt and reaches the same state.
        return session

    def status(self, profile: Profile) -> dict:
        with self._locked(profile):
            session = self._load(profile)
            if session is None:
                return {"profile": profile.name, "gateway": profile.gateway, "team_id": profile.team_id, "state": "logged-out"}
            state = "re-login-required" if session.needs_login or session.blocked else (
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
