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

from virtual_key_mapper.adapters import Credential, IntegrationContext
from virtual_key_mapper.cli import main
from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.integrations.codex import Codex, config_path as codex_config
from virtual_key_mapper.integrations.common import ATTRIBUTION_VARIABLE, header_variable
from virtual_key_mapper.integrations.opencode import OpenCode, PLACEHOLDER_KEY, config_path as opencode_config
from virtual_key_mapper.labels import LabelDefinition, OutputOverride
from virtual_key_mapper.profiles import Profile, ProfileStore
from virtual_key_mapper.registry import gateway_for
from virtual_key_mapper.shells import SHELLS, assignments, hook, without_daemon
from isolation import isolate_tool_configuration
from live import unavailable

TAG = header_variable("x-litellm-tags")
METADATA = header_variable("x-litellm-spend-logs-metadata")


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.root = isolate_tool_configuration(self)
        self.home = self.root / "credentials"
        self.home.mkdir()
        self.profile = Profile(
            "unit", "https://gateway.example", {"team_id": "t"},
            labels=(LabelDefinition("customer", required=True), LabelDefinition("project")),
        )
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
        self.assertEqual((config["model_provider"], config["model"], config["approval_policy"]), ("vkm", "alias", "never"))
        provider = config["model_providers"]["vkm"]
        self.assertEqual(provider["base_url"], "https://gateway.example/v1")
        self.assertEqual(provider["env_http_headers"], {"x-litellm-tags": TAG, "x-litellm-spend-logs-metadata": METADATA})
        self.assertEqual(provider["auth"]["command"], sys.executable)
        self.assertEqual(provider["auth"]["args"][-4:], ["token", "--profile", "unit", "--require-attribution-env"])
        self.assertIn(str(self.home), provider["auth"]["args"])
        self.assertEqual(path.read_text(encoding="utf-8").count("vkm tables"), 2)
        Codex().remove(self.context)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_setup_registers_headers_that_are_switched_off(self):
        # config.toml is written once: a later change to the outputs must not need a new setup.
        profile = Profile("unit", "https://gateway.example", {"team_id": "t"}, outputs={"metadata": OutputOverride(None, False)})
        Codex().setup(IntegrationContext(profile, gateway_for(profile), "secret", self.home))
        provider = tomllib.loads(codex_config().read_text(encoding="utf-8"))["model_providers"]["vkm"]
        self.assertEqual(provider["env_http_headers"], {"x-litellm-tags": TAG, "x-litellm-spend-logs-metadata": METADATA})

    def test_setup_refuses_another_provider_and_keeps_an_existing_model(self):
        path = codex_config()
        path.parent.mkdir(parents=True)
        for content in ('model_provider = "openai"\n', '[model_providers.vkm]\nname = "x"\n', "not toml ="):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(CredentialError):
                Codex().setup(self.context)
            self.assertEqual(path.read_text(encoding="utf-8"), content)
        path.write_text('model = "mine"\n', encoding="utf-8")
        notes = Codex().setup(self.context, "alias")
        self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["model"], "mine")
        self.assertTrue(any("left unchanged" in note for note in notes))


    def test_setup_and_run_keep_codex_out_of_its_shared_daemon(self):
        # A shared daemon keeps the environment of the terminal that started it: another folder's customer.
        path = codex_config()
        path.parent.mkdir(parents=True)
        Codex().setup(self.context)
        self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["features"], {"daemon_auto_start": False})
        argv, _ = Codex().command(self.context, None, ["exec", "hi"], None)
        self.assertEqual(argv[:2], ["codex", "--no-daemon"])
        self.assertEqual(argv[-2:], ["exec", "hi"])
        Codex().remove(self.context)
        own = "[features]\nweb_search = true\n"
        path.write_text(own, encoding="utf-8")
        notes = Codex().setup(self.context)
        self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["features"], {"web_search": True})
        self.assertTrue(any("daemon_auto_start = false" in note for note in notes))
        Codex().remove(self.context)
        self.assertEqual(path.read_text(encoding="utf-8"), own)


class OpenCodeSetupTests(ToolTestCase):
    def setup_opencode(self, model="alias", listed=None):
        response = {"data": [{"id": name} for name in (listed or [])]} if listed is not None else None
        with patch("virtual_key_mapper.integrations.opencode.shutil.which", return_value=None), \
                patch("virtual_key_mapper.integrations.opencode.JsonTransport.request",
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
        provider = config["provider"]["vkm"]
        self.assertEqual(provider["options"], {"baseURL": "https://gateway.example/v1", "apiKey": PLACEHOLDER_KEY})
        self.assertEqual(sorted(provider["models"]), ["alias", "other-model"])
        self.assertEqual(config["model"], "vkm/alias")
        self.assertEqual(config["plugin"][0], "other-plugin")
        plugin = self.home / "opencode" / "vkm-unit.js"
        self.assertEqual(config["plugin"][1], plugin.as_uri())
        self.assertIn('"profile": "unit"', plugin.read_text(encoding="utf-8"))
        self.assertIn('"headers": ["x-litellm-tags", "x-litellm-spend-logs-metadata"]', plugin.read_text(encoding="utf-8"))
        self.assertIn('"repo", "show", "--json", "--launch"', plugin.read_text(encoding="utf-8"))
        self.assertNotIn("secret", plugin.read_text(encoding="utf-8"))
        self.setup_opencode(listed=["alias"])
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["plugin"]), 2)
        OpenCode().remove(self.context)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"theme": "dark", "plugin": ["other-plugin"]})

    def test_without_a_model_list_the_given_model_is_used_or_setup_fails(self):
        self.setup_opencode("alias", listed=None)
        self.assertEqual(list(json.loads(opencode_config().read_text(encoding="utf-8"))["provider"]["vkm"]["models"]), ["alias"])
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


PLUGIN_CLI = """
import json, os, sys
args = sys.argv[1:]
if "token" in args:
    print(json.dumps({"access_token": "fresh", "expires_at": None}))
else:
    with open(os.environ["VKM_TEST_CALLS"], "a") as calls:
        calls.write(json.dumps(args) + "\\n")
    print(os.environ["VKM_TEST_SHOWN"])
"""

PLUGIN_HARNESS = """
const { Vkm } = await import(process.argv[1]);
const hooks = await Vkm({ directory: "/work" });
const output = { headers: JSON.parse(process.argv[2]) };
try {
  await hooks["chat.headers"]({ model: { providerID: "vkm" }, provider: { options: { baseURL: process.argv[3] } } }, output);
  process.stdout.write(JSON.stringify(output.headers));
} catch (error) {
  process.stdout.write("ERROR " + error.message);
}
"""


class OpenCodePluginTests(ToolTestCase):
    """The generated plugin itself, under Node, with a stand-in CLI."""

    def call(self, shown, headers=None):
        node = shutil.which("node")
        if node is None:
            unavailable(self, "opencode", "node is not installed")
        stub = self.root / "cli.py"
        stub.write_text(PLUGIN_CLI, encoding="utf-8")
        context = IntegrationContext(self.profile, gateway_for(self.profile), "", self.home, cli=(sys.executable, str(stub)))
        plugin = OpenCode().plugin(context)
        calls = self.root / "calls.txt"
        result = subprocess.run(
            [node, "--input-type=module", "-e", PLUGIN_HARNESS, plugin.as_uri(), json.dumps(headers or {}),
             "https://gateway.example/v1"],
            capture_output=True, text=True, timeout=60, check=False,
            env={**os.environ, "VKM_TEST_SHOWN": json.dumps(shown), "VKM_TEST_CALLS": str(calls)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]

    def test_complete_labels_replace_every_attribution_header(self):
        shown = {"complete": True, "headers": {"x-litellm-tags": "customer:acme"}, "message": None}
        stdout, calls = self.call(shown, {"X-LiteLLM-Tags": "stale", "X-LiteLLM-Spend-Logs-Metadata": "{}", "X-Trace": "t"})
        self.assertEqual(json.loads(stdout), {"X-Trace": "t", "x-litellm-tags": "customer:acme", "Authorization": "Bearer fresh"})
        self.assertEqual([call[2:] for call in calls], [["repo", "show", "--json", "--launch", "--profile", "unit", "--directory", "/work"]])

    def test_incomplete_labels_block_the_request(self):
        message = "Required labels have no value: customer for folder /work. Nothing was sent."
        for shown in (
            {"complete": False, "headers": {}, "message": message},
            {"headers": {"x-litellm-tags": "customer:acme"}},
            {"complete": True, "headers": None},
        ):
            with self.subTest(shown=shown):
                stdout, _ = self.call(shown)
                self.assertTrue(stdout.startswith("ERROR vkm: "), stdout)
                self.assertNotIn("fresh", stdout)
        self.assertIn(message, self.call({"complete": False, "headers": {}, "message": message})[0])


class CliTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        ProfileStore(self.home).add(self.profile)
        self.folder = self.root / "work"
        self.folder.mkdir()

    def call(self, *args, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.dict(os.environ, environ or {}), \
                patch("virtual_key_mapper.cli.find_checkout", return_value=None), \
                patch("virtual_key_mapper.resolve.Path.home", return_value=self.root), \
                patch("virtual_key_mapper.cli.NativeKeyring"), \
                patch("virtual_key_mapper.cli.auth_for") as auth:
            auth.return_value.credential.return_value = Credential("token-secret")
            result = main(["--home", str(self.home), *args])
        return result, stdout.getvalue(), stderr.getvalue(), auth

    def test_token_for_environment_driven_tools_requires_the_attribution_variable(self):
        for name in (TAG, METADATA, ATTRIBUTION_VARIABLE):
            os.environ.pop(name, None)
        result, stdout, stderr, auth = self.call("token", "--require-attribution-env", environ={TAG: "customer:acme"})
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("shell-init", stderr)
        auth.return_value.credential.assert_not_called()
        # Omitted headers (an optional label without a value, an output switched off) do not refuse.
        result, stdout, _, _ = self.call("token", "--require-attribution-env", environ={ATTRIBUTION_VARIABLE: "1"})
        self.assertEqual((result, stdout), (0, "token-secret\n"))

    def test_repo_show_env_sets_or_unsets_the_header_variables(self):
        unset = [f"unset {TAG}", f"unset {METADATA}", f"unset {ATTRIBUTION_VARIABLE}"]
        result, stdout, stderr, _ = self.call("repo", "show", "--env", "bash", "--directory", str(self.folder))
        self.assertEqual(result, 0)
        self.assertEqual(stdout.splitlines(), unset)  # The required customer has no value: nothing is set.
        (self.folder / ".vkm").write_text('[labels]\ncustomer = "acme"\nproject = "web"\n', encoding="utf-8")
        _, stdout, _, _ = self.call("repo", "show", "--env", "pwsh", "--directory", str(self.folder))
        self.assertIn(f"$env:{TAG} = 'customer:acme,project:web'", stdout)
        self.assertIn(
            f'$env:{METADATA} = \'{{"labels":{{"customer":"acme","project":"web"}},"sources":{{"customer":"file","project":"file"}}}}\'',
            stdout,
        )
        self.assertIn(f"$env:{ATTRIBUTION_VARIABLE} = '1'", stdout)
        self.assertNotIn("VKM_LABEL_", stdout)
        self.assertNotIn("VKM_LAUNCH_LABELS", stdout)
        (self.folder / ".vkm").write_text('[labels]\ncustomer = "acme"\n', encoding="utf-8")
        _, stdout, _, _ = self.call("repo", "show", "--env", "bash", "--directory", str(self.folder))
        self.assertIn(f"export {TAG}=customer:acme", stdout)
        (self.folder / ".vkm").write_text("[labels]\ncustomer = 'a,b'\n", encoding="utf-8")
        result, stdout, stderr, _ = self.call("repo", "show", "--env", "fish", "--directory", str(self.folder))
        self.assertEqual(result, 0)
        self.assertIn(f"set -e {TAG}", stdout)
        self.assertIn(f"set -e {ATTRIBUTION_VARIABLE}", stdout)
        self.assertIn(".vkm", stderr)

    def test_repo_show_env_unsets_everything_when_the_labels_cannot_be_resolved(self):
        stale = {TAG: "customer:stale", "VKM_HEADER_X_OLD": "x", ATTRIBUTION_VARIABLE: "1"}
        expected = [f"unset {name}" for name in (TAG, METADATA, "VKM_HEADER_X_OLD", ATTRIBUTION_VARIABLE)]
        with patch("virtual_key_mapper.cli.resolve_labels", side_effect=RuntimeError("boom")):
            result, stdout, stderr, _ = self.call("repo", "show", "--env", "bash", "--directory", str(self.folder), environ=stale)
        self.assertEqual((result, stdout.splitlines()), (0, expected))
        self.assertIn("could not be resolved", stderr)
        # A profile that cannot be loaded (renamed, or a corrupt profiles.json) still unsets what the environment has.
        for args, broken in ((["--profile", "renamed"], None), ([], "{")):
            if broken is not None:
                (self.home / "profiles.json").write_text(broken, encoding="utf-8")
            with self.subTest(args=args):
                result, stdout, _, _ = self.call(
                    "repo", "show", "--env", "bash", "--directory", str(self.folder), *args, environ=stale,
                )
                self.assertEqual(result, 0)
                self.assertEqual(stdout.splitlines(), [f"unset {name}" for name in (TAG, "VKM_HEADER_X_OLD", ATTRIBUTION_VARIABLE)])

    def test_terminal_command_loads_the_hook_without_a_script_file(self):
        # For machines whose execution policy blocks profiles: -Command is not a script file.
        result, stdout, _, _ = self.call("shell-init", "pwsh", "--terminal", "--profile", "unit")
        self.assertEqual(result, 0)
        settings = json.loads("{" + stdout.split("(JSON):\n", 1)[1] + "}")
        args = settings["terminal.integrated.profiles.windows"]["PowerShell (vkm)"]["args"]
        self.assertEqual(args[:2], ["-NoExit", "-Command"])
        self.assertIn(f'-NoExit -Command "{args[2]}"', stdout)
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            unavailable(self, "pwsh", "pwsh is not installed")
        check = subprocess.run(
            [executable, "-NoProfile", "-Command", args[2] + "; [Console]::Write((Get-Command codex).CommandType)"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(check.stdout.strip(), "Function", check.stderr)
        self.assertEqual(self.call("shell-init", "bash", "--terminal")[0], 1)

    def test_shell_init_embeds_this_cli_and_profile(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, stdout, _, _ = self.call("shell-init", shell, "--profile", "unit")
                self.assertEqual(result, 0)
                self.assertIn("repo", stdout)
                self.assertIn("unit", stdout)
                self.assertIn(ATTRIBUTION_VARIABLE, stdout)  # Unset before vkm runs, so a failing vkm cannot leave it set.


class ShellTests(unittest.TestCase):
    VALUES = {"VKM_TEST_A": "it's \"quoted\" $HOME `x` \\n", "VKM_TEST_B": None}

    def shell(self, name):
        if name == "pwsh":
            return shutil.which("pwsh") or shutil.which("powershell")
        found = shutil.which(name)
        if found is None and name == "bash" and os.name == "nt":
            candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe"
            found = str(candidate) if candidate.is_file() else shutil.which("bash", path=r"C:\Program Files\Git\bin")
        return found

    def run_shell(self, name, script, environment=None):
        executable = self.shell(name)
        if executable is None:
            unavailable(self, name, f"{name} is not installed")
        environment = {**os.environ, "VKM_TEST_B": "stale", **(environment or {})}
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
            "bash": 'printf "%s|%s" "$VKM_TEST_A" "${VKM_TEST_B-unset}"',
            "zsh": 'printf "%s|%s" "$VKM_TEST_A" "${VKM_TEST_B-unset}"',
            "fish": 'printf "%s|" "$VKM_TEST_A"; set -q VKM_TEST_B; and printf set; or printf unset',
            "pwsh": '[Console]::Write($env:VKM_TEST_A + "|" + $(if ($env:VKM_TEST_B) { "set" } else { "unset" }))',
        }
        for name, printer in printers.items():
            with self.subTest(shell=name):
                output = self.run_shell(name, assignments(name, self.VALUES) + "\n" + printer)
                self.assertEqual(output.strip(), self.VALUES["VKM_TEST_A"] + "|unset")

    def test_hooks_run_the_command_with_the_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            printer = Path(directory) / "printer.py"
            printer.write_text(
                "import sys\nshell, folder = sys.argv[1], sys.argv[sys.argv.index('--directory') + 1]\n"
                "values = {'VKM_TEST_A': folder}\n"
                "sys.path.insert(0, " + json.dumps(str(Path(__file__).resolve().parents[1] / "src")) + ")\n"
                "from virtual_key_mapper.shells import assignments\nprint(assignments(shell, values))\n",
                encoding="utf-8",
            )
            for name, cd, printer_line in (
                ("bash", f"cd {json.dumps(folder.as_posix())} && __vkm_hook", 'printf "%s" "$VKM_TEST_A"'),
                ("pwsh", f"Set-Location -LiteralPath '{folder}'; prompt | Out-Null", "[Console]::Write($env:VKM_TEST_A)"),
                # zsh (chpwd) and fish (PWD variable event) run the hook on cd, also in scripts.
                ("zsh", f"cd {json.dumps(folder.as_posix())}", 'printf "%s" "$VKM_TEST_A"'),
                ("fish", f"cd {json.dumps(folder.as_posix())}", 'printf "%s" "$VKM_TEST_A"'),
            ):
                with self.subTest(shell=name):
                    argv = [sys.executable, str(printer), name]
                    if name == "bash" and os.name == "nt":
                        argv = [Path(arg).as_posix() if index < 2 else arg for index, arg in enumerate(argv)]
                    output = self.run_shell(name, hook(name, argv) + "\n" + cd + "\n" + printer_line)
                    self.assertEqual(Path(output.strip()).resolve(), folder)

    def test_codex_starts_without_the_shared_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            # A stand-in codex that prints its arguments: a script for POSIX shells, a .cmd for PowerShell on Windows.
            (folder / "codex").write_text('#!/bin/sh\nprintf "%s " "$@"\n', encoding="utf-8")
            (folder / "codex").chmod(0o755)
            (folder / "codex.cmd").write_text("@echo %*\r\n", encoding="utf-8")
            path = {"PATH": str(folder) + os.pathsep + os.environ.get("PATH", "")}
            for name in ("bash", "zsh", "fish", "pwsh"):
                with self.subTest(shell=name):
                    output = self.run_shell(name, without_daemon(name) + "\ncodex exec hi", path)
                    self.assertEqual(output.split(), ["--no-daemon", "exec", "hi"])

    def test_hooks_unset_the_attribution_variable_when_the_command_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve()
            for name, cd, printer in (
                ("bash", f"cd {json.dumps(folder.as_posix())} && __vkm_hook", 'printf "%s" "${VKM_TEST_B-unset}"'),
                ("zsh", f"cd {json.dumps(folder.as_posix())}", 'printf "%s" "${VKM_TEST_B-unset}"'),
                ("fish", f"cd {json.dumps(folder.as_posix())}", "set -q VKM_TEST_B; and printf set; or printf unset"),
                (
                    "pwsh", f"Set-Location -LiteralPath '{folder}'; prompt | Out-Null",
                    '[Console]::Write($(if ($env:VKM_TEST_B) { "set" } else { "unset" }))',
                ),
            ):
                with self.subTest(shell=name):
                    argv = [sys.executable, "-c", "raise SystemExit(1)"]
                    output = self.run_shell(name, hook(name, argv, ("VKM_TEST_B",)) + "\n" + cd + "\n" + printer)
                    self.assertEqual(output.strip(), "unset")

    def test_pwsh_hook_survives_loading_the_profile_twice(self):
        # '. $PROFILE' after an edit must not make the wrapper call itself.
        snippet = hook("pwsh", [sys.executable, "-c", "pass"])
        script = "function global:prompt { 'PS> ' }\n" + snippet + "\n" + snippet + "\n[Console]::Write((prompt) + '|')"
        # A profile that defines its own prompt again (a prompt framework) keeps the new one.
        script += "\nfunction global:prompt { 'NEW> ' }\n" + snippet + "\n[Console]::Write((prompt))"
        self.assertEqual(self.run_shell("pwsh", script), "PS> |NEW> ")

    def test_bash_hook_keeps_the_exit_status_for_later_prompt_commands(self):
        # Prompt frameworks after the hook in PROMPT_COMMAND show the last command's status from $?.
        script = hook("bash", ["true"]) + '\n(exit 7); __vkm_hook; printf "%s|" "$?"'
        self.assertEqual(self.run_shell("bash", script + '; (exit 3); __vkm_hook; printf "%s" "$?"'), "7|3")
