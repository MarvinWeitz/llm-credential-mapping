import contextlib
import io
import json
import os
import shutil
import tempfile
import tomllib
import unittest
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from virtual_key_mapper.adapters import Credential, IntegrationContext
from virtual_key_mapper.auth.litellm_sso import vault_account
from virtual_key_mapper.cli import main
from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.integrations.common import (
    ATTRIBUTION_VARIABLE, TOKEN_VARIABLE, header_environment, header_variable, launch_labels, shell_command,
)
from virtual_key_mapper.labels import LAUNCH_VARIABLE, MISSING_PHRASE, Attribution, Label, LabelDefinition, OutputOverride
from virtual_key_mapper.integrations.opencode import _opencode_command, _opencode_major
from virtual_key_mapper.profiles import Profile, ProfileStore
from virtual_key_mapper.registry import INTEGRATIONS, gateway_for
from virtual_key_mapper.storage import NativeKeyring
from test_sessions import MemoryStore, make_session


HOME = Path(tempfile.mkdtemp())  # For generated files such as the OpenCode plugin.


def tearDownModule():
    shutil.rmtree(HOME, ignore_errors=True)


LABELS = (LabelDefinition("customer", required=True), LabelDefinition("project"))
TAGS = header_variable("x-litellm-tags")
METADATA = header_variable("x-litellm-spend-logs-metadata")


def explicit(labels, source="explicit"):
    """An attribution as `run --label` gives it: the given labels from ``source``, the others without a value."""
    values = dict(labels)
    return Attribution(
        tuple(Label(key, values.get(key), source if key in values else None, "--label" if key in values else None)
              for key in ("customer", "project")),
        ("customer",) if not values.get("customer") else (),
    )


def metadata(labels, source="explicit"):
    labels = {key: value for key, value in labels.items() if value}
    return json.dumps(
        {"labels": labels, "sources": {key: source for key in labels}}, separators=(",", ":"), sort_keys=True,
    )


def _arguments(profile, token, labels):
    context = IntegrationContext(profile, gateway_for(profile), token, HOME)
    return context, explicit(labels) if labels is not None else None


def claude_settings(args):
    return json.loads(args[args.index("--settings") + 1])


def agent_command(agent, profile, token, args, model=None, labels=None):
    context, attribution = _arguments(profile, token, labels)
    return INTEGRATIONS[agent].command(context, attribution, args, model)


def launch(agent, profile, token, args, model=None, labels=None):
    context, attribution = _arguments(profile, token, labels)
    return INTEGRATIONS[agent].launch(context, attribution, args, model)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("a", "https://example.com", {"team_id": "team-a"}, labels=LABELS)

    def test_claude_gets_a_renewing_helper_instead_of_a_token(self):
        inherited = {"ANTHROPIC_API_KEY": "old", "ANTHROPIC_AUTH_TOKEN": "old", "CLAUDE_CODE_USE_BEDROCK": "1"}
        with patch.dict(os.environ, inherited, clear=True):
            args, env = agent_command("claude", self.profile, "secret", ["--resume"], "alias")
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "old")
        self.assertEqual((args[:2], args[3:]), (["claude", "--settings"], ["--resume"]))
        settings = claude_settings(args)
        self.assertEqual(settings["env"], {"ANTHROPIC_BASE_URL": self.profile.gateway, "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"})
        self.assertEqual(settings["model"], "alias")
        self.assertTrue(settings["apiKeyHelper"].endswith(shell_command(["--home", str(HOME), "token", "--profile", "a"])))
        for name in inherited:
            self.assertNotIn(name, env)
        self.assertNotIn("secret", json.dumps([args, env]))

    def test_codex_gets_a_renewing_token_command_instead_of_a_token(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "old", "OPENAI_BASE_URL": "https://elsewhere.example"}):
            args, env = agent_command("codex", self.profile, "credential-secret", ["exec", "hello"], "alias")
        self.assertIn('model_providers.vkm.wire_api="responses"', args)
        self.assertIn('model_providers.vkm.base_url="https://example.com/v1"', args)
        self.assertEqual(args[-4:], ["--model", "alias", "exec", "hello"])
        auth_args = json.loads(next(arg for arg in args if ".auth.args=" in arg).split("=", 1)[1])
        self.assertEqual(auth_args[-4:], ["token", "--profile", "a", "--require-attribution-env"])
        for name in (TOKEN_VARIABLE, "OPENAI_API_KEY", "OPENAI_BASE_URL"):
            self.assertNotIn(name, env)
        self.assertNotIn("credential-secret", json.dumps([args, env]))
        self.assertFalse(any("env_key" in arg for arg in args))

    def test_only_explicit_labels_reach_the_launch_marker(self):
        resolved = explicit({"customer": "acme"}, source="file")
        mixed = Attribution((Label("customer", "acme", "explicit", "--label"), Label("project", None, "explicit", "--label")))
        for agent in ("claude", "codex", "opencode"):
            with self.subTest(agent=agent), patch.dict(os.environ, {LAUNCH_VARIABLE: '{"customer":"stale"}'}, clear=True):
                context, _ = _arguments(self.profile, "secret", None)
                _, env = INTEGRATIONS[agent].command(context, resolved, [], "alias")
                self.assertNotIn(LAUNCH_VARIABLE, env)  # A resolved value is not a launch choice; the stale one goes.
                self.assertFalse(any(name.startswith("VKM_LABEL_") for name in env))
                _, env = INTEGRATIONS[agent].command(context, mixed, [], "alias")
                self.assertEqual(json.loads(env[LAUNCH_VARIABLE]), {"customer": "acme", "project": None})
                self.assertEqual(launch_labels(env), {"customer": "acme", "project": None})
                self.assertFalse(any(name.startswith("VKM_LABEL_") for name in env))

    def test_label_headers_are_isolated_for_every_adapter(self):
        a, b = {"customer": "acme", "project": "web"}, {"customer": "globex"}
        for agent in ("claude", "codex", "opencode"):
            with self.subTest(agent=agent), patch.dict(os.environ, {LAUNCH_VARIABLE: "parent"}, clear=True):
                args_a, env_a = agent_command(agent, self.profile, "same-secret", [], "alias", a)
                args_b, env_b = agent_command(agent, self.profile, "same-secret", [], "alias", b)
                self.assertEqual(json.loads(env_a[LAUNCH_VARIABLE]), a)
                self.assertEqual(json.loads(env_b[LAUNCH_VARIABLE]), b)
                self.assertEqual(os.environ[LAUNCH_VARIABLE], "parent")
                self.assertNotIn("same-secret", " ".join(args_a + args_b))
                if agent == "claude":
                    self.assertEqual(
                        claude_settings(args_a)["env"]["ANTHROPIC_CUSTOM_HEADERS"],
                        f"x-litellm-tags: customer:acme,project:web\nx-litellm-spend-logs-metadata: {metadata(a)}",
                    )
                    self.assertEqual(
                        claude_settings(args_b)["env"]["ANTHROPIC_CUSTOM_HEADERS"],
                        f"x-litellm-tags: customer:globex\nx-litellm-spend-logs-metadata: {metadata(b)}",
                    )
                self.assertNotIn(TOKEN_VARIABLE, env_a)
                if agent == "codex":
                    self.assertIn('model_providers.vkm.env_http_headers.x-litellm-tags="' + TAGS + '"', args_a)
                    self.assertEqual((env_a[TAGS], env_b[TAGS]), ("customer:acme,project:web", "customer:globex"))
                    self.assertEqual((env_a[METADATA], env_b[METADATA]), (metadata(a), metadata(b)))
                    self.assertEqual(env_a[ATTRIBUTION_VARIABLE], "1")
                elif agent == "opencode":
                    for env in (env_a, env_b):
                        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                        self.assertTrue(config["plugin"][-1].endswith("/vkm-a.js"))
                        self.assertNotIn("headers", config["provider"]["vkm"]["options"])

    def test_claude_replaces_inherited_label_headers_but_preserves_other_headers(self):
        existing = "X-Trace: trace\nX-LiteLLM-Tags: old-customer\nx-litellm-tags: another\nx-litellm-spend-logs-metadata: {}"
        profile = Profile(
            "a", "https://example.com", {"team_id": "team-a"}, labels=LABELS, outputs={"metadata": OutputOverride(None, False)},
        )
        with patch.dict(os.environ, {"ANTHROPIC_CUSTOM_HEADERS": existing}):
            args, _ = agent_command("claude", profile, "secret", [], labels={"customer": "acme"})
            self.assertEqual(os.environ["ANTHROPIC_CUSTOM_HEADERS"], existing)
        self.assertEqual(
            claude_settings(args)["env"]["ANTHROPIC_CUSTOM_HEADERS"], "X-Trace: trace\nx-litellm-tags: customer:acme",
        )

    def test_codex_unsets_inherited_header_variables_for_omitted_headers(self):
        profile = Profile(
            "a", "https://example.com", {"team_id": "team-a"}, labels=LABELS, outputs={"metadata": OutputOverride(None, False)},
        )
        with patch.dict(os.environ, {TAGS: "customer:other", METADATA: "{}"}):
            _, env = agent_command("codex", profile, "secret", [], labels={"customer": "acme"})
        self.assertEqual(env[TAGS], "customer:acme")
        self.assertNotIn(METADATA, env)
        with patch.dict(os.environ, {TAGS: "customer:other", ATTRIBUTION_VARIABLE: "1"}):
            _, env = agent_command("codex", profile, "secret", [])
        self.assertNotIn(TAGS, env)
        self.assertNotIn(ATTRIBUTION_VARIABLE, env)

    def test_codex_label_header_overrides_are_valid_toml(self):
        args, env = agent_command("codex", self.profile, "secret", [], labels={"customer": "acme"})
        config = tomllib.loads("\n".join(args[index + 1] for index, value in enumerate(args) if value == "-c"))
        provider = config["model_providers"]["vkm"]
        self.assertEqual(env[provider["env_http_headers"]["x-litellm-tags"]], "customer:acme")
        self.assertEqual(set(provider["env_http_headers"]), {"x-litellm-tags", "x-litellm-spend-logs-metadata"})
        self.assertNotIn("env_key", provider)
        self.assertEqual(provider["auth"]["timeout_ms"], 30000)

    def test_header_environment_lists_every_header_and_the_readiness_flag(self):
        context, _ = _arguments(self.profile, "", None)
        self.assertEqual(header_environment(context, None), {TAGS: None, METADATA: None, ATTRIBUTION_VARIABLE: None})
        self.assertEqual(header_environment(context, explicit({"customer": "acme"})), {
            TAGS: "customer:acme", METADATA: metadata({"customer": "acme"}), ATTRIBUTION_VARIABLE: "1",
        })
        with self.assertRaisesRegex(CredentialError, MISSING_PHRASE):
            header_environment(context, explicit({"project": "web"}))

    def test_claude_invalid_or_auth_overriding_headers_fail_without_secrets(self):
        for header in ("invalid-secret", "X-Trace: secret\x01", "Authorization: secret", "X-API-Key: secret", "Host: secret",
                       "X-LiteLLM-API-Key: Bearer secret", "api-key: secret"):
            with self.subTest(header=header), patch.dict(os.environ, {"ANTHROPIC_CUSTOM_HEADERS": header}):
                with self.assertRaises(CredentialError) as error:
                    agent_command("claude", self.profile, "secret", [], labels={"customer": "acme"})
                self.assertNotIn("secret", str(error.exception))

    def test_invalid_or_missing_labels_cannot_reach_any_adapter(self):
        for agent in ("claude", "codex", "opencode"):
            for labels in ({"customer": "a,b"}, {"customer": "!a"}, {"project": "web"}):
                with self.subTest(agent=agent, labels=labels), self.assertRaises(CredentialError):
                    agent_command(agent, self.profile, "secret", [], "alias", labels)

    def test_opencode_preserves_inline_configuration(self):
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": '{"theme":"dark","provider":{"existing":{}}}'}):
            args, env = agent_command("opencode", self.profile, "secret", [], "alias")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["theme"], "dark")
        self.assertIn("existing", config["provider"])
        self.assertEqual(config["model"], "vkm/alias")
        self.assertEqual(args, ["opencode"])
        self.assertNotIn("secret", env["OPENCODE_CONFIG_CONTENT"])

    def test_opencode_run_does_not_use_root_model_flag(self):
        args, env = agent_command("opencode", self.profile, "secret", ["run", "Explain this repo"], "alias")
        self.assertEqual(args, ["opencode", "run", "Explain this repo"])
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["model"], "vkm/alias")

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
            with patch("virtual_key_mapper.integrations.common.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.subTest(output=output):
                    self.assertEqual(_opencode_major("opencode"), major)

    def test_opencode_version_failure_is_explicit_and_redacted(self):
        for result in (
            subprocess.CompletedProcess([], 1, "secret-error", "secret-error"),
            subprocess.CompletedProcess([], 0, "", ""),
        ):
            with patch("virtual_key_mapper.integrations.common.subprocess.run", return_value=result):
                with self.subTest(result=result), self.assertRaises(CredentialError) as error:
                    _opencode_major("opencode")
                self.assertNotIn("secret-error", str(error.exception))
        with patch("virtual_key_mapper.integrations.common.subprocess.run", side_effect=subprocess.TimeoutExpired("opencode", 10)):
            with self.assertRaises(CredentialError):
                _opencode_major("opencode")

    def test_opencode_unknown_major_is_rejected(self):
        with self.assertRaises(CredentialError):
            _opencode_command([], 3)

    def test_launch_opencode_v2_uses_child_only_private_configuration(self):
        with patch("virtual_key_mapper.integrations.common.shutil.which", return_value="opencode"), \
                patch("virtual_key_mapper.integrations.common.subprocess.run") as run:
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
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["model"], "vkm/alias")
            self.assertNotIn("secret", " ".join(child_call.args[0]))

    def test_launch_opencode_v1_keeps_original_execution_mode(self):
        with patch("virtual_key_mapper.integrations.common.shutil.which", return_value="opencode"), \
                patch("virtual_key_mapper.integrations.common.subprocess.run") as run:
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
        with patch("virtual_key_mapper.integrations.common.shutil.which", return_value="claude"), \
                patch("virtual_key_mapper.integrations.common.subprocess.run") as run:
            run.return_value.returncode = 7
            self.assertEqual(launch("claude", self.profile, "secret", [], None), 7)
            self.assertFalse(run.call_args.kwargs["check"])

    def test_missing_agent_has_actionable_error(self):
        with patch("virtual_key_mapper.integrations.common.shutil.which", return_value=None):
            with self.assertRaises(CredentialError):
                launch("codex", self.profile, "secret", [], None)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        # Launch outside any Git checkout, whatever directory the tests run in.
        outside_git = patch("virtual_key_mapper.cli.find_checkout", return_value=None)
        outside_git.start()
        self.addCleanup(outside_git.stop)

    def call(self, *args):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(["--home", str(self.home), *args])

    def test_profile_commands_need_no_keyring(self):
        with patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("profile", "add", "a", "--gateway", "https://example.com", "--team-id", "a"), 0)
            self.assertEqual(self.call("profile", "use", "a"), 0)
            self.assertEqual(self.call("profile", "list"), 0)
        self.assertIn("Selected profile", self.stdout.getvalue())

    def test_token_command_has_exact_stdout(self):
        store = ProfileStore(self.home)
        store.add(Profile("a", "https://example.com", {"team_id": "a"}))
        with patch("virtual_key_mapper.cli.NativeKeyring"), \
                patch("virtual_key_mapper.cli.auth_for") as manager:
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
        with patch("virtual_key_mapper.cli.NativeKeyring", return_value=vault):
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
        with patch("virtual_key_mapper.cli.NativeKeyring", return_value=vault):
            self.assertEqual(self.call("token", "--json"), 0)
        value = json.loads(self.stdout.getvalue())
        self.assertEqual(set(value), {"access_token", "expires_at", "profile", "user"})
        self.assertEqual((value["access_token"], value["profile"], value["user"]), ("access-secret", "a", "employee"))
        self.assertNotIn("refresh-secret", self.stdout.getvalue())

    def test_profile_show_lists_endpoints_without_credentials(self):
        with patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("profile", "add", "a", "--gateway", "https://example.com", "--team-id", "t"), 0)
            self.stdout.truncate(0)
            self.stdout.seek(0)
            self.assertEqual(self.call("profile", "show", "--json"), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), {
            "name": "a",
            "gateway": {"type": "litellm", "url": "https://example.com"},
            "auth": {"type": "litellm-sso", "team_id": "t"},
            "rules": None,
            "endpoints": {
                "anthropic-messages": "https://example.com",
                "openai-chat": "https://example.com/v1",
                "openai-responses": "https://example.com/v1",
            },
            "labels": [],
            "outputs": {},
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
        with patch("virtual_key_mapper.cli.NativeKeyring", return_value=MemoryStore()):
            self.assertEqual(self.call("token"), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("No login", self.stderr.getvalue())

    def test_run_forwards_args_and_exit_status(self):
        ProfileStore(self.home).add(Profile("a", "https://example.com", {"team_id": "a"}, labels=LABELS))
        with patch("virtual_key_mapper.cli.NativeKeyring"), \
                patch("virtual_key_mapper.cli.auth_for") as manager, \
                patch("virtual_key_mapper.cli.launch", return_value=9) as launched:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("run", "--profile", "a", "--label", "customer=acme", "claude", "--", "--resume"), 9)
            self.assertEqual(launched.call_args.args[3], ["--resume"])
            label = launched.call_args.args[2].get("customer")
            self.assertEqual((label.value, label.source), ("acme", "explicit"))

    def test_opencode_run_forwards_subcommand_and_prompt(self):
        ProfileStore(self.home).add(Profile("a", "https://example.com", {"team_id": "a"}, labels=LABELS))
        with patch("virtual_key_mapper.cli.NativeKeyring"), \
                patch("virtual_key_mapper.cli.auth_for") as manager, \
                patch("virtual_key_mapper.cli.launch", return_value=0) as launched:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(
                self.call("run", "--profile", "a", "--label", "customer=acme", "--model", "alias", "opencode", "--", "run", "Explain this repo"), 0
            )
            self.assertEqual(launched.call_args.args[3:], (["run", "Explain this repo"], "alias"))
            self.assertEqual(launched.call_args.args[2].values, {"customer": "acme"})

    def test_keyring_refuses_plaintext_backend(self):
        with patch("virtual_key_mapper.storage.keyring.get_keyring", return_value=object()):
            with self.assertRaises(CredentialError):
                NativeKeyring()
