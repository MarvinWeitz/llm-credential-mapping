"""Opt-in: the real OS credential vault (Windows Credential Manager, macOS Keychain, Secret Service).

These tests write and remove entries in the vault of whoever runs them, under unique accounts, so they
run only with LLM_CREDENTIAL_MAPPING_TEST_VAULT=1. CI sets it on every platform (on Linux inside a
D-Bus session with an unlocked GNOME keyring). The sign-in uses a local fake gateway; no secrets.
"""

import math
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.auth.litellm_sso import SessionManager, vault_account
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.storage import NativeKeyring
from live import binary_tests
from test_oauth import OAuthFixture

EXPECTED_BACKENDS = {
    "win32": ("keyring.backends.Windows",),
    "darwin": ("keyring.backends.macOS",),
    "linux": ("keyring.backends.SecretService", "keyring.backends.kwallet"),
}


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@binary_tests("vault", "LLM_CREDENTIAL_MAPPING_TEST_VAULT")
class NativeVaultTests(unittest.TestCase):
    def setUp(self):
        self.vault = NativeKeyring()
        self.accounts = []

    def tearDown(self):
        for account in self.accounts:
            try:
                if self.vault.get(account) is not None:
                    self.vault.delete(account)
            except Exception:  # Best effort: never leave the vault in a worse state because of cleanup.
                pass

    def account(self):
        account = "test-" + uuid.uuid4().hex
        self.accounts.append(account)
        return account

    def raw(self, service, account):
        return self.vault.backend.get_password(service, account)

    def fragment_services(self, account):
        pointer = self.vault._object(self.raw(self.vault._service(account), account))
        manifest = self.vault._manifest(pointer)
        return self.vault._fragment_services(account, manifest) if manifest else []

    def test_the_platform_vault_is_used(self):
        module = type(self.vault.backend).__module__
        self.assertIn(module, EXPECTED_BACKENDS.get(sys.platform, ()), module)

    def test_small_session_roundtrip_and_delete(self):
        account = self.account()
        self.assertIsNone(self.vault.get(account))
        value = {"token": "small", "text": "Grüße"}
        self.vault.put(account, value)
        self.assertEqual(self.vault.get(account), value)
        self.assertEqual(self.fragment_services(account), [])
        self.vault.delete(account)
        self.assertIsNone(self.vault.get(account))

    def test_large_session_is_split_and_reassembled(self):
        # Windows Credential Manager holds at most 2560 bytes per entry; this needs several fragments.
        account = self.account()
        value = {"token": "x" * 10_000, "text": "Grüße " * 200}
        self.vault.put(account, value)
        self.assertEqual(self.vault.get(account), value)
        services = self.fragment_services(account)
        self.assertGreaterEqual(len(services), math.ceil(10_000 / NativeKeyring.ENTRY_CHARS))
        self.assertTrue(all(self.raw(service, account) for service in services))
        self.vault.delete(account)
        self.assertIsNone(self.vault.get(account))
        self.assertTrue(all(self.raw(service, account) is None for service in services))

    def test_replacing_a_large_session_removes_the_old_fragments(self):
        account = self.account()
        self.vault.put(account, {"token": "a" * 6000})
        old = self.fragment_services(account)
        self.vault.put(account, {"token": "b" * 6000})
        self.assertEqual(self.vault.get(account), {"token": "b" * 6000})
        self.assertTrue(old)
        self.assertTrue(all(self.raw(service, account) is None for service in old))
        self.vault.put(account, {"token": "small again"})
        self.assertEqual(self.vault.get(account), {"token": "small again"})

    def test_sign_in_renewal_and_logout_against_a_gateway(self):
        fixture = OAuthFixture()
        self.addCleanup(fixture.close)
        # Long enough to be split across vault entries, like real gateway tokens can be.
        fixture.access_token = "access-" + "a" * 4000
        fixture.refresh_token = "refresh-" + "r" * 4000
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory))
            profile = Profile("vault", fixture.origin, {"team_id": "team-a"}, allow_http=True)
            clock = Clock()
            manager = SessionManager(store, self.vault, clock=clock)
            self.accounts.append(vault_account(store.home, profile))
            with patch("llm_credential_mapping.auth.litellm_oauth.webbrowser.open", fixture.browser):
                session = manager.login(profile, lambda message: None, timeout=10)
            self.assertEqual(session.user_id, "employee")
            self.assertEqual(manager.token(profile), fixture.access_token)

            # A second manager reads the grant back from the vault, as another process would.
            other = SessionManager(store, NativeKeyring(), clock=clock)
            self.assertEqual(other.token(profile), fixture.access_token)

            fixture.access_token, fixture.refresh_token = "renewed-" + "b" * 4000, "rotated-" + "s" * 4000
            clock.now = session.expires_at - 60
            self.assertEqual(other.token(profile), fixture.access_token)
            # The renewed grant is timed by the real clock; the first manager reads it from the vault.
            clock.now = time.time()
            self.assertEqual(manager.token(profile), fixture.access_token)
            refreshes = [body for path, body in fixture.posts if path == "/token" and body["grant_type"] == "refresh_token"]
            self.assertEqual(len(refreshes), 1)

            manager.logout(profile, lambda message: None)
            self.assertEqual(manager.status(profile)["state"], "logged-out")
            revoked = [body["token"] for path, body in fixture.posts if path == "/revoke"]
            self.assertEqual(revoked, [fixture.refresh_token])
