import json
import tempfile
import unittest
from pathlib import Path

from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.gateways.litellm import OUTPUTS
from virtual_key_mapper.labels import LAUNCH_VARIABLE, MISSING_PHRASE, NO_LABELS_NOTICE, parse_definitions
from virtual_key_mapper.profiles import Profile, ProfileStore
from virtual_key_mapper.repositories import Checkout, RepositoryStore
from virtual_key_mapper.resolve import (
    find_label_file, label_config, missing_error, read_label_file, resolve_labels, show_document, write_label_file,
)
from virtual_key_mapper.rules import Rules

DOCUMENT = {
    "version": "2.0",
    "labels": [
        {"key": "customer", "required": True, "values": [{"id": "acme", "name": "Acme"}, {"id": "globex"}]},
        {"key": "project"},
    ],
    "rules": [
        {"match": "github.com/acme-*/**", "labels": {"customer": "acme"}},
        {"match": "**", "labels": {"project": "shared"}},
    ],
}


class LabelFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.nested = self.home / "src" / "acme" / "repo" / "sub"
        self.nested.mkdir(parents=True)

    def test_nearest_file_wins_and_the_search_stops_at_home(self):
        self.assertIsNone(find_label_file(self.nested, self.home))
        (self.home / ".vkm").write_text('[labels]\ncustomer = "ignored"\n', encoding="utf-8")
        (self.root / ".vkm").write_text('[labels]\ncustomer = "ignored"\n', encoding="utf-8")
        self.assertIsNone(find_label_file(self.nested, self.home))
        acme = write_label_file(self.home / "src" / "acme", {"customer": "acme"}, self.home)
        self.assertEqual(find_label_file(self.nested, self.home), acme)
        repo = write_label_file(self.home / "src" / "acme" / "repo", {"project": "web"}, self.home)
        self.assertEqual(find_label_file(self.nested, self.home), repo)
        self.assertEqual(read_label_file(repo), {"project": "web"})

    def test_home_and_its_parents_are_refused_for_writing(self):
        for folder in (self.home, self.root):
            with self.subTest(folder=folder), self.assertRaises(CredentialError):
                write_label_file(folder, {"customer": "acme"}, self.home)

    def test_writing_merges_keys_and_keeps_explicit_empty_values(self):
        folder = self.home / "src"
        path = write_label_file(folder, {"customer": "acme", "project": "web"}, self.home)
        write_label_file(folder, {"project": "", "team": 'a"b'}, self.home)
        self.assertEqual(read_label_file(path), {"customer": "acme", "project": "", "team": 'a"b'})
        self.assertTrue(path.read_text(encoding="utf-8").startswith("# Labels for AI tool requests"))
        too_large = {f"k{index}": "z" * 256 for index in range(16)}
        for labels in ({"Customer": "x"}, {"customer": "a,b"}, too_large):
            with self.subTest(labels=list(labels)[:2]), self.assertRaises(CredentialError):
                write_label_file(folder, labels, self.home)
        self.assertEqual(read_label_file(path)["customer"], "acme")

    def test_folders_outside_home_search_up_to_the_root(self):
        outside = self.root / "work" / "repo"
        outside.mkdir(parents=True)
        path = write_label_file(self.root / "work", {"customer": "work"}, self.home)
        self.assertEqual(find_label_file(outside, self.home), path)

    def test_invalid_files_are_errors_not_silently_ignored(self):
        path = self.nested / ".vkm"
        for content in (
            "customer = 'acme'", "[labels]\ncustomer = 'a,b'", "[labels]\ncustomer = 1", "[labels]\nCustomer = 'x'",
            "[labels]\ncustomer = 'a'\n[other]\nx = 'b'", "[labels.customer]\nx = 'a'", "not toml",
            "[labels]\ncustomer = ' a'", "[labels]\ncustomer = '" + "a" * 5000 + "'",
            "[labels]\ncustomer = " + "[" * 2000,
        ):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content[:30]), self.assertRaises(CredentialError):
                read_label_file(path)
        path.write_text("[labels]\n", encoding="utf-8")
        self.assertEqual(read_label_file(path), {})


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.store = ProfileStore(self.root / "config")
        self.profile = Profile("a", "https://gateway.example", {"team_id": "t"})
        self.directory = self.home / "repo"
        self.directory.mkdir(parents=True)
        self.checkout = Checkout(self.directory, "github.com/me/fork", "github.com/acme-eu/web")
        self.rules = Rules.parse(DOCUMENT, "test")
        self.config = label_config(self.profile, OUTPUTS, self.rules)
        self.bindings = RepositoryStore(self.store)

    def resolve(self, checkout=None, explicit=None, environ=None, config=None, directory=None, **options):
        return resolve_labels(
            self.store, self.profile, config or self.config, directory or self.directory, checkout, explicit=explicit,
            environ=environ or {}, home=self.home, **options,
        )

    def write(self, **labels):
        write_label_file(self.directory, labels, self.home)

    @staticmethod
    def found(attribution, key):
        label = attribution.get(key)
        return label.value, label.source

    def test_order_explicit_environment_local_file_rules_per_key(self):
        found = self.resolve(self.checkout)
        self.assertEqual((self.found(found, "customer"), self.found(found, "project")), (("acme", "rules"), ("shared", "rules")))
        self.assertIn("github.com/acme-eu/web", found.get("customer").detail)
        self.assertEqual((found.repository, found.complete), ("github.com/me/fork", True))
        self.write(project="web")
        found = self.resolve(self.checkout)
        self.assertEqual((self.found(found, "customer"), self.found(found, "project")), (("acme", "rules"), ("web", "file")))
        self.write(customer="globex")
        self.assertEqual(self.found(self.resolve(self.checkout), "customer"), ("globex", "file"))
        self.bindings.bind(self.profile.gateway, "github.com/acme-eu/web", {"customer": "upstream", "project": "up"})
        self.bindings.bind(self.profile.gateway, "github.com/me/fork", {"customer": "mine"})
        found = self.resolve(self.checkout)
        self.assertEqual((self.found(found, "customer"), self.found(found, "project")), (("mine", "local"), ("up", "local")))
        self.assertEqual(found.get("project").detail, "repo bind for github.com/acme-eu/web")
        environment = {"VKM_LABEL_CUSTOMER": "env", "VKM_LABEL_PROJECT": ""}
        found = self.resolve(self.checkout, environ=environment)
        self.assertEqual((self.found(found, "customer"), found.get("customer").detail), (("env", "env"), "VKM_LABEL_CUSTOMER"))
        self.assertEqual(self.found(found, "project"), ("up", "local"), "an empty variable counts as unset")
        found = self.resolve(self.checkout, {"customer": "cli"}, environment)
        self.assertEqual((self.found(found, "customer"), found.sources), (("cli", "explicit"), {"customer": "explicit", "project": "local"}))

    def test_no_value_on_purpose_stops_the_search(self):
        self.write(project="")
        found = self.resolve(self.checkout)
        self.assertEqual((self.found(found, "project"), found.values), ((None, "file"), {"customer": "acme"}))
        found = self.resolve(self.checkout, {"customer": ""})
        self.assertEqual((found.missing, self.found(found, "customer")), (("customer",), (None, "explicit")))
        rules = Rules.parse({**DOCUMENT, "rules": [
            {"match": "**", "labels": {"customer": None}}, {"match": "**", "labels": {"customer": "acme"}},
        ]}, "t")
        found = self.resolve(self.checkout, config=label_config(self.profile, OUTPUTS, rules))
        self.assertEqual((found.missing, self.found(found, "customer")), (("customer",), (None, "rules")))

    def test_the_nearest_file_wins_as_a_whole(self):
        parent = self.home / "src"
        nested = parent / "repo"
        nested.mkdir(parents=True)
        write_label_file(parent, {"customer": "globex", "project": "parent"}, self.home)
        write_label_file(nested, {"customer": "acme"}, self.home)
        found = self.resolve(directory=nested)
        self.assertEqual((found.get("project").value, found.get("customer").value), (None, "acme"))

    def test_the_launch_marker_is_not_a_source(self):
        self.write(customer="globex")
        found = self.resolve(self.checkout, environ={LAUNCH_VARIABLE: '{"customer":"acme"}'})
        self.assertEqual(self.found(found, "customer"), ("globex", "file"))

    def test_outside_git_only_explicit_environment_and_files_apply(self):
        found = self.resolve()
        self.assertEqual((found.missing, found.repository), (("customer",), None))
        self.write(customer="acme")
        found = self.resolve()
        self.assertEqual((self.found(found, "customer"), found.repository), (("acme", "file"), None))
        upstream_only = Checkout(self.directory, None, "github.com/acme-eu/web")
        self.assertIsNone(self.resolve(upstream_only).repository, "metadata names a repository only with an origin")

    def test_unknown_keys_are_ignored_and_listed_but_refused_on_the_command_line(self):
        self.write(customer="acme", team="t")
        self.bindings.bind(self.profile.gateway, "github.com/me/fork", {"cost": "c"})
        found = self.resolve(self.checkout, environ={
            "VKM_LABEL_REGION": "eu", "VKM_LABEL_BAD": "a,b", "VKM_LABEL_EMPTY": "", "VKM_LABEL_project": "cased",
        })
        self.assertEqual(
            sorted((item.key, item.value, item.source, item.reason) for item in found.ignored),
            [("VKM_LABEL_BAD", None, "env", "unknown label"), ("VKM_LABEL_REGION", "eu", "env", "unknown label"),
             ("VKM_LABEL_project", "cased", "env", "unknown label"), ("cost", "c", "local", "unknown label"),
             ("team", "t", "file", "unknown label")],
        )
        self.assertEqual(found.values, {"customer": "acme", "project": "shared"})
        with self.assertRaises(CredentialError) as error:
            self.resolve(self.checkout, {"custmer": "acme"})
        self.assertIn("custmer", str(error.exception))
        found = self.resolve(self.checkout, {"custmer": "acme"}, unknown_explicit="ignore")
        self.assertEqual(found.ignored[0].to_dict(), {
            "key": "custmer", "value": "acme", "source": "explicit", "detail": "--label", "reason": "unknown label",
        })

    def test_sources_limit_where_a_label_may_come_from(self):
        labels = [{**DOCUMENT["labels"][0], "sources": ["explicit", "rules"]}, DOCUMENT["labels"][1]]
        config = label_config(self.profile, OUTPUTS, Rules.parse({**DOCUMENT, "labels": labels}, "t"))
        self.write(customer="globex")
        found = self.resolve(self.checkout, environ={"VKM_LABEL_CUSTOMER": "env"}, config=config)
        self.assertEqual(self.found(found, "customer"), ("acme", "rules"))
        self.assertEqual(
            [(item.source, item.reason) for item in found.ignored], [("env", "source not allowed"), ("file", "source not allowed")],
        )
        self.assertEqual(self.found(self.resolve(self.checkout, {"customer": "cli"}, config=config), "customer"), ("cli", "explicit"))
        labels = [{**DOCUMENT["labels"][0], "sources": ["explicit", "env"]}, DOCUMENT["labels"][1]]
        config = label_config(self.profile, OUTPUTS, Rules.parse({**DOCUMENT, "labels": labels}, "t"))
        found = self.resolve(self.checkout, config=config)
        self.assertEqual((self.found(found, "customer"), found.missing), ((None, None), ("customer",)))
        self.assertIn(("customer", "acme", "rules", "source not allowed"), [
            (item.key, item.value, item.source, item.reason) for item in found.ignored
        ])

    def test_invalid_and_unlisted_values_are_errors_naming_their_source(self):
        for explicit, environ in (({"customer": "a,b"}, None), (None, {"VKM_LABEL_CUSTOMER": " x"})):
            with self.subTest(explicit=explicit, environ=environ), self.assertRaises(CredentialError) as error:
                self.resolve(self.checkout, explicit, environ)
            self.assertIn("--label" if explicit else "VKM_LABEL_CUSTOMER", str(error.exception))
        labels = [{**DOCUMENT["labels"][0], "strict": True}, DOCUMENT["labels"][1]]
        config = label_config(self.profile, OUTPUTS, Rules.parse({**DOCUMENT, "labels": labels}, "t"))
        self.write(customer="unlisted")
        with self.assertRaises(CredentialError) as error:
            self.resolve(self.checkout, config=config)
        self.assertIn(".vkm", str(error.exception))
        self.assertIn("labels values customer", str(error.exception))
        self.assertEqual(self.resolve(self.checkout, {"customer": "globex"}, config=config).values["customer"], "globex")

    def test_profile_labels_resolve_like_central_ones(self):
        profile = Profile("a", "https://gateway.example", {"team_id": "t"}, labels=parse_definitions([{"key": "team"}], "profile", "t"))
        config = label_config(profile, OUTPUTS, self.rules)
        self.write(team="blue")
        found = self.resolve(self.checkout, config=config)
        self.assertEqual(config.headers(found)["x-litellm-tags"], "customer:acme,project:shared,team:blue")

    def test_missing_message_names_keys_and_ways_out(self):
        found = self.resolve()
        message = str(missing_error(found, self.directory, None))
        self.assertTrue(message.startswith(MISSING_PHRASE + " customer"))
        for expected in (str(self.directory), "repo bind --shared --label", "run --label", "Nothing was sent"):
            self.assertIn(expected, message)
        self.assertIn("github.com/me/fork", str(missing_error(found, self.directory, self.checkout)))

    def test_show_document_shape(self):
        incomplete = show_document(self.config, self.resolve(), self.directory, None)
        self.assertEqual(set(incomplete), {"labels", "missing", "ignored", "repository", "complete", "headers", "message", "notices"})
        self.assertEqual((incomplete["complete"], incomplete["headers"], incomplete["missing"]), (False, {}, ["customer"]))
        self.assertTrue(incomplete["message"].startswith(MISSING_PHRASE))
        complete = show_document(self.config, self.resolve(self.checkout), self.directory, self.checkout)
        self.assertEqual(complete["labels"][0], {
            "key": "customer", "value": "acme", "name": "Acme", "source": "rules",
            "detail": "rule github.com/acme-*/** matched github.com/acme-eu/web", "required": True, "description": None,
        })
        self.assertEqual((complete["complete"], complete["message"], complete["repository"]), (True, None, "github.com/me/fork"))
        self.assertEqual(complete["headers"]["x-litellm-tags"], "customer:acme,project:shared")
        json.dumps(complete)
        empty = label_config(self.profile, OUTPUTS, None)
        nothing = show_document(empty, self.resolve(self.checkout, config=empty), self.directory, self.checkout)
        self.assertEqual((nothing["labels"], nothing["message"]), ([], NO_LABELS_NOTICE))
        self.assertEqual(list(nothing["headers"]), ["x-litellm-spend-logs-metadata"])

    def test_header_errors_make_the_document_incomplete(self):
        self.write(customer="default")
        rules = Rules.parse({**DOCUMENT, "outputs": {"litellm": {"tags": {"format": "${customer}"}}}}, "t")
        config = label_config(self.profile, OUTPUTS, rules)
        shown = show_document(config, self.resolve(self.checkout, config=config), self.directory, self.checkout)
        self.assertEqual((shown["complete"], shown["headers"]), (False, {}))
        self.assertIn("reserved", shown["message"])


if __name__ == "__main__":
    unittest.main()
