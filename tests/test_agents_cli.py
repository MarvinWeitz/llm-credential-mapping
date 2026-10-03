import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from llm_credential_mapping.agents import TOKEN_VARIABLE, agent_command, launch
from llm_credential_mapping.cli import main
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.storage import NativeKeyring
from test_sessions import MemoryStore


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("a", "https://example.com", "team-a")

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

    def test_opencode_preserves_inline_configuration(self):
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": '{"theme":"dark","provider":{"existing":{}}}'}):
            args, env = agent_command("opencode", self.profile, "secret", [], "alias")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["theme"], "dark")
        self.assertIn("existing", config["provider"])
        self.assertEqual(args[-1], "llm-credential-mapping/alias")
        self.assertNotIn("secret", env["OPENCODE_CONFIG_CONTENT"])

    def test_invalid_opencode_settings_are_not_discarded(self):
        for existing in ("{", "[]", '{"provider":[]}'):
            with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": existing}):
                with self.subTest(existing=existing), self.assertRaises(CredentialError):
                    agent_command("opencode", self.profile, "secret", [], "alias")

    def test_opencode_requires_model(self):
        with self.assertRaises(CredentialError):
            agent_command("opencode", self.profile, "secret", [])

    def test_launch_preserves_child_exit_status(self):
        with patch("llm_credential_mapping.agents.shutil.which", return_value="claude"), \
                patch("llm_credential_mapping.agents.subprocess.run") as run:
            run.return_value.returncode = 7
            self.assertEqual(launch("claude", self.profile, "secret", [], None), 7)
            self.assertFalse(run.call_args.kwargs["check"])

    def test_missing_agent_has_actionable_error(self):
        with patch("llm_credential_mapping.agents.shutil.which", return_value=None):
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

    def test_project_commands_need_no_keyring(self):
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("project", "add", "a", "--gateway", "https://example.com", "--team-id", "a"), 0)
            self.assertEqual(self.call("project", "use", "a"), 0)
            self.assertEqual(self.call("project", "list"), 0)
        self.assertIn("Selected project", self.stdout.getvalue())

    def test_token_command_has_exact_stdout(self):
        store = ProfileStore(self.home)
        store.add(Profile("a", "https://example.com", "a"))
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager") as manager:
            manager.return_value.token.return_value = "access-secret"
            self.assertEqual(self.call("token", "--project", "a"), 0)
        self.assertEqual(self.stdout.getvalue(), "access-secret\n")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_missing_login_reports_stderr_not_stdout(self):
        ProfileStore(self.home).add(Profile("a", "https://example.com", "a"))
        with patch("llm_credential_mapping.cli.NativeKeyring", return_value=MemoryStore()):
            self.assertEqual(self.call("token"), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("No login", self.stderr.getvalue())

    def test_run_forwards_args_and_exit_status(self):
        ProfileStore(self.home).add(Profile("a", "https://example.com", "a"))
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager") as manager, \
                patch("llm_credential_mapping.cli.launch", return_value=9) as launched:
            manager.return_value.token.return_value = "secret"
            self.assertEqual(self.call("run", "--project", "a", "claude", "--", "--resume"), 9)
            self.assertEqual(launched.call_args.args[3], ["--resume"])

    def test_keyring_refuses_plaintext_backend(self):
        with patch("llm_credential_mapping.storage.keyring.get_keyring", return_value=object()):
            with self.assertRaises(CredentialError):
                NativeKeyring()
