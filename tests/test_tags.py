import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from llm_credential_mapping.adapters import Api, Credential
from llm_credential_mapping.customers import Attribution, Tag, validate_customer
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.gateways.litellm import LiteLLMGateway, list_tags
from llm_credential_mapping.profiles import Profile
from llm_credential_mapping.transport import JsonTransport


class TagTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("unit", "https://gateway.example", {"team_id": "business-unit"})

    def test_exact_names_are_preserved_and_wire_constraints_are_checked(self):
        for name in ("customer:A", "Customer A", "a" * 256):
            self.assertEqual(validate_customer(name), name)
        for name in ("", None, [], "a" * 257, " a", "a ", "a,b", "a\nb", "a\tb", "\x00", "\x7f", "\u00e9"):
            with self.subTest(name=name), self.assertRaises(CredentialError):
                validate_customer(name)

    def test_gateway_maps_apis_and_attribution_to_litellm_conventions(self):
        gateway = LiteLLMGateway(self.profile)
        self.assertEqual(gateway.endpoint(Api.ANTHROPIC_MESSAGES), "https://gateway.example")
        self.assertEqual(gateway.endpoint(Api.OPENAI_CHAT), "https://gateway.example/v1")
        self.assertEqual(gateway.endpoint(Api.OPENAI_RESPONSES), "https://gateway.example/v1")
        headers = gateway.attribution_headers(Attribution("customer:A", "rules", "rule", "github.com/acme/répo"))
        self.assertEqual(headers, {
            "x-litellm-tags": "customer:A",
            "x-litellm-spend-logs-metadata": '{"customer_source":"rules","repository":"github.com/acme/r\\u00e9po"}',
        })
        self.assertTrue(all(value.isascii() for value in headers.values()))
        self.assertEqual(set(headers), set(gateway.attribution_header_names()))
        headers = gateway.attribution_headers(Attribution("customer:A", "explicit", "--customer"))
        self.assertEqual(headers["x-litellm-spend-logs-metadata"], '{"customer_source":"explicit"}')

    def test_gateway_catalogue_uses_the_credential(self):
        with patch("llm_credential_mapping.gateways.litellm.list_tags", return_value=[Tag("customer:A")]) as listed:
            self.assertEqual(LiteLLMGateway(self.profile).visible_customers(Credential("secret")), [Tag("customer:A")])
        listed.assert_called_once_with(self.profile, "secret")

    def test_catalogue_uses_bearer_header_and_sorts(self):
        transport = Mock(spec=JsonTransport)
        transport.get_list.return_value = [
            {"name": "customer:B", "description": "B", "models": [], "spend": 12},
            {"name": "user-agent", "description": None},
            {"name": "customer:A"},
        ]
        self.assertEqual(
            list_tags(self.profile, "credential-secret", transport=transport),
            [Tag("customer:A"), Tag("customer:B", "B"), Tag("user-agent")],
        )
        transport.get_list.assert_called_once_with(
            "https://gateway.example/tag/list", headers={"Authorization": "Bearer credential-secret"},
        )

    def test_empty_scope_stays_empty_not_a_fabricated_customer(self):
        transport = Mock(spec=JsonTransport)
        transport.get_list.return_value = []
        self.assertEqual(list_tags(self.profile, "secret", transport=transport), [])

    def test_bad_rows_and_duplicates_are_errors(self):
        for rows in ([None], [{}], [{"name": 42}], [{"name": "a", "description": []}], [{"name": "a"}, {"name": "a"}]):
            transport = Mock(spec=JsonTransport)
            transport.get_list.return_value = rows
            with self.subTest(rows=rows), self.assertRaises(CredentialError):
                list_tags(self.profile, "secret", transport=transport)

    def test_failed_discovery_is_not_a_successful_empty_list(self):
        transport = Mock(spec=JsonTransport)
        transport.get_list.side_effect = CredentialError("Gateway returned HTTP 403.")
        with self.assertRaises(CredentialError) as error:
            list_tags(self.profile, "secret", transport=transport)
        self.assertIn("403", str(error.exception))
        self.assertIn("--customer", str(error.exception))
        self.assertNotIn("secret", str(error.exception))


class TagHttpTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.body = [{"name": "customer:A", "description": "Customer A"}]
        self.status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                raw = json.dumps(owner.body).encode()
                self.send_response(owner.status)
                if owner.status == 307:
                    self.send_header("Location", owner.origin + "/stolen")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.profile = Profile("unit", self.origin, {"team_id": "unit"}, allow_http=True)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_real_http_discovery_and_oauth_object_contract_remains_strict(self):
        self.assertEqual(list_tags(self.profile, "shared-secret"), [Tag("customer:A", "Customer A")])
        self.assertEqual(self.requests, [("/tag/list", "Bearer shared-secret")])
        with self.assertRaises(CredentialError):
            JsonTransport().request("GET", self.origin + "/tag/list")

    def test_wrong_shape_and_oversized_list_are_rejected(self):
        for body in ({"tags": []}, ["a" * 1_048_577]):
            self.body = body
            with self.subTest(shape=type(body)), self.assertRaises(CredentialError):
                list_tags(self.profile, "secret")

    def test_errors_are_redacted_and_redirects_never_forward_the_credential(self):
        self.body = {"error": "credential-secret"}
        for status in (401, 403, 404, 500, 307):
            self.status = status
            self.requests.clear()
            with self.subTest(status=status), self.assertRaises(CredentialError) as error:
                list_tags(self.profile, "credential-secret")
            self.assertNotIn("credential-secret", str(error.exception))
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(self.requests[0][0], "/tag/list")
