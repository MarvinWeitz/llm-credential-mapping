import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import ProfileStore
from llm_credential_mapping.repositories import RepositoryStore, current_repository, repository_identity


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RepositoryStore(ProfileStore(self.home))
        self.gateway = "https://gateway.example"
        self.repository = "github.com/company/repo"

    def test_transport_aliases_credentials_and_github_case_normalize(self):
        for remote in (
            "https://github.com/Company/Repo.git",
            "https://user:password@github.com:443/company/repo",
            "git@github.com:company/repo.git",
            "ssh://git@github.com:22/company/repo.git",
            "git://github.com:9418/company/repo.git",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_identity(remote), self.repository)
        self.assertEqual(repository_identity("ssh://git@git.example:2222/Group/Repo.git"), "git.example:2222/Group/Repo")
        self.assertEqual(repository_identity("git@[::1]:group/repo.git"), "[::1]/group/repo")

    def test_local_ambiguous_and_injected_remote_values_are_rejected_without_echoing(self):
        for remote in (
            "", "https://secret@git.example", "https://git.example/group/repo?secret=a",
            "https://git.example/group/../repo", "ssh://git.example:invalid/group/repo",
            "https://git.example/group/repo%0Asecret", "file:///secret/repo", "C:\\secret\\repo",
            "/secret/repo", "https://git.example/group/repo\n", "https://git.example/group//repo",
        ):
            with self.subTest(remote=remote), self.assertRaises(CredentialError) as error:
                repository_identity(remote)
            self.assertNotIn("secret", str(error.exception))

    def test_git_detection_from_a_subdirectory_needs_no_network(self):
        root = self.home / "checkout"
        root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:company/repo.git"],
            check=True, capture_output=True,
        )
        child = root / "nested"
        child.mkdir()
        self.assertEqual(current_repository(child), self.repository)

    def test_missing_git_or_origin_is_actionable_and_redacted(self):
        with patch("llm_credential_mapping.repositories.subprocess.run", side_effect=FileNotFoundError("secret")):
            with self.assertRaises(CredentialError) as error:
                current_repository(self.home)
            self.assertIn("--customer", str(error.exception))
            self.assertNotIn("secret", str(error.exception))
        with self.assertRaises(CredentialError):
            current_repository(self.home)

    def test_binding_persists_per_gateway_and_explicit_changes_are_required(self):
        self.assertIsNone(self.store.get(self.gateway, self.repository))
        self.store.bind(self.gateway, self.repository, "customer:A")
        other = RepositoryStore(ProfileStore(self.home))
        self.assertEqual(other.get(self.gateway, self.repository), "customer:A")
        self.assertIsNone(other.get("https://other.example", self.repository))
        with self.assertRaises(CredentialError):
            other.bind(self.gateway, self.repository, "customer:B")
        other.bind(self.gateway, self.repository, "customer:B", replace=True)
        self.assertEqual(self.store.list(self.gateway), [
            {"gateway": self.gateway, "repository": self.repository, "customer": "customer:B"},
        ])
        other.unbind(self.gateway, self.repository)
        self.assertIsNone(self.store.get(self.gateway, self.repository))
        with self.assertRaises(CredentialError):
            other.unbind(self.gateway, self.repository)

    def test_invalid_binding_inputs_cannot_create_corrupt_configuration(self):
        for gateway, repository, customer in (
            ("https://gateway.example/v1", self.repository, "customer:A"),
            (self.gateway, None, "customer:A"),
            (self.gateway, "repo\ninjected", "customer:A"),
            (self.gateway, self.repository, "customer:A,customer:B"),
        ):
            with self.subTest(gateway=gateway, repository=repository), self.assertRaises(CredentialError):
                self.store.bind(gateway, repository, customer)
        self.assertFalse(self.store.path.exists())

    def test_concurrent_first_bindings_do_not_silently_overwrite(self):
        def bind(customer):
            try:
                RepositoryStore(ProfileStore(self.home)).bind(self.gateway, self.repository, customer)
                return True
            except CredentialError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(bind, ("customer:A", "customer:B")))
        self.assertEqual(sorted(results), [False, True])
        self.assertIn(self.store.get(self.gateway, self.repository), ("customer:A", "customer:B"))

    def test_failed_atomic_replacement_preserves_previous_mapping(self):
        self.store.bind(self.gateway, self.repository, "customer:A")
        with patch("llm_credential_mapping.profiles.os.replace", side_effect=OSError("full")):
            with self.assertRaises(CredentialError):
                self.store.bind(self.gateway, self.repository, "customer:B", replace=True)
        self.assertEqual(self.store.get(self.gateway, self.repository), "customer:A")
        self.assertTrue(self.store.path.exists())
        self.assertTrue(all(path.name in ("repositories.json", "repositories.lock") for path in self.home.iterdir()))

    def test_corrupt_or_invalid_config_is_not_replaced(self):
        for value in (
            "{", json.dumps({"version": True, "bindings": {}}),
            json.dumps({"version": 1, "bindings": []}),
            json.dumps({"version": 1, "bindings": {"bad": {"gateway": self.gateway, "repository": self.repository, "customer": "a"}}}),
        ):
            self.store.path.write_text(value, encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(CredentialError):
                self.store.bind(self.gateway, self.repository, "customer:A")
            self.assertEqual(self.store.path.read_text(encoding="utf-8"), value)
