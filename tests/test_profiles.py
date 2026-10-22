import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.labels import LabelDefinition, OutputOverride, definitions_from_dict
from virtual_key_mapper.profiles import Profile, ProfileStore, gateway_origin, with_label, with_output, without_label

DEFINITIONS = {
    "labels": [{"key": "team", "required": True, "values": [{"id": "blue", "name": "Blue"}], "strict": True}, {"key": "project"}],
    "outputs": {"tags": {"format": ["team:${team}", "team:${team}/${project}"]}, "metadata": {"enabled": False}},
}


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

    def test_gateway_under_a_path_prefix_is_rejected_explicitly(self):
        for url in ("https://llm.corp.example/litellm", "https://llm.corp.example/litellm/"):
            with self.subTest(url=url), self.assertRaises(CredentialError) as result:
                Profile("a", url, {"team_id": "team-a"})
            self.assertIn("served at the host root", str(result.exception))
            self.assertIn("'/litellm", str(result.exception))

    def test_invalid_names_and_teams(self):
        for name in ("", "../profile", "two profiles", "a" * 65):
            with self.subTest(name=name), self.assertRaises(CredentialError):
                Profile(name, "https://example.com", {"team_id": "team-a"})
        for team in ("", " team-a", "team-a\n"):
            with self.subTest(team=team), self.assertRaises(CredentialError):
                Profile("a", "https://example.com", {"team_id": team})

    def test_gateway_and_sign_in_kinds_and_their_settings_are_validated(self):
        for args in (
            ({"team_id": "t"}, "unknown", "litellm-sso"),
            ({"team_id": "t"}, "litellm", "unknown"),
            ({}, "litellm", "litellm-sso"),
            ({"team_id": "t", "extra": "x"}, "litellm", "litellm-sso"),
            ({"team_id": 7}, "litellm", "litellm-sso"),
            ("t", "litellm", "litellm-sso"),
        ):
            with self.subTest(args=args), self.assertRaises(CredentialError):
                Profile("a", "https://example.com", *args)

    def test_account_changes_with_gateway_or_sign_in_settings(self):
        a = Profile("a", "https://example.com", {"team_id": "team-a"})
        self.assertEqual(a.account, Profile("a", "https://EXAMPLE.com/", {"team_id": "team-a"}).account)
        for other in (
            Profile("a", "https://other.example", {"team_id": "team-a"}),
            Profile("a", "https://example.com", {"team_id": "team-b"}),
        ):
            self.assertNotEqual(a.account, other.account)

    def test_first_profile_is_default_and_selection_persists(self):
        a = Profile("a", "https://example.com", {"team_id": "team-a"})
        b = Profile("b", "https://example.com", {"team_id": "team-b"})
        self.store.add(a)
        self.store.add(b)
        self.assertEqual(self.store.get(), a)
        self.store.select("b")
        self.assertEqual(self.store.get(), b)
        self.assertEqual(self.store.get("a"), a)
        self.assertEqual(self.store.list(), ("b", [a, b]))
        self.assertNotEqual(a.account, b.account)

    def test_existing_profile_cannot_be_redirected(self):
        self.store.add(Profile("a", "https://example.com", {"team_id": "a"}))
        with self.assertRaises(CredentialError):
            self.store.add(Profile("a", "https://attacker.example", {"team_id": "b"}))
        self.assertEqual(self.store.get().gateway, "https://example.com")

    def test_missing_or_unknown_profile_is_an_error(self):
        with self.assertRaises(CredentialError):
            self.store.get()
        with self.assertRaises(CredentialError):
            self.store.select("missing")

    def test_invalid_config_is_not_silently_replaced(self):
        for value in (
            {"version": 2, "active": None, "profiles": {}},
            {"version": True, "active": None, "profiles": {}},
            {"version": 1, "active": [], "profiles": {}},
            {"version": 1, "active": "missing", "profiles": {}},
            {"version": 1, "active": None, "profiles": {"a": {"gateway": "https://example.com"}}},
        ):
            self.store.path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(CredentialError):
                self.store.list()

    def test_labels_and_outputs_persist_in_order_and_are_not_part_of_the_account(self):
        labels, outputs = definitions_from_dict(DEFINITIONS)
        profile = Profile("a", "https://example.com", {"team_id": "t"}, labels=labels, outputs=outputs)
        self.assertEqual(profile.definitions(), DEFINITIONS)
        self.assertEqual(profile.account, Profile("a", "https://example.com", {"team_id": "t"}).account)
        self.store.add(profile)
        self.assertEqual(self.store.get("a"), profile)
        saved = json.loads(self.store.path.read_text(encoding="utf-8"))["profiles"]["a"]
        self.assertEqual((saved["labels"], saved["outputs"]), (DEFINITIONS["labels"], DEFINITIONS["outputs"]))
        plain = Profile("b", "https://example.com", {"team_id": "t"})
        self.assertNotIn("labels", plain.to_dict())

    def test_invalid_labels_or_outputs_are_refused(self):
        for labels, outputs in (
            ((LabelDefinition("a"), LabelDefinition("a")), {}),
            ((LabelDefinition("a-b"), LabelDefinition("a_b")), {}),
            ((LabelDefinition("a", origin="rules"),), {}),
            ([LabelDefinition("a")], {}),
            ((), {"end_user": OutputOverride(("x",))}),
            ((), {"metadata": OutputOverride(("x",))}),
            ((), {"tags": OutputOverride(("${a}", "${b}"))}),
            ((), {"tags": {"enabled": False}}),
        ):
            with self.subTest(labels=labels, outputs=outputs), self.assertRaises(CredentialError):
                Profile("a", "https://example.com", {"team_id": "t"}, labels=labels, outputs=outputs)
        for item in (
            {"labels": [{"key": "a", "requried": True}]}, {"labels": {"a": {}}}, {"outputs": {"tags": {"format": "$x"}}},
            {"outputs": {"tags": {"enabled": True}}}, {"extra": 1},
        ):
            stored = {"gateway": {"type": "litellm", "url": "https://example.com", "allow_http": False},
                      "auth": {"type": "litellm-sso", "team_id": "t"}, **item}
            self.store.path.write_text(json.dumps({"version": 1, "active": "a", "profiles": {"a": stored}}), encoding="utf-8")
            with self.subTest(item=item), self.assertRaises(CredentialError):
                self.store.list()

    def test_update_changes_labels_outputs_and_rules_but_not_the_sign_in(self):
        self.store.add(Profile("a", "https://example.com", {"team_id": "t"}))
        profile = self.store.update("a", lambda p: with_label(p, LabelDefinition("team")))
        profile = self.store.update("a", lambda p: with_label(p, LabelDefinition("project")))
        profile = self.store.update("a", lambda p: with_label(p, LabelDefinition("team", required=True)))
        self.assertEqual([(item.key, item.required) for item in profile.labels], [("team", True), ("project", False)])
        profile = self.store.update("a", lambda p: with_output(p, "metadata", OutputOverride(None, False)))
        self.assertEqual(self.store.get("a").outputs, {"metadata": OutputOverride(None, False)})
        profile = self.store.update("a", lambda p: with_output(without_label(p, "project"), "metadata", None))
        self.assertEqual((profile.outputs, [item.key for item in profile.labels]), ({}, ["team"]))
        with self.assertRaises(CredentialError):
            without_label(profile, "project")
        self.assertEqual(self.store.update("a", lambda p: replace(p, rules="https://rules.example/r.json")).rules, "https://rules.example/r.json")
        with self.assertRaises(CredentialError):
            self.store.update("a", lambda p: replace(p, auth={"team_id": "other"}))
        with self.assertRaises(CredentialError):
            self.store.update("missing", lambda p: p)
        self.assertEqual(self.store.get("a").auth, {"team_id": "t"})

    def test_corrupt_json_is_reported(self):
        for text in ("{", "[" * 100_000):  # Too deeply nested JSON is corrupt too, not a RecursionError.
            self.store.path.write_text(text, encoding="utf-8")
            with self.subTest(text=text[:10]), self.assertRaises(CredentialError):
                self.store.list()
