import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from virtual_key_mapper.adapters import Credential, IntegrationContext
from virtual_key_mapper.cli import main
from virtual_key_mapper.customers import Attribution
from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.integrations.claude import Claude, settings_path
from virtual_key_mapper.integrations.common import CUSTOMER_VARIABLE, shell_command
from virtual_key_mapper.profiles import Profile, ProfileStore
from virtual_key_mapper.registry import gateway_for
from isolation import isolate_tool_configuration

ATTRIBUTION = Attribution("customer:A", "file", "/repo/.vkm", "github.com/acme/web")
HEADERS = 'x-litellm-tags: customer:A\nx-litellm-spend-logs-metadata: {"customer_source":"file","repository":"github.com/acme/web"}'


class ClaudeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        isolate_tool_configuration(self)
        self.config = Path(os.environ["CLAUDE_CONFIG_DIR"])
        self.home = self.root / "credentials"
        self.home.mkdir()
        for name in (
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS", "VKM_CUSTOMER",
            CUSTOMER_VARIABLE, "CLAUDE_PROJECT_DIR",
        ):
            os.environ.pop(name, None)
        self.profile = Profile("unit", "https://gateway.example", {"team_id": "t"})
        self.context = IntegrationContext(self.profile, gateway_for(self.profile), "", self.home)
        self.claude = Claude()
        self.settings = settings_path()

    def read(self, path=None):
        return json.loads((path or self.settings).read_text(encoding="utf-8"))


class SetupTests(ClaudeTestCase):
    def test_setup_configures_helper_gateway_model_and_guard_and_keeps_other_settings(self):
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({
            "theme": "dark", "env": {"OTHER": "1"},
            "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-hook"}]}]},
        }), encoding="utf-8")
        self.claude.setup(self.context, "alias")
        self.claude.setup(self.context, "alias")
        data = self.read()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"], {"OTHER": "1", "ANTHROPIC_BASE_URL": "https://gateway.example"})
        self.assertEqual(data["model"], "alias")
        self.assertIn(shell_command(["virtual_key_mapper", "--home", str(self.home), "token", "--profile", "unit"]), data["apiKeyHelper"])
        commands = [hook["command"] for entry in data["hooks"]["UserPromptSubmit"] for hook in entry["hooks"]]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], "other-hook")
        self.assertTrue(commands[1].endswith(shell_command(["claude", "guard", "--profile", "unit"])))

    def test_setup_refuses_another_tools_credential_setup_unless_replacing(self):
        for existing in ({"apiKeyHelper": "vault-cli get key"}, {"env": {"ANTHROPIC_BASE_URL": "https://other.example"}}):
            self.config.mkdir(exist_ok=True)
            self.settings.write_text(json.dumps(existing), encoding="utf-8")
            with self.subTest(existing=existing):
                with self.assertRaises(CredentialError):
                    self.claude.setup(self.context)
                self.assertEqual(self.read(), existing)
                self.claude.setup(self.context, replace=True)
                self.assertIn("virtual_key_mapper", self.read()["apiKeyHelper"])

    def test_invalid_settings_are_never_replaced(self):
        self.config.mkdir()
        for content in ("{", "[]", '{"env": []}'):
            self.settings.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(CredentialError):
                self.claude.setup(self.context)
            self.assertEqual(self.settings.read_text(encoding="utf-8"), content)

    def test_remove_undoes_only_what_setup_wrote(self):
        self.config.mkdir()
        self.settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        self.claude.setup(self.context, "alias")
        self.claude.remove(self.context)
        self.assertEqual(self.read(), {"theme": "dark"})
        self.assertFalse((self.home / "claude.json").exists())
        self.claude.setup(self.context, "alias")
        data = self.read()
        data["model"] = "my-own-choice"
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        self.claude.remove(self.context)
        self.assertEqual(self.read(), {"theme": "dark", "model": "my-own-choice"})

    def test_setup_and_remove_write_through_a_symlinked_settings_file(self):
        # Dotfile managers (stow, home-manager) link the settings file into a repository.
        managed = self.root / "dotfiles" / "settings.json"
        managed.parent.mkdir()
        managed.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        self.config.mkdir()
        try:
            self.settings.symlink_to(managed)
        except OSError:
            self.skipTest("symbolic links are not available")
        self.claude.setup(self.context)
        self.assertTrue(self.settings.is_symlink())
        self.assertIn("apiKeyHelper", self.read(managed))
        self.claude.remove(self.context)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(self.read(managed), {"theme": "dark"})

    def test_setup_keeps_and_remove_restores_the_users_own_model_and_gateway(self):
        self.config.mkdir()
        own = {"model": "opus", "env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}}
        self.settings.write_text(json.dumps(own), encoding="utf-8")
        notes = self.claude.setup(self.context, "sonnet")
        self.assertEqual(self.read()["model"], "opus")
        self.assertTrue(any("already sets a model" in note for note in notes))
        self.claude.setup(self.context, "sonnet")
        self.claude.remove(self.context)
        self.assertEqual(self.read(), own)

    def test_rerunning_setup_without_a_model_keeps_ownership_of_ours(self):
        self.config.mkdir()
        self.settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        self.claude.setup(self.context, "sonnet")
        self.claude.setup(self.context)
        self.assertEqual(self.read()["model"], "sonnet")
        self.claude.remove(self.context)
        self.assertEqual(self.read(), {"theme": "dark"})

    def test_static_credentials_in_the_environment_are_reported(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "x"}):
            notes = self.claude.setup(self.context)
        self.assertTrue(any("ANTHROPIC_AUTH_TOKEN" in note for note in notes))


class ShellCommandTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows quoting")
    def test_windows_commands_work_under_cmd_and_git_bash(self):
        # Claude Code runs hooks through Git Bash on Windows; unquoted backslashes would be stripped there.
        self.assertEqual(
            shell_command([r"C:\Program Files\Py\python.exe", "-m", "x", r"C:\Users\me\cfg"]),
            '"C:/Program Files/Py/python.exe" "-m" "x" "C:/Users/me/cfg"',
        )
        for unsafe in ("50%", "a$HOME", "a`b`", 'a"b', "a!b"):
            with self.subTest(unsafe=unsafe), self.assertRaises(CredentialError):
                shell_command(["python", unsafe])


class GuardTests(ClaudeTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.sub = self.repo / "sub"
        self.sub.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True, capture_output=True)
        self.local = self.claude.local_settings(self.sub, self.repo)

    def guard(self, attribution=ATTRIBUTION, headers=None, **environ):
        if headers is not None:
            environ["ANTHROPIC_CUSTOM_HEADERS"] = headers
        return self.claude.guard(self.context, self.sub, self.repo, attribution, "UNMAPPED", environ)

    def test_local_settings_follow_claudes_lookup(self):
        expected = (self.sub if os.name == "nt" else self.repo) / ".claude" / "settings.local.json"
        self.assertEqual(self.local, expected)
        self.assertEqual(self.claude.local_settings(self.sub, None), self.sub / ".claude" / "settings.local.json")

    def test_unmapped_prompts_are_blocked_without_writing(self):
        self.assertEqual(self.guard(None), "UNMAPPED")
        self.assertFalse(self.local.exists())

    def test_first_prompt_assigns_the_customer_and_blocks_once(self):
        reason = self.guard()
        self.assertIn("customer:A", reason)
        self.assertIn("Send the prompt again", reason)
        self.assertEqual(self.read(self.local)["env"]["ANTHROPIC_CUSTOM_HEADERS"], HEADERS)
        self.assertIsNone(self.guard(headers=HEADERS))
        self.assertIn("has not loaded", self.guard())

    def test_a_symlinked_local_settings_file_is_replaced_not_followed(self):
        # A cloned repository controls this path; following the link would let it pick the file we write.
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        self.local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.local.symlink_to(outside)
        except OSError:
            self.skipTest("symbolic links are not available")
        self.guard()
        self.assertFalse(self.local.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "{}")

    def test_written_file_is_excluded_from_git(self):
        self.guard()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.repo, capture_output=True, text=True,
            check=True,
        ).stdout
        self.assertNotIn("settings.local.json", status)

    def test_a_changed_assignment_is_rewritten_and_blocks_once(self):
        self.guard()
        other = Attribution("customer:B", "local", "repo bind", "github.com/acme/web")
        reason = self.guard(other, HEADERS)
        self.assertIn("customer:B", reason)
        self.assertIn("x-litellm-tags: customer:B", self.read(self.local)["env"]["ANTHROPIC_CUSTOM_HEADERS"])

    def test_headers_from_run_claude_are_a_deliberate_override(self):
        override = {"headers": "x-litellm-tags: customer:override", CUSTOMER_VARIABLE: "customer:override"}
        self.assertIsNone(self.guard(**override))
        self.assertIsNone(self.guard(None, **override))
        self.assertFalse(self.local.exists())

    def test_an_inherited_override_does_not_cover_other_headers(self):
        # Started from a terminal that `run codex` opened: the variable is inherited, the headers are not its.
        stale = {"headers": "x-litellm-tags: customer:old", CUSTOMER_VARIABLE: "customer:codex"}
        self.assertIn("Send the prompt again", self.guard(**stale))

    def test_headers_from_elsewhere_are_replaced(self):
        self.assertIn("Send the prompt again", self.guard(headers="x-litellm-tags: customer:other"))
        self.assertIn("x-litellm-tags: customer:A", self.read(self.local)["env"]["ANTHROPIC_CUSTOM_HEADERS"])
        self.assertEqual(self.guard(None, headers="x-litellm-tags: customer:other"), "UNMAPPED")

    def test_stale_headers_keep_unmapped_prompts_blocked(self):
        # Claude keeps ANTHROPIC_CUSTOM_HEADERS in its environment after the local file stops setting it.
        self.guard()
        self.assertEqual(self.guard(None, HEADERS), "UNMAPPED")
        self.assertEqual(self.guard(None, HEADERS), "UNMAPPED")
        self.local.unlink()
        other = Attribution("customer:B", "local", "repo bind", "github.com/acme/web")
        self.assertIn("customer:B", self.guard(other, HEADERS))

    def test_unrelated_local_settings_and_headers_are_kept(self):
        self.local.parent.mkdir(parents=True)
        self.local.write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls)"]}, "env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Trace: t", "A": "1"},
        }), encoding="utf-8")
        self.guard()
        data = self.read(self.local)
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(data["env"]["A"], "1")
        self.assertEqual(data["env"]["ANTHROPIC_CUSTOM_HEADERS"], "X-Trace: t\n" + HEADERS)
        self.guard(None, HEADERS)
        self.assertEqual(self.read(self.local)["env"], {"ANTHROPIC_CUSTOM_HEADERS": "X-Trace: t", "A": "1"})


class CliTests(ClaudeTestCase):
    def setUp(self):
        super().setUp()
        self.store = ProfileStore(self.home)
        self.store.add(self.profile)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", "https://github.com/acme/web"],
            check=True, capture_output=True,
        )

    def call(self, *args, stdin="", environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch("sys.stdin", io.StringIO(stdin)), patch.dict(os.environ, environ or {}), \
                patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            result = main(["--home", str(self.home), *args])
        return result, stdout.getvalue(), stderr.getvalue()

    def guard(self, environ=None):
        result, stdout, _ = self.call("claude", "guard", stdin=json.dumps({"cwd": str(self.repo)}), environ=environ)
        self.assertEqual(result, 0)
        return json.loads(stdout)["reason"] if stdout else None

    def test_guard_assigns_from_the_resolver_and_follows_rebinds(self):
        self.assertIn("repo bind", self.guard())
        self.assertEqual(self.call("repo", "bind", "--directory", str(self.repo), "--customer", "customer:A")[0], 0)
        self.assertIn("customer:A", self.guard())
        local = self.claude.local_settings(self.repo, self.repo)
        headers = self.read(local)["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        self.assertIsNone(self.guard({"ANTHROPIC_CUSTOM_HEADERS": headers}))
        result, _, stderr = self.call("repo", "bind", "--directory", str(self.repo), "--customer", "customer:B")
        self.assertEqual(result, 0)
        self.assertIn("Updated", stderr)
        self.assertIn("x-litellm-tags: customer:B", self.read(local)["env"]["ANTHROPIC_CUSTOM_HEADERS"])

    def test_guard_uses_the_directory_claude_started_in(self):
        # After the model runs 'cd sub', the payload's cwd moves but Claude still reads the starting folder's settings.
        self.assertEqual(self.call("repo", "bind", "--directory", str(self.repo), "--customer", "customer:A")[0], 0)
        nested = self.repo / "sub"
        subprocess.run(["git", "init", "--quiet", str(nested)], check=True, capture_output=True)
        result, stdout, _ = self.call(
            "claude", "guard", stdin=json.dumps({"cwd": str(nested)}), environ={"CLAUDE_PROJECT_DIR": str(self.repo)},
        )
        self.assertEqual(result, 0)
        self.assertIn("customer:A", json.loads(stdout)["reason"])
        self.assertTrue(self.claude.local_settings(self.repo, self.repo).is_file())
        self.assertFalse((nested / ".claude").exists())

    def test_guard_fails_closed(self):
        (self.repo / ".vkm").write_text("customer = 'a,b'", encoding="utf-8")
        self.assertIn(".vkm", self.guard())
        with patch("virtual_key_mapper.cli.find_checkout", side_effect=RuntimeError("boom")):
            self.assertIn("could not check", self.guard())

    def test_setup_needs_a_sign_in_and_can_be_removed(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with patch("virtual_key_mapper.cli.NativeKeyring"), patch("virtual_key_mapper.cli.auth_for") as auth:
                auth.return_value.credential.side_effect = CredentialError("No login for 'unit'")
                self.assertEqual(main(["--home", str(self.home), "setup", "claude"]), 1)
                auth.return_value.credential.side_effect = None
                auth.return_value.credential.return_value = Credential("secret")
                self.assertEqual(main(["--home", str(self.home), "setup", "claude", "--model", "alias"]), 0)
            self.assertEqual(main(["--home", str(self.home), "setup", "claude", "--remove"]), 0)
        self.assertIn("No login", stderr.getvalue())
        self.assertIn("disableLoginPrompt", stdout.getvalue())
        self.assertNotIn("apiKeyHelper", self.read())
