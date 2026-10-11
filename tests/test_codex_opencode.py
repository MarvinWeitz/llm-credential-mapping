import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.adapters import Credential, IntegrationContext
from llm_credential_mapping.cli import main
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.integrations.codex import Codex, config_path as codex_config
from llm_credential_mapping.integrations.common import header_variable
from llm_credential_mapping.integrations.opencode import OpenCode, PLACEHOLDER_KEY, config_path as opencode_config
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.registry import gateway_for
from llm_credential_mapping.shells import SHELLS, assignments, hook
from isolation import isolate_tool_configuration
from live import unavailable

TAG = header_variable("x-litellm-tags")
METADATA = header_variable("x-litellm-spend-logs-metadata")


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.root = isolate_tool_configuration(self)
        self.home = self.root / "credentials"
        self.home.mkdir()
        self.profile = Profile("unit", "https://gateway.example", {"team_id": "t"})
        self.context = IntegrationContext(self.profile, gateway_for(self.profile), "secret", self.home)


class CodexSetupTests(ToolTestCase):
    def test_setup_adds_managed_blocks_around_existing_settings_and_remove_restores_them(self):
        path = codex_config()
        path.parent.mkdir(parents=True)
        original = 'approval_policy = "never"\n\n[profiles.fast]\nmodel = "small"\n'
        path.write_text(original, encoding="utf-8")
        Codex().setup(self.context, "alias")
        Codex().setup(self.context, "alias")
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((config["model_provider"], config["model"], config["approval_policy"]), ("llm-credentials", "alias", "never"))
        provider = config["model_providers"]["llm-credentials"]
        self.assertEqual(provider["base_url"], "https://gateway.example/v1")
        self.assertEqual(provider["env_http_headers"], {"x-litellm-tags": TAG, "x-litellm-spend-logs-metadata": METADATA})
        self.assertEqual(provider["auth"]["command"], sys.executable)
        self.assertEqual(provider["auth"]["args"][-4:], ["token", "--profile", "unit", "--require-attribution-env"])
        self.assertIn(str(self.home), provider["auth"]["args"])
        self.assertEqual(path.read_text(encoding="utf-8").count("llm-credentials tables"), 2)
        Codex().remove(self.context)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_setup_refuses_another_provider_and_keeps_an_existing_model(self):
        path = codex_config()
        path.parent.mkdir(parents=True)
        for content in ('model_provider = "openai"\n', '[model_providers.llm-credentials]\nname = "x"\n', "not toml ="):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(CredentialError):
                Codex().setup(self.context)
            self.assertEqual(path.read_text(encoding="utf-8"), content)
        path.write_text('model = "mine"\n', encoding="utf-8")
        notes = Codex().setup(self.context, "alias")
        self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["model"], "mine")
        self.assertTrue(any("left unchanged" in note for note in notes))


class OpenCodeSetupTests(ToolTestCase):
    def setup_opencode(self, model="alias", listed=None):
        response = {"data": [{"id": name} for name in (listed or [])]} if listed is not None else None
        with patch("llm_credential_mapping.integrations.opencode.shutil.which", return_value=None), \
                patch("llm_credential_mapping.integrations.opencode.JsonTransport.request",
                      return_value=response, side_effect=None if response else CredentialError("offline")) as request:
            notes = OpenCode().setup(self.context, model)
        return notes, request

    def test_setup_registers_provider_plugin_and_models_and_remove_restores(self):
        path = opencode_config()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"theme": "dark", "plugin": ["other-plugin"]}), encoding="utf-8")
        _, request = self.setup_opencode(listed=["alias", "other-model"])
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer secret"})
        config = json.loads(path.read_text(encoding="utf-8"))
        provider = config["provider"]["llm-credentials"]
        self.assertEqual(provider["options"], {"baseURL": "https://gateway.example/v1", "apiKey": PLACEHOLDER_KEY})
        self.assertEqual(sorted(provider["models"]), ["alias", "other-model"])
        self.assertEqual(config["model"], "llm-credentials/alias")
        self.assertEqual(config["plugin"][0], "other-plugin")
        plugin = self.home / "opencode" / "llm-credentials-unit.js"
        self.assertEqual(config["plugin"][1], plugin.as_uri())
        self.assertIn('"profile": "unit"', plugin.read_text(encoding="utf-8"))
        self.assertNotIn("secret", plugin.read_text(encoding="utf-8"))
        self.setup_opencode(listed=["alias"])
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["plugin"]), 2)
        OpenCode().remove(self.context)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"theme": "dark", "plugin": ["other-plugin"]})

    def test_without_a_model_list_the_given_model_is_used_or_setup_fails(self):
        self.setup_opencode("alias", listed=None)
        self.assertEqual(list(json.loads(opencode_config().read_text(encoding="utf-8"))["provider"]["llm-credentials"]["models"]), ["alias"])
        with self.assertRaises(CredentialError):
            self.setup_opencode(None, listed=None)

    def test_commented_or_invalid_configuration_is_left_alone(self):
        path = opencode_config()
        path.parent.mkdir(parents=True)
        path.with_suffix(".jsonc").write_text("{ // mine\n}", encoding="utf-8")
        with self.assertRaises(CredentialError):
            self.setup_opencode()
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(CredentialError):
            self.setup_opencode()
        self.assertEqual(path.read_text(encoding="utf-8"), "{")


class CliTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        ProfileStore(self.home).add(self.profile)
        self.folder = self.root / "work"
        self.folder.mkdir()

    def call(self, *args, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.dict(os.environ, environ or {}), \
                patch("llm_credential_mapping.cli.find_checkout", return_value=None), \
                patch("llm_credential_mapping.customers.Path.home", return_value=self.root), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as auth:
            auth.return_value.credential.return_value = Credential("token-secret")
            result = main(["--home", str(self.home), *args])
        return result, stdout.getvalue(), stderr.getvalue(), auth

    def test_token_for_environment_driven_tools_requires_the_customer_variables(self):
        for name in (TAG, METADATA):
            os.environ.pop(name, None)
        result, stdout, stderr, auth = self.call("token", "--require-attribution-env")
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("shell-init", stderr)
        auth.return_value.credential.assert_not_called()
        result, stdout, _, _ = self.call("token", "--require-attribution-env", environ={TAG: "customer:A", METADATA: "{}"})
        self.assertEqual((result, stdout), (0, "token-secret\n"))

    def test_repo_show_env_sets_or_unsets_the_header_variables(self):
        result, stdout, stderr, _ = self.call("repo", "show", "--env", "bash", "--directory", str(self.folder))
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(stdout.splitlines(), [f"unset {TAG}", f"unset {METADATA}"])
        (self.folder / ".llm-customer").write_text("customer = \"customer:A\"\n", encoding="utf-8")
        _, stdout, _, _ = self.call("repo", "show", "--env", "pwsh", "--directory", str(self.folder))
        self.assertIn(f"$env:{TAG} = 'customer:A'", stdout)
        self.assertIn(f"$env:{METADATA} = '{{\"customer_source\":\"file\"}}'", stdout)
        (self.folder / ".llm-customer").write_text("customer = 'a,b'\n", encoding="utf-8")
        result, stdout, stderr, _ = self.call("repo", "show", "--env", "fish", "--directory", str(self.folder))
        self.assertEqual(result, 0)
        self.assertIn(f"set -e {TAG}", stdout)
        self.assertIn(".llm-customer", stderr)

    def test_shell_init_embeds_this_cli_and_profile(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, stdout, _, _ = self.call("shell-init", shell, "--profile", "unit")
                self.assertEqual(result, 0)
                self.assertIn("repo", stdout)
                self.assertIn("unit", stdout)


class ShellTests(unittest.TestCase):
    VALUES = {"LLM_CREDENTIALS_TEST_A": "it's \"quoted\" $HOME `x` \\n", "LLM_CREDENTIALS_TEST_B": None}

    def shell(self, name):
        if name == "pwsh":
            return shutil.which("pwsh") or shutil.which("powershell")
        found = shutil.which(name)
        if found is None and name == "bash" and os.name == "nt":
            candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe"
            found = str(candidate) if candidate.is_file() else shutil.which("bash", path=r"C:\Program Files\Git\bin")
        return found

    def run_shell(self, name, script):
        executable = self.shell(name)
        if executable is None:
            unavailable(self, name, f"{name} is not installed")
        environment = {**os.environ, "LLM_CREDENTIALS_TEST_B": "stale"}
        with tempfile.TemporaryDirectory() as directory:
            # No user startup files: they could change the environment under test.
            command = {
                "zsh": [executable, "-f", "-c", script],
                "fish": [executable, "--no-config", "-c", script],
            }.get(name, [executable, "-c", script])
            if name == "pwsh":
                # Like a profile: PowerShell reads multi-line functions correctly only from a script file.
                path = Path(directory) / "script.ps1"
                path.write_text(script, encoding="utf-8-sig")
                command = [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
            return subprocess.run(command, capture_output=True, text=True, env=environment, timeout=60, check=False).stdout

    def test_assignments_survive_quotes_and_unset_stale_values(self):
        printers = {
            "bash": 'printf "%s|%s" "$LLM_CREDENTIALS_TEST_A" "${LLM_CREDENTIALS_TEST_B-unset}"',
            "zsh": 'printf "%s|%s" "$LLM_CREDENTIALS_TEST_A" "${LLM_CREDENTIALS_TEST_B-unset}"',
            "fish": 'printf "%s|" "$LLM_CREDENTIALS_TEST_A"; set -q LLM_CREDENTIALS_TEST_B; and printf set; or printf unset',
            "pwsh": '[Console]::Write($env:LLM_CREDENTIALS_TEST_A + "|" + $(if ($env:LLM_CREDENTIALS_TEST_B) { "set" } else { "unset" }))',
        }
        for name, printer in printers.items():
            with self.subTest(shell=name):
                output = self.run_shell(name, assignments(name, self.VALUES) + "\n" + printer)
                self.assertEqual(output.strip(), self.VALUES["LLM_CREDENTIALS_TEST_A"] + "|unset")

    def test_hooks_run_the_command_with_the_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            printer = Path(directory) / "printer.py"
            printer.write_text(
                "import sys\nshell, folder = sys.argv[1], sys.argv[sys.argv.index('--directory') + 1]\n"
                "values = {'LLM_CREDENTIALS_TEST_A': folder}\n"
                "sys.path.insert(0, " + json.dumps(str(Path(__file__).resolve().parents[1] / "src")) + ")\n"
                "from llm_credential_mapping.shells import assignments\nprint(assignments(shell, values))\n",
                encoding="utf-8",
            )
            for name, cd, printer_line in (
                ("bash", f"cd {json.dumps(folder.as_posix())} && __llm_credentials_hook", 'printf "%s" "$LLM_CREDENTIALS_TEST_A"'),
                ("pwsh", f"Set-Location -LiteralPath '{folder}'; prompt | Out-Null", "[Console]::Write($env:LLM_CREDENTIALS_TEST_A)"),
                # zsh (chpwd) and fish (PWD variable event) run the hook on cd, also in scripts.
                ("zsh", f"cd {json.dumps(folder.as_posix())}", 'printf "%s" "$LLM_CREDENTIALS_TEST_A"'),
                ("fish", f"cd {json.dumps(folder.as_posix())}", 'printf "%s" "$LLM_CREDENTIALS_TEST_A"'),
            ):
                with self.subTest(shell=name):
                    argv = [sys.executable, str(printer), name]
                    if name == "bash" and os.name == "nt":
                        argv = [Path(arg).as_posix() if index < 2 else arg for index, arg in enumerate(argv)]
                    output = self.run_shell(name, hook(name, argv) + "\n" + cd + "\n" + printer_line)
                    self.assertEqual(Path(output.strip()).resolve(), folder)
