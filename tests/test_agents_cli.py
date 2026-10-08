import contextlib
import io
import json
import os
import tempfile
import tomllib
import unittest
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from llm_credential_mapping.adapters import Credential, IntegrationContext
from llm_credential_mapping.auth.litellm_sso import vault_account
from llm_credential_mapping.cli import main
from llm_credential_mapping.customers import Attribution
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.integrations.common import CUSTOMER_VARIABLE, TOKEN_VARIABLE, header_variable
from llm_credential_mapping.integrations.opencode import _opencode_command, _opencode_major
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.registry import INTEGRATIONS, gateway_for
from llm_credential_mapping.storage import NativeKeyring
from test_sessions import MemoryStore, make_session


def _arguments(profile, token, customer):
    context = IntegrationContext(profile, gateway_for(profile), token)
    return context, Attribution(customer, "explicit", "--customer") if customer is not None else None


def agent_command(agent, profile, token, args, model=None, customer=None):
    context, attribution = _arguments(profile, token, customer)
    return INTEGRATIONS[agent].command(context, attribution, args, model)


def launch(agent, profile, token, args, model=None, customer=None):
    context, attribution = _arguments(profile, token, customer)
    return INTEGRATIONS[agent].launch(context, attribution, args, model)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("a", "https://example.com", {"team_id": "team-a"})

    def test_claude_sets_gateway_only_in_child_environment(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "old", "CLAUDE_CODE_USE_BEDROCK": "1"}, clear=True):
            args, env = agent_command("claude", self.profile, "secret", ["--resume"], "alias")
            self.assertEqual(args, ["claude", "--resume"])
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            self.assertEqual(env["ANTHROPIC_BASE_URL"], self.profile.gateway)
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "secret")
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "old")

    def test_codex_uses_responses_provider_without_secret_arguments(self):
        args, env = agent_command("codex", self.profile, "credential-secret", ["exec", "hello"], "alias")
        self.assertIn('model_providers.llm-credential-mapping.wire_api="responses"', args)
        self.assertIn('model_providers.llm-credential-mapping.env_key="' + TOKEN_VARIABLE + '"', args)
        self.assertNotIn("credential-secret", " ".join(args))
        self.assertEqual(env[TOKEN_VARIABLE], "credential-secret")
        self.assertEqual(args[-4:], ["--model", "alias", "exec", "hello"])

    def test_customer_headers_are_isolated_for_every_adapter(self):
        for agent in ("claude", "codex", "opencode"):
            with self.subTest(agent=agent), patch.dict(os.environ, {CUSTOMER_VARIABLE: "parent"}, clear=True):
                args_a, env_a = agent_command(agent, self.profile, "same-secret", [], "alias", "customer:A")
                args_b, env_b = agent_command(agent, self.profile, "same-secret", [], "alias", "customer:B")
                self.assertEqual(env_a[TOKEN_VARIABLE], env_b[TOKEN_VARIABLE])
                self.assertEqual(env_a[CUSTOMER_VARIABLE], "customer:A")
                self.assertEqual(env_b[CUSTOMER_VARIABLE], "customer:B")
                self.assertEqual(os.environ[CUSTOMER_VARIABLE], "parent")
                self.assertNotIn("same-secret", " ".join(args_a + args_b))
                if agent == "claude":
                    self.assertEqual(env_a["ANTHROPIC_CUSTOM_HEADERS"], "x-litellm-tags: customer:A")
                    self.assertEqual(env_b["ANTHROPIC_CUSTOM_HEADERS"], "x-litellm-tags: customer:B")
                elif agent == "codex":
                    variable = header_variable("x-litellm-tags")
                    self.assertIn(
                        'model_providers.llm-credential-mapping.env_http_headers.x-litellm-tags="' + variable + '"',
                        args_a,
                    )
                    self.assertEqual((env_a[variable], env_b[variable]), ("customer:A", "customer:B"))
                else:
                    for env, customer in ((env_a, "customer:A"), (env_b, "customer:B")):
                        options = json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["llm-credential-mapping"]["options"]
                        self.assertEqual(options["headers"], {"x-litellm-tags": customer})

    def test_claude_replaces_inherited_tags_but_preserves_other_headers(self):
        existing = "X-Trace: trace\nX-LiteLLM-Tags: old-customer\nx-litellm-tags: another"
        with patch.dict(os.environ, {"ANTHROPIC_CUSTOM_HEADERS": existing}):
            _, env = agent_command("claude", self.profile, "secret", [], customer="customer:A")
            self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "X-Trace: trace\nx-litellm-tags: customer:A")
            self.assertEqual(os.environ["ANTHROPIC_CUSTOM_HEADERS"], existing)

    def test_codex_customer_header_overrides_are_valid_toml(self):
        args, env = agent_command("codex", self.profile, "secret", [], customer="customer:A")
        config = tomllib.loads("\n".join(args[index + 1] for index, value in enumerate(args) if value == "-c"))
        provider = config["model_providers"]["llm-credential-mapping"]
        self.assertEqual(env[provider["env_http_headers"]["x-litellm-tags"]], "customer:A")
        self.assertEqual(env[provider["env_key"]], "secret")

    def test_claude_invalid_or_auth_overriding_headers_fail_without_secrets(self):
        for header in ("invalid-secret", "X-Trace: secret\x01", "Authorization: secret", "X-API-Key: secret", "Host: secret"):
            with self.subTest(header=header), patch.dict(os.environ, {"ANTHROPIC_CUSTOM_HEADERS": header}):
                with self.assertRaises(CredentialError) as error:
                    agent_command("claude", self.profile, "secret", [], customer="customer:A")
                self.assertNotIn("secret", str(error.exception))

    def test_invalid_customer_cannot_reach_any_adapter(self):
        for agent in ("claude", "codex", "opencode"):
            with self.subTest(agent=agent), self.assertRaises(CredentialError):
                agent_command(agent, self.profile, "secret", [], "alias", "customer:A,customer:B")

    def test_opencode_preserves_inline_configuration(self):
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": '{"theme":"dark","provider":{"existing":{}}}'}):
            args, env = agent_command("opencode", self.profile, "secret", [], "alias")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["theme"], "dark")
        self.assertIn("existing", config["provider"])
        self.assertEqual(config["model"], "llm-credential-mapping/alias")
        self.assertEqual(args, ["opencode"])
        self.assertNotIn("secret", env["OPENCODE_CONFIG_CONTENT"])

    def test_opencode_run_does_not_use_root_model_flag(self):
        args, env = agent_command("opencode", self.profile, "secret", ["run", "Explain this repo"], "alias")
        self.assertEqual(args, ["opencode", "run", "Explain this repo"])
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["model"], "llm-credential-mapping/alias")

    def test_opencode_exec_has_corrective_error(self):
        with self.assertRaises(CredentialError) as result:
            agent_command("opencode", self.profile, "secret", ["exec", "Explain"], "alias")
        self.assertIn("uses 'run'", str(result.exception))

    def test_opencode_v2_private_server_argument_placement(self):
        for args, expected in (
            ([], ["opencode", "--standalone"]),
            (["--continue"], ["opencode", "--standalone", "--continue"]),
            (["run", "Explain this repo"], ["opencode", "run", "--standalone", "Explain this repo"]),
            (["run", "--format", "json", "Explain"], ["opencode", "run", "--standalone", "--format", "json", "Explain"]),
        ):
            with self.subTest(args=args):
                self.assertEqual(_opencode_command(args, 2), expected)

    def test_opencode_v2_interactive_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(_opencode_command([directory], 2), ["opencode", "--standalone", directory])

    def test_opencode_v1_does_not_get_v2_flags(self):
        self.assertEqual(_opencode_command(["run", "Explain"], 1), ["opencode", "run", "Explain"])
        self.assertEqual(_opencode_command([], 1), ["opencode"])

    def test_opencode_shared_server_attachment_is_rejected_in_both_versions(self):
        for major in (1, 2):
            for args in (["run", "--attach", "http://localhost:4096"], ["run", "--attach=http://localhost:4096"]):
                with self.subTest(major=major, args=args), self.assertRaises(CredentialError):
                    _opencode_command(args, major)

    def test_opencode_v2_shared_server_and_other_subcommands_are_rejected(self):
        for args in (
            ["--server", "http://localhost:4096"],
            ["run", "--server=http://localhost:4096", "Explain"],
            ["run", "--standalone=false", "Explain"],
            ["api"],
        ):
            with self.subTest(args=args), self.assertRaises(CredentialError):
                _opencode_command(args, 2)

    def test_opencode_v2_preserves_literal_arguments_after_separator(self):
        self.assertEqual(
            _opencode_command(["run", "--", "--server"], 2),
            ["opencode", "run", "--standalone", "--", "--server"],
        )

    def test_opencode_version_formats(self):
        for output, major in (("1.2.3\n", 1), ("opencode v2.0.22\n", 2), ("2.0.22-beta.1", 2)):
            with patch("llm_credential_mapping.integrations.common.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.subTest(output=output):
                    self.assertEqual(_opencode_major("opencode"), major)

    def test_opencode_version_failure_is_explicit_and_redacted(self):
        for result in (
            subprocess.CompletedProcess([], 1, "secret-error", "secret-error"),
            subprocess.CompletedProcess([], 0, "", ""),
        ):
            with patch("llm_credential_mapping.integrations.common.subprocess.run", return_value=result):
                with self.subTest(result=result), self.assertRaises(CredentialError) as error:
                    _opencode_major("opencode")
                self.assertNotIn("secret-error", str(error.exception))
        with patch("llm_credential_mapping.integrations.common.subprocess.run", side_effect=subprocess.TimeoutExpired("opencode", 10)):
            with self.assertRaises(CredentialError):
                _opencode_major("opencode")

    def test_opencode_unknown_major_is_rejected(self):
        with self.assertRaises(CredentialError):
            _opencode_command([], 3)

    def test_launch_opencode_v2_uses_child_only_private_configuration(self):
        with patch("llm_credential_mapping.integrations.common.shutil.which", return_value="opencode"), \
                patch("llm_credential_mapping.integrations.common.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "opencode v2.0.22\n", ""),
                subprocess.CompletedProcess([], 7),
            ]
            self.assertEqual(launch("opencode", self.profile, "secret", ["run", "Explain"], "alias"), 7)
            version_call, child_call = run.call_args_list
            self.assertEqual(version_call.args[0], ["opencode", "--version"])
            self.assertNotIn("env", version_call.kwargs)
            self.assertEqual(child_call.args[0], ["opencode", "run", "--standalone", "Explain"])
            env = child_call.kwargs["env"]
            self.assertEqual(env[TOKEN_VARIABLE], "secret")
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["model"], "llm-credential-mapping/alias")
            self.assertNotIn("secret", " ".join(child_call.args[0]))

    def test_launch_opencode_v1_keeps_original_execution_mode(self):
        with patch("llm_credential_mapping.integrations.common.shutil.which", return_value="opencode"), \
                patch("llm_credential_mapping.integrations.common.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "1.2.3\n", ""),
                subprocess.CompletedProcess([], 0),
            ]
            self.assertEqual(launch("opencode", self.profile, "secret", ["run", "Explain"], "alias"), 0)
            self.assertEqual(run.call_args.args[0], ["opencode", "run", "Explain"])

    def test_invalid_opencode_settings_are_not_discarded(self):
        for existing in ("{", "[]", '{"provider":[]}'):
            with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": existing}):
                with self.subTest(existing=existing), self.assertRaises(CredentialError):
                    agent_command("opencode", self.profile, "secret", [], "alias")

    def test_opencode_requires_model(self):
        with self.assertRaises(CredentialError):
            agent_command("opencode", self.profile, "secret", [])

    def test_launch_preserves_child_exit_status(self):
        with patch("llm_credential_mapping.integrations.common.shutil.which", return_value="claude"), \
                patch("llm_credential_mapping.integrations.common.subprocess.run") as run:
            run.return_value.returncode = 7
            self.assertEqual(launch("claude", self.profile, "secret", [], None), 7)
            self.assertFalse(run.call_args.kwargs["check"])

    def test_missing_agent_has_actionable_error(self):
        with patch("llm_credential_mapping.integrations.common.shutil.which", return_value=None):
            with self.assertRaises(CredentialError):
                launch("codex", self.profile, "secret", [], None)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def call(self, *args):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(["--home", str(self.home), *args])

    def test_profile_commands_need_no_keyring(self):
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("profile", "add", "a", "--gateway", "https://example.com", "--team-id", "a"), 0)
            self.assertEqual(self.call("profile", "use", "a"), 0)
            self.assertEqual(self.call("profile", "list"), 0)
        self.assertIn("Selected profile", self.stdout.getvalue())

    def test_token_command_has_exact_stdout(self):
        store = ProfileStore(self.home)
        store.add(Profile("a", "https://example.com", {"team_id": "a"}))
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager:
            manager.return_value.credential.return_value = Credential("access-secret")
            self.assertEqual(self.call("token", "--profile", "a"), 0)
        self.assertEqual(self.stdout.getvalue(), "access-secret\n")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_token_that_can_no_longer_be_renewed_is_released_with_a_stderr_warning(self):
        store = ProfileStore(self.home)
        profile = Profile("a", "https://example.com", {"team_id": "a"})
        store.add(profile)
        vault = MemoryStore()
        vault.put(vault_account(self.home, profile), replace(make_session(profile), needs_login=True).to_dict())
        with patch("llm_credential_mapping.cli.NativeKeyring", return_value=vault):
            self.assertEqual(self.call("token"), 0)
        self.assertEqual(self.stdout.getvalue(), "access-secret\n")
        self.assertIn("login --profile a", self.stderr.getvalue())
        self.assertNotIn("access-secret", self.stderr.getvalue())

    def test_token_json_has_expiry_and_context_but_no_refresh_token(self):
        store = ProfileStore(self.home)
        profile = Profile("a", "https://example.com", {"team_id": "a"})
        store.add(profile)
        vault = MemoryStore()
        vault.put(vault_account(self.home, profile), make_session(profile).to_dict())
        with patch("llm_credential_mapping.cli.NativeKeyring", return_value=vault):
            self.assertEqual(self.call("token", "--json"), 0)
        value = json.loads(self.stdout.getvalue())
        self.assertEqual(set(value), {"access_token", "expires_at", "profile", "user"})
        self.assertEqual((value["access_token"], value["profile"], value["user"]), ("access-secret", "a", "employee"))
        self.assertNotIn("refresh-secret", self.stdout.getvalue())

    def test_profile_show_lists_endpoints_without_credentials(self):
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("profile", "add", "a", "--gateway", "https://example.com", "--team-id", "t"), 0)
            self.stdout.truncate(0)
            self.stdout.seek(0)
            self.assertEqual(self.call("profile", "show", "--json"), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), {
            "name": "a",
            "gateway": {"type": "litellm", "url": "https://example.com"},
            "auth": {"type": "litellm-sso", "team_id": "t"},
            "endpoints": {
                "anthropic-messages": "https://example.com",
                "openai-chat": "https://example.com/v1",
                "openai-responses": "https://example.com/v1",
            },
        })

    def test_profile_add_validates_settings_for_the_chosen_sign_in_kind(self):
        self.assertEqual(self.call("profile", "add", "a", "--gateway", "https://example.com"), 1)
        self.assertIn("team ID", self.stderr.getvalue())
        for extra in (["--gateway-type", "other"], ["--auth", "other"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.call("profile", "add", "a", "--gateway", "https://example.com", "--team-id", "t", *extra)
        self.assertFalse((self.home / "profiles.json").exists())

    def test_missing_login_reports_stderr_not_stdout(self):
        ProfileStore(self.home).add(Profile("a", "https://example.com", {"team_id": "a"}))
        with patch("llm_credential_mapping.cli.NativeKeyring", return_value=MemoryStore()):
            self.assertEqual(self.call("token"), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("No login", self.stderr.getvalue())

    def test_run_forwards_args_and_exit_status(self):
        ProfileStore(self.home).add(Profile("a", "https://example.com", {"team_id": "a"}))
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.cli.launch", return_value=9) as launched:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("run", "--profile", "a", "--customer", "customer:A", "claude", "--", "--resume"), 9)
            self.assertEqual(launched.call_args.args[3], ["--resume"])
            self.assertEqual(launched.call_args.args[2], Attribution("customer:A", "explicit", "--customer"))

    def test_opencode_run_forwards_subcommand_and_prompt(self):
        ProfileStore(self.home).add(Profile("a", "https://example.com", {"team_id": "a"}))
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.cli.launch", return_value=0) as launched:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(
                self.call("run", "--profile", "a", "--customer", "customer:A", "--model", "alias", "opencode", "--", "run", "Explain this repo"), 0
            )
            self.assertEqual(launched.call_args.args[3:], (["run", "Explain this repo"], "alias"))
            self.assertEqual(launched.call_args.args[2].customer, "customer:A")

    def test_keyring_refuses_plaintext_backend(self):
        with patch("llm_credential_mapping.storage.keyring.get_keyring", return_value=object()):
            with self.assertRaises(CredentialError):
                NativeKeyring()
