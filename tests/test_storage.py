import json
import unittest
from unittest.mock import Mock, patch

from keyring.errors import KeyringError

from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.storage import NativeKeyring


class StorageTests(unittest.TestCase):
    def setUp(self):
        backend_type = type("WinVaultKeyring", (), {"__module__": "keyring.backends.Windows"})
        self.backend = backend_type()
        self.backend.get_password = Mock()
        self.backend.set_password = Mock()
        self.backend.delete_password = Mock()
        with patch("llm_credential_mapping.storage.keyring.get_keyring", return_value=self.backend):
            self.store = NativeKeyring()

    def test_accounts_have_independent_native_services(self):
        self.store.put("a", {"credential": "a"})
        self.store.put("b", {"credential": "b"})
        calls = self.backend.set_password.call_args_list
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        self.store.delete("a")
        self.assertEqual(self.backend.delete_password.call_args.args[0], calls[0].args[0])

    def test_json_session_roundtrip_and_missing_credential(self):
        value = {"access_token": "synthetic", "refresh_token": "test"}
        self.backend.get_password.return_value = json.dumps(value)
        self.assertEqual(self.store.get("a"), value)
        self.backend.get_password.return_value = None
        self.assertIsNone(self.store.get("a"))

    def test_invalid_json_has_actionable_error(self):
        for value in ("{", "[]"):
            self.backend.get_password.return_value = value
            with self.subTest(value=value), self.assertRaises(CredentialError) as result:
                self.store.get("a")
            self.assertIn("OS vault", str(result.exception))

    def test_native_backend_failures_are_redacted(self):
        for error in (KeyringError("synthetic-secret"), OSError("synthetic-secret")):
            for method, call in (
                (self.backend.get_password, lambda: self.store.get("a")),
                (self.backend.set_password, lambda: self.store.put("a", {})),
                (self.backend.delete_password, lambda: self.store.delete("a")),
            ):
                method.side_effect = error
                with self.subTest(error=type(error).__name__, method=method), self.assertRaises(CredentialError) as result:
                    call()
                self.assertNotIn("synthetic-secret", str(result.exception))
                method.side_effect = None
