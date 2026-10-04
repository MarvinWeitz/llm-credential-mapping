import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from llm_credential_mapping.claude import (
    CONTEXT_VARIABLE, ClaudeConfiguration, ClaudeContext, check, helper_command, helper_token,
    load_configuration, project_directory, setup, verify_wrapper, wrapped_command, wrapper_main,
)
from llm_credential_mapping.cli import main
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.litellm import Session
from llm_credential_mapping.profiles import Profile, ProfileStore, atomic_write_json
from llm_credential_mapping.repositories import RepositoryStore
from test_sessions import MemoryStore


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = ProfileStore(self.home)
        self.profile = Profile("unit", "https://gateway.example", "team-a")
        self.store.add(self.profile)
        self.repository = "github.com/company/repo"
        self.repositories = RepositoryStore(self.store)
        self.repositories.bind(self.profile.gateway, self.repository, "customer:A")
        self.secrets = MemoryStore()
        self.session = Session(
            self.profile.gateway, self.profile.team_id, "employee", "client",
            "access-secret", "refresh-secret", time.time() + 3600,
        )
        self.secrets.put(self.profile.account, self.session.to_dict())
        self.configuration = ClaudeConfiguration("unit", "model-alias")
        self.context = ClaudeContext(
            str(self.home), self.profile.name, self.profile.account, "employee",
            self.repository, "customer:A", "model-alias",
        )

    def save_configuration(self):
        atomic_write_json(self.home / "claude.json", {"version": 1, **self.configuration.__dict__})

    def test_setup_validates_access_and_writes_only_nonsecret_global_adapter_config(self):
        with patch("llm_credential_mapping.claude.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.claude.NativeKeyring", return_value=self.secrets), \
                patch("llm_credential_mapping.claude.verify_wrapper", return_value=Path(sys.executable)):
            result = setup(self.store, "unit", "model-alias")
        self.assertEqual(result, {
            "claudeCode.claudeProcessWrapper": sys.executable,
            "claudeCode.disableLoginPrompt": True,
            "claudeCode.environmentVariables": [
                {"name": "LLM_CREDENTIAL_MAPPING_HOME", "value": str(self.home.resolve())},
            ],
        })
        self.assertEqual(load_configuration(self.store), self.configuration)
        self.assertNotIn("secret", (self.home / "claude.json").read_text())
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".vscode").exists())

    def test_setup_fails_before_writing_when_wrapper_missing_repo_unbound_or_vault_locked(self):
        with patch("llm_credential_mapping.claude.wrapper_path", return_value=self.home / "missing"):
            with self.assertRaises(CredentialError):
                setup(self.store, "unit", "model-alias")
        with patch("llm_credential_mapping.claude.verify_wrapper", return_value=Path(sys.executable)), \
                patch("llm_credential_mapping.claude.current_repository", return_value="github.com/company/unbound"), \
                patch("llm_credential_mapping.claude.NativeKeyring", side_effect=AssertionError):
            with self.assertRaises(CredentialError) as error:
                setup(self.store, "unit", "model-alias")
            self.assertIn("repo bind", str(error.exception))
        with patch("llm_credential_mapping.claude.verify_wrapper", return_value=Path(sys.executable)), \
                patch("llm_credential_mapping.claude.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.claude.NativeKeyring", side_effect=CredentialError("OS vault locked")):
            with self.assertRaises(CredentialError):
                setup(self.store, "unit", "model-alias")
        self.assertFalse((self.home / "claude.json").exists())

    def test_configuration_and_context_validate_types_and_corruption(self):
        for profile, model in ((None, "alias"), ("../unit", "alias"), ("unit", None), ("unit", ""), ("unit", " bad")):
            with self.subTest(profile=profile, model=model), self.assertRaises(CredentialError):
                ClaudeConfiguration(profile, model)
        for value in ("{", '{"version":true,"profile":"unit","model":"alias"}', '{"version":1,"profile":42,"model":"alias"}'):
            (self.home / "claude.json").write_text(value)
            with self.subTest(value=value), self.assertRaises(CredentialError):
                load_configuration(self.store)
        for encoded in ("", "[]", "{", self.context.encode().replace('"version": 1', '"version": true')):
            with patch.dict(os.environ, {CONTEXT_VARIABLE: encoded}), self.assertRaises(CredentialError):
                ClaudeContext.from_environment()
        with patch.dict(os.environ, {CONTEXT_VARIABLE: self.context.encode()}):
            self.assertEqual(ClaudeContext.from_environment(), self.context)

    def test_check_is_nonsecret_and_renews_an_expiring_login(self):
        self.save_configuration()
        self.secrets.put(self.profile.account, replace(self.session, expires_at=time.time() + 1).to_dict())
        client = Mock()
        client.refresh.return_value = replace(self.session, access_token="renewed-secret", refresh_token="rotated-secret")
        with patch("llm_credential_mapping.claude.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.claude.NativeKeyring", return_value=self.secrets), \
                patch("llm_credential_mapping.claude.verify_wrapper", return_value=Path(sys.executable)):
            from llm_credential_mapping.sessions import SessionManager
            manager = SessionManager(self.store, self.secrets, lambda profile: client)
            with patch("llm_credential_mapping.claude.SessionManager", return_value=manager):
                result = check(self.store)
        self.assertEqual(result["customer"], "customer:A")
        self.assertEqual(result["state"], "ready")
        self.assertNotIn("secret", json.dumps(result))
        client.refresh.assert_called_once()

    def test_helper_does_not_reread_global_profile_model_or_repository_mapping(self):
        b = Profile("another-unit", self.profile.gateway, "team-b")
        self.store.add(b)
        self.store.select(b.name)
        self.repositories.bind(self.profile.gateway, self.repository, "customer:B", replace=True)
        atomic_write_json(self.home / "claude.json", {"version": 1, "profile": b.name, "model": "other-model"})
        with patch.dict(os.environ, {CONTEXT_VARIABLE: self.context.encode()}), \
                patch("llm_credential_mapping.claude.NativeKeyring", return_value=self.secrets):
            self.assertEqual(helper_token(), "access-secret")

    def test_helper_rejects_employee_or_profile_changes_and_uncertain_refresh(self):
        with patch.dict(os.environ, {CONTEXT_VARIABLE: self.context.encode()}), \
                patch("llm_credential_mapping.claude.NativeKeyring", return_value=self.secrets):
            for change in ({"user_id": "other-employee"}, {"refresh_pending": True}):
                self.secrets.put(self.profile.account, replace(self.session, **change).to_dict())
                with self.subTest(change=change), self.assertRaises(CredentialError):
                    helper_token()
            self.secrets.put(self.profile.account, self.session.to_dict())
            foreign = replace(self.context, account="changed-account")
            with patch.dict(os.environ, {CONTEXT_VARIABLE: foreign.encode()}), self.assertRaises(CredentialError):
                helper_token()

    def test_wrapped_settings_preserve_hooks_but_replace_static_auth_and_customer(self):
        existing = {
            "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": []}]},
            "permissions": {"deny": ["Bash(rm *)"]},
            "env": {"X_UNRELATED": "preserved", "ANTHROPIC_AUTH_TOKEN": "old-secret", "ANTHROPIC_CUSTOM_HEADERS": "X-Trace: trace"},
        }
        arguments = [
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--settings", json.dumps(existing), "--model", "old-model", "--fallback-model=old-fallback",
        ]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "parent-secret", "CLAUDE_CODE_USE_BEDROCK": "1"}, clear=True):
            command, env = wrapped_command(sys.executable, arguments, self.context, self.profile)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "parent-secret")
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(settings["hooks"], existing["hooks"])
        self.assertEqual(settings["permissions"], existing["permissions"])
        self.assertEqual(settings["env"]["X_UNRELATED"], "preserved")
        self.assertEqual(settings["env"]["ANTHROPIC_CUSTOM_HEADERS"], "X-Trace: trace\nx-litellm-tags: customer:A")
        for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
            self.assertEqual(env[key], "")
            self.assertEqual(settings["env"][key], "")
        self.assertEqual(env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"], "60000")
        self.assertIn("-I", settings["apiKeyHelper"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "model-alias")
        self.assertEqual(command.count("--settings"), 1)
        self.assertEqual(command[command.index("--model") + 1], "model-alias")
        self.assertNotIn("secret", " ".join(command))
        self.assertEqual(command[1:5], ["--input-format", "stream-json", "--output-format", "stream-json"])

    def test_file_settings_and_literal_prompt_arguments_are_preserved(self):
        settings_file = self.home / "claude settings.json"
        settings_file.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
        command, _ = wrapped_command(
            sys.executable, ["--settings=" + str(settings_file), "--", "--model", "literal-text"], self.context, self.profile,
        )
        self.assertEqual(command[-3:], ["--", "--model", "literal-text"])
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(settings["permissions"], {"allow": ["Read"]})
        self.assertEqual(json.loads(settings_file.read_text()), {"permissions": {"allow": ["Read"]}})

    def test_invalid_settings_and_conflicting_auth_headers_are_errors(self):
        for arguments in (
            ["--settings"], ["--model"], ["--settings", "[]"], ["--settings", '{"env":[]}'],
            ["--settings", '{"env":{"X":42}}'], ["--settings", '{"env":{"ANTHROPIC_CUSTOM_HEADERS":"Host: secret"}}'],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CredentialError) as error:
                wrapped_command(sys.executable, arguments, self.context, self.profile)
            self.assertNotIn("secret", str(error.exception))

    def test_helper_command_quotes_python_paths_and_uses_isolated_python(self):
        with patch("llm_credential_mapping.claude.sys.executable", r"C:\Program Files\Python\python.exe"):
            if os.name == "nt":
                self.assertIn('"C:\\Program Files\\Python\\python.exe"', helper_command())
            else:
                import shlex
                self.assertEqual(shlex.split(helper_command())[0], r"C:\Program Files\Python\python.exe")
        if os.name == "nt":
            with patch("llm_credential_mapping.claude.sys.executable", r"C:\bad&path\python.exe"), self.assertRaises(CredentialError):
                helper_command()

    def test_wrapper_self_check_reports_denied_or_failed_execution_without_raw_stderr(self):
        with patch("llm_credential_mapping.claude.wrapper_path", return_value=Path(sys.executable)):
            for failure in (PermissionError("secret"), subprocess.TimeoutExpired("secret", 15)):
                with patch("llm_credential_mapping.claude.subprocess.run", side_effect=failure):
                    with self.assertRaises(CredentialError) as error:
                        verify_wrapper()
                    self.assertNotIn("secret", str(error.exception))
            with patch(
                "llm_credential_mapping.claude.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "secret"),
            ), self.assertRaises(CredentialError) as error:
                verify_wrapper()
            self.assertNotIn("secret", str(error.exception))

    def test_wrapper_stdout_is_reserved_for_the_claude_protocol(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.claude.default_home", return_value=self.home), \
                patch("llm_credential_mapping.claude.load_configuration", return_value=self.configuration), \
                patch("llm_credential_mapping.claude.resolve_context", return_value=(self.context, self.profile)), \
                patch("llm_credential_mapping.claude._execute", return_value=7) as execute, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(wrapper_main([sys.executable, "--input-format", "stream-json"]), 7)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("customer:A", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())
        self.assertEqual(execute.call_args.args[0][0], sys.executable)

    def test_version_probe_needs_no_repository_or_keyring(self):
        with patch("llm_credential_mapping.claude.resolve_context", side_effect=AssertionError), \
                patch("llm_credential_mapping.claude._execute", return_value=0) as execute:
            self.assertEqual(wrapper_main([sys.executable, "--version"]), 0)
        self.assertEqual(execute.call_args.args[0], [sys.executable, "--version"])

    def test_explicit_sdk_project_root_is_used_even_when_process_cwd_is_not_a_repo(self):
        root = self.home / "checkout"
        root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:company/repo.git"],
            check=True, capture_output=True,
        )
        self.save_configuration()
        stderr = io.StringIO()
        with patch("llm_credential_mapping.claude.default_home", return_value=self.home), \
                patch("llm_credential_mapping.claude.Path.cwd", return_value=self.home), \
                patch("llm_credential_mapping.claude.NativeKeyring", return_value=self.secrets), \
                patch("llm_credential_mapping.claude._execute", return_value=0) as execute, \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(wrapper_main([
                sys.executable, "--project-config-root=" + str(root), "--input-format", "stream-json",
            ]), 0)
        self.assertIn("customer:A", stderr.getvalue())
        self.assertIn("--project-config-root=" + str(root), execute.call_args.args[0])

    def test_project_root_argument_forms_and_literal_separator(self):
        self.assertEqual(project_directory([], self.home), self.home)
        for arguments in (["--project-config-root", str(self.home)], ["--project-config-root=" + str(self.home)]):
            self.assertEqual(project_directory(arguments, self.home), self.home.resolve())
        self.assertEqual(project_directory(["--project-config-root=nested"], self.home), (self.home / "nested").resolve())
        self.assertEqual(project_directory(["--", "--project-config-root=literal"], self.home), self.home)
        for arguments in (
            ["--project-config-root"], ["--project-config-root", "--input-format"],
            ["--project-config-root="], ["--project-config-root=bad\npath"],
            ["--project-config-root=a", "--project-config-root=b"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CredentialError):
                project_directory(arguments, self.home)

    def test_failed_config_probe_reports_cwd_and_does_not_guess_the_only_saved_repo(self):
        self.save_configuration()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.claude.default_home", return_value=self.home), \
                patch("llm_credential_mapping.claude.Path.cwd", return_value=self.home), \
                patch.dict(os.environ, {"CLAUDE_CODE_CONFIG_PROBE": "1"}), \
                patch("llm_credential_mapping.claude.NativeKeyring", side_effect=AssertionError), \
                patch("llm_credential_mapping.claude._execute", side_effect=AssertionError), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(wrapper_main([sys.executable, "--input-format", "stream-json"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("cwd=" + json.dumps(str(self.home)), stderr.getvalue())
        self.assertIn("configuration-probe", stderr.getvalue())
        self.assertIn("No fallback repository", stderr.getvalue())

    def test_wrapper_failure_is_stderr_only_and_has_host_diagnostic(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(wrapper_main(["--version"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("claude check", stderr.getvalue())

    def test_internal_helper_cli_has_exact_stdout_and_never_displays_secret_on_failure(self):
        for exception in (False, True):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {CONTEXT_VARIABLE: self.context.encode()}), \
                    patch("llm_credential_mapping.claude.NativeKeyring", return_value=self.secrets), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if exception:
                    self.secrets.put(self.profile.account, replace(self.session, user_id="other-user").to_dict())
                self.assertEqual(main(["claude", "token"]), 1 if exception else 0)
            self.assertEqual(stdout.getvalue(), "" if exception else "access-secret\n")
            self.assertNotIn("access-secret", stderr.getvalue())

    def test_setup_cli_prints_mergeable_json_and_check_is_nonsecret(self):
        expected = {"claudeCode.claudeProcessWrapper": "/installed/wrapper", "claudeCode.disableLoginPrompt": True}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.cli.claude_setup", return_value=expected) as configured, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main([
                "--home", str(self.home), "claude", "setup", "--profile", "unit", "--model", "model-alias",
            ]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        self.assertEqual(configured.call_args.args[1:], ("unit", "model-alias"))
        self.assertIn("Remote User Settings", stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.cli.claude_check", return_value={"state": "ready"}) as checked, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--home", str(self.home), "claude", "check", "--directory", str(self.home)]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"state": "ready"})
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(checked.call_args.args[1], self.home)

    def test_two_child_sessions_and_their_shell_helpers_keep_separate_contexts(self):
        child = (
            "import json, os, subprocess, sys; "
            "settings = json.loads(sys.argv[sys.argv.index('--settings') + 1]); "
            "os.environ.update(settings['env']); "
            "sys.stdin.readline(); "
            "helper = subprocess.run(settings['apiKeyHelper'], shell=True, capture_output=True, text=True, check=True); "
            "print(json.dumps({'customer': os.environ['LLM_CREDENTIAL_MAPPING_CUSTOMER'], "
            "'profile': json.loads(os.environ['LLM_CREDENTIAL_MAPPING_CLAUDE_CONTEXT'])['profile'], "
            "'helper': helper.stdout.strip(), 'headers': os.environ['ANTHROPIC_CUSTOM_HEADERS']}))"
        )
        helper = [sys.executable, "-c", "import os,json;print(json.loads(os.environ['" + CONTEXT_VARIABLE + "'])['customer'])"]
        import shlex
        shell_command = subprocess.list2cmdline(helper) if os.name == "nt" else shlex.join(helper)
        with patch("llm_credential_mapping.claude.helper_command", return_value=shell_command):
            command_a, env_a = wrapped_command(sys.executable, ["-c", child], self.context, self.profile)
            b = replace(self.context, profile="other-unit", customer="customer:B")
            command_b, env_b = wrapped_command(sys.executable, ["-c", child], b, self.profile)
        a = subprocess.Popen(command_a, env=env_a, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.repositories.bind(self.profile.gateway, self.repository, "customer:B", replace=True)
            result_b = subprocess.run(command_b, env=env_b, input="go\n", capture_output=True, text=True, timeout=10, check=True)
            output_a, errors_a = a.communicate("go\n", timeout=10)
            self.assertEqual(a.returncode, 0, errors_a)
            first, second = json.loads(output_a), json.loads(result_b.stdout)
            self.assertEqual(first["customer"], "customer:A")
            self.assertEqual(first["helper"], "customer:A")
            self.assertEqual(second["customer"], "customer:B")
            self.assertEqual(second["helper"], "customer:B")
            self.assertIn("customer:A", first["headers"])
            self.assertIn("customer:B", second["headers"])
        finally:
            if a.poll() is None:
                a.kill()
                a.wait()
