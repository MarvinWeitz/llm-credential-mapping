import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.gateways.litellm import OUTPUTS
from virtual_key_mapper.profiles import Profile, ProfileStore
from virtual_key_mapper.repositories import repository_identity
from virtual_key_mapper.resolve import label_config, load_config
from virtual_key_mapper.rules import RULES_CACHE_SECONDS, Glob, Rules, RulesLoader

LABELS = [
    {"key": "customer", "required": True, "values": [{"id": "acme", "name": "Acme"}, {"id": "other"}]},
    {"key": "project"},
]
DOCUMENT = {
    "version": "2.0",
    "labels": LABELS,
    "outputs": {"litellm": {"tags": {"format": ["${customer}", "${customer}:${project}"]}}},
    "rules": [
        {"match": "github.com/acme-*/**", "labels": {"customer": "acme"}},
        {"match": "github.com/**", "labels": {"customer": "other", "project": "web"}},
        {"match": "**", "labels": {"project": None}},
    ],
}


def document(**changes):
    return {**DOCUMENT, **changes}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class RulesTests(unittest.TestCase):
    def test_each_key_comes_from_the_first_matching_rule_that_sets_it(self):
        rules = Rules.parse(DOCUMENT, "test")
        self.assertEqual(rules.match(("github.com/acme-eu/web",)), {
            "customer": ("acme", "github.com/acme-*/**", "github.com/acme-eu/web"),
            "project": ("web", "github.com/**", "github.com/acme-eu/web"),
        })
        self.assertEqual(rules.match(("gitlab.com/acme-eu/web",)), {"project": (None, "**", "gitlab.com/acme-eu/web")})
        self.assertEqual(rules.match(()), {})

    def test_globs_match_whole_lowercase_identities_in_order_origin_first(self):
        rules = Rules.parse(DOCUMENT, "test")
        self.assertEqual(rules.match(("github.com/acme-eu/deep/web",))["customer"][0], "acme")
        self.assertEqual(rules.match(("github.com/acme/web",))["customer"][0], "other")
        self.assertEqual(rules.match(("gitlab.com/me/fork", "github.com/acme-eu/web"))["customer"][2], "github.com/acme-eu/web")
        fork = rules.match(("github.com/me/fork", "github.com/acme-eu/web"))["customer"]
        self.assertEqual(fork[:2], ("acme", "github.com/acme-*/**"), "rule order beats origin before upstream")
        single = Rules.parse(document(rules=[{"match": "GitHub.com/a?c/*", "labels": {"project": "p"}}]), "test")
        self.assertTrue(single.match(("github.com/abc/repo",)))
        self.assertFalse(single.match(("github.com/abc/repo/x",)))
        self.assertFalse(single.match(("github.com/a/c/repo",)))

    def test_globs_match_without_backtracking(self):
        for pattern, identity, expected in (
            ("**", "", True), ("**", "a/b", True), ("a/**/c", "a/c", False), ("a/**/c", "a//c", True),
            ("a/**/c", "a/b/x/c", True), ("a**c", "a/b/c", True), ("*", "", True), ("*", "a/b", False),
            ("a*c", "ac", True), ("a*c", "abbc", True), ("a*c", "a/c", False), ("a?c", "abc", True), ("a?c", "a/c", False),
            ("a?c", "ac", False), ("*/x", "a/x", True), ("*/x", "a/b/x", False), ("a.c", "abc", False),
        ):
            with self.subTest(pattern=pattern, identity=identity):
                self.assertEqual(Glob.compile(pattern).fullmatch(identity), expected)
        rules = Rules.parse(document(rules=[{"match": "*a" * 15 + "*b", "labels": {"project": "p"}}]), "test")
        start = time.monotonic()
        self.assertEqual(rules.match(("a" * 2000,)), {})
        self.assertEqual(rules.match(("a" * 2000 + "b",))["project"][0], "p")
        self.assertLess(time.monotonic() - start, 5)

    def test_rules_match_azure_names_with_spaces_as_repo_show_prints_them(self):
        identity = repository_identity("https://dev.azure.com/org/My%20Project/_git/My%20Repo")
        rules = Rules.parse(document(rules=[{"match": "dev.azure.com/org/My Project/**", "labels": {"project": "p"}}]), "test")
        self.assertEqual(rules.match((identity,))["project"][:2], ("p", "dev.azure.com/org/my project/**"))

    def test_versions(self):
        self.assertEqual(Rules.parse({"version": "2.0"}, "test").notices, ())
        newer = Rules.parse(
            document(version="2.7", extra=1, labels=[{**LABELS[0], "new": True}, LABELS[1]],
                     rules=[{"match": "**", "labels": {"customer": "acme"}, "when": "x"}]),
            "test",
        )
        self.assertEqual(newer.version, "2.7")
        self.assertIn("2.7", newer.notices[0])
        self.assertEqual(newer.labels[0].key, "customer")
        for version in ("3.0", "1.0", "2", 2, 2.0, "2.0.1", "02.0", "v2.0", None):
            with self.subTest(version=version), self.assertRaises(CredentialError):
                Rules.parse(document(version=version), "test")
        with self.assertRaises(CredentialError):
            Rules.parse(document(rules=[{"match": "**", "labels": {"customer": "a,b"}}], version="2.1"), "test")

    def test_unknown_fields_are_refused_with_a_known_minor(self):
        for changed in (
            document(extra=[]), document(labels=[{"key": "customer", "requried": True}]),
            document(rules=[{"match": "**", "labels": {"project": "p"}, "when": "x"}]),
            document(labels=[{"key": "customer", "values": [{"id": "a", "tag": "b"}]}], rules=[]),
            document(outputs={"litellm": {"tags": {"format": "${customer}", "extra": 1}}}),
        ):
            with self.subTest(document=changed), self.assertRaises(CredentialError):
                Rules.parse(changed, "test")

    def test_the_document_must_be_valid_on_its_own(self):
        for changed in (
            [], {}, document(rules=[{"match": "x"}]), document(rules=[{"match": "x", "labels": {}}]),
            document(rules=[{"match": "a\tb", "labels": {"project": "p"}}]),
            document(rules=[{"match": "github.com/acme/** ", "labels": {"project": "p"}}]),
            document(rules=[{"match": "x", "labels": {"project": "a,b"}}]),
            document(rules=[{"match": "x", "labels": {"team": "a"}}]),
            document(labels=[{**LABELS[0], "strict": True}], rules=[{"match": "x", "labels": {"customer": "unlisted"}}]),
            document(rules=[{"match": "/".join(["**"] * 9), "labels": {"project": "p"}}]),
            document(rules=[{"match": "**", "labels": {"project": "p"}}] * 1001),
            document(outputs={"litellm": {"tags": {"format": "${team}"}}}),
            document(outputs={"litellm": {"tags": {"format": ["${customer}", "${project}"]}}}),
            document(outputs={"tags": {"format": "${customer}"}}),
            document(labels=[{"key": "a-b"}, {"key": "a_b"}], rules=[], outputs={}),
        ):
            with self.subTest(document=changed), self.assertRaises(CredentialError):
                Rules.parse(changed, "test")
        strict = document(labels=[{**LABELS[0], "strict": True}, LABELS[1]])
        self.assertEqual(Rules.parse(strict, "test").labels[0].strict, True)
        # A rule for a label whose sources exclude rules does not invalidate the document; resolution ignores it.
        unsourced = document(labels=[{**LABELS[0], "sources": ["explicit"]}, LABELS[1]])
        self.assertEqual(Rules.parse(unsourced, "test").match(("github.com/acme-x/web",))["customer"][0], "acme")

    def test_outputs_are_read_per_gateway_type(self):
        rules = Rules.parse(document(outputs={"other": {"x": {"enabled": False}}, **DOCUMENT["outputs"]}), "test")
        profile = Profile("a", "https://gateway.example", {"team_id": "t"})
        config = label_config(profile, OUTPUTS, rules)
        self.assertEqual(config.output("tags").origin, "rules")
        self.assertEqual([entry.text for entry in config.output("tags").formats], ["${customer}", "${customer}:${project}"])
        self.assertIs(config.rules, rules)


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
                raw = owner.document if isinstance(owner.document, bytes) else json.dumps(owner.document).encode()
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

    def profile(self, gateway, rules, **fields):
        return Profile("a", gateway, {"team_id": "t"}, allow_http=True, rules=rules, **fields)

    def loader(self, gateway, rules, validate=None):
        def token():
            self.tokens.append(1)
            return "gateway-secret"

        return RulesLoader(self.store, self.profile(gateway, rules), token, self.clock, validate)

    def test_cache_is_fresh_for_an_hour_then_revalidated_with_its_etag(self):
        load = self.loader("http://localhost:1", self.server.origin + "/rules.json")
        self.assertEqual(load().rules[0].labels, (("customer", "acme"),))
        self.assertEqual(load().labels[0].values[0].name, "Acme")
        self.assertEqual(len(self.server.requests), 1)
        self.clock.now += RULES_CACHE_SECONDS + 1
        self.server.status = 304
        self.assertEqual(load().rules[0].labels, (("customer", "acme"),))
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
        stale = load()
        self.assertEqual(stale.rules[0].labels, (("customer", "acme"),))
        self.assertIn("503", stale.notices[-1])
        other = self.loader("http://localhost:1", self.server.origin + "/other.json")
        with self.assertRaises(CredentialError) as error:
            other()
        self.assertIn("503", str(error.exception))

    def test_stale_cache_is_used_when_a_newly_published_document_is_invalid(self):
        load = self.loader("http://localhost:1", self.server.origin + "/rules.json")
        load()
        self.clock.now += RULES_CACHE_SECONDS + 1
        self.server.document = document(version="1.0")
        self.assertEqual(load().rules[0].labels, (("customer", "acme"),))
        other = self.loader("http://localhost:1", self.server.origin + "/other.json")
        with self.assertRaises(CredentialError):
            other()

    def test_stale_cache_is_used_when_a_publication_has_unhashable_or_deeply_nested_values(self):
        load = self.loader("http://localhost:1", self.server.origin + "/rules.json")
        load()
        for published in (
            document(labels=[{"key": "customer", "required": True, "sources": [["rules"]]}]),
            document(outputs={"litellm": {"tags": {"format": [{"x": 1}]}}}),
            b"[" * 100_000,
        ):
            self.clock.now += RULES_CACHE_SECONDS + 1
            self.server.document = published
            with self.subTest(published=str(published)[:60]):
                rules = load()
                self.assertEqual(rules.rules[0].labels, (("customer", "acme"),))
                self.assertIn("last good copy", rules.notices[-1])
        other = self.loader("http://localhost:1", self.server.origin + "/other.json")
        with self.assertRaises(CredentialError):
            other()

    def test_a_published_document_that_does_not_fit_the_profile_falls_back_to_the_cache(self):
        url = self.server.origin + "/rules.json"
        load_config(self.store, self.profile("http://localhost:1", url), OUTPUTS, clock=self.clock)
        self.clock.now += RULES_CACHE_SECONDS + 1
        # Valid on its own, but a required label reaches no output with this gateway's metadata switched off.
        self.server.document = document(outputs={"litellm": {"tags": {"format": "${project}"}, "metadata": {"enabled": False}}})
        config = load_config(self.store, self.profile("http://localhost:1", url), OUTPUTS, clock=self.clock)
        self.assertEqual([entry.text for entry in config.output("tags").formats], ["${customer}", "${customer}:${project}"])
        self.assertTrue(any("last good copy" in notice for notice in config.notices))
        with self.assertRaises(CredentialError) as error:
            load_config(self.store, self.profile("http://localhost:1", self.server.origin + "/new.json"), OUTPUTS, clock=self.clock)
        self.assertIn("reaches no output", str(error.exception))

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
        for path in (Path(self.temp.name) / "rules-cache").iterdir():
            path.write_text("[" * 100_000, encoding="utf-8")
        load()
        self.assertEqual(len(self.server.requests), 3)

    def test_local_rules_file(self):
        path = Path(self.temp.name) / "rules.json"
        path.write_text(json.dumps(DOCUMENT), encoding="utf-8")
        self.assertEqual(self.loader("http://localhost:1", str(path))().rules[0].labels, (("customer", "acme"),))
        for text in ("{", "[" * 100_000):
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text[:10]), self.assertRaises(CredentialError):
                self.loader("http://localhost:1", str(path))()


if __name__ == "__main__":
    unittest.main()
