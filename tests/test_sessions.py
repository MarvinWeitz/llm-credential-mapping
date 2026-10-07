import copy
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from llm_credential_mapping import sessions
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.litellm import Session
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.sessions import SessionManager, refresh_margin
from llm_credential_mapping.transport import GatewayError


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return copy.deepcopy(self.values.get(account))

    def put(self, account, value):
        self.values[account] = copy.deepcopy(value)

    def delete(self, account):
        del self.values[account]


def make_session(
    profile: Profile, *, user_id="employee", client_id="client", access_token="access-secret",
    refresh_token="refresh-secret", issued_at=None, lifetime=3600.0,
) -> Session:
    """A stored grant for a profile, with endpoints on the profile's gateway."""
    issued_at = time.time() if issued_at is None else issued_at
    return Session(
        profile.gateway, profile.team_id, user_id, client_id, access_token, refresh_token,
        issued_at, issued_at + lifetime,
        profile.gateway + "/token", profile.gateway + "/revoke", profile.gateway,
    )


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profiles = ProfileStore(Path(self.temp.name))
        self.profile = Profile("a", "https://example.com", "team-a")
        self.secrets = MemoryStore()
        self.client = Mock()
        self.notify = Mock()
        self.clock = Clock()
        self.manager = SessionManager(self.profiles, self.secrets, lambda profile: self.client, self.clock)
        self.session = make_session(self.profile, issued_at=self.clock.now)
        self.client.login.return_value = self.session

    def store(self, session):
        self.secrets.put(self.profile.account, session.to_dict())

    def expired(self):
        return replace(self.session, issued_at=self.clock.now - 3601, expires_at=self.clock.now - 1)

    def stored(self):
        return Session.from_dict(self.secrets.get(self.profile.account))

    def renewal(self, **changes):
        renewed = replace(
            self.session, access_token="new", refresh_token="rotated",
            issued_at=self.clock.now, expires_at=self.clock.now + 3600,
        )
        return replace(renewed, **changes)

    def test_login_persists_profile_scoped_session(self):
        session = self.manager.login(self.profile, self.notify)
        self.assertEqual(session, self.session)
        self.assertEqual(self.manager.token(self.profile), "access-secret")
        self.client.refresh.assert_not_called()

    def test_wrong_team_is_revoked_and_not_saved(self):
        self.client.login.return_value = replace(self.session, team_id="wrong")
        with self.assertRaises(CredentialError):
            self.manager.login(self.profile, self.notify)
        self.assertIsNone(self.secrets.get(self.profile.account))
        self.client.revoke.assert_called_once()

    def test_foreign_renewal_endpoint_is_rejected_at_login(self):
        self.client.login.return_value = replace(self.session, token_endpoint="https://attacker.example/token")
        with self.assertRaises(CredentialError):
            self.manager.login(self.profile, self.notify)
        self.assertIsNone(self.secrets.get(self.profile.account))

    def test_wrong_team_preserves_previous_login(self):
        self.store(self.session)
        self.client.login.return_value = replace(self.session, team_id="wrong")
        with self.assertRaises(CredentialError):
            self.manager.login(self.profile, self.notify)
        self.assertEqual(self.manager.token(self.profile), "access-secret")

    def test_failed_rejected_grant_cleanup_is_reported(self):
        self.client.login.return_value = replace(self.session, team_id="wrong")
        self.client.revoke.side_effect = CredentialError("offline")
        with self.assertRaises(CredentialError):
            self.manager.login(self.profile, self.notify)
        self.notify.assert_called_once()

    def test_relogin_revokes_previous_refresh_after_saving_new(self):
        self.store(self.session)
        self.client.login.return_value = replace(self.session, access_token="new", refresh_token="new-refresh")
        self.manager.login(self.profile, self.notify)
        self.client.revoke.assert_called_once_with(self.session)
        self.assertEqual(self.manager.token(self.profile), "new")

    def test_relogin_replaces_an_unreadable_previous_session(self):
        self.secrets.put(self.profile.account, {"version": 1})
        self.manager.login(self.profile, self.notify)
        self.assertEqual(self.manager.token(self.profile), "access-secret")
        self.client.revoke.assert_not_called()

    def test_login_does_not_hold_the_profile_lock_while_waiting_for_the_browser(self):
        self.store(self.session)
        results = []

        def browser_login(*args, **options):
            worker = threading.Thread(target=lambda: results.append(self.manager.token(self.profile)))
            worker.start()
            worker.join(timeout=5)
            return replace(self.session, access_token="new", refresh_token="new-refresh")

        self.client.login.side_effect = browser_login
        with patch.object(sessions, "LOCK_WAIT_SECONDS", 1):
            self.manager.login(self.profile, self.notify)
        self.assertEqual(results, ["access-secret"])

    def test_unknown_login_fails_without_browser(self):
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.client.login.assert_not_called()

    def test_expected_employee_is_checked_before_returning_or_refreshing_credentials(self):
        for session in (self.session, self.expired()):
            self.store(session)
            with self.subTest(expires_at=session.expires_at), self.assertRaises(CredentialError):
                self.manager.token(self.profile, expected_user="other-employee")
            self.assertFalse(self.stored().needs_login)
        self.client.refresh.assert_not_called()

    def test_refresh_margin_starts_renewal_early_without_looping_on_short_lifetimes(self):
        for lifetime, margin in ((86_400, 21_600), (3600, 900), (1800, 600), (600, 300), (0, 60)):
            with self.subTest(lifetime=lifetime):
                self.assertEqual(refresh_margin(make_session(self.profile, issued_at=0, lifetime=lifetime)), margin)

    def test_token_is_returned_without_renewal_outside_the_margin(self):
        self.store(self.session)
        self.clock.now += 3600 - 901
        self.assertEqual(self.manager.token(self.profile), "access-secret")
        self.client.refresh.assert_not_called()

    def test_refresh_starts_inside_the_margin_and_saves_rotated_tokens(self):
        self.store(self.session)
        self.clock.now += 3600 - 899
        self.client.refresh.return_value = self.renewal()
        self.assertEqual(self.manager.token(self.profile), "new")
        self.client.refresh.assert_called_once_with(self.session)
        self.assertEqual(self.stored().refresh_token, "rotated")

    def test_transient_failure_keeps_a_valid_token_and_retries_later(self):
        self.store(self.session)
        self.clock.now += 3600 - 600
        for status in (None, 408, 429, 500, 503):
            self.client.refresh.side_effect = GatewayError("unavailable", status=status)
            with self.subTest(status=status):
                self.assertEqual(self.manager.token(self.profile), "access-secret")
                self.assertEqual(self.stored(), self.session)
        self.assertEqual(self.client.refresh.call_count, 5)

    def test_transient_failure_near_expiry_is_an_error_but_keeps_the_grant(self):
        self.store(self.session)
        self.clock.now += 3600 - 30
        self.client.refresh.side_effect = GatewayError("unavailable")
        with self.assertRaises(CredentialError) as result:
            self.manager.token(self.profile)
        self.assertIn("retried", str(result.exception))
        self.assertEqual(self.stored(), self.session)
        self.client.refresh.side_effect = None
        self.client.refresh.return_value = self.renewal()
        self.assertEqual(self.manager.token(self.profile), "new")

    def test_refused_grant_requires_login_without_further_refreshes(self):
        for status in (400, 401, 403, 404):
            self.client.refresh.reset_mock()
            self.store(self.expired())
            self.client.refresh.side_effect = GatewayError("refused", status=status)
            with self.subTest(status=status):
                with self.assertRaises(CredentialError) as result:
                    self.manager.token(self.profile)
                self.assertIn("login", str(result.exception))
                self.assertTrue(self.stored().needs_login)
                with self.assertRaises(CredentialError):
                    self.manager.token(self.profile)
                self.client.refresh.assert_called_once()
                self.assertEqual(self.manager.status(self.profile)["state"], "re-login-required")

    def test_unexpected_renewal_response_requires_login(self):
        self.store(self.expired())
        self.client.refresh.side_effect = CredentialError("Renewed credential changed employee identity.")
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.assertTrue(self.stored().needs_login)

    def test_interrupted_renewal_keeps_the_grant_for_a_retry(self):
        self.store(self.expired())
        self.client.refresh.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.manager.token(self.profile)
        self.assertFalse(self.stored().needs_login)
        self.client.refresh.side_effect = None
        self.client.refresh.return_value = self.renewal()
        self.assertEqual(self.manager.token(self.profile), "new")

    def test_parallel_refreshes_use_one_rotating_grant(self):
        self.store(self.expired())
        self.client.refresh.return_value = self.renewal()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.manager.token(self.profile), range(4)))
        self.assertEqual(values, ["new"] * 4)
        self.client.refresh.assert_called_once()

    def test_failed_rotated_save_revokes_new_grant(self):
        self.store(self.expired())
        self.client.refresh.return_value = self.renewal()
        self.secrets.put = Mock(side_effect=CredentialError("vault locked"))
        with self.assertRaises(CredentialError) as result:
            self.manager.token(self.profile)
        self.assertIn("revoked", str(result.exception))
        self.client.revoke.assert_called_once_with(self.client.refresh.return_value)

    def test_failed_login_save_revokes_new_grant(self):
        self.secrets.put = Mock(side_effect=CredentialError("vault locked"))
        with self.assertRaises(CredentialError):
            self.manager.login(self.profile, self.notify)
        self.client.revoke.assert_called_once_with(self.session)

    def test_sessions_are_isolated_between_profiles(self):
        self.manager.login(self.profile, self.notify)
        b = Profile("b", "https://example.com", "team-b")
        self.client.login.return_value = replace(self.session, team_id="team-b", access_token="b")
        self.manager.login(b, self.notify)
        self.assertEqual(self.manager.token(self.profile), "access-secret")
        self.assertEqual(self.manager.token(b), "b")

    def test_status_does_not_expose_secrets_or_refresh(self):
        self.manager.login(self.profile, self.notify)
        status = str(self.manager.status(self.profile))
        self.assertIn("employee", status)
        self.assertNotIn("access-secret", status)
        self.assertNotIn("refresh-secret", status)
        self.client.refresh.assert_not_called()

    def test_logout_revokes_and_deletes(self):
        self.manager.login(self.profile, self.notify)
        self.manager.logout(self.profile)
        self.client.revoke.assert_called_once_with(self.session)
        self.assertIsNone(self.secrets.get(self.profile.account))

    def test_failed_logout_keeps_grant_for_retry(self):
        self.manager.login(self.profile, self.notify)
        self.client.revoke.side_effect = CredentialError("offline")
        with self.assertRaises(CredentialError):
            self.manager.logout(self.profile)
        self.assertIsNotNone(self.secrets.get(self.profile.account))

    def test_invalid_or_unsupported_stored_grant_is_an_error(self):
        for value in (
            {"version": 1},
            {**self.session.to_dict(), "version": 1},
            {**self.session.to_dict(), "needs_login": "no"},
            {**self.session.to_dict(), "issued_at": self.session.expires_at + 1},
            {**self.session.to_dict(), "expires_at": float("nan")},
        ):
            self.secrets.put(self.profile.account, value)
            with self.subTest(value=value), self.assertRaises(CredentialError):
                self.manager.token(self.profile)

    def test_stored_endpoints_must_stay_on_the_gateway(self):
        for field in ("token_endpoint", "revocation_endpoint", "resource"):
            self.store(replace(self.session, **{field: "https://attacker.example/token"}))
            with self.subTest(field=field), self.assertRaises(CredentialError):
                self.manager.token(self.profile)

    def test_repr_does_not_expose_credentials(self):
        self.assertNotIn("access-secret", repr(self.session))
        self.assertNotIn("refresh-secret", repr(self.session))
