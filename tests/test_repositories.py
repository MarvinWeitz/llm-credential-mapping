import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import ProfileStore
from llm_credential_mapping.repositories import RepositoryStore, current_repository, find_checkout, repository_identity


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RepositoryStore(ProfileStore(self.home))
        self.gateway = "https://gateway.example"
        self.repository = "github.com/company/repo"

    def test_transport_aliases_credentials_case_and_ports_normalize(self):
        for remote in (
            "https://github.com/Company/Repo.git",
            "https://user:password@github.com:443/company/repo",
            "git@github.com:company/repo.git",
            "ssh://git@github.com:22/company/repo.git",
            "git://github.com:9418/company/repo.git",
            "https://GitHub.com/COMPANY/REPO",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_identity(remote), self.repository)
        self.assertEqual(repository_identity("ssh://git@git.example:2222/Group/Repo.git"), "git.example/group/repo")
        self.assertEqual(repository_identity("https://GitLab.com/Group/Sub/Repo"), "gitlab.com/group/sub/repo")
        self.assertEqual(repository_identity("git@[::1]:group/repo.git"), "[::1]/group/repo")

    def test_azure_devops_forms_share_one_identity(self):
        for remote in (
            "https://dev.azure.com/Org/Project/_git/Repo",
            "https://org@dev.azure.com/org/project/_git/repo",
            "git@ssh.dev.azure.com:v3/Org/Project/Repo",
            "ssh://git@ssh.dev.azure.com/v3/org/project/repo",
            "https://org.visualstudio.com/Project/_git/Repo",
            "https://org.visualstudio.com/DefaultCollection/Project/_git/Repo",
            "org@vs-ssh.visualstudio.com:v3/org/project/repo",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_identity(remote), "dev.azure.com/org/project/repo")

    def test_ssh_host_aliases_resolve_only_for_plain_host_names(self):
        aliases = {"github-work": "github.com"}
        seen = []

        def resolve(host):
            seen.append(host)
            return aliases.get(host)

        self.assertEqual(repository_identity("git@github-work:company/repo.git", resolve), self.repository)
        self.assertEqual(repository_identity("ssh://git@github-work/company/repo", resolve), self.repository)
        self.assertEqual(repository_identity("https://github-work/company/repo", resolve), "github-work/company/repo")
        self.assertEqual(repository_identity("git@-oProxyCommand=x:company/repo", resolve), "-oproxycommand=x/company/repo")
        self.assertEqual(seen, ["github-work", "github-work"])

    def test_find_checkout_reads_origin_and_upstream_without_network(self):
        root = self.home / "fork"
        root.mkdir()
        for args in (
            ["init", "--quiet", str(root)],
            ["-C", str(root), "remote", "add", "origin", "git@github.com:me/repo.git"],
            ["-C", str(root), "remote", "add", "upstream", "https://github.com/Company/Repo"],
        ):
            subprocess.run(["git", *args], check=True, capture_output=True)
        checkout = find_checkout(root, None)
        self.assertEqual((checkout.origin, checkout.upstream), ("github.com/me/repo", self.repository))
        self.assertEqual(checkout.identities, ("github.com/me/repo", self.repository))
        self.assertIsNone(find_checkout(self.home, None))

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

    def test_git_errors_identify_the_stage_without_exposing_raw_stderr(self):
        cases = [
            ([subprocess.CompletedProcess([], 1, "", "fatal: secret-not-a-repo")], "working tree"),
            ([
                subprocess.CompletedProcess([], 0, str(self.home), ""),
                subprocess.CompletedProcess([], 1, "", "fatal: secret-no-remote"),
                subprocess.CompletedProcess([], 1, "", "fatal: secret-no-remote"),
            ], "origin remote"),
            ([subprocess.CompletedProcess([], 128, "", "fatal: detected dubious ownership secret-path")], "dubious ownership"),
        ]
        for results, expected in cases:
            with self.subTest(expected=expected), \
                    patch("llm_credential_mapping.repositories.subprocess.run", side_effect=results):
                with self.assertRaises(CredentialError) as error:
                    current_repository(self.home)
                self.assertIn(expected, str(error.exception))
                self.assertNotIn("secret", str(error.exception))

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
