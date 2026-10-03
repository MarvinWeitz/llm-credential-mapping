import json
import os
import unittest
from unittest.mock import Mock, patch

from keyring.errors import KeyringError, PasswordDeleteError

from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.storage import NativeKeyring


class StorageTests(unittest.TestCase):
    def setUp(self):
        backend_type = type("WinVaultKeyring", (), {"__module__": "keyring.backends.Windows"})
        self.backend = backend_type()
        self.entries = {}
        self.backend.get_password = Mock(side_effect=lambda service, account: self.entries.get(service))
        self.backend.set_password = Mock(side_effect=self.write)
        self.backend.delete_password = Mock(side_effect=self.remove)
        with patch("llm_credential_mapping.storage.keyring.get_keyring", return_value=self.backend):
            self.store = NativeKeyring()

    def write(self, service, account, raw):
        if len(raw.encode("utf-16-le")) > 2560:
            raise OSError("Entry too large")
        self.entries[service] = raw

    def remove(self, service, account):
        if service not in self.entries:
            raise PasswordDeleteError("Missing entry")
        del self.entries[service]

    def test_accounts_have_independent_native_services(self):
        self.store.put("a", {"credential": "a"})
        self.store.put("b", {"credential": "b"})
        calls = self.backend.set_password.call_args_list
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        self.store.delete("a")
        self.assertEqual(self.backend.delete_password.call_args.args[0], calls[0].args[0])

    def test_json_session_roundtrip_and_missing_credential(self):
        value = {"access_token": "synthetic", "refresh_token": "test"}
        self.entries[self.store._service("a")] = json.dumps(value)
        self.assertEqual(self.store.get("a"), value)
        self.entries.clear()
        self.assertIsNone(self.store.get("a"))

    def test_invalid_json_has_actionable_error(self):
        for value in ("{", "[]"):
            self.entries[self.store._service("a")] = value
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
                method.side_effect = {
                    self.backend.get_password: lambda service, account: self.entries.get(service),
                    self.backend.set_password: self.write,
                    self.backend.delete_password: self.remove,
                }[method]

    def test_exact_native_entry_threshold(self):
        for size in (1280, 1281):
            value = {"data": "x" * (size - len(json.dumps({"data": ""})))}
            self.assertEqual(len(json.dumps(value)), size)
            self.store.put("a", value)
            with self.subTest(size=size):
                self.assertEqual(self.store.get("a"), value)
                self.assertTrue(all(len(raw.encode("utf-16-le")) <= 2560 for raw in self.entries.values()))
                self.assertEqual(len(self.entries), 1 if size == 1280 else 3)
            self.store.delete("a")
            self.assertFalse(self.entries)

    def test_large_unicode_session_roundtrip(self):
        value = {"access_token": "a" * 7000, "refresh_token": "r" * 5000, "user_id": "\u2603" * 100}
        self.store.put("a", value)
        self.assertEqual(self.store.get("a"), value)
        self.assertTrue(all(len(raw.encode("utf-16-le")) <= 2560 for raw in self.entries.values()))
        self.assertEqual(self.backend.set_password.call_args_list[-1].args[0], self.store._service("a"))

    def test_replacing_fragments_cleans_old_generation(self):
        self.store.put("a", {"secret": "a" * 6000})
        old = set(self.entries) - {self.store._service("a")}
        self.store.put("a", {"secret": "b" * 6000})
        self.assertTrue(old.isdisjoint(self.entries))
        self.assertEqual(self.store.get("a"), {"secret": "b" * 6000})
        self.store.put("a", {"secret": "small"})
        self.assertEqual(set(self.entries), {self.store._service("a")})

    def test_previous_session_remains_readable_until_pointer_commit(self):
        old = {"secret": "old" * 2000}
        self.store.put("a", old)

        def write(service, account, raw):
            self.assertEqual(self.store.get(account), old)
            self.write(service, account, raw)

        self.backend.set_password.side_effect = write
        new = {"secret": "new" * 3000}
        self.store.put("a", new)
        self.assertEqual(self.store.get("a"), new)

    def test_reported_commit_failure_does_not_delete_published_fragments(self):
        self.store.put("a", {"secret": "previous"})

        def write(service, account, raw):
            self.write(service, account, raw)
            if service == self.store._service("a"):
                raise OSError("commit-result-uncertain")

        self.backend.set_password.side_effect = write
        new = {"secret": "new" * 2000}
        with self.assertRaises(CredentialError):
            self.store.put("a", new)
        self.assertEqual(self.store.get("a"), new)

    def test_failed_fragment_write_preserves_previous_session(self):
        self.store.put("a", {"secret": "previous"})
        before = self.entries.copy()
        count = 0

        def write(service, account, raw):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("fragment-write-failed")
            self.write(service, account, raw)

        self.backend.set_password.side_effect = write
        with self.assertRaises(CredentialError):
            self.store.put("a", {"secret": "new" * 2000})
        self.assertEqual(self.entries, before)
        self.assertEqual(self.store.get("a"), {"secret": "previous"})

    def test_failed_pointer_write_preserves_previous_generation(self):
        self.store.put("a", {"secret": "previous" * 1000})
        before = self.entries.copy()

        def write(service, account, raw):
            if service == self.store._service("a"):
                raise OSError("pointer-write-failed")
            self.write(service, account, raw)

        self.backend.set_password.side_effect = write
        with self.assertRaises(CredentialError):
            self.store.put("a", {"secret": "new" * 2000})
        self.assertEqual(self.entries, before)
        self.assertEqual(self.store.get("a"), {"secret": "previous" * 1000})

    def test_interrupted_fragment_write_preserves_previous_session(self):
        self.store.put("a", {"secret": "previous"})
        before = self.entries.copy()
        self.backend.set_password.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.store.put("a", {"secret": "new" * 2000})
        self.assertEqual(self.entries, before)

    def test_missing_or_corrupted_fragments_fail_closed(self):
        for corrupt in (False, True):
            self.entries.clear()
            self.store.put("a", {"secret": "a" * 6000})
            service = next(service for service in self.entries if service != self.store._service("a"))
            if corrupt:
                self.entries[service] = "tampered"
            else:
                del self.entries[service]
            with self.subTest(corrupt=corrupt), self.assertRaises(CredentialError):
                self.store.get("a")

    def test_invalid_manifests_do_not_read_arbitrary_entries(self):
        self.store.put("a", {"secret": "a" * 6000})
        original = json.loads(self.entries[self.store._service("a")])
        for change in (
            {"version": True}, {"generation": "../other"}, {"parts": True},
            {"parts": 10000}, {"sha256": "invalid"},
        ):
            manifest = {self.store.MANIFEST: {**original[self.store.MANIFEST], **change}}
            self.entries[self.store._service("a")] = json.dumps(manifest)
            with self.subTest(change=change), self.assertRaises(CredentialError):
                self.store.get("a")

    def test_cleanup_failure_warns_without_invalidating_committed_session(self):
        self.store.put("a", {"secret": "a" * 6000})
        self.backend.delete_password.side_effect = OSError("secret-error")
        with self.assertLogs("llm_credential_mapping.storage", level="WARNING") as captured:
            self.store.put("a", {"secret": "current"})
        self.assertEqual(self.store.get("a"), {"secret": "current"})
        self.assertNotIn("secret-error", str(captured.output))
        self.assertIn("unused OS-vault fragment", str(captured.output))

    def test_deleting_large_session_removes_all_fragments(self):
        self.store.put("a", {"secret": "a" * 6000})
        self.store.delete("a")
        self.assertIsNone(self.store.get("a"))
        self.assertFalse(self.entries)

    def test_oversized_and_reserved_values_are_rejected_before_writing(self):
        for value in ({self.store.MANIFEST: {}}, {"secret": "a" * self.store.MAX_CHARS}):
            with self.subTest(reserved=self.store.MANIFEST in value), self.assertRaises(CredentialError):
                self.store.put("a", value)
        self.backend.set_password.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows-specific backend exception")
    def test_real_windows_error_class_is_redacted(self):
        from keyring.backends.Windows import pywintypes

        self.backend.set_password.side_effect = pywintypes.error(1783, "CredWrite", "secret-error")
        with self.assertRaises(CredentialError) as result:
            self.store.put("a", {"secret": "synthetic"})
        self.assertNotIn("secret-error", str(result.exception))
