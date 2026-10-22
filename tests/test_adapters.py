"""The integrations and the CLI work with any gateway and auth adapter, not only LiteLLM."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import url2pathname

from virtual_key_mapper import registry
from virtual_key_mapper.adapters import Api, Credential, IntegrationContext
from virtual_key_mapper.cli import main
from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.integrations.common import header_variable, shell_command
from virtual_key_mapper.labels import JSON, LIST, Attribution, Label, LabelDefinition, Output
from virtual_key_mapper.profiles import Profile


LABELS = (LabelDefinition("customer", required=True),)
META = '{"labels":{"customer":"A"},"sources":{"customer":"explicit"}}'


class FakeGateway:
    """A gateway with its own paths and outputs and no catalogue."""

    apis = {
        Api.ANTHROPIC_MESSAGES: "/anthropic",
        Api.OPENAI_CHAT: "/openai/chat",
        Api.OPENAI_RESPONSES: "/openai/responses",
    }

    def __init__(self, profile):
        self.profile = profile

    def endpoint(self, api):
        if api not in self.apis:
            raise CredentialError(f"The fake gateway does not offer the {api} API.")
        return self.profile.gateway + self.apis[api]

    @staticmethod
    def outputs():
        return (
            Output("customer", LIST, "x-test-customer", lambda keys: tuple(f"${{{key}}}" for key in keys[:1])),
            Output("meta", JSON, "x-test-meta"),
        )

    def catalogue(self, key, credential, config):
        return None


class FixedKeyAuth:
    """A fixed key without expiry, standing in for e.g. a gateway virtual key."""

    def __init__(self, profile, store, secrets):
        self.profile = profile

    def login(self, notify, **options):
        return self.credential()

    def logout(self, notify):
        pass

    def status(self):
        return {"profile": self.profile.name, "state": "ready"}

    def credential(self):
        return Credential("fixed-secret")


def fixed_settings(values):
    if values:
        raise CredentialError("The fixed-key test auth takes no settings.")
    return {}


class FakeAdapterTests(unittest.TestCase):
    def setUp(self):
        adapters = (
            patch.dict(registry.GATEWAYS, {"fake": FakeGateway}),
            patch.dict(registry.AUTH, {"fixed": registry.AuthKind(FixedKeyAuth, fixed_settings)}),
        )
        for adapter in adapters:
            adapter.start()
            self.addCleanup(adapter.stop)
        self.profile = Profile("fake", "https://fake.example", {}, "fake", "fixed", labels=LABELS)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.context = IntegrationContext(self.profile, FakeGateway(self.profile), "fixed-secret", Path(home.name))
        self.attribution = Attribution((Label("customer", "A", "explicit", "--label"),))

    def command(self, agent, args=(), model="alias"):
        with patch.dict(os.environ, {}, clear=True):
            return registry.INTEGRATIONS[agent].command(self.context, self.attribution, list(args), model)

    def test_claude_uses_the_gateway_endpoint_and_headers(self):
        args, _ = self.command("claude")
        env = json.loads(args[args.index("--settings") + 1])["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://fake.example/anthropic")
        self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], f"x-test-customer: A\nx-test-meta: {META}")

    def test_codex_maps_every_attribution_header_to_its_variable(self):
        args, env = self.command("codex")
        provider = "model_providers.vkm."
        self.assertIn(provider + 'base_url="https://fake.example/openai/responses"', args)
        for name, value in (("x-test-customer", "A"), ("x-test-meta", META)):
            self.assertIn(f'{provider}env_http_headers.{name}="{header_variable(name)}"', args)
            self.assertEqual(env[header_variable(name)], value)

    def test_opencode_plugin_targets_the_gateway_chat_endpoint(self):
        _, env = self.command("opencode")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["provider"]["vkm"]["options"]["baseURL"], "https://fake.example/openai/chat")
        plugin = Path(url2pathname(urlsplit(config["plugin"][-1]).path)).read_text(encoding="utf-8")
        self.assertIn('"baseURL": "https://fake.example/openai/chat"', plugin)
        self.assertNotIn("litellm", plugin.lower())

    def test_missing_api_fails_clearly(self):
        with patch.dict(FakeGateway.apis, clear=True), self.assertRaises(CredentialError) as error:
            self.command("claude")
        self.assertIn("does not offer", str(error.exception))

    def test_no_litellm_names_reach_any_tool(self):
        for agent in registry.INTEGRATIONS:
            with self.subTest(agent=agent):
                args, env = self.command(agent)
                self.assertNotIn("litellm", json.dumps([args, env]).lower())

    def test_sign_in_kind_can_restrict_gateway_kinds(self):
        with self.assertRaises(CredentialError):
            Profile("mixed", "https://fake.example", {"team_id": "t"}, "fake", "litellm-sso")

    def test_cli_runs_end_to_end_with_other_adapters(self):
        with tempfile.TemporaryDirectory() as home:
            stdout, stderr = io.StringIO(), io.StringIO()

            def call(*args):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    return main(["--home", home, *args])

            completed = subprocess.CompletedProcess([], 0)
            with patch("virtual_key_mapper.cli.NativeKeyring"), \
                    patch("virtual_key_mapper.cli.find_checkout", return_value=None), \
                    patch("virtual_key_mapper.integrations.common.shutil.which", return_value="claude"), \
                    patch("virtual_key_mapper.integrations.common.subprocess.run", return_value=completed) as run:
                self.assertEqual(call(
                    "profile", "add", "fake", "--gateway", "https://fake.example",
                    "--gateway-type", "fake", "--auth", "fixed", "--label", "customer", "--required",
                ), 0)
                stdout.truncate(0)
                stdout.seek(0)
                self.assertEqual(call("token", "--json"), 0)
                self.assertEqual(json.loads(stdout.getvalue()), {
                    "access_token": "fixed-secret", "expires_at": None, "profile": "fake", "user": None,
                })
                self.assertEqual(call("labels", "values", "customer"), 0)
                self.assertIn("no catalogue", stderr.getvalue())
                self.assertEqual(call("run", "--label", "customer=A", "claude"), 0)
            argv = run.call_args.args[0]
            settings = json.loads(argv[argv.index("--settings") + 1])
            self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://fake.example/anthropic")
            self.assertIn("x-test-customer: A", settings["env"]["ANTHROPIC_CUSTOM_HEADERS"])
            self.assertIn(shell_command(["--home", str(Path(home).resolve())]), settings["apiKeyHelper"])
            self.assertNotIn("fixed-secret", json.dumps([argv, run.call_args.kwargs["env"]]))
            saved = json.loads((Path(home) / "profiles.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["profiles"]["fake"], {
                "gateway": {"type": "fake", "url": "https://fake.example", "allow_http": False},
                "auth": {"type": "fixed"},
                "labels": [{"key": "customer", "required": True}],
            })
