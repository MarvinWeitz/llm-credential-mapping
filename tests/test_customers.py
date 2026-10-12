import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from llm_credential_mapping.customers import (
    RULES_CACHE_SECONDS, Rules, RulesLoader, find_customer_file, read_customer_file, resolve_customer,
    unmapped_error, write_customer_file,
)
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.repositories import Checkout, RepositoryStore, repository_identity

DOCUMENT = {
    "version": 1,
    "customers": [{"id": "customer-acme", "name": "Acme"}],
    "rules": [
        {"match": "github.com/acme-*/**", "customer": "customer-acme"},
        {"match": "github.com/**", "customer": "customer-other"},
    ],
}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class CustomerFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.nested = self.home / "src" / "acme" / "repo" / "sub"
        self.nested.mkdir(parents=True)

    def test_nearest_file_wins_and_the_search_stops_at_home(self):
        self.assertIsNone(find_customer_file(self.nested, self.home))
        for folder in (self.root, self.home):
            write_customer_file(folder, "customer-ignored")
        self.assertIsNone(find_customer_file(self.nested, self.home))
        acme = write_customer_file(self.home / "src" / "acme", "customer-acme")
        self.assertEqual(find_customer_file(self.nested, self.home), acme)
        repo = write_customer_file(self.home / "src" / "acme" / "repo", "customer-repo")
        self.assertEqual(find_customer_file(self.nested, self.home), repo)
        self.assertEqual(read_customer_file(repo), "customer-repo")

    def test_folders_outside_home_search_up_to_the_root(self):
        outside = self.root / "work" / "repo"
        outside.mkdir(parents=True)
        path = write_customer_file(self.root / "work", "customer-work")
        self.assertEqual(find_customer_file(outside, self.home), path)

    def test_invalid_files_are_errors_not_silently_ignored(self):
        path = self.nested / ".llm-customer"
        for content in (
            "customer = 'a,b'", "customer = 1", "other = 'x'", "customer = 'a'\nextra = 'b'", "not toml",
            "customer = '" + "a" * 5000 + "'",
        ):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content[:30]), self.assertRaises(CredentialError):
                read_customer_file(path)


class RulesTests(unittest.TestCase):
    def test_globs_match_whole_lowercase_identities_in_order_origin_first(self):
        rules = Rules.parse(DOCUMENT, "test")
        self.assertEqual(rules.match(("github.com/acme-eu/web",))[0], "customer-acme")
        self.assertEqual(rules.match(("github.com/acme-eu/deep/web",))[0], "customer-acme")
        self.assertEqual(rules.match(("github.com/acme/web",))[0], "customer-other")
        self.assertIsNone(rules.match(("gitlab.com/acme-eu/web",)))
        self.assertEqual(rules.match(("gitlab.com/me/fork", "github.com/acme-eu/web"))[2], "github.com/acme-eu/web")
        fork = rules.match(("github.com/me/fork", "github.com/acme-eu/web"))
        self.assertEqual(fork[:2], ("customer-acme", "github.com/acme-*/**"), "rule order beats origin before upstream")
        self.assertEqual(rules.match(("github.com/me/fork", "gitlab.com/x/y"))[0], "customer-other")
        single = Rules.parse({"version": 1, "rules": [{"match": "GitHub.com/a?c/*", "customer": "c"}]}, "test")
        self.assertIsNotNone(single.match(("github.com/abc/repo",)))
        self.assertIsNone(single.match(("github.com/abc/repo/x",)))
        self.assertIsNone(single.match(("github.com/a/c/repo",)))

    def test_rules_match_azure_names_with_spaces_as_repo_show_prints_them(self):
        identity = repository_identity("https://dev.azure.com/org/My%20Project/_git/My%20Repo")
        rules = Rules.parse({"version": 1, "rules": [{"match": "dev.azure.com/org/My Project/**", "customer": "c"}]}, "test")
        self.assertEqual(rules.match((identity,))[:2], ("c", "dev.azure.com/org/my project/**"))

    def test_invalid_documents_are_rejected(self):
        for document in (
            [], {"version": 2}, {"version": 1, "extra": []}, {"version": 1, "rules": [{"match": "x"}]},
            {"version": 1, "rules": [{"match": "a\tb", "customer": "c"}]},
            {"version": 1, "rules": [{"match": "x", "customer": "a,b"}]},
            {"version": 1, "customers": [{"name": "no id"}]},
            {"version": 1, "customers": [{"id": "c", "tag": "old field"}]},
        ):
            with self.subTest(document=document), self.assertRaises(CredentialError):
                Rules.parse(document, "test")


class RulesServer:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.document = DOCUMENT
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization"), self.headers.get("If-None-Match")))
                if owner.status != 200:
                    self.send_response(owner.status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = json.dumps(owner.document).encode()
                self.send_response(200)
                self.send_header("ETag", '"v1"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RulesLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProfileStore(Path(self.temp.name))
        self.server = RulesServer()
        self.addCleanup(self.server.close)
        self.clock = Clock()
        self.tokens = []

    def loader(self, gateway, rules):
        profile = Profile("a", gateway, {"team_id": "t"}, allow_http=True, rules=rules)

        def token():
            self.tokens.append(1)
            return "gateway-secret"

        return RulesLoader(self.store, profile, token, self.clock)

    def test_cache_is_fresh_for_an_hour_then_revalidated_with_its_etag(self):
        load = self.loader("http://localhost:1", self.server.origin + "/rules.json")
        self.assertEqual(load().rules[0][1], "customer-acme")
        self.assertEqual(load().customers[0].name, "customer-acme")
        self.assertEqual(len(self.server.requests), 1)
        self.clock.now += RULES_CACHE_SECONDS + 1
        self.server.status = 304
        self.assertEqual(load().rules[0][1], "customer-acme")
        self.assertEqual(self.server.requests[-1][2], '"v1"')
        self.server.status = 200
        self.assertEqual(len(self.server.requests), 2)
        load()
        self.assertEqual(len(self.server.requests), 2, "a 304 renews the cache's freshness")

    def test_stale_cache_is_used_during_an_outage_but_not_without_a_cache(self):
        load = self.loader("http://localhost:1", self.server.origin + "/rules.json")
        load()
        self.clock.now += RULES_CACHE_SECONDS + 1
        self.server.status = 503
        self.assertEqual(load().rules[0][1], "customer-acme")
        other = self.loader("http://localhost:1", self.server.origin + "/other.json")
        with self.assertRaises(CredentialError) as error:
            other()
        self.assertIn("503", str(error.exception))

    def test_stale_cache_is_used_when_a_newly_published_document_is_invalid(self):
        load = self.loader("http://localhost:1", self.server.origin + "/rules.json")
        load()
        self.clock.now += RULES_CACHE_SECONDS + 1
        self.server.document = {"version": 1, "rules": [{"match": "x", "customer": "a,b"}]}
        self.assertEqual(load().rules[0][1], "customer-acme")
        other = self.loader("http://localhost:1", self.server.origin + "/other.json")
        with self.assertRaises(CredentialError):
            other()

    def test_credential_is_only_sent_to_the_gateway_origin(self):
        self.loader("http://localhost:1", self.server.origin + "/elsewhere.json")()
        self.assertEqual((self.server.requests[-1][1], self.tokens), (None, []))
        self.loader(self.server.origin, self.server.origin + "/gateway.json")()
        self.assertEqual((self.server.requests[-1][1], len(self.tokens)), ("Bearer gateway-secret", 1))

    def test_corrupt_cache_is_refetched(self):
        load = self.loader("http://localhost:1", self.server.origin + "/rules.json")
        load()
        for path in (Path(self.temp.name) / "rules-cache").iterdir():
            path.write_text('{"url": 1}', encoding="utf-8")
        load()
        self.assertEqual(len(self.server.requests), 2)

    def test_local_rules_file(self):
        path = Path(self.temp.name) / "rules.json"
        path.write_text(json.dumps(DOCUMENT), encoding="utf-8")
        self.assertEqual(self.loader("http://localhost:1", str(path))().rules[0][1], "customer-acme")
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(CredentialError):
            self.loader("http://localhost:1", str(path))()


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = ProfileStore(self.root / "config")
        self.profile = Profile("a", "https://gateway.example", {"team_id": "t"})
        self.directory = self.root / "home" / "repo"
        self.directory.mkdir(parents=True)
        self.checkout = Checkout(self.directory, "github.com/me/fork", "github.com/acme-eu/web")
        self.rules = Rules.parse(DOCUMENT, "test")

    def resolve(self, checkout=None, explicit=None, environ=None, rules=None):
        return resolve_customer(
            self.store, self.profile, self.directory, checkout, explicit=explicit,
            rules=lambda: rules, environ=environ or {}, home=self.root / "home",
        )

    def test_order_explicit_environment_local_file_rules(self):
        self.assertIsNone(self.resolve(self.checkout))
        found = self.resolve(self.checkout, rules=self.rules)
        self.assertEqual((found.customer, found.source, found.repository), ("customer-acme", "rules", "github.com/me/fork"))
        self.assertIn("github.com/acme-eu/web", found.detail)
        write_customer_file(self.directory, "customer-file")
        self.assertEqual(self.resolve(self.checkout, rules=self.rules).source, "file")
        RepositoryStore(self.store).bind(self.profile.gateway, "github.com/acme-eu/web", "customer-upstream")
        self.assertEqual(self.resolve(self.checkout, rules=self.rules).customer, "customer-upstream")
        RepositoryStore(self.store).bind(self.profile.gateway, "github.com/me/fork", "customer-mine")
        self.assertEqual(self.resolve(self.checkout, rules=self.rules).customer, "customer-mine")
        environment = {"LLM_CREDENTIALS_CUSTOMER": "customer-env"}
        self.assertEqual(self.resolve(self.checkout, environ=environment).source, "explicit")
        self.assertEqual(self.resolve(self.checkout, "customer-cli", environment).customer, "customer-cli")

    def test_outside_git_only_explicit_and_files_apply(self):
        self.assertIsNone(self.resolve(rules=self.rules))
        write_customer_file(self.directory, "customer-file")
        found = self.resolve(rules=self.rules)
        self.assertEqual((found.customer, found.repository), ("customer-file", None))

    def test_invalid_explicit_customer_is_an_error(self):
        with self.assertRaises(CredentialError):
            self.resolve(explicit="a,b")

    def test_unmapped_message_names_the_repository_or_folder_and_the_ways_out(self):
        self.assertIn("github.com/me/fork", str(unmapped_error(self.directory, self.checkout)))
        message = str(unmapped_error(self.directory, None))
        for expected in (str(self.directory), "repo bind --shared", "--customer"):
            self.assertIn(expected, message)
