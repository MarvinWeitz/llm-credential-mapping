import contextlib
import io
import itertools
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.cli import main
from llm_credential_mapping.editor_settings import _Document, editing
from llm_credential_mapping.errors import CredentialError


WRAPPER = "claudeCode.claudeProcessWrapper"
LOGIN = "claudeCode.disableLoginPrompt"
ENVIRONMENT = "claudeCode.environmentVariables"
HOME = "LLM_CREDENTIAL_MAPPING_HOME"
BUS = "DBUS_SESSION_BUS_ADDRESS"


class ClaudeDisableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.target = self.home / "editor" / "settings.json"
        self.generated = {
            WRAPPER: str(self.home / "old-venv" / "llm-credentials-claude"),
            LOGIN: True,
            ENVIRONMENT: [
                {"name": HOME, "value": str(self.home / "credentials")},
                {"name": BUS, "value": "unix:path=/tmp/fixture-vault/bus"},
            ],
        }

    def write(self, text):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(text.encode("utf-8"))

    def disable(self, source=None):
        if source is not None:
            self.write(source)
        with editing(self.target) as editor:
            changed = editor.disable()
        return changed, self.target.read_bytes().decode("utf-8") if self.target.exists() else None

    def cli(self, target=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = ["--home", str(self.home / "credentials"), "claude", "disable"]
        if target is not None:
            arguments.extend(["--settings-file", str(target)])
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_disconnects_wrapper_and_login_override_but_keeps_bus_and_unrelated_settings(self):
        original = {
            "editor.tabSize": 4,
            **self.generated,
        }
        original[ENVIRONMENT].append({"name": "OTHER", "value": "fixture-secret"})
        changed, saved = self.disable(json.dumps(original))
        self.assertTrue(changed)
        self.assertEqual(json.loads(saved), {
            "editor.tabSize": 4,
            ENVIRONMENT: [
                {"name": BUS, "value": "unix:path=/tmp/fixture-vault/bus"},
                {"name": "OTHER", "value": "fixture-secret"},
            ],
        })

    def test_only_bootstrap_document_becomes_an_empty_object(self):
        original = {**self.generated, ENVIRONMENT: self.generated[ENVIRONMENT][:1]}
        changed, saved = self.disable(json.dumps(original))
        self.assertTrue(changed)
        self.assertEqual(json.loads(saved), {})

    def test_property_order_and_trailing_comma_variants(self):
        fields = [
            (WRAPPER, self.generated[WRAPPER]),
            (LOGIN, True),
            ("unrelated", {"keep": [1, 2]}),
        ]
        for order, trailing in itertools.product(itertools.permutations(fields), (False, True)):
            with self.subTest(order=[name for name, _ in order], trailing=trailing):
                source = "{" + ",".join(json.dumps(name) + ":" + json.dumps(value) for name, value in order)
                source += ("," if trailing else "") + "}"
                saved = _Document(source).disable()
                self.assertIsInstance(saved, str)
                self.assertEqual(_Document(saved).root.value, {"unrelated": {"keep": [1, 2]}})
                if not trailing:
                    self.assertEqual(json.loads(saved), {"unrelated": {"keep": [1, 2]}})

    def test_environment_order_and_trailing_comma_variants(self):
        entries = [*self.generated[ENVIRONMENT], {"name": "OTHER", "value": "keep"}]
        for order, trailing in itertools.product(itertools.permutations(entries), (False, True)):
            with self.subTest(order=[item["name"] for item in order], trailing=trailing):
                array = "[" + ",".join(json.dumps(item) for item in order) + ("," if trailing else "") + "]"
                source = "{" + json.dumps(WRAPPER) + ":" + json.dumps(self.generated[WRAPPER])
                source += "," + json.dumps(ENVIRONMENT) + ":" + array + "}"
                saved = _Document(source).disable()
                self.assertIsInstance(saved, str)
                expected = {ENVIRONMENT: [item for item in order if item["name"] != HOME]}
                self.assertEqual(_Document(saved).root.value, expected)
                if not trailing:
                    self.assertEqual(json.loads(saved), expected)

    def test_preserves_unrelated_comments_bom_crlf_and_permissions(self):
        source = (
            '\ufeff// header\r\n{\r\n'
            '\t"editor.tabSize": 4, // keep this\r\n'
            '\t"claudeCode.claudeProcessWrapper": "C:\\\\old\\\\llm-credentials-claude.exe", // wrapper note\r\n'
            '\t"claudeCode.disableLoginPrompt": true,\r\n'
            '\t"claudeCode.environmentVariables": [\r\n'
            '\t\t/* home */ {"name": "LLM_CREDENTIAL_MAPPING_HOME", "value": "old"},\r\n'
            '\t\t// retain bus\r\n'
            '\t\t{"name": "DBUS_SESSION_BUS_ADDRESS", "value": "unix:path=/bus"},\r\n'
            '\t],\r\n'
            '}\r\n'
        )
        self.write(source)
        mode = stat.S_IMODE(self.target.stat().st_mode)
        changed, saved = self.disable()
        self.assertTrue(changed)
        self.assertTrue(saved.startswith("\ufeff// header\r\n"))
        self.assertIn('\t"editor.tabSize": 4, // keep this\r\n', saved)
        self.assertIn("// wrapper note\r\n", saved)
        self.assertIn("// retain bus\r\n", saved)
        self.assertEqual(saved.count("\n"), saved.count("\r\n"))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), mode)
        self.assertEqual(_Document(saved).root.value, {
            "editor.tabSize": 4, ENVIRONMENT: [{"name": BUS, "value": "unix:path=/bus"}],
        })

    def test_duplicate_owned_properties_and_environment_entries_are_all_removed(self):
        wrapper = json.dumps(self.generated[WRAPPER])
        source = (
            '{"unrelated":1,"unrelated":2,'
            f'"{WRAPPER}":{wrapper},"{WRAPPER}":{wrapper},'
            f'"{LOGIN}":true,"{LOGIN}":false,'
            f'"{ENVIRONMENT}":[{{"name":"{HOME}","value":"first"}},{{"name":"{HOME}","value":"second"}}],'
            f'"{ENVIRONMENT}":[{{"name":"{HOME}","value":"third"}},{{"name":"OTHER","value":"keep"}}]'
            "}"
        )
        changed, saved = self.disable(source)
        self.assertTrue(changed)
        self.assertIn('"unrelated":1,"unrelated":2', saved)
        self.assertNotIn(HOME, saved)
        self.assertEqual(json.loads(saved), {"unrelated": 2, ENVIRONMENT: [{"name": "OTHER", "value": "keep"}]})

    def test_partial_setup_is_recognised_by_the_home_entry_without_a_wrapper(self):
        for wrapper in ({}, {WRAPPER: ""}, {WRAPPER: None}):
            with self.subTest(wrapper=wrapper):
                source = {**wrapper, LOGIN: True, ENVIRONMENT: self.generated[ENVIRONMENT][:1]}
                changed, saved = self.disable(json.dumps(source))
                self.assertTrue(changed)
                self.assertEqual(json.loads(saved), {})

    def test_another_or_conflicting_wrapper_is_rejected_without_exposing_or_modifying_settings(self):
        for other in ("other-wrapper", "llm-credentials-claude-custom", "fixture-secret", 1):
            with self.subTest(other=other):
                original = json.dumps({**self.generated, WRAPPER: other})
                self.write(original)
                with self.assertRaises(CredentialError) as error:
                    self.disable()
                self.assertNotIn("fixture-secret", str(error.exception))
                self.assertEqual(self.target.read_text(), original)
        wrapper = json.dumps(self.generated[WRAPPER])
        self.write(f'{{"{WRAPPER}":{wrapper},"{WRAPPER}":"other"}}')
        with self.assertRaises(CredentialError):
            self.disable()

    def test_no_bootstrap_is_byte_identical_and_does_not_create_a_missing_settings_file(self):
        for source in ("", " ", "// preferences\r\n", "{}", '{"unrelated":true}', json.dumps({LOGIN: True})):
            with self.subTest(source=source):
                self.write(source)
                with patch("llm_credential_mapping.editor_settings.atomic_write_text", side_effect=AssertionError):
                    changed, saved = self.disable()
                self.assertFalse(changed)
                self.assertEqual(saved, source)
        missing = self.home / "missing" / "settings.json"
        with editing(missing) as editor:
            self.assertFalse(editor.disable())
        self.assertFalse(missing.exists())

    def test_repeated_disable_is_byte_identical_and_setup_can_reenable(self):
        self.disable(json.dumps(self.generated))
        saved = self.target.read_bytes()
        with patch("llm_credential_mapping.editor_settings.atomic_write_text", side_effect=AssertionError):
            changed, _ = self.disable()
        self.assertFalse(changed)
        self.assertEqual(self.target.read_bytes(), saved)
        with editing(self.target) as editor:
            editor.apply(self.generated)
        self.assertEqual(json.loads(self.target.read_text()), {
            **self.generated,
            ENVIRONMENT: [self.generated[ENVIRONMENT][1], self.generated[ENVIRONMENT][0]],
        })

    def test_invalid_settings_and_failed_writes_do_not_publish_partial_removal(self):
        for source in ('{"private":"fixture-secret",', f'{{"{ENVIRONMENT}":null}}'):
            with self.subTest(source=source):
                self.write(source)
                with self.assertRaises(CredentialError) as error:
                    self.disable()
                self.assertNotIn("fixture-secret", str(error.exception))
                self.assertEqual(self.target.read_text(), source)
        original = json.dumps(self.generated)
        self.write(original)
        with patch("llm_credential_mapping.profiles.os.replace", side_effect=OSError("fixture-secret")):
            with self.assertRaises(CredentialError) as error:
                self.disable()
        self.assertNotIn("fixture-secret", str(error.exception))
        self.assertEqual(self.target.read_text(), original)

    def test_external_changes_are_detected_for_removal_and_noop(self):
        for source in (json.dumps(self.generated), "{}"):
            with self.subTest(source=source):
                self.write(source)
                with editing(self.target) as editor:
                    self.target.write_bytes(b'{"external":true}')
                    with self.assertRaises(CredentialError):
                        editor.disable()
                self.assertEqual(self.target.read_bytes(), b'{"external":true}')

    def test_cli_preserves_all_cli_state_without_accessing_vault_repository_or_token_helper(self):
        credentials = self.home / "credentials"
        credentials.mkdir()
        snapshots = {}
        for name in ("projects.json", "repositories.json", "workspaces.json", "claude.json"):
            snapshots[name] = b'{"private":"fixture-secret"}\n'
            (credentials / name).write_bytes(snapshots[name])
        self.write(json.dumps(self.generated))
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.current_repository", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.helper_token", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.claude_setup", side_effect=AssertionError):
            result, stdout, stderr = self.cli(self.target)
        self.assertEqual(result, 0, stderr)
        self.assertIn("Disconnected", stdout)
        self.assertIn("Reload", stderr)
        self.assertIn("normal sign-in", stderr)
        self.assertNotIn("fixture-secret", stdout + stderr)
        for name, raw in snapshots.items():
            self.assertEqual((credentials / name).read_bytes(), raw)

    def test_cli_detects_remote_machine_settings_even_with_profiles(self):
        server = self.home / "agent"
        (server / "data" / "User" / "profiles" / "fixture").mkdir(parents=True)
        target = server / "data" / "Machine" / "settings.json"
        target.parent.mkdir()
        target.write_text(json.dumps(self.generated), encoding="utf-8")
        with patch.dict(os.environ, {"VSCODE_AGENT_FOLDER": str(server)}, clear=True):
            result, stdout, stderr = self.cli()
        self.assertEqual(result, 0, stderr)
        self.assertIn("Disconnected", stdout)
        self.assertEqual(json.loads(target.read_text()), {ENVIRONMENT: self.generated[ENVIRONMENT][1:]})

    def test_cli_errors_are_visible_without_printing_existing_settings_or_success(self):
        self.write(json.dumps({**self.generated, WRAPPER: "fixture-secret"}))
        result, stdout, stderr = self.cli(self.target)
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("different Claude process wrapper", stderr)
        self.assertNotIn("fixture-secret", stderr)
        self.write(json.dumps(self.generated))
        with patch("llm_credential_mapping.editor_settings.atomic_write_text", side_effect=PermissionError("fixture-secret")):
            result, stdout, stderr = self.cli(self.target)
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot atomically update", stderr)
        self.assertNotIn("fixture-secret", stderr)

    def test_cli_repeated_disable_reports_noop_without_claiming_a_second_disconnect(self):
        self.write(json.dumps(self.generated))
        self.assertEqual(self.cli(self.target)[0], 0)
        with patch("llm_credential_mapping.editor_settings.atomic_write_text", side_effect=AssertionError):
            result, stdout, stderr = self.cli(self.target)
        self.assertEqual(result, 0, stderr)
        self.assertIn("nothing was changed", stdout)
        self.assertNotIn("Disconnected", stdout)
