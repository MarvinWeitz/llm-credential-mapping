import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.profiles import ProfileStore
from virtual_key_mapper.repositories import RepositoryStore, find_checkout, repository_identity


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

    def test_ssh_over_443_endpoints_keep_the_forge_identity(self):
        # GitHub's documented port-443 workaround: Host github.com / HostName ssh.github.com / Port 443.
        endpoints = {"github.com": "ssh.github.com", "gitlab.com": "altssh.gitlab.com", "bitbucket.org": "altssh.bitbucket.org"}
        for remote, identity in (
            ("git@github.com:company/repo.git", self.repository),
            ("ssh://git@ssh.github.com:443/company/repo.git", self.repository),
            ("git@gitlab.com:group/sub/repo.git", "gitlab.com/group/sub/repo"),
            ("ssh://git@altssh.gitlab.com:443/group/sub/repo.git", "gitlab.com/group/sub/repo"),
            ("git@bitbucket.org:team/repo.git", "bitbucket.org/team/repo"),
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_identity(remote, endpoints.get), identity)
        self.assertEqual(repository_identity("https://ssh.github.com/company/repo"), "ssh.github.com/company/repo")

    def test_bitbucket_server_https_scm_prefix_matches_the_ssh_form(self):
        for remote in (
            "https://bitbucket.example.com/scm/proj/repo.git",
            "https://user@bitbucket.example.com/scm/PROJ/repo.git",
            "ssh://git@bitbucket.example.com:7999/proj/repo.git",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_identity(remote), "bitbucket.example.com/proj/repo")
        self.assertEqual(repository_identity("https://bitbucket.example.com/scm/~me/repo.git"), "bitbucket.example.com/~me/repo")
        # Only the three-segment Bitbucket Server form on self-hosted forges; cloud forges keep their paths.
        for remote, identity in (
            ("https://git.example/scm/repo.git", "git.example/scm/repo"),
            ("https://git.example/scm/a/b/repo.git", "git.example/scm/a/b/repo"),
            ("https://gitlab.com/scm/sub/repo.git", "gitlab.com/scm/sub/repo"),
            ("https://dev.azure.com/scm/project/_git/repo", "dev.azure.com/scm/project/repo"),
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_identity(remote), identity)

    def test_common_forge_identities_are_unchanged_so_existing_assignments_keep_their_keys(self):
        # repositories.json keys are hashes of these identities; changing them orphans assignments.
        for remote, identity in (
            ("https://github.com/Company/Repo.git", "github.com/company/repo"),
            ("git@github.com:company/repo.git", "github.com/company/repo"),
            ("https://gitlab.com/group/sub/repo.git", "gitlab.com/group/sub/repo"),
            ("git@gitlab.com:group/sub/repo.git", "gitlab.com/group/sub/repo"),
            ("https://gitlab.example/group/repo.git", "gitlab.example/group/repo"),
            ("git@gitlab.example:group/repo.git", "gitlab.example/group/repo"),
            ("https://dev.azure.com/org/project/_git/repo", "dev.azure.com/org/project/repo"),
            ("git@ssh.dev.azure.com:v3/org/project/repo", "dev.azure.com/org/project/repo"),
            ("https://dev.azure.com/org/My%20Project/_git/My%20Repo", "dev.azure.com/org/my project/my repo"),
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_identity(remote, lambda host: host), identity)
                self.assertEqual(
                    RepositoryStore._key(self.gateway, repository_identity(remote)),
                    RepositoryStore._key(self.gateway, identity),
                )

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
        checkout = find_checkout(child, None)
        self.assertEqual((checkout.root.resolve(), checkout.origin, checkout.upstream), (root.resolve(), self.repository, None))

    def test_missing_git_no_checkout_or_no_origin_are_not_errors(self):
        with patch("virtual_key_mapper.repositories.subprocess.run", side_effect=FileNotFoundError("secret")):
            self.assertIsNone(find_checkout(self.home, None))
        results = [
            subprocess.CompletedProcess([], 0, str(self.home), ""),
            subprocess.CompletedProcess([], 1, "", "fatal: secret-no-remote"),
            subprocess.CompletedProcess([], 0, "file:///secret/local", ""),
        ]
        with patch("virtual_key_mapper.repositories.subprocess.run", side_effect=results):
            checkout = find_checkout(self.home, None)
        self.assertEqual((checkout.origin, checkout.upstream), (None, None))

    def test_dubious_ownership_is_an_error_without_raw_stderr(self):
        result = subprocess.CompletedProcess([], 128, "", "fatal: detected dubious ownership secret-path")
        with patch("virtual_key_mapper.repositories.subprocess.run", return_value=result):
            with self.assertRaises(CredentialError) as error:
                find_checkout(self.home, None)
        self.assertIn("dubious ownership", str(error.exception))
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
        with patch("virtual_key_mapper.profiles.os.replace", side_effect=OSError("full")):
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
