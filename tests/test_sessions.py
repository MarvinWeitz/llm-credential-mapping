import copy
import tempfile
import time
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.litellm import Session
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.sessions import SessionManager


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return copy.deepcopy(self.values.get(account))

    def put(self, account, value):
        self.values[account] = copy.deepcopy(value)

    def delete(self, account):
        del self.values[account]


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profiles = ProfileStore(Path(self.temp.name))
        self.profile = Profile("a", "https://example.com", "team-a")
        self.secrets = MemoryStore()
        self.client = Mock()
        self.notify = Mock()
        self.manager = SessionManager(self.profiles, self.secrets, lambda profile: self.client)
        self.session = Session(
            self.profile.gateway, self.profile.team_id, "employee", "client",
            "access-secret", "refresh-secret", time.time() + 3600,
        )
        self.client.login.return_value = self.session

    def test_login_persists_project_scoped_session(self):
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

    def test_wrong_team_preserves_previous_login(self):
        self.secrets.put(self.profile.account, self.session.to_dict())
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
        self.secrets.put(self.profile.account, self.session.to_dict())
        self.client.login.return_value = replace(self.session, access_token="new")
        self.manager.login(self.profile, self.notify)
        self.client.revoke.assert_called_once_with(self.session)
        self.assertEqual(self.manager.token(self.profile), "new")

    def test_unknown_login_fails_without_browser(self):
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.client.login.assert_not_called()

    def test_refresh_saves_rotated_tokens(self):
        old = replace(self.session, expires_at=time.time() + 10)
        self.secrets.put(self.profile.account, old.to_dict())
        new = replace(self.session, access_token="new", refresh_token="rotated")
        self.client.refresh.return_value = new
        self.assertEqual(self.manager.token(self.profile), "new")
        saved = self.secrets.get(self.profile.account)
        self.assertEqual(saved["refresh_token"], "rotated")
        self.assertFalse(saved["refresh_pending"])

    def test_failed_renewal_is_not_replayed(self):
        old = replace(self.session, expires_at=time.time() - 10)
        self.secrets.put(self.profile.account, old.to_dict())
        self.client.refresh.side_effect = CredentialError("offline")
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.assertTrue(self.secrets.get(self.profile.account)["refresh_pending"])
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.client.refresh.assert_called_once()

    def test_interrupted_renewal_is_not_replayed(self):
        self.secrets.put(self.profile.account, replace(self.session, expires_at=0).to_dict())
        self.client.refresh.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.manager.token(self.profile)
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.client.refresh.assert_called_once()

    def test_parallel_refreshes_use_one_rotating_grant(self):
        self.secrets.put(self.profile.account, replace(self.session, expires_at=0).to_dict())
        self.client.refresh.return_value = replace(self.session, access_token="new", refresh_token="rotated")
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.manager.token(self.profile), range(4)))
        self.assertEqual(values, ["new"] * 4)
        self.client.refresh.assert_called_once()

    def test_failed_pending_save_does_not_send_refresh(self):
        self.secrets.put(self.profile.account, replace(self.session, expires_at=0).to_dict())
        self.secrets.put = Mock(side_effect=CredentialError("vault locked"))
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.client.refresh.assert_not_called()

    def test_failed_rotated_save_revokes_new_grant_and_requires_login(self):
        self.secrets.put(self.profile.account, replace(self.session, expires_at=0).to_dict())
        self.client.refresh.return_value = replace(self.session, access_token="new", refresh_token="rotated")
        original_put = self.secrets.put
        calls = []

        def put(account, value):
            calls.append(value)
            if len(calls) == 2:
                raise CredentialError("vault locked")
            original_put(account, value)

        self.secrets.put = put
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.client.revoke.assert_called_once_with(self.client.refresh.return_value)
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)
        self.client.refresh.assert_called_once()

    def test_failed_login_save_revokes_new_grant(self):
        self.secrets.put = Mock(side_effect=CredentialError("vault locked"))
        with self.assertRaises(CredentialError):
            self.manager.login(self.profile, self.notify)
        self.client.revoke.assert_called_once_with(self.session)

    def test_sessions_are_isolated_between_projects(self):
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

    def test_invalid_stored_grant_is_an_error(self):
        self.secrets.put(self.profile.account, {"version": 1})
        with self.assertRaises(CredentialError):
            self.manager.token(self.profile)

    def test_repr_does_not_expose_credentials(self):
        self.assertNotIn("access-secret", repr(self.session))
        self.assertNotIn("refresh-secret", repr(self.session))
