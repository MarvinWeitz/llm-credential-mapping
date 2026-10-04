import contextlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.cli import main
from llm_credential_mapping.copilot import CopilotConnection, CopilotStore, connection_key
from llm_credential_mapping.copilot_models import ModelsDocument, models_file
from llm_credential_mapping.editor_settings import editing_document
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.litellm import Session
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.repositories import RepositoryStore
from test_sessions import MemoryStore


REFERENCE = "${input:chat.lm.secret.fixture}"


class CopilotModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.target = self.home / "editor" / "chatLanguageModels.json"
        self.profiles = ProfileStore(self.home / "config")
        self.profile = Profile("unit", "https://gateway.example", "team")
        self.profiles.add(self.profile)
        self.repository = "github.com/company/repo"
        RepositoryStore(self.profiles).bind(self.profile.gateway, self.repository, "customer:A")
        self.connection = CopilotConnection(
            "unit", self.profile.account, "employee", self.repository, "customer:A", "model-alias",
            str(self.home), 8743, 32000, 4096,
        )
        self.connections = CopilotStore(self.profiles)
        self.vault = MemoryStore()
        self.session = Session(
            self.profile.gateway, self.profile.team_id, "employee", "client",
            "synthetic-access", "synthetic-refresh", time.time() + 3600,
        )
        self.vault.put(self.profile.account, self.session.to_dict())

    def group(self, **fields):
        return {
            "name": self.connection.group_name, "vendor": "customendpoint", "apiKey": REFERENCE, **fields,
        }

    def write(self, source, target=None):
        target = target if target is not None else self.target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.encode("utf-8"))

    def apply(self, source=None):
        if source is not None:
            self.write(source)
        with editing_document(self.target, ModelsDocument) as editor:
            changed = editor.write(editor.document.merge(self.connection))
        return self.target.read_bytes().decode("utf-8"), changed

    def cli(self, options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.copilot.NativeKeyring", return_value=self.vault), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main([
                "--home", str(self.profiles.home), "copilot", "setup", "--profile", "unit",
                "--model", self.connection.model, "--directory", str(self.home), *options,
            ])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_adds_model_fields_without_replacing_registration_and_is_idempotent(self):
        source = json.dumps([self.group(custom={"keep": True})])
        updated, changed = self.apply(source)
        self.assertTrue(changed)
        group = json.loads(updated)[0]
        self.assertEqual(group, {**self.group(custom={"keep": True}), **self.connection.model_settings()})
        with patch("llm_credential_mapping.editor_settings.atomic_write_text", side_effect=AssertionError):
            self.assertEqual(self.apply(), (updated, False))

    def test_merges_matching_model_and_preserves_other_groups_models_and_custom_fields(self):
        other_group = '{"name":"Other","vendor":"openai","apiKey":"${input:other}","extra":{"text":"keep"}}'
        other_model = {"id": "other-model", "url": "https://other.example", "private": "keep"}
        old_model = {
            "id": self.connection.model, "name": "old", "url": "https://old.example",
            "toolCalling": 1, "vision": 0, "apiType": "responses", "streaming": False,
            "requestHeaders": {"X-Custom": "unchanged"},
        }
        group = self.group(apiType="messages", models=[other_model, old_model], custom="unchanged")
        updated, _ = self.apply("[" + other_group + ", " + json.dumps(group) + "]")
        self.assertIn(other_group, updated)
        applied = json.loads(updated)[1]
        self.assertEqual(applied["apiKey"], REFERENCE)
        self.assertEqual(applied["custom"], "unchanged")
        self.assertEqual(applied["models"][0], other_model)
        self.assertEqual(applied["apiType"], "chat-completions")
        expected = {
            **old_model, **self.connection.model_settings()["models"][0], "apiType": "chat-completions",
        }
        self.assertEqual(applied["models"][1], expected)
        self.assertIs(applied["models"][1]["toolCalling"], True)
        self.assertIs(applied["models"][1]["vision"], False)
        self.assertEqual(self.apply(), (updated, False))

    def test_appends_a_missing_model_to_empty_or_populated_arrays_without_duplicates(self):
        for existing in ([], [{"id": "other-model", "extra": True}]):
            with self.subTest(existing=existing):
                updated, _ = self.apply(json.dumps([self.group(models=existing)]))
                self.assertEqual(
                    json.loads(updated)[0]["models"], [*existing, self.connection.model_settings()["models"][0]],
                )
                self.assertEqual(self.apply(), (updated, False))

    def test_retains_comments_bom_unicode_trailing_commas_and_crlf(self):
        source = (
            '\ufeff// providers\r\n[\r\n  {\r\n'
            f'    "name": {json.dumps(self.connection.group_name)},\r\n'
            '    "vendor": "customendpoint",\r\n'
            f'    "apiKey": "{REFERENCE}", // keep the native reference\r\n'
            '    "custom": {"text": "\u00e4 // /* keep */",},\r\n'
            '    "models": [\r\n'
            '      {"id":"other","url":"https://example/a/*b*/"} // keep other model\r\n'
            '    ],\r\n'
            '  },\r\n]\r\n'
        )
        updated, _ = self.apply(source)
        self.assertTrue(updated.startswith("\ufeff// providers\r\n"))
        self.assertIn(f'"apiKey": "{REFERENCE}", // keep the native reference\r\n', updated)
        self.assertIn('"custom": {"text": "\u00e4 // /* keep */",},\r\n', updated)
        self.assertIn('{"id":"other","url":"https://example/a/*b*/"}, // keep other model\r\n', updated)
        self.assertEqual(updated.count("\n"), updated.count("\r\n"))
        group = ModelsDocument(updated).root.value[0]
        self.assertEqual(group["models"][1], self.connection.model_settings()["models"][0])
        self.assertEqual(self.apply(), (updated, False))

    def test_preserves_comments_inside_the_matching_model(self):
        source = (
            "[" + json.dumps(self.group())[:-1]
            + ', "models": [{"id":"model-alias", /* keep */ "url":"https://old.example",'
            ' "name":"old", "custom":true,}],}]'
        )
        updated, _ = self.apply(source)
        self.assertIn("/* keep */", updated)
        self.assertTrue(ModelsDocument(updated).root.value[0]["models"][0]["custom"])
        self.assertEqual(self.apply(), (updated, False))

    def test_different_vendor_with_the_same_name_is_not_modified(self):
        other = {**self.group(), "vendor": "openai"}
        updated, _ = self.apply(json.dumps([other, self.group()]))
        self.assertEqual(json.loads(updated)[0], other)
        self.assertEqual(json.loads(updated)[1]["models"], self.connection.model_settings()["models"])

    def test_missing_or_renamed_groups_are_not_created_or_guessed(self):
        for groups in ([], [{**self.group(), "name": "renamed"}], [{**self.group(), "vendor": "openai"}]):
            source = json.dumps(groups)
            with self.subTest(groups=groups):
                self.write(source)
                with self.assertRaises(CredentialError) as error:
                    self.apply()
                self.assertIn("register", str(error.exception))
                self.assertIn(json.dumps(self.connection.group_name), str(error.exception))
                self.assertEqual(self.target.read_text(), source)

    def test_literal_missing_or_malformed_secret_references_are_rejected_without_echoing(self):
        for secret in (
            None, "", "fixture-private-secret", 42, "${input:}", "${env:KEY}",
            "${input:key}suffix", "${input:key with spaces}", "${input:key:extra}", "${input:{nested}}",
        ):
            group = self.group(apiKey=secret)
            if secret is None:
                del group["apiKey"]
            source = json.dumps([group])
            with self.subTest(secret=secret):
                self.write(source)
                with self.assertRaises(CredentialError) as error:
                    self.apply()
                self.assertIn("native apiKey", str(error.exception))
                self.assertNotIn("fixture-private-secret", str(error.exception))
                self.assertEqual(self.target.read_text(), source)

    def test_malformed_jsonc_and_provider_shapes_are_not_written_or_echoed(self):
        for source in (
            "{", "{}", "null", "[,]", "[42]", '[{"private":"fixture-private-secret",}] trailing',
            '[{"private":NaN}]', '[{"private":Infinity}]', '[{"private":"fixture-private-secret",',
        ):
            with self.subTest(source=source):
                self.write(source)
                with self.assertRaises(CredentialError) as error:
                    self.apply()
                self.assertNotIn("fixture-private-secret", str(error.exception))
                self.assertEqual(self.target.read_text(), source)

    def test_duplicate_matching_groups_models_or_properties_are_rejected(self):
        model = self.connection.model_settings()["models"][0]
        sources = [
            json.dumps([self.group(), self.group()]),
            json.dumps([self.group(models=[model, model])]),
            "[" + json.dumps(self.group())[:-1] + ', "apiKey":"${input:another}"}]',
            "[" + json.dumps(self.group())[:-1] + ', "vendor":"customendpoint"}]',
            "[" + json.dumps(self.group())[:-1] + ', "models":[], "models":[]}]',
            "[" + json.dumps(self.group())[:-1] + ', "models":[{"id":"model-alias","name":"one","name":"two"}]}]',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.write(source)
                with self.assertRaises(CredentialError):
                    self.apply()
                self.assertEqual(self.target.read_text(), source)

    def test_invalid_model_arrays_are_not_replaced(self):
        for models in (None, {}, [None], ["fixture-private-secret"]):
            source = json.dumps([self.group(models=models)])
            with self.subTest(models=models):
                self.write(source)
                with self.assertRaises(CredentialError) as error:
                    self.apply()
                self.assertNotIn("fixture-private-secret", str(error.exception))
                self.assertEqual(self.target.read_text(), source)

    def test_disabled_connections_cannot_be_applied(self):
        source = json.dumps([self.group()])
        with self.assertRaisesRegex(CredentialError, "disabled"):
            ModelsDocument(source).merge(replace(self.connection, enabled=False))

    def test_write_failures_preserve_previous_bytes_permissions_and_do_not_echo_contents(self):
        source = json.dumps([self.group()])
        self.write(source)
        permissions = stat.S_IMODE(self.target.stat().st_mode)
        with patch("llm_credential_mapping.profiles.os.replace", side_effect=OSError("fixture-private-secret")):
            with self.assertRaises(CredentialError) as error:
                self.apply()
        self.assertNotIn("fixture-private-secret", str(error.exception))
        self.assertEqual(self.target.read_text(), source)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), permissions)
        self.assertLessEqual(
            {item.name for item in self.target.parent.iterdir()},
            {"chatLanguageModels.json", "chatLanguageModels.json.llm-credentials.lock"},
        )
        self.apply()
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), permissions)

    def test_external_content_and_permission_changes_abort_the_write(self):
        self.write(json.dumps([self.group()]))
        with editing_document(self.target, ModelsDocument) as editor:
            self.target.write_text("[]")
            with self.assertRaisesRegex(CredentialError, "changed during setup"):
                editor.write(editor.document.merge(self.connection))
        self.assertEqual(self.target.read_text(), "[]")
        self.write(json.dumps([self.group()]))
        permissions = stat.S_IMODE(self.target.stat().st_mode)
        try:
            with editing_document(self.target, ModelsDocument) as editor:
                self.target.chmod(stat.S_IRUSR)
                with self.assertRaisesRegex(CredentialError, "permissions changed"):
                    editor.write(editor.document.merge(self.connection))
            self.assertFalse(stat.S_IMODE(self.target.stat().st_mode) & stat.S_IWUSR)
        finally:
            self.target.chmod(permissions)

    def test_invalid_encoding_read_only_and_oversized_model_files_fail_before_editing(self):
        self.write("[]")
        for raw in (b"\xff", b" " * 2_097_153):
            with self.subTest(raw_size=len(raw)):
                self.target.write_bytes(raw)
                with self.assertRaises(CredentialError):
                    with editing_document(self.target, ModelsDocument):
                        self.fail("Invalid input must not enter the editor.")
                self.assertEqual(self.target.read_bytes(), raw)
        self.write("[]")
        with patch("llm_credential_mapping.editor_settings.os.access", return_value=False):
            with self.assertRaises(CredentialError):
                with editing_document(self.target, ModelsDocument):
                    self.fail("Read-only files must not enter the editor.")

    def test_concurrent_applies_are_serialized_and_do_not_duplicate_models(self):
        self.write(json.dumps([self.group(models=[])]))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result() for future in (executor.submit(self.apply), executor.submit(self.apply))]
        self.assertEqual(sum(changed for _, changed in results), 1)
        self.assertEqual(json.loads(self.target.read_text())[0]["models"], self.connection.model_settings()["models"])

    def test_resolves_standard_desktop_and_portable_paths_on_each_platform(self):
        scenarios = [
            ("win32", "APPDATA", ("roaming", "Code", "User")),
            ("darwin", None, ("Library", "Application Support", "Code", "User")),
            ("linux", None, (".config", "Code", "User")),
            ("linux", "XDG_CONFIG_HOME", ("xdg", "Code", "User")),
            ("linux", "VSCODE_PORTABLE", ("portable", "user-data", "User")),
        ]
        for index, (platform, variable, parts) in enumerate(scenarios):
            home = self.home / str(index)
            target = home.joinpath(*parts, "chatLanguageModels.json")
            self.write("[]", target)
            env = {variable: str(home / parts[0])} if variable is not None else {}
            with self.subTest(platform=platform, variable=variable), \
                    patch("llm_credential_mapping.copilot_models.sys.platform", platform), \
                    patch("llm_credential_mapping.copilot_models.Path.home", return_value=home), \
                    patch.dict(os.environ, env, clear=True):
                self.assertEqual(models_file(), target)
                self.assertEqual(models_file(target), target)

    def test_insiders_detection_requires_only_one_desktop_installation(self):
        root = self.home / "roaming"
        insiders = root / "Code - Insiders" / "User" / "chatLanguageModels.json"
        stable = root / "Code" / "User" / "chatLanguageModels.json"
        self.write("[]", insiders)
        with patch("llm_credential_mapping.copilot_models.sys.platform", "win32"), \
                patch.dict(os.environ, {"APPDATA": str(root)}, clear=True):
            self.assertEqual(models_file(), insiders)
            self.write("[]", stable)
            with self.assertRaisesRegex(CredentialError, "--models-file"):
                models_file()
            self.assertEqual(models_file(stable), stable)

    def test_named_profiles_require_explicit_target_including_portable_profiles(self):
        for portable in (False, True):
            root = self.home / ("portable" if portable else "roaming")
            user = root / "user-data" / "User" if portable else root / "Code" / "User"
            default = user / "chatLanguageModels.json"
            named = user / "profiles" / "profile-id" / "chatLanguageModels.json"
            self.write("[]", default)
            self.write("[]", named)
            env = {"VSCODE_PORTABLE" if portable else "APPDATA": str(root)}
            with self.subTest(portable=portable), \
                    patch("llm_credential_mapping.copilot_models.sys.platform", "win32"), \
                    patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(CredentialError, "profiles"):
                    models_file()
                self.assertEqual(models_file(named), named)
                self.assertEqual(models_file(default), default)

    def test_remote_environment_requires_an_explicit_accessible_desktop_file(self):
        self.write("[]")
        for variable in ("VSCODE_AGENT_FOLDER", "REMOTE_CONTAINERS", "CODESPACES", "SSH_CONNECTION"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: "fixture"}, clear=True):
                with self.assertRaisesRegex(CredentialError, "desktop"):
                    models_file()
                self.assertEqual(models_file(self.target), self.target)

    def test_workspace_remote_machine_wrong_filename_and_missing_files_are_rejected(self):
        targets = [
            self.home / ".vscode" / "chatLanguageModels.json",
            self.home / ".vscode" / "nested" / "chatLanguageModels.json",
            self.home / "data" / "Machine" / "chatLanguageModels.json",
            self.home / ".vscode-server" / "data" / "User" / "chatLanguageModels.json",
            self.home / ".vscode-server-insiders" / "data" / "User" / "chatLanguageModels.json",
            self.home / "settings.json",
        ]
        for target in targets:
            self.write("[]", target)
            with self.subTest(target=target), self.assertRaises(CredentialError):
                models_file(target)
        with self.assertRaisesRegex(CredentialError, "does not exist"):
            models_file(self.target)
        self.assertFalse(self.target.parent.exists())
        self.write("[]", self.target)
        with patch.object(Path, "resolve", return_value=targets[0]), self.assertRaises(CredentialError):
            models_file(self.target)

    def test_missing_or_relative_configuration_roots_fail_explicitly(self):
        scenarios = [
            ("win32", {}),
            ("win32", {"APPDATA": "relative"}),
            ("linux", {"XDG_CONFIG_HOME": "relative"}),
            ("linux", {"VSCODE_PORTABLE": "relative"}),
            ("linux", {"XDG_CONFIG_HOME": str(self.home / "missing")}),
        ]
        for platform, env in scenarios:
            with self.subTest(platform=platform, env=env), \
                    patch("llm_credential_mapping.copilot_models.sys.platform", platform), \
                    patch.dict(os.environ, env, clear=True), self.assertRaises(CredentialError):
                models_file()
        self.assertFalse((self.home / "missing").exists())

    def test_cli_apply_keeps_output_shape_and_keys_private_and_reuses_existing_connection(self):
        self.write(json.dumps([self.group(models=[])]))
        result, stdout, stderr = self.cli(["--apply", "--models-file", str(self.target)])
        self.assertEqual(result, 0, stderr)
        self.assertEqual(json.loads(stdout), self.connection.model_settings())
        self.assertEqual(self.connections.select(), self.connection)
        key = connection_key(self.connection, self.vault)
        for secret in (key, self.session.access_token, self.session.refresh_token):
            self.assertNotIn(secret, stdout + stderr + self.target.read_text())
        self.assertIn("Applied", stderr)
        self.assertIn("explicitly", stderr)
        self.assertNotIn("No editor files were changed", stderr)
        original = self.target.read_bytes()
        with patch("llm_credential_mapping.editor_settings.atomic_write_text", side_effect=AssertionError):
            result, _, stderr = self.cli(["--apply", "--models-file", str(self.target)])
        self.assertEqual(result, 0, stderr)
        self.assertIn("Already up to date", stderr)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(connection_key(self.connection, self.vault), key)

    def test_cli_passes_port_and_capabilities_to_applied_settings(self):
        self.connection = replace(
            self.connection, port=8744, max_input_tokens=64000, max_output_tokens=8192, vision=True,
        )
        self.write(json.dumps([self.group()]))
        result, stdout, stderr = self.cli([
            "--apply", "--models-file", str(self.target), "--port", "8744", "--max-input-tokens", "64000",
            "--max-output-tokens", "8192", "--vision",
        ])
        self.assertEqual(result, 0, stderr)
        self.assertEqual(json.loads(stdout), self.connection.model_settings())
        self.assertEqual(json.loads(self.target.read_text())[0]["models"], self.connection.model_settings()["models"])
        self.assertEqual(self.connections.select(), self.connection)

    def test_cli_apply_can_use_the_detected_target(self):
        self.write(json.dumps([self.group()]))
        with patch("llm_credential_mapping.cli.models_file", return_value=self.target) as detect:
            result, _, stderr = self.cli(["--apply"])
        self.assertEqual(result, 0, stderr)
        detect.assert_called_once_with(None)

    def test_cli_preview_does_not_read_or_edit_model_files(self):
        source = json.dumps([self.group()])
        self.write(source)
        with patch("llm_credential_mapping.cli.models_file", side_effect=AssertionError):
            result, stdout, stderr = self.cli([])
        self.assertEqual(result, 0, stderr)
        self.assertEqual(json.loads(stdout), self.connection.model_settings())
        self.assertIn("No editor files were changed", stderr)
        self.assertIn("--apply --models-file", stderr)
        self.assertEqual(self.target.read_text(), source)

    def test_models_file_without_apply_is_an_error_before_connection_setup(self):
        with patch("llm_credential_mapping.cli.copilot_setup", side_effect=AssertionError):
            result, stdout, stderr = self.cli(["--models-file", str(self.target)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--models-file requires --apply", stderr)
        self.assertFalse(self.connections.path.exists())
        self.assertFalse(self.target.exists())

    def test_missing_or_malformed_editor_files_fail_before_connection_setup(self):
        with patch("llm_credential_mapping.cli.copilot_setup", side_effect=AssertionError):
            result, stdout, _ = self.cli(["--apply", "--models-file", str(self.target)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        for source in ('{"private":"fixture-private-secret",', "{}", "[42]"):
            self.write(source)
            with self.subTest(source=source), patch("llm_credential_mapping.cli.copilot_setup", side_effect=AssertionError):
                result, stdout, stderr = self.cli(["--apply", "--models-file", str(self.target)])
            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            self.assertNotIn("fixture-private-secret", stderr)
            self.assertFalse(self.connections.path.exists())
            self.assertEqual(self.target.read_text(), source)

    def test_cli_registration_or_write_failure_reports_retained_connection_and_preserves_file(self):
        for source, write_failure in (
            ("[]", False),
            (json.dumps([self.group(apiKey="fixture-private-secret")]), False),
            (json.dumps([self.group()]), True),
        ):
            self.write(source)
            guard = patch(
                "llm_credential_mapping.editor_settings.atomic_write_text",
                side_effect=CredentialError("Cannot atomically update model settings."),
            ) if write_failure else contextlib.nullcontext()
            with self.subTest(write_failure=write_failure), guard:
                result, stdout, stderr = self.cli(["--apply", "--models-file", str(self.target)])
            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            self.assertIn("was saved, but model settings were not applied", stderr)
            self.assertNotIn("fixture-private-secret", stderr)
            self.assertEqual(self.connections.select(), self.connection)
            self.assertEqual(self.target.read_text(), source)
            self.assertEqual(self.vault.get(self.profile.account), self.session.to_dict())