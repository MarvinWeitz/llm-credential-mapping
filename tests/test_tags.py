import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from virtual_key_mapper.adapters import Api, Credential, IntegrationContext
from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.gateways.litellm import LiteLLMGateway, list_tags
from virtual_key_mapper.labels import Attribution, Choice, Label, LabelConfig, OutputOverride, parse_definitions
from virtual_key_mapper.profiles import Profile
from virtual_key_mapper.transport import JsonTransport


def Tag(name, description=None):
    return Choice(name, description=description)


def labels_config(tags=None, labels=({"key": "customer", "required": True}, {"key": "project"})):
    outputs = {} if tags is None else {"tags": OutputOverride(tuple(tags)) if tags else OutputOverride(None, False)}
    return LabelConfig.build(
        LiteLLMGateway.outputs(), central_labels=parse_definitions(list(labels), "rules", "test"), central_outputs=outputs,
    )


class TagTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("unit", "https://gateway.example", {"team_id": "business-unit"})

    def test_gateway_maps_apis_and_labels_to_litellm_conventions(self):
        gateway = LiteLLMGateway(self.profile)
        self.assertEqual(gateway.endpoint(Api.ANTHROPIC_MESSAGES), "https://gateway.example")
        self.assertEqual(gateway.endpoint(Api.OPENAI_CHAT), "https://gateway.example/v1")
        self.assertEqual(gateway.endpoint(Api.OPENAI_RESPONSES), "https://gateway.example/v1")
        attribution = Attribution(
            (Label("customer", "acme", "rules", "rule"), Label("project", "web", "explicit", "--label")),
            repository="github.com/acme/r\u00e9po",
        )
        headers = labels_config().headers(attribution)
        self.assertEqual(headers, {
            "x-litellm-tags": "customer:acme,project:web",
            "x-litellm-spend-logs-metadata": '{"labels":{"customer":"acme","project":"web"},'
                                             '"repository":"github.com/acme/r\\u00e9po","sources":{"customer":"rules","project":"explicit"}}',
        })
        self.assertTrue(all(value.isascii() for value in headers.values()))
        self.assertEqual(set(headers), set(labels_config().header_names()))
        context = IntegrationContext(self.profile, gateway, "")
        self.assertEqual(context.headers(Attribution()), {"x-litellm-spend-logs-metadata": '{"labels":{},"sources":{}}'})
        self.assertEqual(context.headers(None), {})

    def test_gateway_catalogue_inverts_the_tags_format_for_one_key(self):
        rows = [Tag("customer:A", "first"), Tag("customer:"), Tag("customer:a,b"), Tag("project:p"), Tag("[B]"), Tag("customer:!x")]
        gateway = LiteLLMGateway(self.profile)
        with patch("virtual_key_mapper.gateways.litellm.list_tags", return_value=rows) as listed:
            self.assertEqual(gateway.catalogue("customer", lambda: Credential("secret"), labels_config()), [Choice("A", description="first")])
            self.assertEqual(gateway.catalogue("project", lambda: Credential("secret"), labels_config()), [Choice("p")])
            self.assertEqual(
                gateway.catalogue("customer", lambda: Credential("secret"), labels_config(["[${customer}]", "customer:${customer}"])),
                [Choice("A", description="first"), Choice("B")],
            )
        listed.assert_called_with(self.profile, "secret")
        with patch("virtual_key_mapper.gateways.litellm.list_tags") as listed:
            both = ({"key": "customer", "required": True}, {"key": "project", "required": True})
            for tags in (["${customer}:${project}"], []):
                with self.subTest(tags=tags):
                    self.assertIsNone(gateway.catalogue("customer", Mock(side_effect=AssertionError), labels_config(tags, both)))
            listed.assert_not_called()

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

    def test_automatic_tags_and_bad_rows_are_skipped_not_fatal(self):
        transport = Mock(spec=JsonTransport)
        transport.get_list.return_value = [
            None, {}, {"name": 42}, {"name": "b", "description": []}, {"name": "x" * 513}, {"name": "a,b"}, {"name": "default"},
            {"name": "&acme"}, {"name": "User-Agent: claude-cli"}, {"name": "User-Agent: claude-cli/2.0.14 (external, cli)"},
            {"name": "user-agent: curl/8.5"}, {"name": "Acme: EU"}, {"name": "customer:A", "description": "first"}, {"name": "customer:A"},
            {"name": "Customer B", "description": "This is just a spend tag"}, {"name": "Default"},
        ]
        self.assertEqual(
            list_tags(self.profile, "secret", transport=transport),
            [Tag("Acme: EU", None), Tag("Customer B", "This is just a spend tag"), Tag("Default"), Tag("customer:A", "first")],
        )

    def test_failed_discovery_is_not_a_successful_empty_list(self):
        transport = Mock(spec=JsonTransport)
        transport.get_list.side_effect = CredentialError("Gateway returned HTTP 403.")
        with self.assertRaises(CredentialError) as error:
            list_tags(self.profile, "secret", transport=transport)
        self.assertIn("403", str(error.exception))
        self.assertIn("--label", str(error.exception))
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
