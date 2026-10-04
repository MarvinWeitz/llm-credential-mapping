import contextlib
import io
import json
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.cli import main
from llm_credential_mapping.copilot import (
    CopilotStore, connection_key, setup, validate_connection,
)
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.litellm import Session
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.repositories import RepositoryStore
from test_sessions import MemoryStore


class CopilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.profiles = ProfileStore(self.home / "config")
        self.profile = Profile("unit", "https://gateway.example", "team")
        self.profiles.add(self.profile)
        self.repository = "github.com/company/repo"
        RepositoryStore(self.profiles).bind(self.profile.gateway, self.repository, "customer:A")
        self.vault = MemoryStore()
        self.session = Session(
            self.profile.gateway, self.profile.team_id, "employee", "client",
            "access-secret", "refresh-secret", time.time() + 3600,
        )
        self.vault.put(self.profile.account, self.session.to_dict())
        self.connections = CopilotStore(self.profiles)

    def setup_connection(self, **options):
        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.copilot.NativeKeyring", return_value=self.vault):
            return setup(self.profiles, "unit", "model-alias", self.home, **options)

    def test_setup_reuses_repository_binding_and_keeps_both_credentials_out_of_config_and_snippet(self):
        connection = self.setup_connection()
        key = connection_key(connection, self.vault)
        self.assertEqual(len(key), 43)
        self.assertNotEqual(key, self.session.access_token)
        self.assertEqual(connection.customer, "customer:A")
        self.assertEqual(connection.user_id, "employee")
        self.assertEqual(connection.directory, str(self.home))
        saved = self.connections.path.read_text()
        snippet = json.dumps(connection.model_settings())
        for secret in (key, self.session.access_token, self.session.refresh_token):
            self.assertNotIn(secret, saved + snippet)
        fields = connection.model_settings()
        self.assertEqual(set(fields), {"apiType", "models"})
        self.assertEqual(fields["apiType"], "chat-completions")
        self.assertTrue(fields["models"][0]["toolCalling"])
        self.assertFalse(fields["models"][0]["vision"])
        self.assertEqual(fields["models"][0]["id"], "model-alias")
        self.assertIn(connection.id, fields["models"][0]["url"])
        self.assertTrue(fields["models"][0]["url"].endswith("/v1/chat/completions"))

    def test_setup_and_disable_are_reversible_with_the_same_local_key_and_no_editor_files(self):
        connection = self.setup_connection()
        key = connection_key(connection, self.vault)
        with patch("llm_credential_mapping.copilot.NativeKeyring", side_effect=AssertionError):
            disabled = self.connections.disable()
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.id, connection.id)
        renewed = self.setup_connection()
        self.assertTrue(renewed.enabled)
        self.assertEqual(connection_key(renewed, self.vault), key)
        self.assertEqual(renewed.id, connection.id)
        self.assertFalse((self.home / ".vscode").exists())
        self.assertFalse((self.profiles.home / "settings.json").exists())

    def test_different_contexts_never_reuse_an_identity_and_port_conflicts_are_explicit(self):
        original = self.setup_connection()
        RepositoryStore(self.profiles).bind(self.profile.gateway, self.repository, "customer:B", replace=True)
        with self.assertRaisesRegex(CredentialError, "port"):
            self.setup_connection()
        new = self.setup_connection(port=8744)
        self.assertNotEqual(new.id, original.id)
        self.assertNotEqual(connection_key(new, self.vault), connection_key(original, self.vault))
        self.assertEqual(original.customer, "customer:A")
        with self.assertRaisesRegex(CredentialError, "Multiple"):
            self.connections.select()
        self.assertEqual(self.connections.select(new.id), new)
        self.connections.disable(original.id)
        reused_port = self.setup_connection()
        self.assertEqual(reused_port.port, original.port)
        self.assertNotEqual(reused_port.id, original.id)

    def test_missing_repository_binding_and_invalid_origin_fail_before_vault_access(self):
        with patch("llm_credential_mapping.copilot.current_repository", return_value="github.com/company/unbound"), \
                patch("llm_credential_mapping.copilot.NativeKeyring", side_effect=AssertionError):
            with self.assertRaisesRegex(CredentialError, "binding"):
                setup(self.profiles, "unit", "model-alias", self.home)
        self.assertFalse(self.connections.path.exists())
        with patch("llm_credential_mapping.copilot.current_repository", side_effect=CredentialError("No origin")), \
                patch("llm_credential_mapping.copilot.NativeKeyring", side_effect=AssertionError):
            with self.assertRaisesRegex(CredentialError, "No origin"):
                setup(self.profiles, "unit", "model-alias", self.home)

    def test_model_validation_is_shared_and_unsafe_ports_limits_or_flags_are_rejected(self):
        connection = self.setup_connection()
        for values in (
            {"model": ""}, {"model": "bad model"}, {"port": 0}, {"port": 1023}, {"port": 65536},
            {"port": True}, {"max_input_tokens": 0}, {"max_output_tokens": 2_000_001},
            {"vision": "true"}, {"enabled": 1}, {"directory": "relative"}, {"account": "bad"},
        ):
            with self.subTest(values=values), self.assertRaises(CredentialError):
                replace(connection, **values)
        for port in (1024, 65535):
            self.assertEqual(replace(connection, port=port).port, port)
        self.assertEqual(replace(connection, max_output_tokens=2_000_000).max_output_tokens, 2_000_000)

    def test_stale_checkout_or_disabled_connection_fails_before_constructing_a_keyring(self):
        connection = self.setup_connection()
        with patch("llm_credential_mapping.copilot.current_repository", return_value="github.com/company/changed"), \
                patch("llm_credential_mapping.copilot.NativeKeyring", side_effect=AssertionError):
            with self.assertRaisesRegex(CredentialError, "origin changed"):
                validate_connection(self.profiles, connection)
        with patch("llm_credential_mapping.copilot.NativeKeyring", side_effect=AssertionError):
            with self.assertRaisesRegex(CredentialError, "disabled"):
                validate_connection(self.profiles, replace(connection, enabled=False))

    def test_profile_and_employee_changes_are_not_silently_adopted(self):
        connection = self.setup_connection()
        self.vault.put(self.profile.account, replace(self.session, user_id="another").to_dict())
        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository):
            with self.assertRaisesRegex(CredentialError, "employee changed"):
                validate_connection(self.profiles, connection, self.vault)
        self.profiles.path.write_text(json.dumps({
            "version": 1, "active": "unit", "projects": {
                "unit": {"gateway": "https://changed.example", "team_id": "team", "allow_http": False},
            },
        }))
        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository):
            with self.assertRaisesRegex(CredentialError, "profile changed"):
                validate_connection(self.profiles, connection, self.vault)

    def test_corrupt_configuration_or_local_key_is_not_replaced_with_defaults(self):
        connection = self.setup_connection()
        for value in (
            [], {"version": True, "connections": {}},
            {"version": 1, "connections": {"wrong": asdict(connection)}},
            {"version": 1, "connections": {connection.id: {"profile": "unit"}}},
        ):
            with self.subTest(value=value):
                self.connections.path.write_text(json.dumps(value))
                with self.assertRaises(CredentialError):
                    self.connections.list()
                self.assertEqual(json.loads(self.connections.path.read_text()), value)
        self.vault.put(connection.key_account, {"version": 1, "key": "fixture-secret"})
        with self.assertRaises(CredentialError) as error:
            connection_key(connection, self.vault)
        self.assertNotIn("fixture-secret", str(error.exception))
        self.connections.path.unlink()
        with self.assertRaises(CredentialError):
            self.connections.put(connection, self.vault)
        self.assertFalse(self.connections.path.exists())
        self.assertEqual(self.vault.get(connection.key_account)["key"], "fixture-secret")

    def test_config_write_failure_keeps_the_previous_connection_and_does_not_erase_other_state(self):
        connection = self.setup_connection()
        saved = self.connections.path.read_bytes()
        with patch("llm_credential_mapping.profiles.os.replace", side_effect=OSError("private")), \
                self.assertRaises(CredentialError):
            self.connections.disable(connection.id)
        self.assertEqual(self.connections.path.read_bytes(), saved)
        self.assertEqual(self.vault.get(self.profile.account), self.session.to_dict())

    def test_cli_setup_list_key_and_disable_distinguish_nonsecret_output_from_explicit_local_key(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.copilot.NativeKeyring", return_value=self.vault), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main([
                "--home", str(self.profiles.home), "copilot", "setup", "--profile", "unit", "--model", "model-alias",
                "--directory", str(self.home),
            ]), 0, stderr.getvalue())
        connection = self.connections.select()
        key = connection_key(connection, self.vault)
        self.assertEqual(json.loads(stdout.getvalue()), connection.model_settings())
        self.assertNotIn(key, stdout.getvalue() + stderr.getvalue())
        self.assertIn("KEEP", stderr.getvalue())
        self.assertIn("explicitly", stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.copilot.NativeKeyring", return_value=self.vault), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--home", str(self.profiles.home), "copilot", "key"]), 0)
        self.assertEqual(stdout.getvalue().strip(), key)
        self.assertNotIn("access-secret", stdout.getvalue() + stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--home", str(self.profiles.home), "copilot", "list"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())[0]["connection"], connection.id)
        self.assertNotIn(key, stdout.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("llm_credential_mapping.copilot.NativeKeyring", side_effect=AssertionError), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--home", str(self.profiles.home), "copilot", "disable"]), 0)
        self.assertFalse(self.connections.select().enabled)
        self.assertIn("normal GitHub Copilot", stderr.getvalue())
        self.assertEqual(self.vault.get(self.profile.account), self.session.to_dict())

    def test_no_connection_and_unknown_identifier_are_visible_errors(self):
        with self.assertRaisesRegex(CredentialError, "setup"):
            self.connections.select()
        connection = self.setup_connection()
        with self.assertRaisesRegex(CredentialError, "No such"):
            self.connections.select("unknown")
        self.assertEqual(self.connections.select(connection.id), connection)

    def test_invalid_setup_limits_fail_before_vault_access_or_grant_renewal(self):
        for values in (
            {"port": 1023}, {"max_input_tokens": 0}, {"max_output_tokens": True}, {"vision": "true"},
        ):
            with self.subTest(values=values), \
                    patch("llm_credential_mapping.copilot.NativeKeyring", side_effect=AssertionError):
                with self.assertRaises(CredentialError):
                    setup(self.profiles, "unit", "model-alias", self.home, **values)
        self.assertFalse(self.connections.path.exists())

    def test_every_context_field_affects_identity_but_enabled_does_not(self):
        connection = self.setup_connection()
        for values in (
            {"profile": "other"}, {"account": "a" * 64}, {"user_id": "another"},
            {"repository": "github.com/company/other"}, {"customer": "customer:B"},
            {"model": "other-model"}, {"directory": str(self.home / "other")}, {"port": 8744},
            {"max_input_tokens": 64000}, {"max_output_tokens": 8192}, {"vision": True},
        ):
            with self.subTest(values=values):
                self.assertNotEqual(replace(connection, **values).id, connection.id)
        self.assertEqual(replace(connection, enabled=False).id, connection.id)
