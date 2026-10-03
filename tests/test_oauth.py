import base64
import hashlib
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.litellm import Discovery, LiteLLMClient, LoopbackCallback, Session
from llm_credential_mapping.profiles import Profile
from llm_credential_mapping.transport import JsonTransport


def discovery(origin):
    return {
        "contract_version": 1,
        "issuer": origin,
        "authorization_endpoint": origin + "/authorize",
        "registration_endpoint": origin + "/register",
        "token_endpoint": origin + "/token",
        "revocation_endpoint": origin + "/revoke",
        "resource": origin,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
    }


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("a", "https://gateway.example", "team-a")
        self.data = discovery(self.profile.gateway)

    def test_supported_discovery(self):
        self.assertEqual(Discovery.parse(self.data, self.profile).resource, self.profile.gateway)

    def test_foreign_endpoints_are_rejected(self):
        for field in ("issuer", "resource", "token_endpoint", "revocation_endpoint"):
            value = {**self.data, field: "https://attacker.example/token"}
            with self.subTest(field=field), self.assertRaises(CredentialError):
                Discovery.parse(value, self.profile)

    def test_bad_contract_and_pkce_are_rejected(self):
        for change in (
            {"contract_version": 2}, {"contract_version": True},
            {"code_challenge_methods_supported": ["plain"]},
            {"token_endpoint_auth_methods_supported": ["client_secret_basic"]},
            {"grant_types_supported": ["authorization_code"]},
        ):
            with self.subTest(change=change), self.assertRaises(CredentialError):
                Discovery.parse({**self.data, **change}, self.profile)

    def test_invalid_expiry_and_token_type_are_rejected(self):
        base = {
            "token_type": "Bearer", "access_token": "access", "refresh_token": "refresh",
            "user_id": "u", "team_id": "team-a", "expires_in": 100,
        }
        for change in (
            {"expires_in": True}, {"expires_in": -1}, {"expires_in": float("nan")},
            {"expires_in": float("inf")}, {"token_type": "Other"}, {"access_token": "a\nb"},
        ):
            with self.subTest(change=change), self.assertRaises(CredentialError):
                Session.from_response({**base, **change}, self.profile, "client")


class OAuthFixture:
    def __init__(self):
        self.posts = []
        self.challenge = None
        self.team = "team-a"
        self.user = "employee"
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, data, code=200):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/.well-known/litellm-cli-auth":
                    self.respond(discovery(fixture.origin))
                elif self.path == "/redirect":
                    self.send_response(307)
                    self.send_header("Location", fixture.origin + "/stolen")
                    self.end_headers()
                elif self.path == "/error":
                    self.respond({"secret": "SHOULD-NOT-BE-PRINTED"}, 401)
                elif self.path == "/array":
                    self.respond([])
                elif self.path == "/large":
                    self.respond({"data": "x" * 1_048_577})
                else:
                    self.respond({"error": "not-found"}, 404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if "application/json" in self.headers["Content-Type"]:
                    body = json.loads(raw)
                else:
                    body = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
                fixture.posts.append((self.path, body))
                if self.path == "/register":
                    self.respond({
                        "client_id": "client",
                        "token_endpoint_auth_method": "none",
                        "redirect_uris": body["redirect_uris"],
                    })
                elif self.path == "/token":
                    if body["resource"] != fixture.origin:
                        self.respond({"error": "wrong-resource"}, 400)
                        return
                    if body["grant_type"] == "authorization_code":
                        challenge = base64.urlsafe_b64encode(
                            hashlib.sha256(body["code_verifier"].encode()).digest()
                        ).rstrip(b"=").decode()
                        if challenge != fixture.challenge or body["code"] != "test-code":
                            self.respond({"error": "wrong-pkce"}, 400)
                            return
                    self.respond({
                        "token_type": "Bearer",
                        "access_token": "fixture-access",
                        "refresh_token": "fixture-refresh",
                        "expires_in": 3600,
                        "user_id": fixture.user,
                        "team_id": fixture.team,
                    })
                elif self.path == "/revoke":
                    self.respond({})
                else:
                    self.respond({"error": "not-found"}, 404)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.browser_threads = []

    def browser(self, url):
        query = parse_qs(urlsplit(url).query)
        self.challenge = query["code_challenge"][0]
        callback = query["redirect_uri"][0]
        parameters = urlencode({"state": query["state"][0], "code": "test-code"})

        def deliver():
            with urlopen(callback + "?" + parameters, timeout=5) as response:
                response.read()

        thread = threading.Thread(target=deliver, daemon=True)
        self.browser_threads.append(thread)
        thread.start()
        return True

    def close(self):
        for thread in self.browser_threads:
            thread.join(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class OAuthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = OAuthFixture()
        self.addCleanup(self.fixture.close)
        self.profile = Profile("a", self.fixture.origin, "team-a", True)
        self.client = LiteLLMClient(self.profile)

    def login(self):
        with patch("llm_credential_mapping.litellm.webbrowser.open", self.fixture.browser):
            return self.client.login(lambda message: None, timeout=5)

    def test_actual_http_pkce_login_refresh_and_revocation(self):
        session = self.login()
        self.assertEqual(session.user_id, "employee")
        session.check_binding(self.profile)
        new = self.client.refresh(session)
        self.assertEqual(new.user_id, session.user_id)
        self.client.revoke(new)
        paths = [path for path, body in self.fixture.posts]
        self.assertEqual(paths, ["/register", "/token", "/token", "/revoke"])
        self.assertEqual(self.fixture.posts[2][1]["grant_type"], "refresh_token")

    def test_refresh_cannot_switch_employee(self):
        session = self.login()
        self.fixture.user = "someone-else"
        with self.assertRaises(CredentialError):
            self.client.refresh(session)
        self.assertEqual(self.fixture.posts[-1][0], "/revoke")

    def test_refresh_cannot_switch_team(self):
        session = self.login()
        self.fixture.team = "team-b"
        with self.assertRaises(CredentialError):
            self.client.refresh(session)

    def test_browser_failure_does_not_wait_or_exchange_code(self):
        with patch("llm_credential_mapping.litellm.webbrowser.open", return_value=False):
            with self.assertRaises(CredentialError):
                self.client.login(lambda message: None, timeout=5)
        self.assertEqual([path for path, body in self.fixture.posts], ["/register"])

    def test_redirects_are_not_followed(self):
        with self.assertRaises(CredentialError) as result:
            JsonTransport().request("GET", self.fixture.origin + "/redirect")
        self.assertIn("redirect refused", str(result.exception))

    def test_http_error_bodies_are_not_exposed(self):
        with self.assertRaises(CredentialError) as result:
            JsonTransport().request("GET", self.fixture.origin + "/error")
        self.assertIn("401", str(result.exception))
        self.assertNotIn("SHOULD-NOT-BE-PRINTED", str(result.exception))

    def test_non_object_and_large_responses_are_rejected(self):
        for path in ("/array", "/large"):
            with self.subTest(path=path), self.assertRaises(CredentialError):
                JsonTransport().request("GET", self.fixture.origin + path)


class CallbackTests(unittest.TestCase):
    def callback(self, parameters):
        callback = LoopbackCallback("expected-state", Mock())
        self.addCleanup(callback.close)
        results = []

        def request():
            try:
                with urlopen(callback.redirect_uri + "?" + urlencode(parameters), timeout=5) as response:
                    results.append(response.status)
            except Exception as error:
                results.append(type(error).__name__)

        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        return callback, thread, results

    def test_state_mismatch_and_unicode_do_not_accept_code(self):
        for state in ("wrong", "\u2603"):
            callback, thread, results = self.callback({"state": state, "code": "secret"})
            with self.subTest(state=state), self.assertRaises(CredentialError):
                callback.wait(0.1)
            thread.join(timeout=5)
            self.assertIsNone(callback.result)

    def test_browser_denial_is_reported(self):
        callback, thread, results = self.callback({"state": "expected-state", "error": "access_denied"})
        with self.assertRaises(CredentialError) as result:
            callback.wait(2)
        thread.join(timeout=5)
        self.assertIn("denied", str(result.exception))

    def test_partial_http_request_does_not_extend_login_timeout(self):
        callback = LoopbackCallback("state", Mock())
        self.addCleanup(callback.close)
        connection = socket.create_connection(callback.server.server_address, timeout=2)
        self.addCleanup(connection.close)
        connection.sendall(b"GET /callback HTTP/1.1\r\nHost:")
        start = time.monotonic()
        with self.assertRaises(CredentialError):
            callback.wait(0.1)
        self.assertLess(time.monotonic() - start, 1)

    def test_callback_errors_are_reported_without_request_data(self):
        notify = Mock()
        callback = LoopbackCallback("state", notify)
        self.addCleanup(callback.close)
        callback.server.handle_error(None, ("127.0.0.1", 12345))
        notify.assert_called_once()
        self.assertIn("callback connection failed", notify.call_args.args[0])
