import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from filelock import Timeout

from llm_credential_mapping.cli import main
from llm_credential_mapping.editor_settings import SettingsEdit, _Document, editing, settings_file
from llm_credential_mapping.errors import CredentialError


WRAPPER = "claudeCode.claudeProcessWrapper"
LOGIN = "claudeCode.disableLoginPrompt"
ENVIRONMENT = "claudeCode.environmentVariables"


class EditorSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.target = self.home / "editor" / "settings.json"
        self.generated = {
            WRAPPER: str(self.home / "llm-credentials-claude"),
            LOGIN: True,
            ENVIRONMENT: [
                {"name": "LLM_CREDENTIAL_MAPPING_HOME", "value": str(self.home / "credentials")},
                {"name": "DBUS_SESSION_BUS_ADDRESS", "value": "unix:path=/tmp/vault-fixture/bus"},
            ],
        }

    def write(self, source):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(source.encode("utf-8"))

    def apply(self, source=None):
        if source is not None:
            self.write(source)
        with editing(self.target) as editor:
            editor.apply(self.generated)
        return self.target.read_bytes().decode("utf-8")

    def test_creates_readable_settings_and_is_byte_identical_when_applied_again(self):
        first = self.apply()
        self.assertEqual(json.loads(first), self.generated)
        with patch("llm_credential_mapping.editor_settings.atomic_write_text", side_effect=AssertionError):
            self.assertEqual(self.apply(), first)

    def test_preserves_comments_trailing_commas_unicode_bom_and_crlf(self):
        original = (
            '\ufeff// preferences\r\n{\r\n'
            '\t"editor.fontSize": 15, // keep this\r\n'
            '\t"unrelated": {"url": "https://example/a/*b*/", "text": "\u00e4,} //",},\r\n'
            '\t/* preserve */ "claudeCode.disableLoginPrompt": false,\r\n'
            '}\r\n'
        )
        updated = self.apply(original)
        parsed = _Document(updated).root.value
        self.assertEqual(parsed["editor.fontSize"], 15)
        self.assertEqual(parsed["unrelated"], {"url": "https://example/a/*b*/", "text": "\u00e4,} //"})
        self.assertTrue(updated.startswith("\ufeff// preferences\r\n"))
        self.assertIn('\t"editor.fontSize": 15, // keep this\r\n', updated)
        self.assertIn('\t/* preserve */ "claudeCode.disableLoginPrompt": true,\r\n', updated)
        self.assertEqual(updated.count("\n"), updated.count("\r\n"))
        self.assertEqual(self.apply(), updated)

    def test_merges_only_generated_environment_values_and_keeps_unrelated_entries(self):
        original = (
            '{\n  "claudeCode.environmentVariables": [\n'
            '    // Proxy configuration\n'
            '    {"value": "https://proxy.example", "name": "HTTPS_PROXY"},\n'
            '    {"name": "LLM_CREDENTIAL_MAPPING_HOME", /* old home */ "value": "/old"},\n'
            '    {"name": "LLM_CREDENTIAL_MAPPING_HOME", "value": "/duplicate"},\n'
            '    {"name": "DBUS_SESSION_BUS_ADDRESS", "value": "unix:path=/old-bus"},\n'
            '  ],\n  "editor.tabSize": 4\n}\n'
        )
        updated = self.apply(original)
        data = _Document(updated).root.value
        self.assertIn('// Proxy configuration\n    {"value": "https://proxy.example", "name": "HTTPS_PROXY"},', updated)
        self.assertIn("/* old home */", updated)
        self.assertEqual(data["editor.tabSize"], 4)
        rows = data[ENVIRONMENT]
        self.assertEqual(rows[0], {"value": "https://proxy.example", "name": "HTTPS_PROXY"})
        self.assertEqual(len(rows), 4)
        for row in rows[1:]:
            expected = next(entry["value"] for entry in self.generated[ENVIRONMENT] if entry["name"] == row["name"])
            self.assertEqual(row["value"], expected)
        self.assertEqual(self.apply(), updated)

    def test_appends_missing_environment_entries_without_dropping_array_comments(self):
        original = (
            '{"claudeCode.environmentVariables": [\n'
            '  {"name":"X","value":"unchanged"} // last entry\n'
            '], "other": [1,2,3]}'
        )
        updated = self.apply(original)
        self.assertIn('{"name":"X","value":"unchanged"}, // last entry', updated)
        self.assertIn('"other": [1,2,3]', updated)
        self.assertEqual(len(_Document(updated).root.value[ENVIRONMENT]), 3)
        self.assertEqual(self.apply(), updated)

    def test_supports_compact_empty_and_comments_only_documents(self):
        for source in (
            "", " ", "// preferences", "/* preferences */\n", "\r\n", "\ufeff// preferences\r\n",
            "/* preferences */\r\n", "{}", '{"x":1}', '{"x":1,}', '{"x":[1,2,],}',
        ):
            with self.subTest(source=source):
                updated = self.apply(source)
                data = _Document(updated).root.value
                for key, value in self.generated.items():
                    self.assertEqual(data[key], value)
                if "\r\n" in source:
                    self.assertEqual(updated.count("\n"), updated.count("\r\n"))
                self.assertEqual(self.apply(), updated)

    def test_owned_login_setting_replaces_equal_numeric_values_with_a_boolean(self):
        for original in (1, 1.0):
            with self.subTest(original=original):
                data = json.loads(self.apply(json.dumps({LOGIN: original})))
                self.assertIs(data[LOGIN], True)

    def test_missing_generated_bus_does_not_remove_an_existing_user_environment_entry(self):
        original = {ENVIRONMENT: [{"name": "DBUS_SESSION_BUS_ADDRESS", "value": "unix:path=/user-bus"}]}
        self.generated[ENVIRONMENT] = self.generated[ENVIRONMENT][:1]
        data = json.loads(self.apply(json.dumps(original)))
        self.assertIn(original[ENVIRONMENT][0], data[ENVIRONMENT])

    def test_duplicate_owned_properties_are_updated_without_changing_other_duplicates(self):
        source = '{"other":1,"other":2,"claudeCode.disableLoginPrompt":false,"claudeCode.disableLoginPrompt":false}'
        updated = self.apply(source)
        self.assertIn('"other":1,"other":2', updated)
        self.assertEqual(updated.count('"claudeCode.disableLoginPrompt":true'), 2)

    def test_invalid_jsonc_and_environment_shapes_are_not_overwritten_or_echoed(self):
        for source in (
            "{,", "{,}", "[,]", '{"x":,}', '{"x":[1,,]}', '{"x":NaN}', '{"x":Infinity}', '{"x":/*unfinished',
            '{"x":"unterminated}', '[]', 'null', '{"private": "fixture-secret",',
            '{"claudeCode.environmentVariables":null}', '{"claudeCode.environmentVariables":{}}',
            '{"claudeCode.environmentVariables":[{"name":"X","value":42}]}',
        ):
            with self.subTest(source=source):
                self.write(source)
                with self.assertRaises(CredentialError) as error:
                    with editing(self.target) as editor:
                        editor.apply(self.generated)
                self.assertNotIn("fixture-secret", str(error.exception))
                self.assertEqual(self.target.read_bytes(), source.encode("utf-8"))

    def test_decoder_recursion_errors_are_reported_without_echoing_or_overwriting_settings(self):
        self.write('{"private":"fixture-secret"}')
        with patch("llm_credential_mapping.editor_settings.json.loads", side_effect=RecursionError("fixture-secret")):
            with self.assertRaises(CredentialError) as error:
                with editing(self.target):
                    self.fail("Invalid settings must fail before entering the edit.")
        self.assertNotIn("fixture-secret", str(error.exception))
        self.assertEqual(self.target.read_bytes(), b'{"private":"fixture-secret"}')

    def test_new_file_write_failure_does_not_publish_partial_settings(self):
        with editing(self.target) as editor:
            with patch("llm_credential_mapping.profiles.os.replace", side_effect=OSError("fixture-secret")):
                with self.assertRaises(CredentialError) as error:
                    editor.apply(self.generated)
        self.assertNotIn("fixture-secret", str(error.exception))
        self.assertFalse(self.target.exists())
        self.assertLessEqual({item.name for item in self.target.parent.iterdir()}, {"settings.json.llm-credentials.lock"})

    def test_atomic_failure_keeps_existing_settings_and_permissions(self):
        self.write('{"other":true}\n')
        mode = stat.S_IMODE(self.target.stat().st_mode)
        with editing(self.target) as editor:
            with patch("llm_credential_mapping.profiles.os.replace", side_effect=OSError("full")), self.assertRaises(CredentialError):
                editor.apply(self.generated)
        self.assertEqual(self.target.read_bytes(), b'{"other":true}\n')
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), mode)
        self.apply()
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), mode)

    def test_external_editor_changes_are_rejected_instead_of_overwritten(self):
        self.write("{}")
        with editing(self.target) as editor:
            self.target.write_text('{"external":true}')
            with self.assertRaises(CredentialError) as error:
                editor.apply(self.generated)
        self.assertIn("changed during setup", str(error.exception))
        self.assertEqual(json.loads(self.target.read_text()), {"external": True})

    def test_external_permission_changes_are_rejected_without_restoring_old_permissions(self):
        self.write("{}")
        mode = stat.S_IMODE(self.target.stat().st_mode)
        try:
            with editing(self.target) as editor:
                self.target.chmod(stat.S_IRUSR)
                with self.assertRaises(CredentialError) as error:
                    editor.apply(self.generated)
            self.assertIn("permissions changed during setup", str(error.exception))
            self.assertFalse(stat.S_IMODE(self.target.stat().st_mode) & stat.S_IWUSR)
            self.assertEqual(self.target.read_bytes(), b"{}")
        finally:
            self.target.chmod(mode)

    def test_output_size_limit_is_checked_before_writing(self):
        self.write(" " * 2_097_152)
        with editing(self.target) as editor:
            with self.assertRaises(CredentialError) as error:
                editor.apply(self.generated)
        self.assertIn("Resulting VS Code settings exceed 2 MiB", str(error.exception))
        self.assertEqual(self.target.read_bytes(), b" " * 2_097_152)

    def test_read_errors_encoding_size_and_read_only_files_fail_explicitly(self):
        self.target.parent.mkdir()
        for raw in (b"\xff", b" " * 2_097_153):
            self.target.write_bytes(raw)
            with self.assertRaises(CredentialError):
                SettingsEdit.read(self.target)
        self.write("{}")
        with patch("llm_credential_mapping.editor_settings.os.access", return_value=False), self.assertRaises(CredentialError):
            SettingsEdit.read(self.target)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("private")), self.assertRaises(CredentialError) as error:
            SettingsEdit.read(self.target)
        self.assertNotIn("private", str(error.exception))

    def test_busy_file_lock_is_a_reported_failure(self):
        with patch("llm_credential_mapping.editor_settings.FileLock.acquire", side_effect=Timeout("fixture")):
            with self.assertRaises(CredentialError) as error:
                with editing(self.target):
                    self.fail("Busy lock entered.")
        self.assertIn("busy", str(error.exception))

    def test_lock_release_failure_reports_that_settings_may_already_be_applied(self):
        with patch("llm_credential_mapping.editor_settings.FileLock") as factory:
            factory.return_value.release.side_effect = OSError("private")
            with self.assertRaises(CredentialError) as error:
                with editing(self.target) as editor:
                    editor.apply(self.generated)
        self.assertIn("may already have been applied", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(json.loads(self.target.read_text()), self.generated)

    def test_concurrent_cli_applies_preserve_other_environment_entries(self):
        self.write(json.dumps({ENVIRONMENT: [{"name": "X", "value": "unchanged"}]}))

        def apply():
            with editing(self.target) as editor:
                editor.apply(self.generated)

        with ThreadPoolExecutor(max_workers=2) as executor:
            for future in (executor.submit(apply), executor.submit(apply)):
                future.result()
        rows = _Document(self.target.read_text()).root.value[ENVIRONMENT]
        self.assertEqual(rows, [{"name": "X", "value": "unchanged"}, *self.generated[ENVIRONMENT]])

    def test_standard_desktop_paths_and_remote_or_portable_overrides(self):
        scenarios = [
            ("win32", {"APPDATA": str(self.home / "roaming")}, self.home / "roaming" / "Code" / "User"),
            ("linux", {}, self.home / ".config" / "Code" / "User"),
            ("linux", {"XDG_CONFIG_HOME": str(self.home / "xdg")}, self.home / "xdg" / "Code" / "User"),
            ("darwin", {}, self.home / "Library" / "Application Support" / "Code" / "User"),
            ("linux", {"VSCODE_AGENT_FOLDER": str(self.home / "agent")}, self.home / "agent" / "data" / "Machine"),
            ("win32", {"VSCODE_PORTABLE": str(self.home / "portable")}, self.home / "portable" / "user-data" / "User"),
        ]
        for platform, environment, directory in scenarios:
            with self.subTest(platform=platform, environment=environment), \
                    patch.dict(os.environ, environment, clear=True), \
                    patch("llm_credential_mapping.editor_settings.sys.platform", platform), \
                    patch("llm_credential_mapping.editor_settings.Path.home", return_value=self.home):
                self.assertEqual(settings_file(), directory / "settings.json")

    def test_generated_payload_is_validated_before_editing(self):
        for changes in (
            {WRAPPER: ""}, {LOGIN: "true"}, {ENVIRONMENT: [{"name": "X", "value": 1}]},
            {ENVIRONMENT: [{"name": "X", "value": "a"}, {"name": "X", "value": "b"}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(CredentialError):
                _Document("{}").merge({**self.generated, **changes})

    def test_remote_server_is_detected_and_conflicting_installations_require_an_override(self):
        server = self.home / ".vscode-server"
        server.mkdir()
        with patch.dict(os.environ, {}, clear=True), \
                patch("llm_credential_mapping.editor_settings.sys.platform", "linux"), \
                patch("llm_credential_mapping.editor_settings.Path.home", return_value=self.home):
            self.assertEqual(settings_file(), server / "data" / "Machine" / "settings.json")
            insiders = self.home / ".vscode-server-insiders"
            insiders.mkdir()
            with self.assertRaises(CredentialError):
                settings_file()
            self.assertEqual(settings_file(self.target), self.target)

    def test_remote_hint_never_falls_back_to_desktop_settings(self):
        (self.home / ".config" / "Code" / "User").mkdir(parents=True)
        with patch.dict(os.environ, {"REMOTE_CONTAINERS": "fixture"}, clear=True), \
                patch("llm_credential_mapping.editor_settings.sys.platform", "linux"), \
                patch("llm_credential_mapping.editor_settings.Path.home", return_value=self.home), \
                self.assertRaises(CredentialError):
            settings_file()

    def test_profiles_require_an_explicit_active_settings_file(self):
        profile = self.home / ".config" / "Code" / "User" / "profiles" / "profile-fixture"
        profile.mkdir(parents=True)
        with patch.dict(os.environ, {}, clear=True), \
                patch("llm_credential_mapping.editor_settings.sys.platform", "linux"), \
                patch("llm_credential_mapping.editor_settings.Path.home", return_value=self.home):
            with self.assertRaises(CredentialError):
                settings_file()
            self.assertEqual(settings_file(profile / "settings.json"), profile / "settings.json")

    def test_workspace_targets_and_relative_configuration_roots_are_rejected(self):
        for path in (
            self.home / ".vscode" / "settings.json", self.home / ".VSCODE" / "settings.json",
            self.home / "workspace.code-workspace", self.home / "other.json",
        ):
            with self.subTest(path=path), self.assertRaises(CredentialError):
                settings_file(path)
        for environment in ({"VSCODE_AGENT_FOLDER": "relative"}, {"XDG_CONFIG_HOME": "relative"}, {"VSCODE_PORTABLE": "relative"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                    patch("llm_credential_mapping.editor_settings.sys.platform", "linux"), self.assertRaises(CredentialError):
                settings_file()

    def test_apply_cli_reports_only_generated_settings_and_preserves_user_secrets(self):
        self.write(json.dumps({"unrelated": "existing-fixture-secret"}))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.cli.claude_setup", return_value=self.generated), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([
                "--home", str(self.home / "credentials"), "claude", "setup",
                "--profile", "unit", "--model", "model-alias", "--apply", "--settings-file", str(self.target),
            ])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), self.generated)
        self.assertNotIn("existing-fixture-secret", stdout.getvalue() + stderr.getvalue())
        self.assertIn("Applied", stderr.getvalue())
        self.assertIn("Reload", stderr.getvalue())
        self.assertEqual(json.loads(self.target.read_text())["unrelated"], "existing-fixture-secret")

    def test_invalid_apply_file_is_rejected_before_setup_changes_cli_config(self):
        self.write("{")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.cli.claude_setup", side_effect=AssertionError), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([
                "--home", str(self.home / "credentials"), "claude", "setup",
                "--model", "alias", "--apply", "--settings-file", str(self.target),
            ])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Invalid VS Code settings JSONC", stderr.getvalue())

    def test_settings_file_flag_requires_apply_and_edit_failures_explain_partial_cli_state(self):
        arguments = [
            "--home", str(self.home / "credentials"), "claude", "setup",
            "--model", "alias", "--settings-file", str(self.target),
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.cli.claude_setup", side_effect=AssertionError), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(arguments), 1)
        self.assertIn("--settings-file requires --apply", stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.cli.claude_setup", return_value=self.generated), \
                patch("llm_credential_mapping.editor_settings.SettingsEdit.apply", side_effect=CredentialError("Cannot write")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main([*arguments, "--apply"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("CLI configuration was saved, but editor settings were not applied", stderr.getvalue())
