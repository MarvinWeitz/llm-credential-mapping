import json
import tempfile
import unittest
from pathlib import Path

from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import Profile, ProfileStore, gateway_origin


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProfileStore(Path(self.temp.name))

    def test_root_url_is_normalized(self):
        self.assertEqual(gateway_origin("https://EXAMPLE.com:443/"), "https://example.com")

    def test_loopback_requires_explicit_opt_in(self):
        for url in ("http://localhost:4000", "http://127.0.0.1:4000", "http://[::1]:4000"):
            with self.subTest(url=url):
                with self.assertRaises(CredentialError):
                    gateway_origin(url)
                self.assertTrue(gateway_origin(url, True).startswith("http://"))

    def test_untrusted_or_invalid_gateway_urls_are_rejected(self):
        for url in (
            "http://remote.example", "https://user:secret@example.com",
            "https://example.com/v1", "https://example.com?token=secret",
            "https://example.com#fragment", "file:///tmp/gateway",
            "https://example.com:invalid", "https://example.com\n",
            "https://example.com\x00",
        ):
            with self.subTest(url=url), self.assertRaises(CredentialError):
                gateway_origin(url, True)

    def test_invalid_names_and_teams(self):
        for name in ("", "../project", "two projects", "a" * 65):
            with self.subTest(name=name), self.assertRaises(CredentialError):
                Profile(name, "https://example.com", "team-a")
        for team in ("", " team-a", "team-a\n"):
            with self.subTest(team=team), self.assertRaises(CredentialError):
                Profile("a", "https://example.com", team)

    def test_first_project_is_default_and_selection_persists(self):
        a = Profile("a", "https://example.com", "team-a")
        b = Profile("b", "https://example.com", "team-b")
        self.store.add(a)
        self.store.add(b)
        self.assertEqual(self.store.get(), a)
        self.store.select("b")
        self.assertEqual(self.store.get(), b)
        self.assertEqual(self.store.get("a"), a)
        self.assertEqual(self.store.list(), ("b", [a, b]))
        self.assertNotEqual(a.account, b.account)

    def test_existing_profile_cannot_be_redirected(self):
        self.store.add(Profile("a", "https://example.com", "a"))
        with self.assertRaises(CredentialError):
            self.store.add(Profile("a", "https://attacker.example", "b"))
        self.assertEqual(self.store.get().gateway, "https://example.com")

    def test_missing_or_unknown_project_is_an_error(self):
        with self.assertRaises(CredentialError):
            self.store.get()
        with self.assertRaises(CredentialError):
            self.store.select("missing")

    def test_invalid_config_is_not_silently_replaced(self):
        for value in (
            {"version": 2, "active": None, "projects": {}},
            {"version": True, "active": None, "projects": {}},
            {"version": 1, "active": [], "projects": {}},
            {"version": 1, "active": "missing", "projects": {}},
            {"version": 1, "active": None, "projects": {"a": {"gateway": "https://example.com"}}},
        ):
            self.store.path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(CredentialError):
                self.store.list()

    def test_corrupt_json_is_reported(self):
        self.store.path.write_text("{", encoding="utf-8")
        with self.assertRaises(CredentialError):
            self.store.list()
